"""How much does a Unicode string look like real text (rather than mojibake)?

Every candidate decoding of a tag or file name is scored here.  The score is
the best of several "reader" profiles - Japanese, Traditional Chinese,
Simplified Chinese, Korean and Western - so a string only scores well when it
is coherent in one of them.  Ideographs are weighted by how common they are,
using the per-language frequency ranks that ship with ``chardet``.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from chardet import big5freq, euckrfreq, gb2312freq, jisfreq

# --------------------------------------------------------------------------
# Character frequency ranks (lower rank = more common)
# --------------------------------------------------------------------------


def _ranks(table, to_bytes, codec: str) -> dict[str, int]:
    ranks: dict[str, int] = {}
    for index, rank in enumerate(table):
        try:
            ch = to_bytes(index).decode(codec)
        except UnicodeDecodeError:
            continue
        if len(ch) == 1 and ch >= "㐀":
            ranks[ch] = min(rank, ranks.get(ch, rank))
    return ranks


def _dbcs_row(lead0: int, per_row: int):
    def to_bytes(index: int) -> bytes:
        return bytes((lead0 + index // per_row, 0xA1 + index % per_row))
    return to_bytes


def _big5_bytes(index: int) -> bytes:
    cell = index % 157
    return bytes((0xA4 + index // 157, 0x40 + cell if cell < 63 else 0xA1 + cell - 63))


# The tables are indexed the way chardet's distribution analysers index them.
_TC = _ranks(big5freq.BIG5_CHAR_TO_FREQ_ORDER, _big5_bytes, "big5")
_SC = _ranks(gb2312freq.GB2312_CHAR_TO_FREQ_ORDER, _dbcs_row(0xB0, 94), "gb2312")
# the first hundred-odd ranks of the JIS table belong to kana and symbols
_JA = {ch: max(rank - 128, 0)
       for ch, rank in _ranks(jisfreq.JIS_CHAR_TO_FREQ_ORDER, _dbcs_row(0xA1, 94), "euc_jp").items()}
_KO = _ranks(euckrfreq.EUCKR_CHAR_TO_FREQ_ORDER, _dbcs_row(0xB0, 94), "euc_kr")


def _second_level(codec: str, lead_range: range, trails: tuple[range, ...]) -> frozenset[str]:
    chars = set()
    for lead in lead_range:
        for trail_range in trails:
            for trail in trail_range:
                try:
                    chars.add(bytes((lead, trail)).decode(codec))
                except UnicodeDecodeError:
                    pass
    return frozenset(chars)


# Valid but uncommon ideographs: the "level 2" sets of each national standard.
_UNCOMMON = (
    _second_level("euc_jp", range(0xD0, 0xF5), (range(0xA1, 0xFF),))
    | _second_level("big5", range(0xC9, 0xFA), (range(0x40, 0x7F), range(0xA1, 0xFF)))
    | _second_level("gb2312", range(0xD8, 0xF8), (range(0xA1, 0xFF),))
)


def _rank_score(rank: int | None) -> float | None:
    # Steep on purpose: bytes read with the wrong code page land on "known"
    # ideographs all the time, but real text is mostly the top thousand.
    if rank is None:
        return None
    for limit, score in ((256, 4.0), (512, 3.5), (1024, 3.0), (1536, 2.25),
                         (2048, 1.5), (3072, 0.75)):
        if rank < limit:
            return score
    return 0.25


def _hangul_score(rank: int | None) -> float | None:
    if rank is None:
        return None
    for limit, score in ((128, 4.0), (256, 3.5), (512, 3.0), (1024, 2.5), (1536, 1.0)):
        if rank < limit:
            return score
    return 0.0


# --------------------------------------------------------------------------
# Character classes
# --------------------------------------------------------------------------

(ASCII, SPACE, HIRA, KATA, HAN, HANGUL, CJK_PUNCT, FULLWIDTH, HALF_KATA,
 HALF_PUNCT, LATIN, LATIN_SIGN, TYPO, FOREIGN, SYMBOL, SYMBOL_ODD, BOX, BAD,
 ALPHABET, BOPOMOFO) = range(20)

#: classes that spell words, as opposed to punctuating or decorating them
_LETTERS = frozenset((HIRA, KATA, HAN, HANGUL, HALF_KATA, LATIN, FOREIGN, ALPHABET, BOPOMOFO))

_SMALL_KATA = frozenset("ァィゥェォャュョヮッ")
_SMALL_KATA_VOWEL = frozenset("ァィゥェォ")
# kana a small katakana vowel can follow (ファ, ティ, ウェ, ヴォ ...)
_SMALL_VOWEL_BASE = frozenset("イウヴキギクグシジスズチツテデトドニヒビピフブプミリ")
_SMALL_HIRA = frozenset("ぁぃぅぇぉゃゅょゎっ")
# single katakana that do appear on their own between ideographs (四季ノ唄, 霞ヶ関)
_LONE_KATA_OK = frozenset("ノヶケヵツ")
_TYPO = frozenset("‐‑‒–—―‘’“”•"
                  "‥…‧′″※·™©®°")
_COMMON_SYMBOLS = frozenset("★☆♪♫♬♭♯♡♥♂♀○●◎◇◆■△▲▽▼→←↑↓⇒⇔∞×÷±℃〒§¶†‡№℡♨☀☁☂☃")


@lru_cache(maxsize=None)
def char_class(ch: str) -> int:
    cp = ord(ch)
    if cp < 0x80:
        return ASCII if (cp >= 0x20 and cp != 0x7F) or ch in "\t\r\n" else BAD
    if cp < 0xA0 or 0xE000 <= cp <= 0xF8FF or cp == 0xFFFD:
        return BAD
    if ch == "□":
        # the "white square" many converters emit for unmappable characters
        return BOX
    if ch in _TYPO:
        return TYPO
    if ch in _COMMON_SYMBOLS:
        return SYMBOL
    if cp == 0xA0 or cp == 0x3000:
        return SPACE
    category = unicodedata.category(ch)
    if category in ("Cn", "Cs", "Co", "Cc"):
        return BAD
    if cp < 0x180:
        # ª º µ count as letters to Unicode, but turn up in mojibake, not in words
        return LATIN if category[0] == "L" and cp not in (0xAA, 0xBA, 0xB5) else LATIN_SIGN
    if cp < 0x250:
        # Latin Extended-B: only the Pinyin and Romanian letters are in real use
        return LATIN if 0x1CD <= cp <= 0x1DC or 0x218 <= cp <= 0x21B else FOREIGN
    if cp in (0x2C7, 0x2C9, 0x2CA, 0x2CB, 0x2D9):
        return TYPO  # Bopomofo tone marks
    if 0x370 <= cp < 0x530:
        return ALPHABET if category[0] == "L" else SYMBOL_ODD  # Greek, Cyrillic
    if 0x3105 <= cp <= 0x312F:
        return BOPOMOFO
    if cp == 0x3005 or cp == 0x3006 or cp == 0x3007:
        return HAN  # 々 〆 〇
    if 0x3001 <= cp <= 0x303F or cp == 0x30FB:
        return CJK_PUNCT
    if 0x3041 <= cp <= 0x3096 or cp in (0x309D, 0x309E):
        return HIRA
    if 0x30A1 <= cp <= 0x30FA or cp in (0x30FC, 0x30FD, 0x30FE) or 0x31F0 <= cp <= 0x31FF:
        return KATA
    if 0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF or cp >= 0x20000:
        return HAN
    if 0xAC00 <= cp <= 0xD7A3:
        return HANGUL
    if 0xFF01 <= cp <= 0xFF60 or 0xFFE0 <= cp <= 0xFFE6:
        return FULLWIDTH
    if 0xFF61 <= cp <= 0xFF65:
        return HALF_PUNCT
    if 0xFF66 <= cp <= 0xFF9F:
        return HALF_KATA
    if 0x2500 <= cp <= 0x259F:
        return SYMBOL_ODD  # box drawing / block elements: a mojibake staple
    if category[0] in "PSN":
        return SYMBOL_ODD if category in ("So", "Sm", "Sk", "No") else SYMBOL
    return FOREIGN


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Plausibility:
    score: float
    chars: int        #: non-ASCII characters scored
    anomalies: float  #: characters that have no business being in real text
    hard: float       #: the subset no reading of intact text produces (□, impossible kana)
    profile: str
    letters: int = 0  #: how many of ``chars`` spell words

    @property
    def average(self) -> float:
        return self.score / self.chars if self.chars else 0.0


OK, SUSPECT, GARBLED = "ok", "suspect", "garbled"


def verdict(p: Plausibility) -> str:
    if p.chars == 0:
        return OK
    if p.chars >= 4 and p.hard >= 2 and (p.average < 2.0 or p.hard / p.chars >= 0.3):
        return GARBLED
    if (p.anomalies >= 1 and p.average < 2.0) or (p.chars >= 2 and p.average < 0.5):
        return SUSPECT
    return OK


def convincing(p: Plausibility) -> bool:
    """Does the text read well enough that no other explanation is needed?"""
    if p.chars == 0:
        return True
    if p.letters == 0:
        return p.anomalies < 1  # punctuation and symbols only: YA・KU・SO・KU
    if p.profile == "west":
        return p.anomalies < 1 and p.average >= 1.0
    return verdict(p) == OK and p.anomalies < 1 and p.average >= 2.0


class _Tally:
    """Running score of one profile."""

    __slots__ = ("score", "soft", "hard")

    def __init__(self) -> None:
        self.score = self.soft = self.hard = 0.0

    def add(self, score: float, soft: float = 0.0, hard: float = 0.0) -> None:
        self.score += score
        self.soft += soft
        self.hard += hard


def _han(own: dict[str, int], *related: dict[str, int]):
    """Ideograph scorer for a reader whose language uses the ``own`` table.

    Characters only the ``related`` languages use get half credit: Japanese
    titles tagged in Big5 are full of Traditional forms (戀, 學), and GBK
    carries Traditional and Japanese text as readily as Simplified.
    Returns ``(score, soft anomaly weight)``.
    """
    def score(ch: str) -> tuple[float, float]:
        if ch in "々〆〇":
            return 2.5, 0.0
        value = _rank_score(own.get(ch))
        if value is not None:
            return value, 0.0
        value = max((_rank_score(table.get(ch)) or 0.0 for table in related), default=0.0)
        if value:
            return 0.5 * value, 0.0
        if ch in _TC or ch in _SC or ch in _JA:
            return -1.0, 0.5  # an everyday character, but of a language this is not
        return (-1.0, 0.34) if ch in _UNCOMMON else (-2.0, 1.0)
    return score


_han_ja = _han(_JA, _TC)
_han_tc = _han(_TC, _JA)
_han_sc = _han(_SC, _TC, _JA)


def _han_ko(ch: str) -> tuple[float, float]:
    other = max(_rank_score(_JA.get(ch)) or 0.0, _rank_score(_TC.get(ch)) or 0.0)
    return (0.3 * other, 0.0) if other else (-2.0, 1.0)


def is_everyday(ch: str) -> bool:
    """Kana, common punctuation, or an ideograph Japanese or Traditional Chinese uses daily."""
    cls = char_class(ch)
    if cls == HAN:
        return ch in _JA or ch in _TC
    return cls in (HIRA, KATA, CJK_PUNCT, FULLWIDTH, SYMBOL, TYPO)


def _kana_runs(classes: list[int]) -> list[tuple[int, int, int]]:
    """Maximal runs of hiragana or katakana as ``(class, start, end)``."""
    runs, i, n = [], 0, len(classes)
    while i < n:
        cls = classes[i]
        if cls in (HIRA, KATA):
            j = i
            while j < n and classes[j] == cls:
                j += 1
            runs.append((cls, i, j))
            i = j
        else:
            i += 1
    return runs


def _score_kana(text: str, classes: list[int], tally: _Tally) -> None:
    """Score kana the way Japanese is written: in words, not as lone letters."""
    n = len(text)
    runs = _kana_runs(classes)
    for index, (cls, start, end) in enumerate(runs):
        run = text[start:end]
        length = end - start
        beside_han = (start > 0 and classes[start - 1] == HAN) or (end < n and classes[end] == HAN)
        # kana of the other kind directly attached make one phrase (ガンダムの),
        # provided that neighbour is itself a word rather than a stray letter
        before = runs[index - 1] if index and runs[index - 1][2] == start else None
        after = runs[index + 1] if index + 1 < len(runs) and runs[index + 1][1] == end else None
        in_phrase = any(r is not None and r[2] - r[1] >= 2 for r in (before, after))
        # syllables spelled out one by one: ル・シ・ハ・コ・カ・タ
        in_phrase |= (start > 0 and text[start - 1] == "・") or (end < n and text[end] == "・")
        bad = 0
        if cls == KATA:
            # ー may continue a hiragana word (らーめん); small kana may not start anything
            if run[0] in _SMALL_KATA or (run[0] == "ー" and before is None):
                bad += 1
            for k in range(1, length):
                if run[k] in _SMALL_KATA_VOWEL and run[k - 1] not in _SMALL_VOWEL_BASE:
                    bad += 1
            if bad:
                per_char = 0.0
            elif length >= 3 or (length == 2 and in_phrase):
                per_char = 4.0
            elif length == 2:
                per_char = 3.25
            elif in_phrase or (beside_han and run in _LONE_KATA_OK):
                per_char = 3.0
            else:
                per_char = 0.5
            if not bad and run[0] == "ン" and before is None:
                per_char -= 1.0
        else:
            # a small kana cannot start a word - but っ right after a kanji is
            # the most ordinary okurigana there is (持って, 戻った)
            if run[0] in _SMALL_HIRA and not (run[0] == "っ" and start > 0 and classes[start - 1] == HAN):
                bad += 1
            if bad:
                per_char = 0.0
            elif length >= 2:
                per_char = 4.0
            elif beside_han or in_phrase:
                per_char = 3.5
            else:
                per_char = 1.0
        tally.add(per_char * length, hard=bad)


_BRACKETS = ("「」", "『』", "【】", "〖〗", "《》", "〈〉", "〔〕", "（）", "［］", "｛｝")


def _unbalanced_brackets(text: str) -> int:
    return sum(abs(text.count(pair[0]) - text.count(pair[1])) for pair in _BRACKETS)


def _score_common(cls: int, tally: _Tally) -> bool:
    """Classes every CJK profile treats alike.  False if ``cls`` is not one."""
    if cls in (CJK_PUNCT, FULLWIDTH):
        tally.add(1.0)
    elif cls in (SYMBOL, TYPO):
        tally.add(0.5)
    elif cls == SPACE:
        pass
    elif cls == BOX:
        tally.add(-3.0, hard=1)
    elif cls == BAD:
        tally.add(-4.0, hard=1)
    else:
        return False
    return True


def _score_cjk(text: str, classes: list[int], han, japanese: bool, bopomofo: bool = False) -> _Tally:
    tally = _Tally()
    if japanese:
        _score_kana(text, classes, tally)
    has_wide = any(c in (HAN, HIRA, KATA) for c in classes)
    n = len(classes)
    for i, (ch, cls) in enumerate(zip(text, classes)):
        if cls == ASCII or _score_common(cls, tally):
            continue
        if cls in (HIRA, KATA):
            if not japanese:
                # kana are the Japanese reader's to judge; letting them pass
                # here would hide the anomalies that reader finds
                tally.add(-1.0, soft=0.5)
        elif cls == HAN:
            score, odd = han(ch)
            tally.add(score, soft=odd)
        elif cls == HALF_KATA:
            # legitimate as a word of its own (ｶﾞﾝﾀﾞﾑ); next to kanji, or as a
            # stray letter, it is the mark of a Shift_JIS misreading
            in_run = (i > 0 and classes[i - 1] == HALF_KATA) or (i + 1 < n and classes[i + 1] == HALF_KATA)
            if has_wide:
                tally.add(-1.0, soft=1)
            elif in_run:
                tally.add(0.5)
            else:
                tally.add(-0.5, soft=0.34)
        elif cls == HALF_PUNCT:
            tally.add(-1.5, soft=0.5)
        elif cls in (SYMBOL_ODD, LATIN_SIGN):
            tally.add(-1.0, soft=0.34)
        elif cls == BOPOMOFO and bopomofo:
            tally.add(1.0)  # 你是最ㄞˋ: Taiwanese titles play with it
        elif cls == ALPHABET:
            tally.add(-1.0, soft=0.5)  # a stray β or Ω is a symbol; more is another script
        else:  # LATIN, FOREIGN, HANGUL: another script altogether
            tally.add(-2.0, soft=1)
    # an opening bracket that never closes is what a misread ー or □ looks like
    unbalanced = _unbalanced_brackets(text)
    tally.add(-2.0 * unbalanced, soft=0.5 * unbalanced)
    return tally


def _score_korean(text: str, classes: list[int]) -> _Tally:
    tally = _Tally()
    for ch, cls in zip(text, classes):
        if cls == ASCII or _score_common(cls, tally):
            continue
        if cls == HANGUL:
            score = _hangul_score(_KO.get(ch))
            if score is None:
                tally.add(-1.0, soft=0.34)
            else:
                tally.add(score)
        elif cls == HAN:
            score, odd = _han_ko(ch)
            tally.add(score, soft=odd)
        else:
            tally.add(-2.0, soft=1)
    unbalanced = _unbalanced_brackets(text)
    tally.add(-2.0 * unbalanced, soft=0.5 * unbalanced)
    return tally


def _score_western(text: str, classes: list[int]) -> _Tally:
    tally = _Tally()
    n = len(text)
    # accented letters are the exception in words that are otherwise plain
    # ASCII; "ânâ`â~âc" is Shift_JIS shown by a DOS code page, not a word
    plain = sum(1 for ch, cls in zip(text, classes) if cls == ASCII and ch.isalpha())
    wordlike = plain >= sum(1 for cls in classes if cls == LATIN)
    if not wordlike:
        tally.add(0.0, soft=1)

    def letter(i: int) -> bool:
        return 0 <= i < n and (classes[i] == LATIN or (classes[i] == ASCII and text[i].isalpha()))

    def alphabet(i: int) -> bool:
        return 0 <= i < n and classes[i] == ALPHABET

    for i, cls in enumerate(classes):
        if cls == ASCII or cls == SPACE:
            continue
        if cls == ALPHABET:
            # Greek and Cyrillic words are words too (Θανατος, Матрёшка)
            tally.add(1.0 if alphabet(i - 1) or alphabet(i + 1) else 0.0)
        elif cls == LATIN:
            if 0 < i < n - 1 and classes[i - 1] == LATIN and classes[i + 1] == LATIN:
                # three accented letters in a row is not a word in any Western language
                tally.add(-1.0, soft=0.5)
            elif wordlike and (letter(i - 1) or letter(i + 1)):
                tally.add(1.5)
            elif wordlike and text[i - 1:i].strip() == "" and text[i + 1:i + 2].strip() == "":
                tally.add(1.0)  # a word of its own: "Ménage à trois"
            else:
                tally.add(0.25)
        elif cls == TYPO:
            tally.add(0.5)
        elif cls == SYMBOL:
            tally.add(0.25)
        elif cls == LATIN_SIGN:
            tally.add(-1.0, soft=0.5)
        elif cls == BOX:
            tally.add(-3.0, hard=1)
        elif cls == BAD:
            tally.add(-4.0, hard=1)
        else:
            tally.add(-3.0, soft=1)
    return tally


JA, TC, SC, KO, WEST = "ja", "tc", "sc", "ko", "west"
ALL_PROFILES = (JA, TC, SC, KO, WEST)

_SCORERS = {
    JA: lambda text, classes: _score_cjk(text, classes, _han_ja, japanese=True),
    TC: lambda text, classes: _score_cjk(text, classes, _han_tc, japanese=False, bopomofo=True),
    SC: lambda text, classes: _score_cjk(text, classes, _han_sc, japanese=False),
    KO: _score_korean,
    WEST: _score_western,
}


def plausibility(text: str, profiles: tuple[str, ...] = ALL_PROFILES) -> Plausibility:
    """Score ``text`` for each of ``profiles`` and return the best.

    A decoding should only be read by the languages its encoding is used
    for: Shift_JIS bytes that spell everyday Chinese are not Japanese.
    """
    classes = [char_class(ch) for ch in text]
    chars = sum(1 for c in classes if c not in (ASCII, SPACE))
    if chars == 0:
        # only control characters can make pure ASCII implausible
        bad = sum(1 for c in classes if c == BAD)
        return Plausibility(-4.0 * bad, bad, float(bad), float(bad), "ascii")
    profile, tally = max(((name, _SCORERS[name](text, classes)) for name in profiles),
                         key=lambda item: item[1].score)
    return Plausibility(tally.score, chars, tally.soft + tally.hard, tally.hard, profile,
                        sum(1 for c in classes if c in _LETTERS))
