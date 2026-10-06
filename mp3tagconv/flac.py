"""FLAC: bring every file's tags into one consistent shape.

FLAC tags (Vorbis comments) are UTF-8 by definition, so there is rarely an
encoding to convert.  What a collection ripped over twenty years disagrees on
is everything else: whether the tags are Vorbis comments at all (some rippers
put ID3 or APE tags on FLAC files), how keys are spelled, how track numbers
and dates are written.

Planning never touches the file; :mod:`rewrite` carries a plan out.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from mutagen.id3 import Encoding

from . import tags
from .charsets import is_pua
from .detect import Hints
from .japanese import restore_marks
from .plausibility import GARBLED
from .tags import APE, ID3V1, ID3V2, Change, Layout, Slot

VORBIS = "Vorbis"
STREAMINFO, PADDING, APPLICATION, SEEKTABLE, VORBIS_COMMENT, CUESHEET, PICTURE = range(7)

DEFAULT_PADDING = 4096  # room for later edits without rewriting the file
_MAX_BLOCK = 0xFFFFFF

#: The spelling used for keys that have several in the wild (Xiph/MusicBrainz).
ALIASES = {"ALBUM ARTIST": "ALBUMARTIST", "TOTALTRACKS": "TRACKTOTAL", "TOTALDISCS": "DISCTOTAL"}
#: Keys whose value may carry "n/total", and the key that total belongs in.
NUMBERED = {"TRACKNUMBER": "TRACKTOTAL", "DISCNUMBER": "DISCTOTAL"}

# What the fields of other tag formats are called in a Vorbis comment.
_FROM_ID3 = {
    "TIT2": "TITLE", "TIT3": "SUBTITLE", "TPE1": "ARTIST", "TPE2": "ALBUMARTIST",
    "TPE3": "CONDUCTOR", "TALB": "ALBUM", "TCOM": "COMPOSER", "TEXT": "LYRICIST",
    "TRCK": "TRACKNUMBER", "TPOS": "DISCNUMBER", "TDRC": "DATE", "TYER": "DATE",
    "TCON": "GENRE", "TCOP": "COPYRIGHT", "TPUB": "LABEL", "TSRC": "ISRC", "TBPM": "BPM",
    "TENC": "ENCODEDBY", "USLT": "LYRICS",
}
_FROM_APE = {"TRACK": "TRACKNUMBER", "DISC": "DISCNUMBER", "YEAR": "DATE"}
#: Where adopted fields go; the file's own comments keep the order they have.
_ORDER = ("TITLE", "ARTIST", "ALBUM", "ALBUMARTIST", "TRACKNUMBER", "DISCNUMBER", "DATE", "GENRE",
          "COMPOSER", "COMMENT")


class FlacError(ValueError):
    """The file is not a FLAC stream this module can read."""


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    kind: int
    data: bytes      #: empty for padding
    length: int      #: of the block body as stored


def read_blocks(f: BinaryIO, start: int) -> tuple[list[Block], int]:
    """Metadata blocks of the stream at ``start``, and where its audio begins."""
    f.seek(start)
    if f.read(4) != b"fLaC":
        raise FlacError("no fLaC marker")
    blocks = []
    while True:
        header = f.read(4)
        if len(header) < 4:
            raise FlacError("metadata runs past the end of the file")
        kind, length = header[0] & 0x7F, int.from_bytes(header[1:], "big")
        if kind == 127:
            raise FlacError("invalid metadata block")
        if kind == PADDING:
            f.seek(length, 1)
            data = b""
        else:
            data = f.read(length)
            if len(data) < length:
                raise FlacError("metadata runs past the end of the file")
        blocks.append(Block(kind, data, length))
        if header[0] & 0x80:
            return blocks, f.tell()


def parse_comments(data: bytes) -> tuple[bytes, list[bytes]]:
    """Vendor string and raw ``KEY=value`` entries of a VORBIS_COMMENT block."""
    try:
        size, = struct.unpack_from("<I", data, 0)
        vendor = data[4:4 + size]
        count, = struct.unpack_from("<I", data, 4 + size)
        pos, entries = 8 + size, []
        for _ in range(count):
            size, = struct.unpack_from("<I", data, pos)
            if pos + 4 + size > len(data):
                raise FlacError("comment runs past the end of its block")
            entries.append(data[pos + 4:pos + 4 + size])
            pos += 4 + size
    except struct.error:
        raise FlacError("truncated comment block") from None
    return vendor, entries


def build_comments(vendor: bytes, entries: list[bytes]) -> bytes:
    out = [struct.pack("<I", len(vendor)), vendor, struct.pack("<I", len(entries))]
    for entry in entries:
        out += [struct.pack("<I", len(entry)), entry]
    return b"".join(out)


def read_layout(f: BinaryIO) -> Layout:
    """Where the audio frames sit: after the metadata, before any trailing tag."""
    outer = tags.read_layout(f)
    _, audio_start = read_blocks(f, outer.audio_start)
    if audio_start > outer.audio_end:
        raise FlacError("metadata runs into the trailing tags")
    return Layout(outer.size, audio_start, outer.audio_end, outer.id3v2, outer.id3v1_at,
                  outer.apev2_at, outer.lyrics3)


def is_flac(f: BinaryIO) -> bool:
    """Does the file hold a FLAC stream (possibly behind an ID3v2 tag)?"""
    return tags._read_at(f, tags.read_layout(f).audio_start, 4) == b"fLaC"


@dataclass
class Loaded(tags.Loaded):
    """A FLAC file's own comments on top of whatever foreign tags it carries."""

    stream_start: int = 0                                    #: offset of the fLaC marker
    blocks: list[Block] = field(default_factory=list)
    vendor: bytes = b""
    #: comments in file order: (key as stored, slot), or (None, raw entry) if malformed
    comments: list[tuple[str | None, Slot | bytes]] = field(default_factory=list)


