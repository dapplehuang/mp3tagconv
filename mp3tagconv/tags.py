"""Read every tag an MP3 carries, decide what its text really says, and plan a
single ID3v2.4 UTF-8 tag to replace them all.

Planning never touches the file; :mod:`rewrite` carries a plan out.
"""

from __future__ import annotations

import re
import struct
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Mapping

from mutagen.id3 import (APIC, COMM, ID3, TCON, TDRC, TIT2, TXXX, USLT, Encoding, Frames,
                         ID3TimeStamp)

from .charsets import BIG5, EUCJP, EUCKR, GBK, SJIS, WESTERN
from .detect import DEFAULT_PRIORS, HIGH, Detection, Hints, detect, repair_unicode
from .japanese import restore_marks
from .names import misreading, title_from_filename
from .plausibility import GARBLED, OK, SUSPECT, plausibility, verdict

ID3V2, ID3V1, APE, FILENAME = "ID3v2", "ID3v1", "APEv2", "filename"

_LEGACY = (BIG5, SJIS, GBK, EUCJP, EUCKR, WESTERN)


# --------------------------------------------------------------------------
# File layout
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """Where the audio sits between the tags of an MP3 file."""

    size: int
    audio_start: int  #: end of the leading ID3v2 tag(s)
    audio_end: int    #: start of the trailing tags
    id3v2: tuple[int, int] | None = None  #: (major, revision) of the leading tag
    id3v1_at: int | None = None
    apev2_at: tuple[int, int] | None = None  #: items and footer of the APEv2 tag
    lyrics3: bool = False

    @property
    def apev2(self) -> bool:
        return self.apev2_at is not None

    @property
    def tagged(self) -> bool:
        return self.audio_start > 0 or self.audio_end < self.size


def _read_at(f: BinaryIO, pos: int, length: int) -> bytes:
    f.seek(pos)
    return f.read(length)


def read_layout(f: BinaryIO) -> Layout:
    f.seek(0, 2)
    size = f.tell()

    start, version = 0, None
    while start + 10 <= size:
        header = _read_at(f, start, 10)
        if header[:3] != b"ID3" or 0xFF in header[3:5] or any(b & 0x80 for b in header[6:10]):
            break
        body = (header[6] << 21) | (header[7] << 14) | (header[8] << 7) | header[9]
        length = 10 + body + (10 if header[5] & 0x10 else 0)
        if start + length > size:
            break
        version = version or (header[3], header[4])
        start += length

    end, id3v1_at, apev2_at, lyrics3 = size, None, None, False
    while True:
        if end - 128 >= start and _read_at(f, end - 128, 3) == b"TAG":
            end -= 128
            if id3v1_at is None:
                id3v1_at = end
            continue
        if end - 32 >= start:
            footer = _read_at(f, end - 32, 32)
            if footer[:8] == b"APETAGEX":
                body, _, flags = struct.unpack_from("<III", footer, 12)
                length = body + (32 if flags & 0x80000000 else 0)
                if body >= 32 and end - length >= start:
                    apev2_at = apev2_at or (end - body, end)
                    end -= length
                    continue
        if end - 15 >= start:
            marker = _read_at(f, end - 9, 9)
            if marker == b"LYRICS200":
                digits = _read_at(f, end - 15, 6)
                length = int(digits) + 15 if digits.isdigit() else 0
                if length and end - length >= start and _read_at(f, end - length, 11) == b"LYRICSBEGIN":
                    end -= length
                    lyrics3 = True
                    continue
            elif marker == b"LYRICSEND":
                # Lyrics3 v1 has no size field; the lyrics are at most 5100 bytes
                window_at = max(start, end - 5120)
                found = _read_at(f, window_at, end - window_at).rfind(b"LYRICSBEGIN")
                if found >= 0:
                    end = window_at + found
                    lyrics3 = True
                    continue
        break
    return Layout(size, start, end, version, id3v1_at, apev2_at, lyrics3)


# --------------------------------------------------------------------------
# Text slots: every string a tag holds, with the decision made about it
# --------------------------------------------------------------------------


@dataclass
class Slot:
    label: str
    origin: str
    stored: str                  #: the value as a naive reader shows it today
    raw: bytes | None = None     #: legacy bytes awaiting detection
    truncated: bool = False
    result: Detection | None = None

    @property
    def text(self) -> str:
        return self.result.text

    @property
    def verdict(self) -> str:
        return self.result.verdict


