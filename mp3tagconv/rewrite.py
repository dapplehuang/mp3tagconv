"""Carry out tag plans and renames without ever putting the audio at risk.

A file is never modified in place.  The new file - new tag followed by the
untouched audio bytes - is written next to the original and verified, and only
then swapped in; a crash at any point leaves the original intact.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
from pathlib import Path
from typing import BinaryIO

from mutagen.id3 import ID3

from .tags import Layout, Plan, read_layout
from .undo import UndoLog

PADDING = 1024  # room for later edits without rewriting the whole file
_CHUNK = 1 << 20


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


def _write_verified(target: Path, head: bytes, tail: bytes, source: Path, layout: Layout,
                    expect_sha1: str | None, expect_frames: int | None) -> str:
    """Build ``head + audio of source + tail`` beside ``target`` and swap it in."""
    fd, name = tempfile.mkstemp(prefix=".mp3tagconv-", suffix=".tmp", dir=target.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
            out.write(head)
            sha1 = _copy_audio(src, layout, out)
            out.write(tail)
            out.flush()
            os.fsync(out.fileno())
        if expect_sha1 is not None and sha1 != expect_sha1:
            raise VerificationError("audio differs from the recorded original")
        audio_size = layout.audio_end - layout.audio_start
        with open(temp, "rb") as check:
            written = Layout(len(head) + audio_size + len(tail), len(head), len(head) + audio_size)
            if _copy_audio(check, written, None) != sha1:
                raise VerificationError("audio was not written back intact")
            if expect_frames is not None:
                seen = read_layout(check)
                if (seen.audio_start, seen.audio_end) != (len(head), seen.size):
                    raise VerificationError("new file does not parse as tag + audio")
        if expect_frames:
            reread = ID3(temp)
            if reread.version[:2] != (2, 4) or len(reread) != expect_frames:
                raise VerificationError("new tag does not read back as written")
        _swap_in(temp, target)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return sha1


def apply(plan: Plan, undo: UndoLog | None = None, keep_mtime: bool = False) -> None:
    """Replace every tag of ``plan.path`` by the planned ID3v2.4 tag."""
    path, layout, tags = plan.path, plan.layout, plan.tags
    head = b""
    if len(tags):
        buffer = io.BytesIO()
        tags.save(buffer, v1=0, v2_version=4, padding=lambda info: PADDING)
        head = buffer.getvalue()
    stat = path.stat()
    if undo is not None:
        with open(path, "rb") as f:
            old_head = f.read(layout.audio_start)
            sha1 = _copy_audio(f, layout, None)
            old_tail = f.read()
        undo.retag(path, old_head, old_tail, sha1, layout.audio_end - layout.audio_start,
                   stat.st_mtime_ns)
    _write_verified(path, head, b"", path, layout, None, len(tags))
    if keep_mtime:
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def restore(path: Path, head: bytes, tail: bytes, audio_sha1: str, audio_size: int,
            mtime_ns: int) -> None:
    """Put the original tag bytes back around the audio of ``path``."""
    with open(path, "rb") as f:
        layout = read_layout(f)
    if layout.audio_end - layout.audio_start != audio_size:
        raise VerificationError("audio size changed since the conversion")
    _write_verified(path, head, tail, path, layout, audio_sha1, None)
    os.utime(path, ns=(path.stat().st_atime_ns, mtime_ns))


def rename(src: Path, dst: Path) -> None:
    """Rename without overwriting anything."""
    if os.path.lexists(_extended(dst)) and os.path.normcase(src) != os.path.normcase(dst):
        raise FileExistsError(f"{dst.name} already exists")
    os.rename(_extended(src), _extended(dst))