def load(path: Path, hints: Hints, name: str | None = None) -> Loaded:
    # ID3v2 in front, ID3v1/APEv2 behind: read exactly as for an MP3
    outer = tags.load(path, hints, name)
    loaded = Loaded(**{f: getattr(outer, f) for f in outer.__dataclass_fields__})
    if loaded.error:
        return loaded
    try:
        with open(path, "rb") as f:
            loaded.stream_start = outer.layout.audio_start
            loaded.blocks, _ = read_blocks(f, loaded.stream_start)
            loaded.layout = read_layout(f)
        for block in loaded.blocks:
            if block.kind == VORBIS_COMMENT:
                loaded.vendor, entries = parse_comments(block.data)
                for entry in entries:
                    loaded.comments.append(_comment_slot(entry))
                break
    except (FlacError, OSError) as exc:
        loaded.error = f"{type(exc).__name__}: {exc}"
    loaded.native_slots = [slot for _, slot in loaded.comments if isinstance(slot, Slot)]
    return loaded


def _comment_slot(entry: bytes) -> tuple[str | None, Slot | bytes]:
    key, separator, value = entry.partition(b"=")
    if not separator or not key or not all(0x20 <= b <= 0x7D for b in key):
        return None, entry
    name = key.decode("ascii")
    try:
        return name, Slot(name.upper(), VORBIS, value.decode("utf-8"))
    except UnicodeDecodeError:
        # the specification says UTF-8; some taggers wrote the local code page
        return name, Slot(name.upper(), VORBIS, value.decode("latin-1"), value)


# --------------------------------------------------------------------------
# Normalising
# --------------------------------------------------------------------------

_UNKNOWN_PART = r"(?:zz|xx|\?\?|00)"
_SEP = r"[ /.\-]"


def iso_date(text: str) -> str | None:
    """``text`` as an ISO 8601 date, keeping the precision it has.

    None if it is not recognisably a date: ``2006/12/31(C71)`` says more than
    a date can, and is better left alone than cut down.
    """
    text = text.strip()
    match = (re.fullmatch(rf"(\d{{4}}){_SEP}(\d{{1,2}}){_SEP}(\d{{1,2}}|{_UNKNOWN_PART})", text, re.I)
             or re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text)
             or re.fullmatch(rf"(\d{{4}}){_SEP}(\d{{1,2}}|{_UNKNOWN_PART})()", text, re.I)
             or re.fullmatch(r"(\d{4})()()", text))
    if not match:
        return None
    year, month, day = match.groups()
    parts = [year]
    for part, limit in ((month, 12), (day, 31)):
        if not part.isdigit() or int(part) == 0:
            break  # unknown from here on
        if int(part) > limit:
            return None
        parts.append(part.zfill(2))
    return "-".join(parts)


def split_number(text: str) -> tuple[str, str | None] | None:
    """``"03/12"`` -> ``("3", "12")``, ``"03"`` -> ``("3", None)``; None if neither."""
    match = re.fullmatch(r"\s*(\d+)\s*(?:/\s*(\d+)\s*)?", text)
    if not match:
        return None
    number, total = match.groups()
    return str(int(number)), str(int(total)) if total is not None else None


