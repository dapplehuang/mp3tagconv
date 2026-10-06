"""End-to-end: build small MP3s the way old taggers left them, convert, undo."""

import io
import os
import sys
from pathlib import Path

import pytest
from mutagen.apev2 import APENoHeaderError, APEv2
from mutagen.id3 import (COMM, ID3, TALB, TCON, TDAT, TDRC, TIT2, TPE1, TPUB, TRCK, TXXX, TYER,
                         Encoding, ID3NoHeaderError)

from conftest import AUDIO, cp950_view, encode, id3v1, legacy
from mp3tagconv import rewrite
from mp3tagconv.charsets import BIG5, GBK, SJIS
from mp3tagconv.cli import main
from mp3tagconv.runner import Options, run
from mp3tagconv.tags import read_layout
from mp3tagconv.undo import UndoLog, undo


def convert(root: Path, undo_dir: Path | None = None, **options):
    log = UndoLog(undo_dir, root) if undo_dir else None
    try:
        return run(root, Options(apply=True, **options), log)
    finally:
        if log:
            log.close()


def text(path: Path, frame: str) -> list[str]:
    return [str(value) for value in ID3(path)[frame].text]


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_big5_tag_becomes_utf8(tmp_path, make_mp3):
    path = make_mp3(
        "01 - 優しい夜明け.mp3",
        frames=[TIT2(encoding=0, text=[legacy("優しい夜明け", BIG5)]),
                TPE1(encoding=0, text=["See-Saw"]),
                TALB(encoding=0, text=[legacy("「螺旋回廊", SJIS)]),  # another tool, another code page
                TCON(encoding=0, text=["(36)Game"]),
                TYER(encoding=0, text=["20040923"]),
                COMM(encoding=0, lang="eng", desc="", text=[legacy("オープニング曲", BIG5)])],
        v1=id3v1(title=encode("優しい夜明け", BIG5), artist=b"See-Saw", track=7, genre=12))
    result = convert(tmp_path)

    assert result.written == 1 and result.errors == 0
    tags = ID3(path)
    assert tags.version == (2, 4, 0)
    assert text(path, "TIT2") == ["優しい夜明け"]
    assert text(path, "TALB") == ["「螺旋回廊"]
    assert text(path, "COMM::eng") == ["オープニング曲"]
    assert text(path, "TCON") == ["Game"]
    assert text(path, "TDRC") == ["2004-09-23"]
    assert text(path, "TRCK") == ["7"]  # only the ID3v1 tag knew
    assert all(frame.encoding == Encoding.UTF8 for frame in tags.values())
    # nothing but the new tag in front of the untouched audio
    data = path.read_bytes()
    assert data.endswith(AUDIO)
    assert len(data) == tags.size + len(AUDIO)


def test_v23_date_frames_are_combined(tmp_path, make_mp3):
    path = make_mp3("a.mp3", frames=[TYER(encoding=0, text=["2004"]), TDAT(encoding=0, text=["2309"]),
                                     TIT2(encoding=0, text=["x"])])
    convert(tmp_path)
    assert text(path, "TDRC") == ["2004-09-23"]


def test_id3v1_only(tmp_path, make_mp3):
    path = make_mp3("ひとりごと.mp3", v1=id3v1(
        title=encode("ひとりごと(カラオケ)", BIG5), artist=b"KOTOKO", album=b"hitorigoto",
        year=b"2001", comment=encode("疾風雲", BIG5), track=3, genre=36))
    convert(tmp_path)
    assert text(path, "TIT2") == ["ひとりごと(カラオケ)"]
    assert text(path, "TPE1") == ["KOTOKO"]
    assert text(path, "TDRC") == ["2001"]
    assert text(path, "TRCK") == ["3"]
    assert text(path, "TCON") == ["Game"]
    assert text(path, "COMM::XXX") == ["疾風雲"]
    assert path.read_bytes().endswith(AUDIO)


def test_id3v1_field_cut_mid_character(tmp_path, make_mp3):
    title = encode("機動戰士ガンダムSEED SUIT CD!ラクス", BIG5)
    assert title[29:31] == encode("ラ", BIG5)  # the 30-byte field ends inside ラ
    path = make_mp3("a.mp3", v1=id3v1(title=title))
    convert(tmp_path)
    assert text(path, "TIT2") == ["機動戰士ガンダムSEED SUIT CD!"]


def test_apev2_only(tmp_path, make_mp3):
    path = make_mp3("a.mp3", ape={"Title": "少女Q", "Artist": "桃月学園1年C組", "Year": "2002/08/11",
                                  "Track": "1", "Comment": " ", "WORDS": "TOx2RO"})
    convert(tmp_path)
    assert text(path, "TIT2") == ["少女Q"]
    assert text(path, "TPE1") == ["桃月学園1年C組"]
    assert text(path, "TDRC") == ["2002-08-11"]
    assert text(path, "TXXX:WORDS") == ["TOx2RO"]
    assert "COMM::XXX" not in ID3(path)  # blank
    with pytest.raises(APENoHeaderError):
        APEv2(path)


