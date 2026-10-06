import pytest

import corpus
from conftest import cp950_view, windows_1252_view
from mp3tagconv import charsets
from mp3tagconv.names import clean_for_windows, confident_route, fix_name, title_from_filename

# names from a real collection: all of them correct as they are
GOOD = [
    "01 - ウンディーネ.mp3", "ゲーム - I've  Magical Sweetie(お姫さまだっこ) KOTOKO.mp3",
    "KOTOKO マキシアレンジCD hiorigoto", "{hitorigoto} 03 karaoke ひとりごと(カラオケ).mp3",
    "05_絶滅のバラ－ド.mp3", "06_聖コスプレ學園校歌.mp3", "鳥の詩 2004 summer version.mp3",
    "Gift～live version～.mp3", "You Are….mp3", "Tell Me .mp3", "輝～Hikari～.mp3",
    "School Rumble 4位女主角的CD", "塚本天滿", "澤近愛理", "01_#00：Title Call.mp3",
    "12_Distant Love＜Off Vocal Version＞(BONUS TRACK).mp3", "index (japanese).txt",
    "[SOSG][Minami-Ke_Okawari][ED][その声が聞きたくて].mp3", "[奇諾之旅 ED]the Beautiful World - 前田愛.mp3",
    "【MP3】[機動戦士ガンダムSEED_IN] 「静かな夜に」 ラクス・クライン(田中理恵).mp3",
    "亡念之扎姆德OP- Shut Up And Explode - Boom Boom Satellites.mp3", "四季ノ唄.mp3",
    "島みやえい子 - ひぐらしのなく頃に.mp3", "桃華月憚 遊戲OP 月華の螺旋.mp3", "其他", "1.mp3",
    "Beyoncé - Déjà Vu.mp3", "Sigur Rós", "五月天 - 倔強.mp3", "周杰伦 - 青花瓷.mp3",
    "소녀시대 - 다시 만난 세계.mp3", "readme.doc", "KICA-655_sr01.jpg",
]


@pytest.mark.parametrize("name", GOOD)
def test_good_names_are_left_alone(name):
    fix = fix_name(name)
    assert not fix.changed
    assert fix.suggestion is None


def test_no_false_alarms_across_languages():
    for text in (corpus.JAPANESE + corpus.TRADITIONAL_CHINESE + corpus.SIMPLIFIED_CHINESE
                 + corpus.KOREAN + corpus.WESTERN):
        fix = fix_name(text + ".mp3")
        assert not fix.changed and fix.suggestion is None, text


@pytest.mark.parametrize("name", ["01 ひとりごと(カラオケ).mp3", "ゲーム - KOTOKO メドレー.mp3",
                                  "恋愛サーキュレーション.mp3", "優しい夜明け"])
def test_private_use_characters_are_restored(name):
    shown = cp950_view(name)
    assert any(charsets.is_pua(ch) for ch in shown)
    fix = fix_name(shown)
    assert fix.fixed == name
    assert fix.reasons == ("pua",)


@pytest.mark.parametrize("name", ["鳥の詩 2004 summer version.mp3", "その声が聞きたくて.mp3",
                                  "五月天 - 突然好想你.mp3", "スピッツ - ハチミツ"])
def test_utf8_shown_as_cp1252(name):
    fix = fix_name(windows_1252_view(name.encode("utf-8")))
    assert fix.fixed == name
    assert fix.reasons == ("mojibake:cp1252→utf-8",)


@pytest.mark.parametrize("wrong, name", [
    ("cp437", "スピッツ - ハチミツ.mp3"), ("cp437", "ハチミツ.mp3"),
    ("cp850", "ひとりごと.mp3"), ("cp437", "夏川りみ - 涙そうそう.mp3"),
])
def test_shift_jis_shown_in_a_dos_code_page(wrong, name):
    shown = name.encode("cp932").decode(wrong)
    fix = fix_name(shown)
    assert fix.fixed == name
    assert fix.reasons == (f"mojibake:{wrong}→cp932",)


def test_shift_jis_shown_as_cp1252():
    shown = windows_1252_view("ハチミツ".encode("cp932")) + ".mp3"
    assert fix_name(shown).fixed == "ハチミツ.mp3"