@dataclass(frozen=True)
class Change:
    """One line of the report: what happens to one field."""

    field: str
    origin: str
    kind: str        #: decoded | repaired | merged | cleared | from-filename | fixed
    before: str
    after: str
    encoding: str = ""
    confidence: str = ""
    verdict: str = OK
    raw: bytes = b""       #: the legacy bytes, for judging a doubtful decision by hand
    alternative: str = ""  #: the next best reading of them


def _evidence(slot: Slot) -> dict:
    """What a person needs to second-guess the decision made for ``slot``."""
    other = slot.result.runner_up
    return {"raw": slot.raw or b"",
            "alternative": f"[{other.encoding}] {other.text}" if other else ""}


_TEXT_ATTRS = ("text", "desc", "filename", "seller", "people")


def _strings(value) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _map_strings(value, fn: Callable[[str], str]):
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, (list, tuple)):
        return type(value)(_map_strings(item, fn) for item in value)
    return value


def _frame_slots(frame) -> dict[str, Slot]:
    """Slots for the distinct strings of an ID3v2 frame, keyed by stored value."""
    slots: dict[str, Slot] = {}
    legacy = frame.encoding == Encoding.LATIN1
    for attr in _TEXT_ATTRS:
        for value in _strings(getattr(frame, attr, None)):
            if value in slots:
                continue
            raw = None
            if legacy:
                try:
                    raw = value.encode("latin-1")
                except UnicodeEncodeError:
                    pass
            slots[value] = Slot(frame.HashKey, ID3V2, value, raw)
    return slots


_V1_FIELDS = (("title", "TIT2", 3, 30), ("artist", "TPE1", 33, 30), ("album", "TALB", 63, 30))


def _parse_v1(block: bytes) -> tuple[dict[str, Slot], dict[str, str]]:
    """Text slots (by frame id) and plain values of an ID3v1 tag."""
    fields = [(name, fid, block[at:at + width], width) for name, fid, at, width in _V1_FIELDS]
    comment, plain = block[97:127], {}
    if comment[28] == 0 and comment[29] != 0:  # ID3v1.1 track number
        plain["TRCK"] = str(comment[29])
        comment = comment[:28]
    fields.append(("comment", "COMM", comment, len(comment)))
    slots = {}
    for name, fid, data, width in fields:
        raw = data.split(b"\x00")[0].rstrip(b" ")
        if raw:
            slots[fid] = Slot(f"{ID3V1} {name}", ID3V1, raw.decode("latin-1"), raw, len(raw) >= width)
    year = block[93:97].decode("latin-1")
    if year.isdigit() and int(year):
        plain["TDRC"] = year
    if block[127] != 255:
        genre = TCON(encoding=Encoding.UTF8, text=[str(block[127])]).genres
        if genre and genre[0] != "Unknown":
            plain["TCON"] = genre[0]
    return slots, plain


# APEv2 item keys (lower case) and the ID3v2 frame that holds the same thing
_APE_FRAMES = {
    "title": "TIT2", "subtitle": "TIT3", "artist": "TPE1", "album": "TALB",
    "album artist": "TPE2", "albumartist": "TPE2", "composer": "TCOM", "conductor": "TPE3",
    "track": "TRCK", "tracknumber": "TRCK", "disc": "TPOS", "discnumber": "TPOS",
    "year": "TDRC", "date": "TDRC", "genre": "TCON", "comment": "COMM",
    "copyright": "TCOP", "publisher": "TPUB", "label": "TPUB", "isrc": "TSRC",
    "bpm": "TBPM", "lyrics": "USLT", "unsyncedlyrics": "USLT",
}
_APE_PICTURES = {"cover art (front)": 3, "cover art (back)": 4}


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


@dataclass
class Loaded:
    """Everything read from one file, before any decision is final."""

    path: Path
    layout: Layout
    hints: Hints
    name: str                                                          # file name once repaired
    frames: list = field(default_factory=list)                         # ID3v2 frames
    frame_slots: dict[int, dict[str, Slot]] = field(default_factory=dict)
    dropped: int = 0                                                   # unparseable ID3v2 frames
    v1_slots: dict[str, Slot] = field(default_factory=dict)
    v1_plain: dict[str, str] = field(default_factory=dict)
    ape_slots: list[tuple[str, list[Slot]]] = field(default_factory=list)  # (item key, values)
    ape_pictures: list[tuple[int, bytes]] = field(default_factory=list)
    ape_skipped: list[str] = field(default_factory=list)
    native_slots: list[Slot] = field(default_factory=list)             # the format's own tag (FLAC)
    error: str | None = None

    def slots(self) -> Iterator[Slot]:
        yield from self.native_slots
        for slots in self.frame_slots.values():
            yield from slots.values()
        yield from self.v1_slots.values()
        for _, values in self.ape_slots:
            yield from values