def _ape_tag(items: dict[str, bytes]) -> bytes:
    """An APEv2 tag with a footer only; values are written as given."""
    body = b"".join(len(value).to_bytes(4, "little") + bytes(4) + key.encode() + b"\x00" + value
                    for key, value in items.items())
    footer = (b"APETAGEX" + (2000).to_bytes(4, "little") + (len(body) + 32).to_bytes(4, "little")
              + len(items).to_bytes(4, "little") + bytes(12))
    return body + footer


def test_apev2_text_that_is_not_utf8(tmp_path):
    # mutagen rejects the whole tag for this; the title is plain Shift_JIS
    path = tmp_path / "02-雨色パラソル.mp3"
    path.write_bytes(AUDIO + _ape_tag({"Title": "雨色パラソル".encode("cp932"),
                                       "Artist": "大塚愛".encode("utf-8"),
                                       "Track": b"2"}))
    result = convert(tmp_path)
    assert result.errors == 0
    assert text(path, "TIT2") == ["雨色パラソル"]
    assert text(path, "TPE1") == ["大塚愛"]
    assert text(path, "TRCK") == ["2"]
    assert path.read_bytes().endswith(AUDIO)


def test_id3v2_wins_over_apev2_wins_over_id3v1(tmp_path, make_mp3):
    path = make_mp3(
        "a.mp3",
        frames=[TIT2(encoding=0, text=[legacy("鳥の詩", GBK)])],
        ape={"Title": "from APE", "Artist": "key", "Album": "AIR"},
        v1=id3v1(title=b"from v1", artist=b"from v1", album=b"from v1", comment=b"only in v1"))
    convert(tmp_path)
    assert text(path, "TIT2") == ["鳥の詩"]
    assert text(path, "TPE1") == ["key"]
    assert text(path, "TALB") == ["AIR"]
    assert text(path, "COMM::XXX") == ["only in v1"]
    with open(path, "rb") as f:
        layout = read_layout(f)
    assert (layout.id3v1_at, layout.apev2, layout.audio_end) == (None, False, layout.size)


GARBLED_TITLE = bytes.fromhex(
    "c7a5bd4aa142c7a5c7cac7a3c7a6c7a3a1bcc7dcc7a5f9c4c7dcc7a5a1bc").decode("latin-1")
GARBLED_ARTIST = bytes.fromhex("c7dca1bcc7e0c7a5a1bcc7e1").decode("latin-1")
SUSPECT_COMMENT = bytes.fromhex("4c7972696373203a20a1bcbac9c7d7c7c7bf5c").decode("latin-1")


def test_garbled_title_is_taken_from_file_name(tmp_path, make_mp3):
    path = make_mp3("01_メイドさんパラパラ.mp3", frames=[
        TIT2(encoding=0, text=[GARBLED_TITLE]), TPE1(encoding=0, text=[GARBLED_ARTIST]),
        COMM(encoding=0, lang="eng", desc="", text=[SUSPECT_COMMENT]),
        TALB(encoding=0, text=["MAID-san BEST!!"]), TRCK(encoding=0, text=["1"])])
    result = convert(tmp_path)
    tags = ID3(path)
    assert text(path, "TIT2") == ["メイドさんパラパラ"]
    assert text(path, "TALB") == ["MAID-san BEST!!"]
    assert "TPE1" not in tags
    assert not tags.getall("COMM")  # damaged like its neighbours
    kinds = [change.kind for change in result.plans[0].changes]
    assert kinds.count("cleared") == 3 and "from-filename" in kinds


def test_garbled_title_prefers_a_sound_source(tmp_path, make_mp3):
    path = make_mp3("01_x.mp3", frames=[TIT2(encoding=0, text=[GARBLED_TITLE])],
                    ape={"Title": "メイドさんパラパラ (APE)"})
    convert(tmp_path)
    assert text(path, "TIT2") == ["メイドさんパラパラ (APE)"]


def test_utf16_frames_are_rewritten(tmp_path, make_mp3):
    path = make_mp3("a.mp3", frames=[TIT2(encoding=1, text=["四季ノ唄"]),
                                     TPUB(encoding=1, text=["スターチャイルド"])])
    convert(tmp_path)
    tags = ID3(path)
    assert tags.version == (2, 4, 0)
    assert text(path, "TIT2") == ["四季ノ唄"]
    assert {frame.encoding for frame in tags.values()} == {Encoding.UTF8}


