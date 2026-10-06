import pytest
from mutagen.id3 import ID3, TALB, TCOM, TIT2, TPE1

from conftest import cp950_view, encode, id3v1, legacy
from mp3tagconv import rewrite
from mp3tagconv.charsets import BIG5
from mp3tagconv.cli import main
from mp3tagconv.detect import Hints
from mp3tagconv.japanese import restore_marks
from mp3tagconv.names import fix_name
from mp3tagconv.runner import Options, run
from mp3tagconv.undo import UndoLog, undo


@pytest.mark.parametrize("typed, meant", [
    ("01 -ウンディ一ネ", "01 -ウンディーネ"),
    ("02 - シンフォニ一", "02 - シンフォニー"),
    ("スク－ルランブル：塚本八雲イメ一ジアルバム", "スクールランブル：塚本八雲イメージアルバム"),
    ("メイドさんブギ－", "メイドさんブギー"),
    ("絶滅のバラ－ド", "絶滅のバラード"),
    ("ヒ一ロ一になりたい", "ヒーローになりたい"),
    ("ハッピ一 (remix)", "ハッピー (remix)"),
    ("ラクス‧クライン (田中理惠)", "ラクス・クライン (田中理惠)"),
    ("忍‧美由希‧那美のテーマ", "忍・美由希・那美のテーマ"),
])
def test_stand_ins_are_restored(typed, meant):
    assert restore_marks(typed) == meant
    assert restore_marks(meant) == meant


@pytest.mark.parametrize("text", [
    "高瀨一矢",                  # a name
    "ガンダム一年戦争",          # 一年
    "アニメ一覧",                # 一覧
    "アイテム一つ",              # 一つ
    "第一話 ハジマリ",
    "ひと一",                    # only katakana words take ー
    "一",
    "東京－大阪",
    "アニメ－主題歌",            # a dash between a word and kanji
    "A－B",
    "ロック － ポップ",          # a spaced dash
    # － used as a dash: the pair around a subtitle, and a word that already has its ー
    "月の明り －ファイナルファンタジー IV 愛のテーマ－",
    "愛のテーマ－",
    "TALK ON 忍‧美由希‧那美",  # no kana: could be Chinese, where ‧ is right
    "湯姆‧克魯斯",
    "P‧E‧T‧S",
    "サムライチャンプルー",
])
def test_everything_else_is_left_alone(text):
    assert restore_marks(text) == text


def test_real_long_vowel_elsewhere_does_not_stop_a_fix():
    # イメージ is another word; スク－ル still needs its mark
    assert restore_marks("スク－ルランブル：塚本八雲イメージアルバム") == \
        "スクールランブル：塚本八雲イメージアルバム"


def test_path_vouches_for_a_middle_dot():
    hints = Hints.from_names(["kotoko", "ゲーム - I've TALK ON 忍・美由希・那美 KOTOKO & MELL.mp3"])
    assert restore_marks("TALK ON 忍‧美由希‧那美", hints) == "TALK ON 忍・美由希・那美"
    assert restore_marks("湯姆‧克魯斯", hints) == "湯姆‧克魯斯"


def _album(make_mp3):
    return make_mp3(
        "01 - ウンディーネ.mp3",
        frames=[TIT2(encoding=0, text=[legacy("01 -ウンディ一ネ", BIG5)]),
                TPE1(encoding=0, text=[legacy("ラクス‧クライン", BIG5)]),
                TCOM(encoding=0, text=[legacy("高瀨一矢", BIG5)])],
        v1=id3v1(album=encode("スク－ルランブル", BIG5)))


def test_off_by_default(tmp_path, make_mp3):
    path = _album(make_mp3)
    run(tmp_path, Options(apply=True))
    tags = ID3(path)
    assert tags["TIT2"].text == ["01 -ウンディ一ネ"]
    assert tags["TPE1"].text == ["ラクス‧クライン"]
    assert tags["TALB"].text == ["スク－ルランブル"]


def test_option_fixes_every_source(tmp_path, make_mp3):
    path = _album(make_mp3)
    result = run(tmp_path, Options(apply=True, fix_ja_marks=True))
    tags = ID3(path)
    assert tags["TIT2"].text == ["01 -ウンディーネ"]
    assert tags["TPE1"].text == ["ラクス・クライン"]
    assert tags["TALB"].text == ["スクールランブル"]  # merged from ID3v1
    assert tags["TCOM"].text == ["高瀨一矢"]
    fixed = {c.field: (c.before, c.after) for c in result.plans[0].changes if c.kind == "fixed"}
    assert fixed == {"TIT2": ("01 -ウンディ一ネ", "01 -ウンディーネ"),
                     "TPE1": ("ラクス‧クライン", "ラクス・クライン"),
                     "TALB": ("スク－ルランブル", "スクールランブル")}


GARBLED = bytes.fromhex("c7dca1bcc7e0c7a5a1bcc7e1").decode("latin-1")


