"""Strict codecs for the legacy encodings found in MP3 tags and file names.

Python's own ``big5``/``cp950`` codecs cannot be used for the Big5 text this
tool mostly deals with: they lay out the kana rows (0xC6A1-0xC8FE) the way the
Unicode consortium's BIG5.TXT does, while real-world files use the ETEN layout
that "Unicode補完計畫" (UAO) extended.  Decoding with them silently turns
``ウンディ`` into ``ヨ⑦Ыュ``.  The table shipped by the ``uao`` package is
used instead, wrapped in a decoder that - unlike that package's own codec -
rejects invalid input rather than passing it through.
"""

from __future__ import annotations

import codecs
from typing import Callable, Iterator

from uao.b2u import b2u_table as _UAO_B2U

BIG5 = "big5-uao"
SJIS = "cp932"
GBK = "gbk"
EUCJP = "euc-jp"
EUCKR = "cp949"
UTF8 = "utf-8"
WESTERN = "cp1252"

#: Encodings tried for tag text, in tie-breaking order.
TAG_ENCODINGS = (UTF8, BIG5, SJIS, GBK, EUCJP, EUCKR, WESTERN)


def is_pua(ch: str) -> bool:
    return 0xE000 <= ord(ch) <= 0xF8FF


# --------------------------------------------------------------------------
# Big5-UAO
# --------------------------------------------------------------------------

# Unassigned UAO slots map to private-use code points; treat them as invalid.
_BIG5_DECODE: dict[int, str] = {
    code: ch for code, ch in _UAO_B2U.items() if code > 0xFF and not is_pua(ch)
}

# Several Big5 codes can share one character (e.g. 兀 at A461 and C94A); the
# lowest code is the canonical one.
_BIG5_ENCODE: dict[str, bytes] = {}
for _code in sorted(_BIG5_DECODE):
    _BIG5_ENCODE.setdefault(_BIG5_DECODE[_code], _code.to_bytes(2, "big"))


def _iter_big5(data: bytes) -> Iterator[tuple[str, int, int]]:
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b < 0x80:
            yield chr(b), i, i + 1
            i += 1
            continue
        if i + 1 >= n:
            raise UnicodeDecodeError(BIG5, data, i, n, "truncated multibyte sequence")
        ch = _BIG5_DECODE.get((b << 8) | data[i + 1])
        if ch is None:
            raise UnicodeDecodeError(BIG5, data, i, i + 2, "illegal multibyte sequence")
        yield ch, i, i + 2
        i += 2


# Windows maps the Big5 user-defined ranges linearly onto the private use
# area.  Without UAO installed every UAO/ETEN extension character (kana
# included) that went through an ANSI->Unicode conversion ends up there.
#   (first PUA code point, first lead byte, last lead byte)
_EUDC_ROWS = ((0xE000, 0xFA, 0xFE), (0xE311, 0x8E, 0xA0), (0xEEB8, 0x81, 0x8D))
_EUDC_C6 = 0xF6B1  # 0xC6A1-0xC8FE: upper half of row C6, then rows C7 and C8


def _trail(index: int) -> int:
    """Trail byte of the ``index``-th cell of a Big5 row (0x40-0x7E, 0xA1-0xFE)."""
    return 0x40 + index if index < 63 else 0xA1 + index - 63