def test_widened_bytes_in_unicode_frame(tmp_path, make_mp3):
    # a tool "upgraded" the tag to UTF-16 without knowing the bytes were Big5
    path = make_mp3("a.mp3", frames=[TIT2(encoding=1, text=[legacy("優しい夜明け", BIG5)])])
    convert(tmp_path)
    assert text(path, "TIT2") == ["優しい夜明け"]


def test_clean_file_is_not_touched(tmp_path, make_mp3):
    make_mp3("a.mp3", v2_version=4, frames=[
        TIT2(encoding=3, text=["鳥の詩"]), TDRC(encoding=0, text=["2004"]),
        TXXX(encoding=3, desc="x", text=["y"])])
    untagged = make_mp3("b.mp3")
    before = snapshot(tmp_path)
    result = convert(tmp_path)
    assert result.written == 0 and not result.plans
    assert snapshot(tmp_path) == before
    assert untagged.read_bytes() == AUDIO


def test_second_run_is_a_no_op(tmp_path, make_mp3):
    make_mp3("a.mp3", frames=[TIT2(encoding=0, text=[legacy("優しい夜明け", BIG5)])],
             v1=id3v1(title=b"x"))
    assert convert(tmp_path).written == 1
    before = snapshot(tmp_path)
    assert convert(tmp_path).written == 0
    assert snapshot(tmp_path) == before


def test_dry_run_changes_nothing(tmp_path, make_mp3):
    make_mp3(cp950_view("ひとりごと.mp3"), frames=[TIT2(encoding=0, text=[legacy("ひとりごと", BIG5)])])
    before = snapshot(tmp_path)
    result = run(tmp_path, Options(apply=False))
    assert snapshot(tmp_path) == before
    assert len(result.plans) == 1 and result.plans[0].needs_write
    assert [r.fix.fixed for r in result.renames] == ["ひとりごと.mp3"]
    assert result.written == 0


def test_album_settles_an_ambiguous_field(tmp_path, make_mp3):
    # 猫鍋 in Shift_JIS is also 凧緑 in Big5-UAO; the rest of the album decides
    lone = make_mp3("x/16.mp3", frames=[TIT2(encoding=0, text=[legacy("猫鍋", SJIS)])])
    make_mp3("x/01.mp3", frames=[TIT2(encoding=0, text=[legacy("ぱんださんようちえん", SJIS)]),
                                 TALB(encoding=0, text=[legacy("コミックマーケット", SJIS)])])
    convert(tmp_path)
    assert text(lone, "TIT2") == ["猫鍋"]


def test_file_name_settles_an_ambiguous_field(tmp_path, make_mp3):
    path = make_mp3("You Are….mp3", frames=[TIT2(encoding=0, text=[legacy("You Are…", GBK)])])
    convert(tmp_path)
    assert text(path, "TIT2") == ["You Are…"]


def test_force_encoding(tmp_path, make_mp3):
    path = make_mp3("a.mp3", frames=[TIT2(encoding=0, text=[legacy("猫鍋", SJIS)]),
                                     TALB(encoding=0, text=[legacy("優しい夜明け", BIG5)])])
    convert(tmp_path, force_encoding=SJIS)
    assert text(path, "TIT2") == ["猫鍋"]
    assert text(path, "TALB") == ["優しい夜明け"]  # not Shift_JIS: detected as usual


def test_skip_uncertain(tmp_path, make_mp3):
    make_mp3("a.mp3", frames=[TIT2(encoding=0, text=[legacy("猫鍋", SJIS)])])
    before = snapshot(tmp_path)
    result = convert(tmp_path, skip_uncertain=True)
    assert (result.written, result.skipped_uncertain) == (0, 1)
    assert snapshot(tmp_path) == before


def test_names_and_undo(tmp_path, make_mp3):
    root = tmp_path / "lib"
    album = cp950_view("KOTOKO マキシ")
    song = cp950_view("01 ひとりごと.mp3")
    make_mp3(f"{album}/{song}", directory=root,
             frames=[TIT2(encoding=0, text=[legacy("ひとりごと", BIG5)])],
             ape={"Album": "hitorigoto"}, v1=id3v1(title=encode("ひとりごと", BIG5)))
    (root / album / "cover.jpg").write_bytes(b"jpeg")
    (root / "zero\u200bwidth.txt").write_text("x")
    before = snapshot(root)

    result = convert(root, tmp_path / "undo")
    assert result.errors == 0
    converted = root / "KOTOKO マキシ" / "01 ひとりごと.mp3"
    assert sorted(snapshot(root)) == sorted([
        str(Path("KOTOKO マキシ") / "01 ひとりごと.mp3"), str(Path("KOTOKO マキシ") / "cover.jpg"),
        "zerowidth.txt"])
    assert text(converted, "TIT2") == ["ひとりごと"]
    assert text(converted, "TALB") == ["hitorigoto"]

    undone, failed = undo(tmp_path / "undo", None, rewrite.restore, rewrite.rename, print)
    assert (undone, failed) == (4, 0)
    assert snapshot(root) == before


