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