def load(path: Path, hints: Hints, name: str | None = None) -> Loaded:
    with open(path, "rb") as f:
        layout = read_layout(f)
        v1_block = _read_at(f, layout.id3v1_at, 128) if layout.id3v1_at is not None else None
        ape_block = None
        if layout.apev2_at is not None:
            ape_block = _read_at(f, layout.apev2_at[0], layout.apev2_at[1] - layout.apev2_at[0])
    loaded = Loaded(path, layout, hints, name or path.name)
    try:
        if layout.id3v2 is not None:
            # translate=False: keep the frames as written, their text is fixed first
            id3 = ID3(path, translate=False, load_v1=False)
            loaded.frames = list(id3.values())
            loaded.dropped = len(id3.unknown_frames)
            for frame in loaded.frames:
                if hasattr(frame, "encoding"):
                    loaded.frame_slots[id(frame)] = _frame_slots(frame)
        if v1_block is not None:
            loaded.v1_slots, loaded.v1_plain = _parse_v1(v1_block)
        if ape_block is not None:
            _load_ape(ape_block, loaded)
    except Exception as exc:  # mutagen raises a zoo of errors on damaged tags
        loaded.error = f"{type(exc).__name__}: {exc}"
    return loaded


def _ape_items(block: bytes) -> Iterator[tuple[str, int, bytes]]:
    """``(key, kind, value)`` of each item of an APEv2 tag (items + footer).

    Parsed by hand: mutagen refuses the whole tag when one text item is not
    UTF-8, and old taggers happily wrote Shift_JIS or Big5 there.
    """
    count, = struct.unpack_from("<I", block, len(block) - 16)
    pos, end = 0, len(block) - 32
    for _ in range(count):
        if pos + 8 > end:
            break
        size, flags = struct.unpack_from("<II", block, pos)
        key_end = block.find(b"\x00", pos + 8, end)
        if key_end < 0 or key_end + 1 + size > end:
            break
        yield (block[pos + 8:key_end].decode("latin-1"), (flags >> 1) & 3,
               block[key_end + 1:key_end + 1 + size])
        pos = key_end + 1 + size


def _load_ape(block: bytes, loaded: Loaded) -> None:
    for key, kind, value in _ape_items(block):
        if kind == 0:  # text, values separated by NUL
            try:
                slots = [Slot(f"{APE} {key}", APE, text)
                         for text in value.decode("utf-8").split("\x00") if text.strip()]
            except UnicodeDecodeError:
                # not UTF-8 after all: treat like any other legacy bytes
                slots = [Slot(f"{APE} {key}", APE, part.decode("latin-1"), part)
                         for part in value.split(b"\x00") if part.strip()]
            if slots:
                loaded.ape_slots.append((key, slots))
        elif kind == 1 and key.lower() in _APE_PICTURES:
            # "<file name>\0<image data>"
            loaded.ape_pictures.append((_APE_PICTURES[key.lower()], value.partition(b"\x00")[2]))
        else:
            loaded.ape_skipped.append(key)


# --------------------------------------------------------------------------
# Deciding
# --------------------------------------------------------------------------


def _decide(slot: Slot, hints: Hints, priors: Mapping[str, float], force: str | None,
            damaged: bool = False) -> None:
    if slot.raw is None:
        slot.result = repair_unicode(slot.stored, hints=hints, priors=priors)
        return
    if force:
        forced = detect(slot.raw, hints=hints, truncated=slot.truncated, encodings=(force,))
        if forced.encoding == force or forced.encoding == "ascii":
            slot.result = forced
            return
    slot.result = detect(slot.raw, hints=hints, priors=priors, truncated=slot.truncated,
                         damaged_context=damaged)


_WIDENED = "unicode-"  # Detection.encoding prefix of repaired Unicode text
_MISREAD = "misread "  # ... and of Unicode text that was one code page read as another


