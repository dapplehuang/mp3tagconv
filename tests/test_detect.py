import pytest

import corpus
from mp3tagconv import charsets
from mp3tagconv.charsets import BIG5, EUCJP, EUCKR, GBK, SJIS, UTF8, WESTERN
from mp3tagconv.detect import Hints, detect, repair_unicode
from mp3tagconv.plausibility import GARBLED, OK

# Bytes found in real files (declared as Latin-1 or sitting in ID3v1 tags).
REAL = [
    ("30 31 20 2d c7 a2 c7 ef c7 c3 c7 7d a4 40 c7 c9", BIG5, "01 -ウンディ一ネ"),
    ("aa aa b3 a5 a5 d1 a8 cc", BIG5, "牧野由依"),
    ("c6 f0 ae 56 c6 fb c7 65 c7 47 c7 4a c6 f9 20 a1 e3 a4 d1 a8 cf c7 52 c7 a6 c7 b3 c7 a6"
     " c7 a9 a1 e3", BIG5, "お姬さまだっこ ～天使にオシオキ～"),
    ("c7 b9 c7 c7 c7 bf c7 ab c7 d3 c7 e6 c7 ef c7 b7 c7 b5", BIG5, "ソニックプリンセス"),
    ("c0 75 c6 fd c6 ea a9 5d a9 fa c6 f7", BIG5, "優しい夜明け"),
    ("53 65 65 59 6f 75 a4 70 c6 fb c7 51 a5 c3 bb b7 4f 72 69 67 69 6e", BIG5,
     "SeeYou小さな永遠Origin"),
    ("47 65 74 42 61 63 6b 65 72 73 20 b9 dc c1 d9 ab ce 20 4f 52 49 47 49 4e 41 4c", BIG5,
     "GetBackers 奪還屋 ORIGINAL"),
    ("83 6e 83 60 83 7e 83 63", SJIS, "ハチミツ"),
    ("83 58 83 73 83 62 83 63", SJIS, "スピッツ"),
    ("30 37 2e 97 68 e2 d5 8b c8", SJIS, "07.揺籃曲"),
    ("89 c4 90 ec 97 a2 94 fc", SJIS, "夏川里美"),
    ("83 5d 83 43 83 68 90 56 90 a2 8b 49 2f 5a 45 52 4f 20 4f 50", SJIS, "ゾイド新世紀/ZERO OP"),
    ("81 75 97 86 90 f9 89 f1 98 4c", SJIS, "「螺旋回廊"),
    ("f8 42 a4 ce d4 8a", GBK, "鳥の詩"),
    ("d5 db 91 f5 c9 ec d6 ce", GBK, "折戸伸治"),
    ("a4 bd a4 ce c9 f9 a4 ac c2 84 a4 ad a4 bf a4 af a4 c6", GBK, "その声が聞きたくて"),
    ("c4 cf b4 ba cf e3 28 d7 f4 cc d9 c0 fb c4 ce 29", GBK, "南春香(佐藤利奈)"),
]


@pytest.mark.parametrize("hexa, encoding, text", REAL, ids=[t for _, _, t in REAL])
def test_real_tag_bytes(hexa, encoding, text):
    found = detect(bytes.fromhex(hexa))
    assert (found.text, found.encoding) == (text, encoding)
    assert found.verdict == OK


def test_fields_of_one_file_can_differ():
    # album in Shift_JIS, artist in Big5 - in the same ID3v2 tag
    assert detect(bytes.fromhex("8175978690f989f1984c")).encoding == SJIS
    assert detect(bytes.fromhex("ae63b154b3c2aade")).text == "宮崎麻芽"


def test_ascii_needs_no_decision():
    found = detect(b"Shooting Star")
    assert (found.text, found.encoding, found.verdict) == ("Shooting Star", "ascii", OK)


# Japanese that was stored as EUC-KR, read as Shift_JIS and converted to
# Big5: characters the converter could not map became □.
GARBLED_BYTES = [
    # メイドさんパラパラ
    "c7 a5 bd 4a a1 42 c7 a5 c7 ca c7 a3 c7 a6 c7 a3 a1 bc c7 dc c7 a5 f9 c4 c7 dc c7 a5 a1 bc",
    # せいしをかけろ
    "c7 a3 c7 b1 c7 a3 a1 42 c7 a3 c7 a9 c7 a3 a1 bc c7 a5 c7 a3 c7 7c c7 a3 a1 bc",
    "c7 dc a1 bc c7 e0 c7 a5 a1 bc c7 e1",
    "43 6f 6d 70 6f 73 65 20 3a 20 a1 bc ba c9 c7 d7 c7 c7 bf 5c 20 2f 20 41 72 72 61 6e 67 65"
    " 20 3a 20 c7 e0 a1 bc b7 e6 c7 dd",
    # Amai Yume アマイユメ
    "41 6d 61 69 20 59 75 6d 65 c7 a5 a1 75 c7 a5 c8 d4 c7 a5 a1 42 c7 a5 c6 67 a1 bc",
    # メイドさんRock'n Roll
    "c7 a5 bd 4a a1 42 c7 a5 c7 ca c7 a3 c7 a6 c7 a3 a1 bc 6f 63 6b 27 6e 20 52 6f 6c 6c",
]


