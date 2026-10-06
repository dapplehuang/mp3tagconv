"""FLAC: one spelling for every tag, written without disturbing the audio."""

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import COMM, TALB, TCON, TIT2, TPE1, TRCK, TXXX, TYER

from conftest import FLAC_AUDIO, cp950_view, flac_bytes, id3v1, legacy
from mp3tagconv import flac, rewrite
from mp3tagconv.charsets import BIG5
from mp3tagconv.cli import main
from mp3tagconv.flac import iso_date, split_number
from mp3tagconv.runner import Options, run
from mp3tagconv.undo import UndoLog, undo


def convert(root, undo_dir=None, **options):
    log = UndoLog(undo_dir, root) if undo_dir else None
    try:
        return run(root, Options(apply=True, **options), log)
    finally:
        if log:
            log.close()


def comments(path) -> list[tuple[str, str]]:
    return list(FLAC(path).tags or ())


def undo_all(undo_dir):
    return undo(undo_dir, None, rewrite.restore, rewrite.rename, print, rewrite.unpatch)


@pytest.mark.parametrize("text, date", [
    ("2004", "2004"), ("1994 08 25", "1994-08-25"), ("2010/07/07", "2010-07-07"),
    ("2005/4/13", "2005-04-13"), ("2013-04-24", "2013-04-24"), ("1991 09 zz", "1991-09"),
    ("1997 03 ZZ", "1997-03"), ("2006 ", "2006"), ("20040923", "2004-09-23"),
    ("2004.09.23", "2004-09-23"), ("2004-09", "2004-09"), ("2004 00 00", "2004"),
])
def test_iso_date(text, date):
    assert iso_date(text) == date


@pytest.mark.parametrize("text", [
    "2006/12/31(C71)", "2006/05/04(DreamParty東京)", "2002 06 zz-2002 08 zz", "2004-13-01",
    "2004-02-40", "spring 2004", "04", "",
])
def test_iso_date_leaves_what_it_does_not_understand(text):
    assert iso_date(text) is None


@pytest.mark.parametrize("text, parsed", [
    ("3/12", ("3", "12")), ("03/12", ("3", "12")), ("03", ("3", None)), ("12", ("12", None)),
    (" 7 / 09 ", ("7", "9")), ("0", ("0", None)), ("A1", None), ("1 of 12", None), ("", None),
])
def test_split_number(text, parsed):
    assert split_number(text) == parsed


# the way dBpoweramp left it: mixed-case keys, combined numbers, spaced date
DBPOWERAMP = [("Title", "After Winter's Gone"), ("Artist", "CoCo Lee"), ("Album", "Di-Da-Di"),
              ("TRACKNUMBER", "1/12"), ("DISCNUMBER", "1/1"), ("Date", "1999 01 12"),
              ("Genre", "Pop"), ("Style", "Dance-Pop"), ("Style", "Mandarin Pop"),
              ("Album Artist", "CoCo Lee"), ("Composer", " someone ")]


def test_one_spelling_for_everything(tmp_path, make_flac):
    path = make_flac("a.flac", DBPOWERAMP)
    result = convert(tmp_path)
    assert (result.written, result.errors) == (1, 0)
    assert comments(path) == [
        ("TITLE", "After Winter's Gone"), ("ARTIST", "CoCo Lee"), ("ALBUM", "Di-Da-Di"),
        ("TRACKNUMBER", "1"), ("TRACKTOTAL", "12"), ("DISCNUMBER", "1"), ("DISCTOTAL", "1"),
        ("DATE", "1999-01-12"), ("GENRE", "Pop"), ("STYLE", "Dance-Pop"), ("STYLE", "Mandarin Pop"),
        ("ALBUMARTIST", "CoCo Lee"), ("COMPOSER", "someone")]


def test_other_conventions(tmp_path, make_flac):
    path = make_flac("a.flac", [("title", "x"), ("tracknumber", "03"), ("totaltracks", "09"),
                               ("TOTALDISCS", "2"), ("date", "2010/07/07"), ("GENRE", ""),
                               ("comment", " YEAR: 2003"), ("ALBUM ARTIST", "y"),
                               ("ALBUMARTIST", "y")])
    convert(tmp_path)
    assert comments(path) == [("TITLE", "x"), ("TRACKNUMBER", "3"), ("TRACKTOTAL", "9"),
                              ("DISCTOTAL", "2"), ("DATE", "2010-07-07"),
                              ("COMMENT", "YEAR: 2003"), ("ALBUMARTIST", "y")]


