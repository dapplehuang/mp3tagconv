import pytest
from uao.u2b import u2b_table

from mp3tagconv import charsets
from mp3tagconv.charsets import BIG5, EUCJP, GBK, SJIS


def test_big5_reads_kana_the_eten_way():
    # Python's own cp950 decodes this as 'ヨ⑦Ыュ一б'
    raw = bytes.fromhex("c7a2 c7ef c7c3 c77d a440 c7c9")
    assert charsets.decode(raw, BIG5) == "ウンディ一ネ"


@pytest.mark.parametrize("raw", [
    b"\xa4",               # lone lead byte
    b"\xa4\x20",           # impossible trail byte
    b"\xf8\x42\xa4\xce\xd4\x8a",  # GBK 鳥の詩: D4 8A is not Big5
    b"\xff\xff",
])
def test_big5_is_strict(raw):
    with pytest.raises(UnicodeDecodeError):
        charsets.decode(raw, BIG5)


def test_decode_spans_reports_byte_positions():
    spans = charsets.decode_spans("aあb".encode("cp932"), SJIS)
    assert spans == [("a", 0, 1), ("あ", 1, 3), ("b", 3, 4)]


def test_private_use_results_are_rejected():
    # cp932 maps 0xA0 to U+F8F0
    with pytest.raises(UnicodeDecodeError):
        charsets.decode(b"\xa0", SJIS)


def test_eucjp_matches_windows_mapping():
    # Python's euc_jp turns the JIS wave dash into U+301C, Windows into U+FF5E
    text = "ひぐらしのなく頃に～解～"
    as_eucjp = text.replace("～", "〜").encode("euc_jp")
    assert charsets.decode(as_eucjp, EUCJP) == charsets.decode(text.encode("cp932"), SJIS) == text


def test_pua_mapping_agrees_with_uao_table():
    # UAO's Unicode->Big5 table lists the private-use code points Windows
    # assigns to the user-defined Big5 ranges (U+E000-U+F848); ours is computed.
    checked = 0
    for ch, code in u2b_table.items():
        if len(ch) == 1 and "\ue000" <= ch <= "\uf848" and len(code) == 2:
            assert charsets.pua_to_big5(ch) == code, hex(ord(ch))
            checked += 1
    assert checked > 5000
    assert charsets.pua_to_big5("\uf849") is None


def _as_cp950_shows_it(text: str) -> str:
    """What a Windows without UAO makes of Big5-UAO text: PUA for the extensions."""
    out = []
    raw = charsets.encode_as_misread(text, BIG5)
    i = 0
    while i < len(raw):
        if raw[i] < 0x80:
            out.append(chr(raw[i]))
            i += 1
            continue
        pair = raw[i:i + 2]
        try:
            ch = pair.decode("cp950")
            ok = pair[0] not in (0xC6, 0xC7, 0xC8) or pair < b"\xc6\xa1"
        except UnicodeDecodeError:
            ok = False
        if ok:
            out.append(ch)
        else:
            out.append(next(chr(cp) for cp in range(0xE000, 0xF900)
                            if charsets.pua_to_big5(chr(cp)) == pair))
        i += 2
    return "".join(out)


def test_restore_pua():
    shown = _as_cp950_shows_it("01 ひとりごと(カラオケ).mp3")
    assert any(charsets.is_pua(ch) for ch in shown)
    assert charsets.restore_pua(shown) == ("01 ひとりごと(カラオケ).mp3", 9, 0)
    assert charsets.restore_pua("普通の名前") == ("普通の名前", 0, 0)


def test_misread_encoders_round_trip():
    for encoding, text in ((BIG5, "優しい夜明け"), (GBK, "鳥の詩"), (SJIS, "ハチミツ"),
                           (charsets.WESTERN, "Beyoncé \x81")):
        raw = charsets.encode_as_misread(text, encoding)
        if encoding != charsets.WESTERN:
            assert charsets.decode(raw, encoding) == text
    assert charsets.encode_as_misread("\x81é", charsets.WESTERN) == b"\x81\xe9"