@dataclass
class Plan(tags.Plan):
    """What a FLAC file's comments become, and the blocks to write them into."""

    format = "FLAC"
    target = "Vorbis comment"

    stream_start: int = 0
    blocks: list[Block] = field(default_factory=list)
    vendor: bytes = b""
    comments: list[tuple[str, str]] | None = None  #: None leaves the file alone
    verbatim: list[bytes] = field(default_factory=list)  #: malformed entries, kept as they are
    picture: bytes | None = None                   #: cover art found in a foreign tag

    @property
    def needs_write(self) -> bool:
        return self.comments is not None

    @property
    def kept(self) -> int:
        return len(self.comments or ())

    @property
    def foreign(self) -> bool:
        """Are there tags outside the FLAC stream to get rid of?"""
        return self.stream_start > 0 or self.layout.audio_end < self.layout.size

    def entries(self) -> list[bytes]:
        """The planned comments as they are stored."""
        return [f"{key}={value}".encode("utf-8") for key, value in self.comments] + self.verbatim

    def metadata(self, size: int | None = None) -> bytes | None:
        """The stream's marker and metadata blocks with the planned comments.

        With ``size`` the result is exactly that long - by adjusting the
        padding - or None if that cannot be done.
        """
        comment = Block(VORBIS_COMMENT, build_comments(self.vendor, self.entries()), 0)
        body = [b for b in self.blocks if b.kind not in (PADDING, VORBIS_COMMENT)]
        at = next((i for i, b in enumerate(self.blocks) if b.kind == VORBIS_COMMENT), 1)
        body.insert(min(at, len(body)), comment)
        if self.picture is not None:
            body.append(Block(PICTURE, self.picture, 0))
        if any(len(b.data) > _MAX_BLOCK for b in body):
            return None
        used = 4 + sum(4 + len(b.data) for b in body)
        if size is None:
            padding = max(sum(b.length for b in self.blocks if b.kind == PADDING), DEFAULT_PADDING)
        else:
            # the padding block has a 4-byte header of its own
            padding = size - used - 4
            if size != used and not 0 <= padding <= _MAX_BLOCK:
                return None
        pieces = [(b.kind, b.data) for b in body]
        if size is None or size != used:
            pieces.append((PADDING, bytes(min(padding, _MAX_BLOCK))))
        out = [b"fLaC"]
        for index, (kind, data) in enumerate(pieces):
            last = 0x80 if index == len(pieces) - 1 else 0
            out += [bytes((kind | last,)), len(data).to_bytes(3, "big"), data]
        return b"".join(out)


def _picture_block(kind: int, mime: str, data: bytes) -> bytes:
    """A PICTURE block body; dimensions are left for the reader to work out."""
    mime_bytes = mime.encode("ascii", "replace")
    return (struct.pack(">II", kind, len(mime_bytes)) + mime_bytes + struct.pack(">I", 0)
            + struct.pack(">IIII", 0, 0, 0, 0) + struct.pack(">I", len(data)) + data)


def _foreign(loaded: Loaded):
    """Fields of the ID3v2, APEv2 and ID3v1 tags as ``(origin, key, slots or text)``.

    In that order: when two of them know the same field the first is used.
    """
    for frame in loaded.frames:
        slots = loaded.frame_slots.get(id(frame), {})
        if frame.FrameID in ("TYER", "TDRC"):
            yield ID3V2, "DATE", [str(stamp) for stamp in frame.text]
        elif frame.FrameID == "TCON":
            # "(36)Game" means Game: let mutagen read the decided text
            decided = type(frame)(encoding=Encoding.UTF8, text=[slots[t].text for t in frame.text])
            yield ID3V2, "GENRE", [g for g in decided.genres if g != "Unknown"]
        elif frame.FrameID == "TXXX":
            yield ID3V2, slots[frame.desc].text.upper(), [slots[t] for t in frame.text]
        elif frame.FrameID == "COMM":
            if frame.desc == "":
                yield ID3V2, "COMMENT", [slots[t] for t in frame.text]
        elif frame.FrameID == "USLT":
            yield ID3V2, "LYRICS", [slots[frame.text]]
        elif frame.FrameID in _FROM_ID3:
            yield ID3V2, _FROM_ID3[frame.FrameID], [slots[t] for t in frame.text]
    for key, slots in loaded.ape_slots:
        key = key.upper()
        yield APE, ALIASES.get(key, _FROM_APE.get(key, key)), slots
    for frame_id, slot in loaded.v1_slots.items():
        yield ID3V1, "COMMENT" if frame_id == "COMM" else _FROM_ID3[frame_id], [slot]
    for frame_id, value in loaded.v1_plain.items():
        yield ID3V1, _FROM_ID3[frame_id], [value]