def test_explicit_total_wins_over_embedded(tmp_path, make_flac):
    path = make_flac("a.flac", [("TRACKNUMBER", "3/12"), ("TRACKTOTAL", "13")])
    convert(tmp_path)
    assert comments(path) == [("TRACKNUMBER", "3"), ("TRACKTOTAL", "13")]


def test_values_that_are_more_than_a_date_or_number_stay(tmp_path, make_flac):
    path = make_flac("a.flac", [("DATE", "2006/12/31(C71)"), ("TRACKNUMBER", "A1"), ("TITLE", "x")])
    result = convert(tmp_path)
    assert result.written == 0  # nothing else to change either
    assert comments(path) == [("DATE", "2006/12/31(C71)"), ("TRACKNUMBER", "A1"), ("TITLE", "x")]
    assert result.plans[0].notes == ["number-kept:TRACKNUMBER=A1", "date-kept:2006/12/31(C71)"]


def test_patched_in_place(tmp_path, make_flac):
    path = make_flac("a.flac", DBPOWERAMP, padding=4096)
    before = path.read_bytes()
    convert(tmp_path)
    after = path.read_bytes()
    # same size, same audio at the same place: only the metadata bytes differ
    assert len(after) == len(before)
    assert after.endswith(FLAC_AUDIO) and before.endswith(FLAC_AUDIO)
    assert after != before
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("padding", [0, 1, 2, 3, 4, 5, None])
def test_rebuilt_when_the_padding_is_too_small(tmp_path, make_flac, padding):
    # splitting 1/12 needs more room than these have
    path = make_flac("a.flac", [("TRACKNUMBER", "1/12"), ("DISCNUMBER", "1/2")], padding=padding)
    assert convert(tmp_path).errors == 0
    assert comments(path) == [("TRACKNUMBER", "1"), ("TRACKTOTAL", "12"),
                              ("DISCNUMBER", "1"), ("DISCTOTAL", "2")]
    data = path.read_bytes()
    assert data.endswith(FLAC_AUDIO)
    with open(path, "rb") as f:
        blocks, audio_at = flac.read_blocks(f, 0)
    assert audio_at == len(data) - len(FLAC_AUDIO)
    assert blocks[-1].kind == flac.PADDING and blocks[-1].length >= flac.DEFAULT_PADDING


@pytest.mark.parametrize("padding", [0, 4, 9, 64])
def test_exact_fit_uses_what_room_there_is(tmp_path, make_flac, padding):
    # upper-casing keys changes no sizes: any padding, even none, will do
    path = make_flac("a.flac", [("title", "x"), ("artist", "y")], padding=padding)
    size = path.stat().st_size
    convert(tmp_path)
    assert comments(path) == [("TITLE", "x"), ("ARTIST", "y")]
    assert path.stat().st_size == size


def test_other_blocks_survive(tmp_path, make_flac):
    picture = flac._picture_block(3, "image/jpeg", b"\xff\xd8 jpeg data")
    path = make_flac("a.flac", [("title", "x"), ("tracknumber", "1/9")], picture=picture)
    convert(tmp_path)
    audio = FLAC(path)
    assert [(p.type, p.mime, p.data) for p in audio.pictures] == [(3, "image/jpeg", b"\xff\xd8 jpeg data")]
    assert audio.info.sample_rate == 44100 and audio.info.channels == 2
    assert audio.tags.vendor == "reference libFLAC 1.2.1 20070917"


def test_clean_and_untagged_files_are_not_touched(tmp_path, make_flac):
    make_flac("clean.flac", [("TITLE", "鳥の詩"), ("TRACKNUMBER", "3"), ("DATE", "2004-09-23")])
    make_flac("empty.flac", [])
    make_flac("bare.flac", None)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = convert(tmp_path)
    assert (result.written, result.plans) == (0, [])
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert result.files == {"FLAC": 3}


def test_second_run_is_a_no_op(tmp_path, make_flac):
    path = make_flac("a.flac", DBPOWERAMP)
    assert convert(tmp_path).written == 1
    once = path.read_bytes()
    assert convert(tmp_path).written == 0
    assert path.read_bytes() == once


def test_widened_big5_is_repaired(tmp_path, make_flac):
    # a whole album whose tags (and names) are Big5 shown as Western text
    widened = lambda text: legacy(text, BIG5)  # noqa: E731
    album = tmp_path / "Spring Scream"
    names = ["瓢蟲", "廢物", "濁水溪公社"]
    for number, title in enumerate(names, 1):
        make_flac(f"Spring Scream - {widened('虎年春天的吶喊')} - {number:02d} - {widened(title)}.flac",
                  [("Title", widened(title)), ("Album", widened("虎年春天的吶喊")),
                   ("Artist", "Spring Scream")], directory=album)
    result = convert(tmp_path)
    assert result.errors == 0
    for number, title in enumerate(names, 1):
        path = album / f"Spring Scream - 虎年春天的吶喊 - {number:02d} - {title}.flac"
        assert comments(path) == [("TITLE", title), ("ALBUM", "虎年春天的吶喊"),
                                  ("ARTIST", "Spring Scream")]