def test_option_reaches_a_title_taken_from_the_file_name(tmp_path, make_mp3):
    # the file keeps its name here, so the title has to be fixed on its own
    path = make_mp3("09_メイドさんブギ－.mp3", frames=[TIT2(encoding=0, text=[GARBLED])])
    run(tmp_path, Options(apply=True, fix_ja_marks=True, names=False))
    assert ID3(path)["TIT2"].text == ["メイドさんブギー"]


@pytest.mark.parametrize("name, fixed", [
    ("05_絶滅のバラ－ド.mp3", "05_絶滅のバラード.mp3"),
    ("09_メイドさんブギ－.mp3", "09_メイドさんブギー.mp3"),
    ("01 - ウンディ一ネ.mp3", "01 - ウンディーネ.mp3"),
    ("ラクス‧クライン - 水の証.mp3", "ラクス・クライン - 水の証.mp3"),
])
def test_names_are_fixed_too(name, fixed):
    assert not fix_name(name).changed
    fix = fix_name(name, ja_marks=True)
    assert (fix.fixed, fix.reasons) == (fixed, ("ja-marks",))
    assert not fix_name(fixed, ja_marks=True).changed


@pytest.mark.parametrize("name", [
    "07 月の明り －ファイナルファンタジー IV 愛のテーマ－.flac",
    "ガンダム一年戦争.mp3", "アニメ一覧.txt", "東京－大阪.mp3", "湯姆‧克魯斯 - 歌.mp3",
    "高瀨一矢", "第一話.mp3", "ラクス・クライン(田中理恵)-水の証.mp3", "サムライチャンプルー",
])
def test_names_without_stand_ins_are_left_alone(name):
    assert not fix_name(name, ja_marks=True).changed


def test_directory_names_are_fixed_too():
    assert fix_name("スク－ルランブル", is_dir=True, ja_marks=True).fixed == "スクールランブル"


def test_stand_in_beside_private_use_kana():
    # kana that are still private-use characters must be restored first,
    # or the stand-in next to them has no katakana to lean on
    shown = cp950_view("ウンディ一ネ.mp3")
    fix = fix_name(shown, ja_marks=True)
    assert (fix.fixed, fix.reasons) == ("ウンディーネ.mp3", ("pua", "ja-marks"))


def test_file_is_renamed_and_retagged_together(tmp_path, make_mp3):
    root = tmp_path / "lib"
    make_mp3("スク－ル/09_メイドさんブギ－.mp3", directory=root,
             frames=[TIT2(encoding=0, text=[GARBLED]),
                     TALB(encoding=0, text=[legacy("スク－ル", BIG5)])])
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    log = UndoLog(tmp_path / "undo", root)
    result = run(root, Options(apply=True, fix_ja_marks=True), log)
    log.close()

    path = root / "スクール" / "09_メイドさんブギー.mp3"
    assert [p.relative_to(root) for p in root.rglob("*.mp3")] == [path.relative_to(root)]
    assert ID3(path)["TIT2"].text == ["メイドさんブギー"]
    assert ID3(path)["TALB"].text == ["スクール"]
    assert sorted((r.fix.fixed, r.status) for r in result.renames) == [
        ("09_メイドさんブギー.mp3", "done"), ("スクール", "done")]

    undone, failed = undo(tmp_path / "undo", None, rewrite.restore, rewrite.rename, print)
    assert failed == 0
    assert {str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob("*") if p.is_file()} == before


def test_rename_onto_an_existing_name_is_skipped(tmp_path, make_mp3):
    make_mp3("ブギ－.mp3")
    make_mp3("ブギー.mp3")
    result = run(tmp_path, Options(apply=True, fix_ja_marks=True))
    assert [r.status for r in result.renames] == ["conflict"]
    assert {p.name for p in tmp_path.iterdir()} == {"ブギ－.mp3", "ブギー.mp3"}


def test_option_alone_is_reason_to_rewrite(tmp_path, make_mp3):
    # an otherwise clean ID3v2.4 UTF-8 file
    path = make_mp3("a.mp3", v2_version=4, frames=[TIT2(encoding=3, text=["シンフォニ一"]),
                                                   TALB(encoding=3, text=["ARIA"])])
    before = path.read_bytes()
    assert run(tmp_path, Options(apply=True)).written == 0
    assert path.read_bytes() == before
    assert run(tmp_path, Options(apply=True, fix_ja_marks=True)).written == 1
    assert ID3(path)["TIT2"].text == ["シンフォニー"]
    assert run(tmp_path, Options(apply=True, fix_ja_marks=True)).written == 0


def test_cli_flag_and_report(tmp_path, make_mp3, monkeypatch):
    root = tmp_path / "lib"
    make_mp3("a.mp3", directory=root, frames=[TIT2(encoding=0, text=[legacy("シンフォニ一", BIG5)])])
    monkeypatch.chdir(tmp_path)
    assert main(["convert", str(root), "--fix-ja-marks", "--report", "r.txt"]) == 0
    report = (tmp_path / "r.txt").read_text(encoding="utf-8-sig")
    assert "修正" in report and "シンフォニ一 → シンフォニー" in report
    assert "修正的日文符號欄位: 1" in report
