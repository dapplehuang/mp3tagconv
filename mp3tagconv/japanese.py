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

    def lengthens(i: int) -> bool:
        """Is the stand-in at ``i`` the long vowel of a katakana word?"""
        # Only katakana words are lengthened this way, and the mark ends or
        # continues the word: before kanji it is 一年, 一覧, a dash.
        if i == 0 or classes[i - 1] != KATA:
            return False
        if i < last and classes[i + 1] == HAN:
            return False
        if text[i] == "一":
            return text[i + 1:i + 2] not in ("つ", "っ")  # アイテム一つ
        # － is also a dash in its own right.  Whoever typed a real ー in the
        # same word had no need of a stand-in: 愛のテーマ－ ends a subtitle.
        left = right = i
        while left > 0 and classes[left - 1] == KATA:
            left -= 1
        while right < last and classes[right + 1] == KATA:
            right += 1
        return LONG_VOWEL not in text[left:right + 1]

    # one － that is plainly a dash (－ファイナルファンタジー－) makes them all dashes
    dashes = [i for i, ch in enumerate(text) if ch == "－"]
    dash_is_stand_in = all(lengthens(i) for i in dashes)

    for i, ch in enumerate(text):
        following = text[i + 1] if i < last else ""
        if ch in _LONG_VOWEL_STAND_INS:
            if lengthens(i) and (ch == "一" or dash_is_stand_in):
                out[i] = LONG_VOWEL
        elif ch == _INTERPUNCT:
            # In Chinese text ‧ is the right character (湯姆‧克魯斯).
            if japanese or (hints is not None and (
                    (i > 0 and out[i - 1] + MIDDLE_DOT in hints.bigrams)
                    or (following and MIDDLE_DOT + following in hints.bigrams))):
                out[i] = MIDDLE_DOT
    return "".join(out)