def test_private_use_character_of_unknown_meaning_is_left(tmp_path, make_flac):
    # UAO's table would turn it into an unrelated Simplified character
    path = make_flac("a.flac", [("ARTIST", "許茹蕓\ue044"), ("TITLE", cp950_view("ひとりごと"))])
    result = convert(tmp_path)
    assert comments(path) == [("ARTIST", "許茹蕓\ue044"), ("TITLE", "ひとりごと")]
    assert result.plans[0].notes == ["private-use:ARTIST"]


def test_ja_marks(tmp_path, make_flac):
    path = make_flac("a.flac", [("ARTIST", "ランカ‧リー＝中島 愛"), ("TITLE", "スク－ル")])
    convert(tmp_path)
    assert comments(path)[0] == ("ARTIST", "ランカ‧リー＝中島 愛")
    convert(tmp_path, fix_ja_marks=True)
    assert comments(path) == [("ARTIST", "ランカ・リー＝中島 愛"), ("TITLE", "スクール")]


def test_comment_that_is_not_utf8(tmp_path, make_flac):
    raw = b"TITLE=" + "優しい夜明け".encode("cp932")
    path = make_flac("a.flac", raw_comments=[raw, b"ARTIST=See-Saw"])
    result = convert(tmp_path)
    assert comments(path) == [("TITLE", "優しい夜明け"), ("ARTIST", "See-Saw")]
    assert [c.encoding for c in result.plans[0].changes if c.kind == "decoded"] == ["cp932"]


def test_tags_in_an_id3_header_move_into_the_stream(tmp_path, make_flac):
    path = make_flac("01 CHU-LIP.flac", [], id3_frames=[
        TIT2(encoding=1, text=["キミにカエル。"]), TPE1(encoding=1, text=["大塚 愛"]),
        TALB(encoding=1, text=["CHU-LIP"]), TRCK(encoding=1, text=["2/4"]),
        TYER(encoding=0, text=["2007"]), TCON(encoding=1, text=["(36)Game"]),
        COMM(encoding=1, lang="eng", desc="", text=[" YEAR: 2007"]),
        TXXX(encoding=1, desc="DISCID", text=["1e045d04"])])
    assert path.read_bytes().startswith(b"ID3")
    result = convert(tmp_path)
    assert result.errors == 0
    data = path.read_bytes()
    assert data.startswith(b"fLaC") and data.endswith(FLAC_AUDIO)
    assert comments(path) == [
        ("TITLE", "キミにカエル。"), ("ARTIST", "大塚 愛"), ("ALBUM", "CHU-LIP"),
        ("TRACKNUMBER", "2"), ("TRACKTOTAL", "4"), ("DATE", "2007"), ("GENRE", "Game"),
        ("COMMENT", "YEAR: 2007"), ("DISCID", "1e045d04")]


def test_legacy_bytes_in_a_foreign_id3_tag_are_decoded(tmp_path, make_flac):
    path = make_flac("a.flac", None, id3_frames=[TIT2(encoding=0, text=[legacy("優しい夜明け", BIG5)])])
    convert(tmp_path)
    assert comments(path) == [("TITLE", "優しい夜明け")]


def test_tags_in_trailing_ape_and_id3v1_move_into_the_stream(tmp_path, make_flac):
    path = make_flac("01.LORE.flac", [("genre", "Game")],
                     ape={"Title": "LORE", "Artist": "アルトネリコ", "Track": "1", "Year": "2006",
                          "Genre": "from APE"},
                     v1=id3v1(title=b"from v1", album=b"only in v1"))
    convert(tmp_path)
    assert path.read_bytes().endswith(FLAC_AUDIO)
    # the file's own comment wins, then APEv2, then ID3v1
    assert comments(path) == [("GENRE", "Game"), ("TITLE", "LORE"), ("ARTIST", "アルトネリコ"),
                              ("ALBUM", "only in v1"), ("TRACKNUMBER", "1"), ("DATE", "2006")]


def test_cover_in_a_foreign_tag_becomes_a_picture_block(tmp_path, make_flac):
    from mutagen.id3 import APIC
    path = make_flac("a.flac", [("TITLE", "x")], id3_frames=[
        APIC(encoding=0, mime="image/png", type=3, desc="", data=b"\x89PNG data")])
    convert(tmp_path)
    assert [(p.type, p.mime, p.data) for p in FLAC(path).pictures] == [(3, "image/png", b"\x89PNG data")]