def votes(loaded: Loaded) -> Counter:
    """Encodings this file's unambiguous legacy fields are in.

    Unicode text that proved to be widened legacy bytes votes too, under its
    ``unicode-<encoding>`` label.
    """
    tally: Counter = Counter()
    for slot in loaded.slots():
        r = slot.result
        if slot.raw is not None and r.verdict == OK and r.margin >= HIGH and r.encoding in _LEGACY:
            tally[r.encoding] += 1
        elif slot.raw is None and r.encoding.startswith(_WIDENED) and r.encoding[len(_WIDENED):] in _LEGACY:
            tally[r.encoding] += 1
        elif slot.raw is None and r.encoding == "unicode":
            reading = misreading(slot.stored)
            if reading and reading[2]:
                tally[_MISREAD + reading[1]] += 1
    return tally


def _top(tally: Counter, widened: bool) -> str | None:
    """Most voted-for encoding among the legacy (or the widened) ones."""
    ranked = [key for key, _ in tally.most_common()
              if key.startswith(_WIDENED) == widened and not key.startswith(_MISREAD)]
    return ranked[0] if ranked else None


def first_pass(loaded: Loaded, force: str | None = None) -> None:
    for slot in loaded.slots():
        _decide(slot, loaded.hints, DEFAULT_PRIORS, force)


def second_pass(loaded: Loaded, directory_votes: Counter, force: str | None = None) -> None:
    """Settle the ambiguous fields using what the unambiguous ones turned out to be."""
    priors = dict(DEFAULT_PRIORS)
    own = votes(loaded)
    for tally, bonus in ((own, 2.0), (directory_votes, 1.0)):
        encoding = _top(tally, widened=False)
        if encoding:
            priors[encoding] = priors.get(encoding, 0.0) + bonus
    for slot in loaded.slots():
        if slot.raw is not None and slot.result.margin < HIGH and slot.verdict != GARBLED:
            _decide(slot, loaded.hints, priors, force)
    # a tool that widened one field's bytes to Unicode widened them all, also
    # the titles too short to prove it by themselves
    widened = _top(own, widened=True) or _top(directory_votes, widened=True)
    if widened:
        for slot in loaded.slots():
            if slot.raw is None and slot.result.encoding == "unicode":
                slot.result = repair_unicode(slot.stored, hints=loaded.hints, priors=priors,
                                             trusted=widened[len(_WIDENED):])
    # Unicode text that is one code page read as another (楅栘 for 鈴木): a
    # field that proves the route vouches for the short ones beside it
    readings = {id(slot): misreading(slot.stored) for slot in loaded.slots()
                if slot.raw is None and slot.result.encoding == "unicode"}
    proven = {reading[1] for reading in readings.values() if reading and reading[2]}
    proven |= {key[len(_MISREAD):] for key in directory_votes if key.startswith(_MISREAD)}
    for slot in loaded.slots():
        reading = readings.get(id(slot))
        if reading and reading[1] in proven:
            p = plausibility(reading[0])
            slot.result = Detection(reading[0], _MISREAD + reading[1], 99.0, p, verdict(p))
    # one field beyond repair means the same accident hit its neighbours
    if any(slot.verdict == GARBLED for slot in loaded.slots()):
        for slot in loaded.slots():
            if slot.raw is not None and slot.verdict == SUSPECT:
                _decide(slot, loaded.hints, priors, force, damaged=True)


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


@dataclass
class Plan:
    path: Path
    layout: Layout
    tags: ID3 | None = None      #: the tag to write; None leaves the file alone
    changes: list[Change] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    format = "MP3"
    target = "ID3v2.4 UTF-8"  #: what the file's tags become

    @property
    def needs_write(self) -> bool:
        return self.tags is not None

    @property
    def kept(self) -> int:
        """How many fields the rewritten tag holds."""
        return len(self.tags) if self.tags is not None else 0

    @property
    def uncertain(self) -> bool:
        return any(c.confidence == "low" and c.kind in ("decoded", "repaired") for c in self.changes)


def _confidence(slot: Slot) -> str:
    return slot.result.confidence if slot.result.encoding != "ascii" else ""


def _has(tags: ID3, frame_id: str) -> bool:
    if frame_id == "TDRC":
        return "TDRC" in tags or "TYER" in tags
    if frame_id == "COMM":
        return any(frame.desc == "" for frame in tags.getall("COMM"))
    return bool(tags.getall(frame_id))