def pua_to_big5(ch: str) -> bytes | None:
    """Big5 code a private-use character stands for under code page 950."""
    cp = ord(ch)
    for first, lead_lo, lead_hi in _EUDC_ROWS:
        index = cp - first
        if 0 <= index < (lead_hi - lead_lo + 1) * 157:
            return bytes((lead_lo + index // 157, _trail(index % 157)))
    index = cp - _EUDC_C6
    if 0 <= index < 94:
        return bytes((0xC6, 0xA1 + index))
    index -= 94
    if 0 <= index < 2 * 157:
        return bytes((0xC7 + index // 157, _trail(index % 157)))
    return None


def restore_pua(text: str) -> tuple[str, int, int]:
    """Replace cp950 private-use characters by the characters UAO assigns.

    Returns ``(text, restored, remaining)``.
    """
    if not any(is_pua(ch) for ch in text):
        return text, 0, 0
    out, restored, remaining = [], 0, 0
    for ch in text:
        if is_pua(ch):
            code = pua_to_big5(ch)
            real = _BIG5_DECODE.get(int.from_bytes(code, "big")) if code else None
            if real is not None:
                out.append(real)
                restored += 1
                continue
            remaining += 1
        out.append(ch)
    return "".join(out), restored, remaining


def _encode_big5(text: str) -> bytes:
    out = bytearray()
    for i, ch in enumerate(text):
        if ch < "\x80":
            out.append(ord(ch))
            continue
        code = _BIG5_ENCODE.get(ch) or (pua_to_big5(ch) if is_pua(ch) else None)
        if code is None:
            try:
                # Microsoft-specific mappings UAO redefined (rare)
                code = ch.encode("cp950")
            except UnicodeEncodeError:
                raise UnicodeEncodeError(BIG5, text, i, i + 1, "no Big5 mapping") from None
        out += code
    return bytes(out)


# --------------------------------------------------------------------------
# Generic strict decoding
# --------------------------------------------------------------------------

# JIS X 0208 characters that the Unicode consortium's tables and Windows map
# differently.  Python's euc_jp follows the former; use what Windows (cp932)
# produces so the same text decodes identically from either encoding.
_EUCJP_TO_WINDOWS = str.maketrans({
    "〜": "～",  # WAVE DASH        -> FULLWIDTH TILDE
    "‖": "∥",  # DOUBLE VERTICAL  -> PARALLEL TO
    "−": "－",  # MINUS SIGN       -> FULLWIDTH HYPHEN-MINUS
    "¢": "￠",  # CENT SIGN        -> FULLWIDTH CENT SIGN
    "£": "￡",  # POUND SIGN       -> FULLWIDTH POUND SIGN
    "¬": "￢",  # NOT SIGN         -> FULLWIDTH NOT SIGN
})

_PYTHON_CODEC = {SJIS: "cp932", GBK: "gbk", EUCJP: "euc_jp", EUCKR: "cp949",
                 UTF8: "utf-8", WESTERN: "cp1252"}


def _iter_python(data: bytes, encoding: str) -> Iterator[tuple[str, int, int]]:
    decoder = codecs.getincrementaldecoder(_PYTHON_CODEC[encoding])("strict")
    start = 0
    for i in range(len(data)):
        chunk = decoder.decode(data[i:i + 1])
        if chunk:
            for ch in chunk:
                yield ch, start, i + 1
            start = i + 1
    decoder.decode(b"", final=True)


def decode_spans(data: bytes, encoding: str) -> list[tuple[str, int, int]]:
    """Strictly decode ``data``; return ``(char, start, end)`` byte spans.

    Raises ``UnicodeDecodeError`` on malformed input and on input that only
    decodes to private-use or C1 control characters, which no real tag holds.
    """
    if encoding == BIG5:
        spans = list(_iter_big5(data))
    else:
        spans = list(_iter_python(data, encoding))
        if encoding == EUCJP:
            spans = [(ch.translate(_EUCJP_TO_WINDOWS), s, e) for ch, s, e in spans]
    for ch, s, e in spans:
        if is_pua(ch) or "\x80" <= ch <= "\x9f":
            raise UnicodeDecodeError(encoding, data, s, e, "unassigned code")
    return spans


def decode(data: bytes, encoding: str) -> str:
    return "".join(ch for ch, _, _ in decode_spans(data, encoding))


def _encode_western(text: str) -> bytes:
    # Windows' cp1252 passes the five bytes it leaves undefined through as C1
    # controls, so mojibake produced by it can contain them.
    out = bytearray()
    for i, ch in enumerate(text):
        if "\x80" <= ch <= "\x9f":
            out.append(ord(ch))
        else:
            try:
                out += ch.encode("cp1252")
            except UnicodeEncodeError:
                raise UnicodeEncodeError(WESTERN, text, i, i + 1, "no cp1252 mapping") from None
    return bytes(out)


#: Encoders for the code pages a file name may have been wrongly decoded with.
MISREAD_ENCODERS: dict[str, Callable[[str], bytes]] = {
    BIG5: _encode_big5,
    GBK: lambda s: s.encode("gbk"),
    SJIS: lambda s: s.encode("cp932"),
    EUCKR: lambda s: s.encode("cp949"),
    WESTERN: _encode_western,
    "latin-1": lambda s: s.encode("latin-1"),
    "cp437": lambda s: s.encode("cp437"),
    "cp850": lambda s: s.encode("cp850"),
}


def encode_as_misread(text: str, encoding: str) -> bytes:
    return MISREAD_ENCODERS[encoding](text)