def test_malformed_comment_is_kept_as_it_is(tmp_path, make_flac):
    path = make_flac("a.flac", [("title", "x")], raw_comments=[b"no separator here"])
    result = convert(tmp_path)
    assert result.errors == 0
    with open(path, "rb") as f:
        blocks, _ = flac.read_blocks(f, 0)
    _, entries = flac.parse_comments(next(b.data for b in blocks if b.kind == flac.VORBIS_COMMENT))
    assert entries == [b"TITLE=x", b"no separator here"]


def test_undo_patch_and_rebuild(tmp_path, make_flac):
    root = tmp_path / "lib"
    make_flac("patched.flac", DBPOWERAMP, directory=root)
    make_flac("rebuilt.flac", [("title", "x")], directory=root, ape={"Artist": "y"},
              v1=id3v1(album=b"z"))
    make_flac(cp950_view("ひとりごと.flac"), [("tracknumber", "1/2")], directory=root, padding=0)
    before = {p.name: p.read_bytes() for p in root.iterdir()}

    result = convert(root, tmp_path / "undo")
    assert (result.written, result.errors) == (3, 0)
    assert {p.name for p in root.iterdir()} == {"patched.flac", "rebuilt.flac", "ひとりごと.flac"}
    assert {p.name: p.read_bytes() for p in root.iterdir()} != before

    undone, failed = undo_all(tmp_path / "undo")
    assert failed == 0
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before


def test_failed_patch_is_rolled_back(tmp_path, make_flac, monkeypatch):
    path = make_flac("a.flac", DBPOWERAMP)
    before = path.read_bytes()

    def refuse(plan, audio_start):
        def check(path):
            raise rewrite.VerificationError("simulated")
        return check

    monkeypatch.setattr(rewrite, "_check_flac", refuse)
    result = convert(tmp_path)
    assert (result.written, result.errors) == (0, 1)
    assert path.read_bytes() == before


def test_unpatch_refuses_a_file_that_changed(tmp_path, make_flac):
    root = tmp_path / "lib"
    path = make_flac("a.flac", DBPOWERAMP, directory=root)
    convert(root, tmp_path / "undo")
    # someone retags the file with another tool, growing the metadata
    path.write_bytes(flac_bytes([("TITLE", "changed")], padding=9000))
    changed = path.read_bytes()
    undone, failed = undo_all(tmp_path / "undo")
    assert (undone, failed) == (0, 1)
    assert path.read_bytes() == changed


def test_only_one_format(tmp_path, make_flac, make_mp3):
    song = make_flac("a.flac", [("title", "x")])
    other = make_mp3("b.mp3", v1=id3v1(title=b"x"))
    untouched = other.read_bytes()
    result = convert(tmp_path, only="flac")
    assert (result.written, dict(result.files)) == (1, {"FLAC": 1})
    assert comments(song) == [("TITLE", "x")]
    assert other.read_bytes() == untouched
    assert main(["convert", str(tmp_path), "--only", "mp3", "--report", str(tmp_path / "r.txt")]) == 0


def test_not_a_flac_file_is_reported(tmp_path):
    (tmp_path / "fake.flac").write_bytes(b"RIFF....WAVEfmt ")
    result = convert(tmp_path)
    assert result.errors == 1 and "fLaC" in result.plans[0].error


def test_dry_run_and_report(tmp_path, make_flac, monkeypatch):
    root = tmp_path / "lib"
    path = make_flac("a.flac", DBPOWERAMP, directory=root)
    make_flac("b.flac", [("title", "only case")], directory=root)
    before = path.read_bytes()
    monkeypatch.chdir(tmp_path)
    assert main(["convert", str(root), "--report", "r.txt"]) == 0
    assert path.read_bytes() == before
    report = (tmp_path / "r.txt").read_text(encoding="utf-8-sig")
    assert "FLAC 2" in report
    assert "TRACKNUMBER 1/12 → 1" in report and "+TRACKTOTAL=12" in report
    assert "Album Artist → ALBUMARTIST" in report
    assert "DATE 1999 01 12 → 1999-01-12" in report
    assert "另有 1 個 FLAC 只有鍵名大小寫需要調整" in report

    assert main(["convert", str(root), "--apply", "--report", "r2.txt", "--undo-dir", "undo"]) == 0
    assert comments(path)[3:5] == [("TRACKNUMBER", "1"), ("TRACKTOTAL", "12")]
    assert main(["undo", "undo"]) == 0
    assert path.read_bytes() == before