def _timestamp(text: str) -> str:
    """ID3v2.4 timestamps are ISO 8601; ``2002/08/11`` and ``20040923`` are not."""
    text = text.strip()
    match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text)
    if match and 1 <= int(match[2]) <= 12 and 1 <= int(match[3]) <= 31:
        return "-".join(match.groups())
    return re.sub(r"^(\d{4})[/.](\d{1,2})(?:[/.](\d{1,2}))?$",
                  lambda m: "-".join(part.zfill(2) for part in m.groups() if part), text)


def _upgrade_date(tags: ID3) -> None:
    """Fold the ID3v2.3 TYER/TDAT/TIME frames into TDRC.

    mutagen's own upgrade silently discards a TYER that is not exactly a
    year, and ``20040923`` is a common thing to find there.
    """
    year, date, clock = (tags.pop(key, None) for key in ("TYER", "TDAT", "TIME"))
    if year is None or "TDRC" in tags:
        return
    stamps = []
    for index, text in enumerate(year.text):
        stamp = _timestamp(str(text))
        day = re.fullmatch(r"(\d{2})(\d{2})", str(date.text[index])) \
            if date is not None and index < len(date.text) else None
        if day and re.fullmatch(r"\d{4}", stamp):
            stamp += f"-{day[2]}-{day[1]}"  # TDAT is DDMM
            hour = re.fullmatch(r"(\d{2})(\d{2})", str(clock.text[index])) \
                if clock is not None and index < len(clock.text) else None
            if hour:
                stamp += f"T{hour[1]}:{hour[2]}"
        if stamp:
            stamps.append(stamp)
    if stamps:
        tags.add(TDRC(encoding=Encoding.UTF8, text=stamps))


def _make_frame(frame_id: str, values: list[str]):
    utf8 = Encoding.UTF8
    if frame_id == "COMM":
        return COMM(encoding=utf8, lang="XXX", desc="", text=values)
    if frame_id == "USLT":
        return USLT(encoding=utf8, lang="XXX", desc="", text="\n".join(values))
    if frame_id == "TDRC":
        return TDRC(encoding=utf8, text=[_timestamp(v) for v in values])
    if frame_id.startswith("TXXX:"):
        return TXXX(encoding=utf8, desc=frame_id[5:], text=values)
    return Frames[frame_id](encoding=utf8, text=values)


def _worst(slots: list[Slot]) -> str:
    verdicts = {slot.verdict for slot in slots}
    return GARBLED if GARBLED in verdicts else SUSPECT if SUSPECT in verdicts else OK


