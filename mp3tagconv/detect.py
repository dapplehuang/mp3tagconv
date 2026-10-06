"""Guess which legacy encoding a run of tag bytes is in.

No statistical detector does well on the handful of bytes a song title has,
so every encoding that decodes the bytes strictly is scored for how plausible
the resulting text is (see :mod:`plausibility`) and the context is used to
break ties: characters that also occur in the file's path, and what the rest
of the file and directory turned out to be.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from . import charsets
from .charsets import BIG5, EUCJP, EUCKR, GBK, SJIS, UTF8, WESTERN
from .plausibility import (ALL_PROFILES, GARBLED, JA, KO, OK, SC, TC, WEST, Plausibility,
                           convincing, is_everyday, plausibility, verdict)

#: Tie-breaking bias; this tool is aimed at a Traditional Chinese collection,
#: where EUC-JP and Korean tags are the rare exception.
DEFAULT_PRIORS: Mapping[str, float] = {BIG5: 1.0, SJIS: 0.5, GBK: 0.25, EUCJP: -1.0, EUCKR: -1.0}

#: The languages each encoding is actually used to write.
PROFILES: Mapping[str, tuple[str, ...]] = {
    BIG5: (JA, TC), SJIS: (JA,), EUCJP: (JA,), GBK: (SC, TC, JA), EUCKR: (KO,),
    WESTERN: (WEST,), UTF8: ALL_PROFILES,
}

HIGH, MEDIUM = 6.0, 3.0  # margins above which a detection is confident

# bytes cp1252 reads as a letter: the only ones an accented Western word has
_CP1252_LETTERS = frozenset(
    b for b in range(0x80, 0x100)
    if b not in (0x81, 0x8D, 0x8F, 0x90, 0x9D) and bytes((b,)).decode("cp1252").isalpha()
)


@dataclass(frozen=True)
class Hints:
    """Text known to be right (the file's path) that tag text often repeats."""

    chars: frozenset[str] = frozenset()
    bigrams: frozenset[str] = frozenset()

    @classmethod
    def from_names(cls, names: Iterable[str]) -> "Hints":
        chars, bigrams = set(), set()
        for name in names:
            chars.update(ch for ch in name if ch >= "\x80")
            for a, b in zip(name, name[1:]):
                if a >= "\x80" and b >= "\x80":
                    bigrams.add(a + b)
        return cls(frozenset(chars), frozenset(bigrams))

    def bonus(self, text: str) -> float:
        if not self.chars:
            return 0.0
        singles = sum(1 for ch in text if ch in self.chars)
        pairs = sum(1 for a, b in zip(text, text[1:]) if a + b in self.bigrams)
        return min(1.5 * singles + 3.0 * pairs, 12.0)


@dataclass(frozen=True)
class Candidate:
    encoding: str
    text: str
    plaus: Plausibility
    total: float


@dataclass(frozen=True)
class Detection:
    text: str
    encoding: str  #: "ascii" when there was nothing to decode
    margin: float  #: lead over the best reading that gives different text
    plaus: Plausibility
    verdict: str
    runner_up: Candidate | None = None
    truncated: bool = False  #: a dangling lead byte had to be dropped
    alternatives: tuple[Candidate, ...] = field(default=(), compare=False)

    @property
    def confidence(self) -> str:
        return "high" if self.margin >= HIGH else "medium" if self.margin >= MEDIUM else "low"


def accented_letter_penalty(data: bytes, spans: list[tuple[str, int, int]]) -> float:
    """Penalise a lone multi-byte character that sits where an accented letter would.

    ``Ásgeir`` in cp1252 is also a valid Big5 character followed by ``geir``,
    and the UTF-8 for ``ö`` in ``Björk`` is also a valid GBK ideograph.
    """
    penalty = 0.0
    last = len(spans) - 1
    for k, (ch, start, end) in enumerate(spans):
        if end - start < 2 or not ch.isalpha():
            continue  # punctuation between letters is fine: P‧E‧T‧S
        if (k > 0 and spans[k - 1][0] >= "\x80") or (k < last and spans[k + 1][0] >= "\x80"):
            continue  # part of a run: CJK text
        before = k > 0 and spans[k - 1][0].isalpha()
        after = k < last and spans[k + 1][0].isalpha()
        trail = data[end - 1]
        if data[start] in _CP1252_LETTERS and trail < 0x80 and chr(trail).isalpha():
            # the character swallowed a letter: "Ça va", "Plácido" - but no
            # Western word goes from a small accented letter to a capital
            if before or not (bytes((data[start],)).decode("cp1252").islower() and chr(trail).isupper()):
                penalty += 2.5 if before or after else 1.5
        elif before and after:
            penalty += 3.0
        elif (before or after) and data[start] in _CP1252_LETTERS:
            penalty += 2.0
    return penalty


def _decode(data: bytes, encoding: str, truncated: bool):
    try:
        return charsets.decode_spans(data, encoding), False
    except UnicodeDecodeError:
        if not truncated:
            raise
    # a fixed-width field can cut the last character in half
    for cut in range(1, 4 if encoding == UTF8 else 3):
        try:
            spans = charsets.decode_spans(data[:-cut], encoding)
        except UnicodeDecodeError:
            continue
        return spans, True
    raise UnicodeDecodeError(encoding, data, 0, len(data), "undecodable")


def detect(data: bytes, *, hints: Hints | None = None,
           priors: Mapping[str, float] | None = None, truncated: bool = False,
           damaged_context: bool = False,
           encodings: Iterable[str] = charsets.TAG_ENCODINGS) -> Detection:
    """Decode ``data`` with the most plausible of ``encodings``.

    ``truncated`` says the bytes come from a fixed-width field (ID3v1), so a
    trailing half character is not evidence against an encoding.
    ``damaged_context`` says another field of the same file is beyond repair,
    which lowers the bar for calling this one garbled too.
    """
    if all(b < 0x80 for b in data):
        text = data.decode("ascii")
        p = plausibility(text)
        return Detection(text, "ascii", 99.0, p, verdict(p))

    priors = DEFAULT_PRIORS if priors is None else priors
    by_text: dict[str, tuple[Candidate, bool]] = {}
    by_encoding: dict[str, Candidate] = {}
    for encoding in encodings:
        try:
            spans, cut = _decode(data, encoding, truncated)
        except UnicodeDecodeError:
            continue
        text = "".join(ch for ch, _, _ in spans)
        p = plausibility(text, PROFILES[encoding])
        total = p.score + priors.get(encoding, 0.0)
        if encoding == UTF8:
            # well-formed multi-byte UTF-8 almost never happens by accident
            total += 2.0 * p.chars
        elif encoding != WESTERN:
            total -= accented_letter_penalty(data, spans)
        if hints is not None and p.score > 0:
            total += hints.bonus(text)
        candidate = Candidate(encoding, text, p, total)
        by_encoding[encoding] = candidate
        known = by_text.get(text)
        if known is None or total > known[0].total:
            by_text[text] = (candidate, cut)

    if not by_text:
        text = data.decode("latin-1")
        p = plausibility(text)
        return Detection(text, "latin-1", 0.0, p, GARBLED)

    ranked = sorted(by_text.values(), key=lambda item: -item[0].total)
    best, cut = ranked[0]
    others = tuple(c for c, _ in ranked[1:])

    if not convincing(best.plaus):
        # Nothing reads well.  Text that was converted between code pages
        # twice shows its scars in the Big5 reading (□ for every character
        # the converter could not map, kana in impossible places); prefer
        # that reading - it is what players display today - and say so.
        scarred = sorted(by_encoding.values(), key=lambda c: (c.encoding != BIG5, -c.total))
        for candidate in scarred:
            if verdict(candidate.plaus) == GARBLED or (damaged_context and candidate.plaus.hard >= 1):
                return Detection(candidate.text, candidate.encoding, 0.0, candidate.plaus, GARBLED,
                                 best if best.text != candidate.text else None,
                                 alternatives=others)

    runner_up = others[0] if others else None
    margin = best.total - runner_up.total if runner_up else max(best.total, 0.0)
    return Detection(best.text, best.encoding, margin, best.plaus, verdict(best.plaus),
                     runner_up, cut, others)


def restore_pua(text: str) -> tuple[str, int, int]:
    """:func:`charsets.restore_pua`, keeping only restorations that read as text.

    A private-use code point is whatever its user defined it to be.  UAO's
    table is a good guess when it yields kana or an everyday ideograph, and a
    bad one when it turns a stray glyph into a rare Simplified character.
    """
    return charsets.restore_pua(text, is_everyday)


def repair_unicode(text: str, *, hints: Hints | None = None,
                   priors: Mapping[str, float] | None = None,
                   trusted: str | None = None) -> Detection:
    """Check text that is already Unicode (UTF-16/UTF-8 frames, APEv2 and Vorbis values).

    Two kinds of damage are undone: private-use characters left by a code
    page 950 conversion that did not know UAO, and legacy bytes that were
    widened to Unicode one byte per character (``ÀuÆý`` for ``優し``).
    ``trusted`` names the encoding other fields of the same file were
    widened from; a field that decodes the same way needs no further proof.
    """
    restored, fixed, _ = restore_pua(text)
    if fixed:
        p = plausibility(restored)
        return Detection(restored, "unicode-pua", 99.0, p, verdict(p))

    p = plausibility(text)
    if p.chars and (p.profile == "west" or verdict(p) != OK):
        try:
            raw = charsets.encode_as_misread(text, WESTERN)
        except UnicodeEncodeError:
            raw = b""
        if any(b >= 0x80 for b in raw):
            found = detect(raw, hints=hints, priors=priors)
            legacy = found.encoding not in (WESTERN, "latin-1") and found.verdict == OK
            # proof: it reads far better, or the file's own path spells it this way
            proven = found.margin >= MEDIUM and (
                found.plaus.score >= p.score + HIGH
                or (hints is not None and hints.bonus(found.text) >= 4.5))
            if legacy and (proven or found.encoding == trusted):
                return Detection(found.text, "unicode-" + found.encoding, found.margin,
                                 found.plaus, found.verdict, found.runner_up)
    return Detection(text, "unicode", 99.0, p, verdict(p))
