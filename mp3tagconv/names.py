"""Checks and repairs for file and directory names.

NTFS names are UTF-16, so nothing needs "converting" as such; what goes wrong
is how a name got there:

* An ANSI program on a Windows without UAO turns every Big5 extension
  character (kana included) into a private-use code point that only renders
  with the right font on the same machine.
* An archive or share whose names were in one code page gets read with
  another, leaving mojibake.
* Tools that bypass the Win32 layer leave names Explorer cannot open.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache

from . import charsets
from .charsets import BIG5, EUCJP, EUCKR, GBK, SJIS, UTF8, WESTERN
from .detect import PROFILES
from .japanese import restore_marks
from .plausibility import Plausibility, convincing, plausibility

#: Code pages a name may have been wrongly decoded with ...
_MISREAD_AS = (BIG5, GBK, SJIS, EUCKR, WESTERN, "latin-1", "cp437", "cp850")
#: ... and the encodings its bytes may really have been in.
_REALLY = (UTF8, SJIS, BIG5, GBK, EUCJP, EUCKR)

_ILLEGAL = frozenset('<>:"/\\|?*')
_INVISIBLE = frozenset(
    "­​‌‍‎‏‪‫‬‭‮"
    "⁠⁡⁢⁣⁤⁦⁧⁨⁩﻿"
)
_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"] + [f"COM{i}" for i in range(1, 10)] + [f"LPT{i}" for i in range(1, 10)]
)


@dataclass(frozen=True)
class NameFix:
    original: str
    fixed: str
    reasons: tuple[str, ...] = ()
    #: a likely repair that is not certain enough to apply on its own
    suggestion: str | None = None
    suggestion_reason: str | None = None

    @property
    def changed(self) -> bool:
        return self.fixed != self.original


@dataclass(frozen=True)
class _Reading:
    text: str
    route: str  # "<misread as>→<really>"
    plaus: Plausibility
    total: float
    confident: bool


_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def _split(name: str) -> tuple[str, str]:
    """Stem and extension; only a short ASCII suffix counts as an extension."""
    match = _EXTENSION.search(name)
    return (name[:match.start()], match.group()) if match and match.start() else (name, "")


def _keeps_shape(text: str) -> bool:
    return not any(ch in _ILLEGAL or ch < " " for ch in text)


@lru_cache(maxsize=4096)
def _best_reading(name: str) -> _Reading | None:
    """The most plausible way ``name`` could be a misdecoded other name."""
    as_is, _, _ = charsets.restore_pua(name)
    base = plausibility(as_is)
    best: _Reading | None = None
    for wrong in _MISREAD_AS:
        try:
            raw = charsets.encode_as_misread(name, wrong)
        except UnicodeEncodeError:
            continue
        if all(b < 0x80 for b in raw):
            continue
        for right in _REALLY:
            if right == wrong:
                continue
            try:
                text = charsets.decode(raw, right)
            except UnicodeDecodeError:
                continue
            if text == as_is or text == name or not _keeps_shape(text):
                continue
            p = plausibility(text, PROFILES[right])
            if not convincing(p) or p.anomalies > 0:
                continue
            total = p.score + (2.0 * p.chars if right == UTF8 else 0.0)
            if best is None or total > best.total:
                best = _Reading(text, f"{wrong}→{right}", p, total, False)
    if best is None:
        return None
    gain = best.total - max(base.score, 0.0)
    if gain < 6.0:
        return None
    # A name that already reads well needs overwhelming evidence; one that
    # does not only needs a reading that does.
    if convincing(base) and base.anomalies == 0:
        confident = best.total >= 2.0 * base.score + 6.0
    else:
        confident = best.plaus.average >= 3.0
    return _Reading(best.text, best.route, best.plaus, best.total, confident)


def clean_for_windows(name: str) -> str:
    """Remove what makes a name unusable or deceptive under Windows."""
    out = "".join(
        "_" if "\ud800" <= ch <= "\udfff" else ch
        for ch in name
        if not (ch < " " or "\x7f" <= ch <= "\x9f" or ch in _INVISIBLE)
    )
    # the Win32 layer silently drops these, so Explorer cannot reach the entry
    out = out.rstrip(" .")
    if out.split(".")[0].upper() in _RESERVED:
        out = "_" + out
    return out or "_"


def fix_name(name: str, *, is_dir: bool = False, pua: bool = True, mojibake: bool = True,
             windows: bool = True, ja_marks: bool = False,
             trusted_routes: frozenset[str] = frozenset()) -> NameFix:
    """Work out what ``name`` should be called.

    ``trusted_routes`` are mojibake routes already proven by sibling names;
    a tentative reading along one of them is applied instead of suggested.
    ``ja_marks`` also restores ー and ・ typed as look-alikes (opt-in, see
    :mod:`japanese`).
    """
    # the extension is plain ASCII: it says nothing about the encoding and
    # would only water the evidence down
    stem, extension = (name, "") if is_dir else _split(name)
    text = name
    reasons: list[str] = []
    suggestion = suggestion_reason = None

    reading = _best_reading(stem) if mojibake else None
    if reading is not None and (reading.confident or reading.route in trusted_routes):
        text = reading.text + extension
        reasons.append(f"mojibake:{reading.route}")
    else:
        if reading is not None:
            suggestion, suggestion_reason = reading.text + extension, reading.route
        if pua:
            restored, count, _ = charsets.restore_pua(text)
            if count:
                text = restored
                reasons.append("pua")

    if ja_marks:
        restored = restore_marks(text)
        if restored != text:
            text = restored
            reasons.append("ja-marks")

    if windows:
        cleaned = clean_for_windows(text)
        if cleaned != text:
            text = cleaned
            reasons.append("windows")

    return NameFix(name, text, tuple(reasons), suggestion, suggestion_reason)


def confident_route(name: str, is_dir: bool = False) -> str | None:
    """Mojibake route ``name`` proves on its own, if any."""
    reading = _best_reading(name if is_dir else _split(name)[0])
    return reading.route if reading is not None and reading.confident else None


_TRACK_PREFIX = re.compile(r"^\s*[\[(]?\d{1,3}[\])]?\s*(?:[-_.．、]+\s*|\s+)(?=\S)")


def title_from_filename(filename: str) -> str:
    """Song title implied by a file name: the stem without a track-number prefix."""
    stem = os.path.splitext(filename)[0].strip()
    title = _TRACK_PREFIX.sub("", stem, count=1).strip()
    return title or stem
