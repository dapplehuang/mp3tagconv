"""Carry out tag plans and renames without ever putting the audio at risk.

There are two ways a file gets its new tags:

* Rebuilt.  The new file - new tags followed by the untouched audio bytes - is
  written next to the original and verified, and only then swapped in; a crash
  at any point leaves the original intact.  Every MP3 goes this way.
* Patched.  A FLAC file reserves padding after its metadata, so new metadata
  of exactly the old size can be written over the old without moving - or
  touching - a byte of audio.  That turns hours into minutes for a large
  collection.  The bytes replaced are saved first, and what was written is
  read back.

A FLAC file whose new metadata does not fit, or that has tags outside the
FLAC stream to drop, is rebuilt like an MP3.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
from pathlib import Path
from typing import BinaryIO, Callable

from mutagen.flac import FLAC
from mutagen.id3 import ID3

from . import flac
from .tags import Layout, Plan, read_layout
from .undo import UndoLog

PADDING = 1024  # room for later edits without rewriting the whole file
_CHUNK = 1 << 20
_GUARD = 64     # audio bytes checked to be untouched by a patch


class VerificationError(Exception):
    """The file just written is not what was meant to be written."""


def _copy_audio(src: BinaryIO, layout: Layout, dst: BinaryIO | None) -> str:
    """Copy (or just read) the audio span; return its SHA-1."""
    digest = hashlib.sha1()
    src.seek(layout.audio_start)
    remaining = layout.audio_end - layout.audio_start
    while remaining:
        chunk = src.read(min(_CHUNK, remaining))
        if not chunk:
            raise VerificationError("file is shorter than its layout")
        digest.update(chunk)
        if dst is not None:
            dst.write(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def _extended(path: Path) -> str:
    """Path form that Windows does not normalise (trailing dots, long paths)."""
    text = str(path)
    if not os.path.isabs(text):
        # abspath would itself strip a trailing dot or space from the name
        text = os.path.join(os.getcwd(), text)
    if sys.platform != "win32" or text.startswith("\\\\?\\"):
        return text
    text = text.replace("/", "\\")
    return "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text


def _swap_in(replacement: Path, target: Path) -> None:
    """Atomically put ``replacement`` in ``target``'s place."""
    if sys.platform == "win32":
        import ctypes

        # unlike a plain rename, ReplaceFile keeps the creation time,
        # attributes, ACLs and alternate streams of the file being replaced
        if ctypes.windll.kernel32.ReplaceFileW(_extended(target), _extended(replacement),
                                               None, 0, None, None):
            return
    os.replace(replacement, target)


