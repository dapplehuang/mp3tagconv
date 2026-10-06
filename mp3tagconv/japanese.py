"""Put back the Japanese marks people had to improvise when tagging in Big5.

Standard Big5 has neither the long-vowel mark ー nor the middle dot ・, and
the input methods of the day could not type the ones ETEN added.  Titles were
typed with what was at hand: 一 (the numeral one) or － for ー, and the Chinese
interpunct ‧ for ・.  They look alike but are different characters - a search
for スクール does not find スク－ル.

Nothing here is decoding: the bytes were read correctly, the text itself is
changed.  That is why it is opt-in.  It applies to tag text and to file and
directory names alike.
"""

from __future__ import annotations

from .detect import Hints
from .plausibility import HAN, HIRA, KATA, char_class

LONG_VOWEL = "ー"
MIDDLE_DOT = "・"
_LONG_VOWEL_STAND_INS = "一－"  # U+4E00, U+FF0D
_INTERPUNCT = "‧"              # U+2027


def restore_marks(text: str, hints: Hints | None = None) -> str:
    """Replace stand-ins for ー and ・ where the context shows they are stand-ins.

    ``hints`` (the file's path) can vouch for a middle dot in text that has
    no kana of its own, such as a list of names written in kanji.
    """
    if not any(ch in text for ch in _LONG_VOWEL_STAND_INS + _INTERPUNCT):
        return text
    classes = [char_class(ch) for ch in text]
    japanese = HIRA in classes or KATA in classes
    out = list(text)
    last = len(text) - 1
    for i, ch in enumerate(text):
        following = text[i + 1] if i < last else ""
        if ch in _LONG_VOWEL_STAND_INS:
            # Only katakana words are lengthened this way, and the mark ends
            # or continues the word: before kanji it is 一年, 一覧, a dash.
            if i == 0 or char_class(out[i - 1]) != KATA:
                continue
            if following and classes[i + 1] == HAN:
                continue
            if ch == "一" and following in ("つ", "っ"):
                continue  # アイテム一つ
            out[i] = LONG_VOWEL
        elif ch == _INTERPUNCT:
            # In Chinese text ‧ is the right character (湯姆‧克魯斯).
            if japanese or (hints is not None and (
                    (i > 0 and out[i - 1] + MIDDLE_DOT in hints.bigrams)
                    or (following and MIDDLE_DOT + following in hints.bigrams))):
                out[i] = MIDDLE_DOT
    return "".join(out)