def _misread(text: str, real: str, shown_as: str) -> str | None:
    try:
        return charsets.decode(text.encode(real), shown_as)
    except UnicodeDecodeError:
        return None  # an extractor would have put '?' there: unrecoverable anyway


def test_gbk_shown_as_big5():
    names = ["千里之外", "青花瓷", "周杰伦 - 七里香", "海阔天空", "光辉岁月", "真的爱你",
             "筷子兄弟 - 老男孩", "怒放的生命", "平凡之路", "北京欢迎你", "刘德华 - 忘情水"]
    repaired = 0
    for name in names:
        garbage = _misread(name, "gbk", charsets.BIG5)
        assert garbage is not None
        fix = fix_name(garbage + ".mp3")
        # a three-character name is too little to go on (青花瓷 reads ヤ豪棟);
        # what must never happen is a rename to something else
        if fix.changed:
            assert fix.fixed == name + ".mp3"
            repaired += 1
        else:
            assert fix.suggestion in (None, name + ".mp3")
    assert repaired >= 8


def test_siblings_vouch_for_an_uncertain_name():
    # Shift_JIS katakana read as Big5-UAO are everyday Simplified Chinese
    # characters: not proof on their own...
    lone = _misread("ハチミツ", "cp932", charsets.BIG5)
    assert lone == "汹卧军长"
    fix = fix_name(lone + ".mp3")
    assert not fix.changed
    assert fix.suggestion == "ハチミツ.mp3"
    # ...but the album next to it is beyond doubt, and it was misread the same way
    proof = _misread("スキマスイッチ - ガーネット", "cp932", charsets.BIG5)
    route = confident_route(proof + ".mp3")
    assert route == "big5-uao→cp932"
    assert fix_name(lone + ".mp3", trusted_routes=frozenset([route])).fixed == "ハチミツ.mp3"


def test_extension_is_kept():
    shown = "ハチミツ".encode("cp932").decode("cp437") + ".MP3"
    assert fix_name(shown).fixed == "ハチミツ.MP3"


@pytest.mark.parametrize("name, cleaned", [
    ("zero​width.mp3", "zerowidth.mp3"),
    ("‮gpj.exe", "gpj.exe"),          # right-to-left override
    ("trailing dot.", "trailing dot"),
    ("trailing space ", "trailing space"),
    ("CON", "_CON"), ("aux.mp3", "_aux.mp3"), ("COM1.txt", "_COM1.txt"),
    ("control\x7fchar", "controlchar"),
    ("...", "_"),
    ("Tell Me .mp3", "Tell Me .mp3"),
    ("CONTACT.txt", "CONTACT.txt"),
    ("鳥の詩.mp3", "鳥の詩.mp3"),
])
def test_clean_for_windows(name, cleaned):
    assert clean_for_windows(name) == cleaned


def test_windows_reason_is_reported():
    fix = fix_name("live​.mp3")
    assert (fix.fixed, fix.reasons) == ("live.mp3", ("windows",))


def test_checks_can_be_switched_off():
    shown = cp950_view("ひとりごと.mp3")
    assert not fix_name(shown, pua=False).changed
    garbage = "ハチミツ".encode("cp932").decode("cp437")
    assert not fix_name(garbage, mojibake=False).changed
    assert not fix_name("x​", windows=False).changed


@pytest.mark.parametrize("filename, title", [
    ("01_メイドさんパラパラ.mp3", "メイドさんパラパラ"),
    ("02. Amai Yume.mp3", "Amai Yume"),
    ("03 - just for you.mp3", "just for you"),
    ("04 ルルル.mp3", "ルルル"),
    ("(05) Title.mp3", "Title"),
    ("OMAKE_04_メイドさんRock'n Roll in USA.mp3", "OMAKE_04_メイドさんRock'n Roll in USA"),
    ("Track 01 - #00 Title Call.mp3", "Track 01 - #00 Title Call"),
    ("1.mp3", "1"),
    ("05 05 .mp3", "05"),
    ("2001年宇宙の旅.mp3", "2001年宇宙の旅"),
])
def test_title_from_filename(filename, title):
    assert title_from_filename(filename) == title
