import sys
from pathlib import Path

import pytest
from mutagen.apev2 import APEv2
from mutagen.id3 import ID3

from mp3tagconv import charsets

sys.path.insert(0, str(Path(__file__).parent))

# ten silent MPEG-1 Layer III frames (128 kbps, 44.1 kHz: 417 bytes each)
AUDIO = (b"\xff\xfb\x90\x00" + bytes(413)) * 10

_PYTHON_CODEC = {charsets.SJIS: "cp932", charsets.GBK: "gbk", charsets.EUCJP: "euc_jp",
                 charsets.EUCKR: "cp949", charsets.WESTERN: "cp1252"}


def encode(text: str, encoding: str) -> bytes:
    if encoding == charsets.BIG5:
        return charsets.encode_as_misread(text, charsets.BIG5)
    return text.encode(_PYTHON_CODEC[encoding])


def legacy(text: str, encoding: str) -> str:
    """``text`` as a tagger that only knows Latin-1 hands it to mutagen."""
    return encode(text, encoding).decode("latin-1")


def cp950_view(text: str) -> str:
    """What a Windows without UAO makes of Big5-UAO text.

    Every extension character - kana included - becomes a private-use code
    point; standard Big5 comes out right.
    """
    raw = encode(text, charsets.BIG5)
    out, i = [], 0
    while i < len(raw):
        if raw[i] < 0x80:
            out.append(chr(raw[i]))
            i += 1
            continue
        pair = raw[i:i + 2]
        standard = b"\xa1\x40" <= pair < b"\xc6\xa1" or b"\xc9\x40" <= pair <= b"\xf9\xd5"
        if standard:
            out.append(pair.decode("cp950"))
        else:
            out.append(next(chr(cp) for cp in range(0xE000, 0xF900)
                            if charsets.pua_to_big5(chr(cp)) == pair))
        i += 2
    return "".join(out)


def windows_1252_view(raw: bytes) -> str:
    """``raw`` as Windows shows it in cp1252 (undefined bytes become C1 controls)."""
    return "".join(bytes((b,)).decode("cp1252") if b not in (0x81, 0x8D, 0x8F, 0x90, 0x9D)
                   else chr(b) for b in raw)


def id3v1(title=b"", artist=b"", album=b"", year=b"", comment=b"", track=0, genre=255) -> bytes:
    def pad(data: bytes, width: int) -> bytes:
        return data[:width].ljust(width, b"\x00")
    return (b"TAG" + pad(title, 30) + pad(artist, 30) + pad(album, 30) + pad(year, 4)
            + pad(comment, 28) + bytes((0, track, genre)))


def _flac_block(kind: int, data: bytes, last: bool = False) -> bytes:
    return bytes((kind | (0x80 if last else 0),)) + len(data).to_bytes(3, "big") + data


# STREAMINFO of a 44.1 kHz 16-bit stereo stream; the frames are never decoded
_STREAMINFO = (bytes.fromhex("10001000" "000000" "000000") + (44100 << 44 | 1 << 41 | 15 << 36 | 4410)
               .to_bytes(8, "big") + bytes(16))
FLAC_AUDIO = (b"\xff\xf8\xc9\x18" + bytes(range(256))) * 12


def flac_bytes(comments=None, vendor=b"reference libFLAC 1.2.1 20070917", padding=4096,
               picture: bytes | None = None, raw_comments: list[bytes] | None = None) -> bytes:
    """A FLAC stream with the given comments: ``[(key, value), ...]`` or None for no block."""
    blocks = [(0, _STREAMINFO)]
    if comments is not None or raw_comments is not None:
        entries = [f"{k}={v}".encode("utf-8") for k, v in comments or []] + (raw_comments or [])
        body = len(vendor).to_bytes(4, "little") + vendor + len(entries).to_bytes(4, "little")
        body += b"".join(len(e).to_bytes(4, "little") + e for e in entries)
        blocks.append((4, body))
    if picture is not None:
        blocks.append((6, picture))
    if padding is not None:
        blocks.append((1, bytes(padding)))
    out = b"fLaC" + b"".join(_flac_block(kind, data, i == len(blocks) - 1)
                             for i, (kind, data) in enumerate(blocks))
    return out + FLAC_AUDIO


@pytest.fixture
def make_flac(tmp_path):
    """Write a FLAC file, optionally with the ID3/APE tags some rippers put on them."""

    def make(name: str, comments=None, directory: Path | None = None, id3_frames=(),
             ape=None, v1: bytes | None = None, **stream) -> Path:
        path = (directory or tmp_path) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(flac_bytes(comments, **stream))
        if id3_frames:
            tags = ID3()
            for frame in id3_frames:
                tags.add(frame)
            body = path.read_bytes()
            path.write_bytes(b"")
            tags.save(path, v1=0, v2_version=3)
            with open(path, "ab") as f:
                f.write(body)
        if ape:
            items = APEv2()
            items.update(ape)
            items.save(path)
        if v1 is not None:
            with open(path, "ab") as f:
                f.write(v1)
        return path

    return make


@pytest.fixture
def make_mp3(tmp_path):
    """Write an MP3 with any mix of ID3v2 frames, APEv2 items and an ID3v1 block."""

    def make(name: str, frames=(), v2_version=3, ape=None, v1: bytes | None = None,
             directory: Path | None = None) -> Path:
        path = (directory or tmp_path) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(AUDIO)
        if frames:
            tags = ID3()
            for frame in frames:
                tags.add(frame)
            tags.save(path, v1=0, v2_version=v2_version)
        if ape:
            items = APEv2()
            items.update(ape)
            items.save(path)
        if v1 is not None:
            with open(path, "ab") as f:
                f.write(v1)
        return path

    return make