def test_undo_restores_mtime(tmp_path, make_mp3):
    root = tmp_path / "lib"
    path = make_mp3("a.mp3", directory=root, v1=id3v1(title=b"x"))
    os.utime(path, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    convert(root, tmp_path / "undo", keep_mtime=True)
    assert path.stat().st_mtime_ns == 1_000_000_000_000_000_000
    convert_again = path.read_bytes()
    undo(tmp_path / "undo", None, rewrite.restore, rewrite.rename, print)
    assert path.read_bytes() != convert_again
    assert path.stat().st_mtime_ns == 1_000_000_000_000_000_000


def test_rename_conflict_is_skipped(tmp_path, make_mp3):
    make_mp3("ひとりごと.mp3")
    make_mp3(cp950_view("ひとりごと.mp3"))
    result = convert(tmp_path)
    assert [r.status for r in result.renames] == ["conflict"]
    assert len(list(tmp_path.iterdir())) == 2


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows hides such names")
def test_name_explorer_cannot_open_is_fixed(tmp_path):
    # creatable only through the \\?\ namespace; the Win32 layer strips the dot
    with open(f"\\\\?\\{tmp_path}\\live at budokan.", "wb") as f:
        f.write(b"x")
    result = convert(tmp_path)
    assert [(r.fix.fixed, r.status) for r in result.renames] == [("live at budokan", "done")]
    assert (tmp_path / "live at budokan").read_bytes() == b"x"


def test_replacing_keeps_creation_time(tmp_path, make_mp3):
    path = make_mp3("a.mp3", v1=id3v1(title=b"x"))
    created = path.stat().st_birthtime_ns if hasattr(path.stat(), "st_birthtime_ns") else None
    convert(tmp_path)
    assert not path.read_bytes().endswith(id3v1(title=b"x"))
    if created is not None and sys.platform == "win32":
        assert path.stat().st_birthtime_ns == created


def test_failed_write_leaves_original(tmp_path, make_mp3, monkeypatch):
    path = make_mp3("a.mp3", v1=id3v1(title=b"x"))
    before = path.read_bytes()

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(rewrite, "_swap_in", broken)
    result = convert(tmp_path)
    assert result.written == 0 and result.errors == 1
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["a.mp3"]  # no temp file left behind


_LYRICS3_V2 = b"LYRICSBEGIN" + b"IND0000200" + b"LYR00005hello"


@pytest.mark.parametrize("tail", [
    id3v1(title=b"x"),
    _LYRICS3_V2 + b"%06dLYRICS200" % len(_LYRICS3_V2) + id3v1(title=b"x"),
    b"LYRICSBEGINla la la" + b"LYRICSEND" + id3v1(title=b"x"),
], ids=["id3v1", "lyrics3v2", "lyrics3v1"])
def test_layout_finds_trailing_tags(tmp_path, tail):
    path = tmp_path / "a.mp3"
    path.write_bytes(AUDIO + tail)
    with open(path, "rb") as f:
        layout = read_layout(f)
    assert (layout.audio_start, layout.audio_end) == (0, len(AUDIO))
    assert layout.id3v1_at == len(AUDIO) + len(tail) - 128


def test_layout_with_apev2_between_audio_and_id3v1(tmp_path, make_mp3):
    path = make_mp3("a.mp3", frames=[TIT2(encoding=3, text=["x"])], ape={"Title": "x"},
                    v1=id3v1(title=b"x"))
    with open(path, "rb") as f:
        layout = read_layout(f)
    data = path.read_bytes()
    assert data[layout.audio_start:layout.audio_end] == AUDIO
    assert layout.id3v2 == (3, 0) and layout.apev2 and layout.id3v1_at == len(data) - 128


def test_cli_round_trip(tmp_path, make_mp3, capsys, monkeypatch):
    root = tmp_path / "lib"
    path = make_mp3("a.mp3", directory=root,
                    frames=[TIT2(encoding=0, text=[legacy("優しい夜明け", BIG5)])])
    before = path.read_bytes()
    monkeypatch.chdir(tmp_path)

    assert main(["convert", str(root), "--report", "preview.txt"]) == 0
    assert path.read_bytes() == before
    report = (tmp_path / "preview.txt").read_text(encoding="utf-8-sig")
    assert "優しい夜明け" in report and "big5-uao" in report and "預覽" in report

    assert main(["convert", str(root), "--apply", "--report", "applied.txt",
                 "--undo-dir", "undo"]) == 0
    assert text(path, "TIT2") == ["優しい夜明け"]

    assert main(["undo", "undo"]) == 0
    assert path.read_bytes() == before
    with pytest.raises(ID3NoHeaderError):
        ID3(io.BytesIO(AUDIO))