@pytest.mark.parametrize("hexa", GARBLED_BYTES)
def test_double_mojibake_is_called_garbled(hexa):
    found = detect(bytes.fromhex(hexa))
    assert found.verdict == GARBLED
    assert found.encoding == BIG5  # shown the way players show it today


def test_lost_character_alone_is_not_garbled():
    # ♯ had no Big5 code and became □; the rest of the title is intact
    found = detect(bytes.fromhex("a1 bc 30 31 a1 47 a4 df c6 f2 c5 aa c7 68 c7 72 a4 e9"))
    assert found.text == "□01：心が讀める日"
    assert found.verdict != GARBLED


def test_damaged_context_lowers_the_bar():
    # Lyrics : □盡ホニ璞
    raw = bytes.fromhex("4c 79 72 69 63 73 20 3a 20 a1 bc ba c9 c7 d7 c7 c7 bf 5c")
    assert detect(raw).verdict != GARBLED
    assert detect(raw, damaged_context=True).verdict == GARBLED


def test_truncated_field_may_end_mid_character():
    raw = "南春香(佐藤利奈),南夏奈(井上麻里奈)".encode("gbk")[:29]
    with pytest.raises(UnicodeDecodeError):
        charsets.decode(raw, GBK)
    found = detect(raw, truncated=True)
    assert found.text == "南春香(佐藤利奈),南夏奈(井上"
    assert found.truncated


def test_path_breaks_ties():
    raw = b"You Are\xa1\xad"  # … in GBK, ＃ in Big5
    assert detect(raw).text == "You Are＃"
    assert detect(raw, hints=Hints.from_names(["prismatic", "You Are….mp3"])).text == "You Are…"


def test_priors_break_ties():
    raw = "猫鍋".encode("cp932")
    assert detect(raw, priors={SJIS: 3.0}).text == "猫鍋"
    assert detect(raw, priors={SJIS: 3.0}).margin > detect(raw).margin


@pytest.mark.parametrize("text", ["Beyoncé", "Sigur Rós", "Mötley Crüe", "Ásgeir", "Ça va",
                                  "Plácido Domingo", "© 2004 Sony Music"])
def test_western_text_is_not_taken_for_cjk(text):
    assert detect(text.encode("cp1252")).text == text
    assert detect(text.encode("utf-8")).text == text


def test_common_traditional_characters_are_not_taken_for_kana():
    # every byte pair of this is also hiragana in GBK and EUC-JP
    assert detect(charsets.encode_as_misread("五月天", BIG5)).text == "五月天"
    assert detect("ひとりごと".encode("gbk")).text == "ひとりごと"


def _accuracy(texts, encoding):
    codec = {SJIS: "cp932", EUCJP: "euc_jp", GBK: "gbk", EUCKR: "cp949", WESTERN: "cp1252",
             UTF8: "utf-8"}
    right = total = 0
    for text in texts:
        try:
            raw = charsets.encode_as_misread(text, BIG5) if encoding == BIG5 \
                else text.encode(codec[encoding])
        except UnicodeEncodeError:
            continue
        if all(b < 0x80 for b in raw):
            continue
        total += 1
        right += detect(raw).text == charsets.decode(raw, encoding)
    assert total >= 20
    return right / total


@pytest.mark.parametrize("texts, encoding, floor", [
    (corpus.JAPANESE, SJIS, 0.97), (corpus.JAPANESE, BIG5, 0.95), (corpus.JAPANESE, GBK, 0.95),
    (corpus.JAPANESE, EUCJP, 0.85), (corpus.JAPANESE, UTF8, 0.98),
    (corpus.TRADITIONAL_CHINESE, BIG5, 0.98), (corpus.TRADITIONAL_CHINESE, GBK, 0.98),
    (corpus.SIMPLIFIED_CHINESE, GBK, 0.98), (corpus.SIMPLIFIED_CHINESE, UTF8, 0.98),
    (corpus.KOREAN, EUCKR, 0.75), (corpus.WESTERN, WESTERN, 0.98), (corpus.WESTERN, UTF8, 0.98),
])
def test_accuracy_without_context(texts, encoding, floor):
    assert _accuracy(texts, encoding) >= floor


# ---- text that is already Unicode ---------------------------------------


def test_repair_widened_legacy_bytes():
    # Big5 bytes that some tool "converted" to UTF-16 one byte per character
    widened = bytes.fromhex("c075c6fdc6eaa95da9fac6f7").decode("latin-1")
    fixed = repair_unicode(widened)
    assert fixed.text == "優しい夜明け"
    assert fixed.encoding == "unicode-" + BIG5


def test_repair_private_use_characters():
    shown = "".join(next(chr(cp) for cp in range(0xE000, 0xF900)
                         if charsets.pua_to_big5(chr(cp)) == pair)
                    for pair in (b"\xc7\x59", b"\xc7\x4f"))  # ひ と
    assert repair_unicode(shown + "り").text == "ひとり"


@pytest.mark.parametrize("text", ["四季ノ唄", "サムライチャンプルー", "Beyoncé", "Für Elise",
                                  "奧井雅美", "Sakura（サイドプロテア）", "© 2004"])
def test_good_unicode_is_left_alone(text):
    assert repair_unicode(text).text == text


def test_no_unicode_repairs_across_languages():
    for text in (corpus.JAPANESE + corpus.TRADITIONAL_CHINESE + corpus.SIMPLIFIED_CHINESE
                 + corpus.KOREAN + corpus.WESTERN):
        assert repair_unicode(text).text == text