def plan(loaded: Loaded, fix_marks: bool = False) -> Plan:
    """Turn the decided slots of ``loaded`` into the tag to write.

    ``fix_marks`` also restores ー and ・ where a stand-in was typed for them
    (see :mod:`japanese`).
    """
    layout = loaded.layout
    result = Plan(loaded.path, layout)
    if loaded.error:
        result.error = loaded.error
        return result
    if not layout.tagged:
        return result

    tags = ID3()
    changed = False
    lost_title = False

    def polish(text: str, label: str, origin: str) -> str:
        fixed = restore_marks(text, loaded.hints) if fix_marks else text
        if fixed != text:
            result.changes.append(Change(label, origin, "fixed", text, fixed))
        return fixed

    # 1. the ID3v2 frames, with their text put right
    for frame in loaded.frames:
        slots = loaded.frame_slots.get(id(frame))
        if slots is None:
            tags.add(frame)  # binary frame: nothing to decide
            continue
        if _worst(list(slots.values())) == GARBLED:
            garbled = [s for s in slots.values() if s.verdict == GARBLED]
            shown = " / ".join(s.text for s in garbled)
            result.changes.append(Change(frame.HashKey, ID3V2, "cleared", shown, "",
                                         verdict=GARBLED, **_evidence(garbled[0])))
            lost_title |= frame.FrameID == "TIT2"
            changed = True
            continue
        for slot in slots.values():
            if slot.raw is not None and any(b >= 0x80 for b in slot.raw):
                result.changes.append(Change(slot.label, ID3V2, "decoded", slot.stored, slot.text,
                                             slot.result.encoding, _confidence(slot), slot.verdict,
                                             **_evidence(slot)))
                changed = True
            elif slot.text != slot.stored:
                result.changes.append(Change(slot.label, ID3V2, "repaired", slot.stored, slot.text,
                                             slot.result.encoding, _confidence(slot), slot.verdict))
                changed = True
        decided = {stored: polish(slot.text, slot.label, ID3V2) for stored, slot in slots.items()}
        changed |= any(decided[stored] != slot.text for stored, slot in slots.items())
        for attr in _TEXT_ATTRS:
            value = getattr(frame, attr, None)
            if value is not None:
                setattr(frame, attr, _map_strings(value, decided.__getitem__))
        if frame.encoding in (Encoding.UTF16, Encoding.UTF16BE):
            changed = True
        frame.encoding = Encoding.UTF8
        tags.add(frame)
    if loaded.dropped:
        result.notes.append(f"dropped-frames:{loaded.dropped}")

    # 2. what only the APEv2 and ID3v1 tags know
    offers: dict[str, list[tuple[str, list[Slot] | None, list[str]]]] = {}
    for key, slots in loaded.ape_slots:
        frame_id = _APE_FRAMES.get(key.lower(), f"TXXX:{key}")
        offers.setdefault(frame_id, []).append((APE, slots, []))
    for frame_id, slot in loaded.v1_slots.items():
        offers.setdefault(frame_id, []).append((ID3V1, [slot], []))
    for frame_id, value in loaded.v1_plain.items():
        offers.setdefault(frame_id, []).append((ID3V1, None, [value]))

    for frame_id, sources in offers.items():
        if frame_id == "TIT2":
            lost_title |= any(slots and _worst(slots) == GARBLED for _, slots, _ in sources)
        if _has(tags, frame_id):
            continue
        usable = [(origin, slots, plain) for origin, slots, plain in sources
                  if not slots or _worst(slots) != GARBLED]
        usable.sort(key=lambda source: bool(source[1]) and _worst(source[1]) == SUSPECT)
        if not usable:
            continue
        origin, slots, plain = usable[0]
        values = [slot.text.strip() for slot in slots] if slots else plain
        values = [v for v in values if v]
        if not values:
            continue
        first = slots[0] if slots else None
        result.changes.append(Change(
            frame_id, origin, "merged", "", " / ".join(values),
            first.result.encoding if first else "", _confidence(first) if first else "",
            _worst(slots) if slots else OK, **(_evidence(first) if first else {})))
        tags.add(_make_frame(frame_id, [polish(value, frame_id, origin) for value in values]))
    for kind, data in loaded.ape_pictures:
        if data and not tags.getall("APIC"):
            mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
            tags.add(APIC(encoding=Encoding.UTF8, mime=mime, type=kind, desc="", data=data))
            result.changes.append(Change("APIC", APE, "merged", "", f"<{mime}, {len(data)} bytes>"))
    if loaded.ape_skipped:
        result.notes.append("ape-skipped:" + ",".join(loaded.ape_skipped))
    if layout.lyrics3:
        result.notes.append("lyrics3-removed")

    # 3. a title lost to mojibake is still in the file name
    if lost_title and not tags.getall("TIT2"):
        title = title_from_filename(loaded.name)
        result.changes.append(Change("TIT2", FILENAME, "from-filename", "", title))
        tags.add(TIT2(encoding=Encoding.UTF8, text=[polish(title, "TIT2", FILENAME)]))

    # 4. ID3v2.4 conventions
    _upgrade_date(tags)
    tags.update_to_v24()
    genre = tags.get("TCON")
    if genre is not None and len(genre.text) > 1 and "Unknown" in genre.text:
        genre.text = [g for g in genre.text if g != "Unknown"]  # from "(255)Anime"
    date = tags.get("TDRC")
    if date is not None:
        date.text = [ID3TimeStamp(_timestamp(str(stamp))) for stamp in date.text]
    for key, frame in list(tags.items()):
        if hasattr(frame, "encoding"):
            frame.encoding = Encoding.UTF8
        # a text frame that says nothing is not written at all, and would
        # make the new tag fail to read back as planned
        if key[0] == "T" and not any(str(value) for value in frame.text):
            del tags[key]
            changed = True

    if (changed or layout.id3v2 is None or layout.id3v2[0] != 4
            or layout.id3v1_at is not None or layout.apev2 or layout.lyrics3):
        result.tags = tags
    return result