def _rebuild(target: Path, head: bytes, tail: bytes, layout: Layout, expect_sha1: str | None,
             check: Callable[[Path], None] | None) -> None:
    """Write ``head + audio of target + tail`` beside ``target`` and swap it in.

    ``check`` inspects the new file before it replaces the old one.
    """
    fd, name = tempfile.mkstemp(prefix=".mp3tagconv-", suffix=".tmp", dir=target.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as out, open(target, "rb") as src:
            out.write(head)
            sha1 = _copy_audio(src, layout, out)
            out.write(tail)
            out.flush()
            os.fsync(out.fileno())
        if expect_sha1 is not None and sha1 != expect_sha1:
            raise VerificationError("audio differs from the recorded original")
        audio_size = layout.audio_end - layout.audio_start
        with open(temp, "rb") as written:
            span = Layout(len(head) + audio_size + len(tail), len(head), len(head) + audio_size)
            if _copy_audio(written, span, None) != sha1:
                raise VerificationError("audio was not written back intact")
        if check is not None:
            check(temp)
        _swap_in(temp, target)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _overwrite(path: Path, offset: int, data: bytes, before: Callable[[bytes], None] | None) -> None:
    """Replace ``len(data)`` bytes at ``offset``; nothing after them may change.

    ``before`` is handed the bytes about to be replaced.
    """
    with open(path, "r+b") as f:
        f.seek(offset)
        old = f.read(len(data))
        guard = f.read(_GUARD)
        size = os.fstat(f.fileno()).st_size
        if len(old) != len(data):
            raise VerificationError("file is shorter than its layout")
        if before is not None:
            before(old)
        try:
            f.seek(offset)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            f.seek(offset)
            if (f.read(len(data)) != data or f.read(_GUARD) != guard
                    or os.fstat(f.fileno()).st_size != size):
                raise VerificationError("patch did not land where it was meant to")
        except BaseException:
            f.seek(offset)
            f.write(old)
            f.flush()
            os.fsync(f.fileno())
            raise


def _record(undo: UndoLog | None, path: Path, layout: Layout, mtime_ns: int) -> None:
    """Save everything around the audio before a rebuild."""
    if undo is None:
        return
    with open(path, "rb") as f:
        head = f.read(layout.audio_start)
        sha1 = _copy_audio(f, layout, None)
        tail = f.read()
    undo.retag(path, head, tail, sha1, layout.audio_end - layout.audio_start, mtime_ns)


def _check_mp3(expect_frames: int, head_size: int) -> Callable[[Path], None]:
    def check(path: Path) -> None:
        with open(path, "rb") as f:
            seen = read_layout(f)
        if (seen.audio_start, seen.audio_end) != (head_size, seen.size):
            raise VerificationError("new file does not parse as tag + audio")
        if expect_frames:
            reread = ID3(path)
            if reread.version[:2] != (2, 4) or len(reread) != expect_frames:
                raise VerificationError("new tag does not read back as written")
    return check


def _check_flac(plan: flac.Plan, audio_start: int) -> Callable[[Path], None]:
    def check(path: Path) -> None:
        with open(path, "rb") as f:
            seen = flac.read_layout(f)
            blocks, _ = flac.read_blocks(f, 0)
        if (seen.id3v2, seen.audio_start, seen.audio_end) != (None, audio_start, seen.size):
            raise VerificationError("new file does not parse as metadata + audio")
        comments = [b.data for b in blocks if b.kind == flac.VORBIS_COMMENT]
        if len(comments) != 1 or flac.parse_comments(comments[0]) != (plan.vendor, plan.entries()):
            raise VerificationError("new comments do not read back as written")
        # and once more with a parser that is not ours
        if not plan.verbatim and list(FLAC(path).tags or ()) != plan.comments:
            raise VerificationError("new comments do not read back as written")
    return check


def _apply_mp3(plan: Plan, undo: UndoLog | None, mtime_ns: int) -> None:
    head = b""
    if len(plan.tags):
        buffer = io.BytesIO()
        plan.tags.save(buffer, v1=0, v2_version=4, padding=lambda info: PADDING)
        head = buffer.getvalue()
    _record(undo, plan.path, plan.layout, mtime_ns)
    _rebuild(plan.path, head, b"", plan.layout, None, _check_mp3(len(plan.tags), len(head)))


def _apply_flac(plan: flac.Plan, undo: UndoLog | None, mtime_ns: int) -> None:
    path, layout = plan.path, plan.layout
    patch = None if plan.foreign else plan.metadata(layout.audio_start - plan.stream_start)
    if patch is not None:
        replaced = []

        def save(old: bytes) -> None:
            replaced.append(old)
            if undo is not None:
                undo.patch(path, plan.stream_start, old, mtime_ns)

        _overwrite(path, plan.stream_start, patch, save)
        try:
            _check_flac(plan, layout.audio_start)(path)
        except BaseException:
            _overwrite(path, plan.stream_start, replaced[0], None)
            raise
        return
    head = plan.metadata()
    if head is None:
        raise VerificationError("a metadata block is too large for FLAC")
    _record(undo, path, layout, mtime_ns)
    _rebuild(path, head, b"", layout, None, _check_flac(plan, len(head)))


def apply(plan: Plan, undo: UndoLog | None = None, keep_mtime: bool = False) -> None:
    """Give ``plan.path`` the planned tags, and no others."""
    stat = plan.path.stat()
    if isinstance(plan, flac.Plan):
        _apply_flac(plan, undo, stat.st_mtime_ns)
    else:
        _apply_mp3(plan, undo, stat.st_mtime_ns)
    if keep_mtime:
        os.utime(plan.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def restore(path: Path, head: bytes, tail: bytes, audio_sha1: str, audio_size: int,
            mtime_ns: int) -> None:
    """Undo a rebuild: put the original bytes back around the audio of ``path``."""
    with open(path, "rb") as f:
        layout = flac.read_layout(f) if flac.is_flac(f) else read_layout(f)
    if layout.audio_end - layout.audio_start != audio_size:
        raise VerificationError("audio size changed since the conversion")
    _rebuild(path, head, tail, layout, audio_sha1, None)
    os.utime(path, ns=(path.stat().st_atime_ns, mtime_ns))


def unpatch(path: Path, offset: int, data: bytes, mtime_ns: int) -> None:
    """Undo a patch: write the original metadata back over the new."""
    with open(path, "rb") as f:
        layout = flac.read_layout(f)
    if layout.audio_start != offset + len(data):
        raise VerificationError("metadata size changed since the conversion")
    _overwrite(path, offset, data, None)
    os.utime(path, ns=(path.stat().st_atime_ns, mtime_ns))


def rename(src: Path, dst: Path) -> None:
    """Rename without overwriting anything."""
    if os.path.lexists(_extended(dst)) and os.path.normcase(src) != os.path.normcase(dst):
        raise FileExistsError(f"{dst.name} already exists")
    os.rename(_extended(src), _extended(dst))
