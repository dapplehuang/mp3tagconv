"""Record of everything a run changed, and the means to take it back.

The log is a JSON-lines file written as the run goes, so it is usable even if
the run dies half way.  For every rewritten file it keeps the original bytes
before and after the audio; undoing puts them back around the audio, which a
conversion never alters.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable

LOG_NAME = "undo.jsonl"
VERSION = 1


def posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


class UndoLog:
    def __init__(self, directory: Path, root: Path):
        self.directory = directory
        self.root = root
        (directory / "blobs").mkdir(parents=True, exist_ok=True)
        self._log = open(directory / LOG_NAME, "a", encoding="utf-8")
        self._count = 0
        self._write({"op": "begin", "version": VERSION, "root": str(root),
                     "time": time.strftime("%Y-%m-%d %H:%M:%S")})

    def _write(self, entry: dict) -> None:
        self._log.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._log.flush()
        os.fsync(self._log.fileno())

    def retag(self, path: Path, head: bytes, tail: bytes, audio_sha1: str, audio_size: int,
              mtime_ns: int) -> None:
        """Call before the rewritten file replaces ``path``."""
        self._count += 1
        blob = f"blobs/{self._count:06d}.bin"
        with open(self.directory / blob, "wb") as f:
            f.write(head)
            f.write(tail)
            f.flush()
            os.fsync(f.fileno())
        self._write({"op": "retag", "path": posix(path, self.root), "blob": blob,
                     "head": len(head), "tail": len(tail), "audio_sha1": audio_sha1,
                     "audio_size": audio_size, "mtime_ns": mtime_ns})

    def patch(self, path: Path, offset: int, replaced: bytes, mtime_ns: int) -> None:
        """Call before ``replaced``, at ``offset`` of ``path``, is overwritten in place."""
        self._count += 1
        blob = f"blobs/{self._count:06d}.bin"
        with open(self.directory / blob, "wb") as f:
            f.write(replaced)
            f.flush()
            os.fsync(f.fileno())
        self._write({"op": "patch", "path": posix(path, self.root), "blob": blob,
                     "offset": offset, "size": len(replaced), "mtime_ns": mtime_ns})

    def rename(self, old: Path, new: Path) -> None:
        """Call after ``old`` has become ``new``."""
        self._write({"op": "rename", "from": posix(old, self.root), "to": posix(new, self.root)})

    def close(self) -> None:
        self._log.close()


def read_log(directory: Path) -> tuple[Path, list[dict]]:
    """The root a log was made for and its operations, oldest first."""
    root, operations = None, []
    with open(directory / LOG_NAME, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                break  # a torn last line: the operation it described never happened
            if entry["op"] == "begin":
                root = Path(entry["root"])
            else:
                operations.append(entry)
    if root is None:
        raise ValueError(f"{directory / LOG_NAME}: not an undo log")
    return root, operations


def undo(directory: Path, root: Path | None, restore: Callable[..., None],
         rename: Callable[[Path, Path], None], report: Callable[[str], None],
         unpatch: Callable[..., None] | None = None) -> tuple[int, int]:
    """Reverse every operation of a log, newest first.

    ``restore(path, head, tail, audio_sha1, audio_size, mtime_ns)``,
    ``unpatch(path, offset, data, mtime_ns)`` and ``rename(src, dst)`` do the
    actual work.  Returns ``(undone, failed)``.
    """
    logged_root, operations = read_log(directory)
    root = root or logged_root
    undone = failed = 0
    for entry in reversed(operations):
        try:
            if entry["op"] == "rename":
                rename(root / entry["to"], root / entry["from"])
            elif entry["op"] == "patch":
                data = (directory / entry["blob"]).read_bytes()
                if unpatch is None or len(data) != entry["size"]:
                    raise ValueError("cannot undo an in-place patch")
                unpatch(root / entry["path"], entry["offset"], data, entry["mtime_ns"])
            elif entry["op"] == "retag":
                blob = (directory / entry["blob"]).read_bytes()
                head, tail = blob[:entry["head"]], blob[entry["head"]:]
                if len(tail) != entry["tail"]:
                    raise ValueError("backup blob has the wrong size")
                restore(root / entry["path"], head, tail, entry["audio_sha1"],
                        entry["audio_size"], entry["mtime_ns"])
            undone += 1
        except Exception as exc:
            failed += 1
            report(f"{entry.get('path') or entry.get('to')}: {type(exc).__name__}: {exc}")
    return undone, failed