def plan(loaded: Loaded, fix_marks: bool = False) -> Plan:
    """Turn the decided slots of ``loaded`` into the comments to write."""
    layout = loaded.layout
    result = Plan(loaded.path, layout, stream_start=loaded.stream_start, blocks=loaded.blocks,
                  vendor=loaded.vendor)
    if loaded.error:
        result.error = loaded.error
        return result

    def polish(text: str, key: str, origin: str) -> str:
        fixed = restore_marks(text, loaded.hints) if fix_marks else text
        if fixed != text:
            result.changes.append(Change(key, origin, "fixed", text, fixed))
        return fixed

    def note(kind: str, key: str, before: str, after: str = "") -> None:
        result.changes.append(Change(key, VORBIS, kind, before, after))

    # 1. the file's own comments: content first, then spelling
    entries: list[list[str]] = []
    original = []
    for stored_key, slot in loaded.comments:
        if stored_key is None:
            result.verbatim.append(slot)
            continue
        original.append((stored_key, slot.stored))
        key = stored_key.upper()
        if slot.raw is not None:
            result.changes.append(Change(key, VORBIS, "decoded", slot.stored, slot.text,
                                         slot.result.encoding, slot.result.confidence, slot.verdict,
                                         raw=slot.raw))
        elif slot.text != slot.stored:
            result.changes.append(Change(key, VORBIS, "repaired", slot.stored, slot.text,
                                         slot.result.encoding, slot.result.confidence, slot.verdict))
        value = polish(slot.text, key, VORBIS)
        if key in ALIASES:
            note("renamed", ALIASES[key], stored_key)
            key = ALIASES[key]
        elif key != stored_key:
            note("uppercased", key, stored_key)
        if value != value.strip():
            note("trimmed", key, value)
        if not value.strip():
            note("emptied", key, value)
            continue
        entries.append([key, value.strip()])
    if result.verbatim:
        result.notes.append(f"malformed-comments:{len(result.verbatim)}")

    # 2. what only a foreign tag knows, in the customary order rather than
    #    whatever order that tag happened to store it in
    present = {key for key, _ in entries}
    adopted: dict[str, str] = {}
    rank = {key: index for index, key in enumerate(_ORDER)}
    for origin, key, values in sorted(_foreign(loaded), key=lambda f: rank.get(f[1], len(rank))):
        if key in present or adopted.get(key, origin) != origin:
            continue
        slots = [v for v in values if isinstance(v, Slot)]
        if any(slot.verdict == GARBLED for slot in slots):
            continue
        texts = [(v.text if isinstance(v, Slot) else v).strip() for v in values]
        texts = [polish(text, key, origin) for text in texts if text]
        if not texts:
            continue
        adopted[key] = origin
        first = slots[0] if slots else None
        result.changes.append(Change(
            key, origin, "merged", "", " / ".join(texts),
            first.result.encoding if first else "", first.result.confidence if first else "",
            first.verdict if first else "ok", raw=(first.raw or b"") if first else b""))
        entries += [[key, text] for text in texts]
    if not any(b.kind == PICTURE for b in loaded.blocks):
        apic = next((f for f in loaded.frames if f.FrameID == "APIC" and f.data), None)
        if apic is not None:
            result.picture = _picture_block(apic.type, apic.mime, apic.data)
        elif loaded.ape_pictures:
            kind, data = loaded.ape_pictures[0]
            mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
            result.picture = _picture_block(kind, mime, data)
        if result.picture is not None:
            result.changes.append(Change("PICTURE", ID3V2 if apic is not None else APE, "merged", "",
                                         f"<{len(result.picture)} bytes>"))

    # 3. numbers: "03/12" is a number and a total
    for number_key, total_key in NUMBERED.items():
        totals = []
        for entry in entries:
            if entry[0] in (number_key, total_key):
                parsed = split_number(entry[1])
                if parsed is None:
                    result.notes.append(f"number-kept:{entry[0]}={entry[1]}")
                    continue
                number, total = parsed
                if entry[0] == number_key and total not in (None, "0"):
                    totals.append(total)
                if number != entry[1]:
                    note("reformatted", entry[0], entry[1], number)
                    entry[1] = number
        if totals and not any(key == total_key for key, _ in entries):
            at = max(i for i, (key, _) in enumerate(entries) if key == number_key) + 1
            entries.insert(at, [total_key, totals[0]])
            note("added", total_key, "", totals[0])

    # 4. dates
    for entry in entries:
        if entry[0] == "DATE":
            date = iso_date(entry[1])
            if date is None:
                result.notes.append(f"date-kept:{entry[1]}")
            elif date != entry[1]:
                note("reformatted", "DATE", entry[1], date)
                entry[1] = date

    # 5. aliases can leave the same thing said twice
    comments, seen = [], set()
    for key, value in entries:
        if (key, value) not in seen:
            seen.add((key, value))
            comments.append((key, value))

    for key in dict.fromkeys(key for key, value in comments if any(map(is_pua, value))):
        result.notes.append(f"private-use:{key}")
    if layout.lyrics3:
        result.notes.append("lyrics3-removed")
    if comments != original or result.foreign or result.picture is not None:
        if comments or loaded.comments or result.foreign:
            result.comments = comments
    return result
