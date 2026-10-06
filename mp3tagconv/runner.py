"""Walk a directory tree, plan what to do with every entry, and optionally do it."""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from . import flac, rewrite, tags
from .detect import Hints
from .names import NameFix, confident_route, fix_name
from .undo import UndoLog


@dataclass(frozen=True)
class Options:
    apply: bool = False
    tags: bool = True
    names: bool = True
    force_encoding: str | None = None
    skip_uncertain: bool = False
    keep_mtime: bool = False
    fix_ja_marks: bool = False
    only: str | None = None  #: "mp3" or "flac": leave the tags of the other format alone


@dataclass
class Rename:
    path: Path  #: where the entry was found
    fix: NameFix
    is_dir: bool
    status: str = "planned"  #: planned | done | conflict | error
    error: str = ""

    @property
    def target(self) -> Path:
        return self.path.with_name(self.fix.fixed)


@dataclass
class Result:
    root: Path
    options: Options
    plans: list[tags.Plan] = field(default_factory=list)     #: files with something to report
    renames: list[Rename] = field(default_factory=list)
    suggestions: list[Rename] = field(default_factory=list)  #: possible mojibake, left alone
    files: Counter = field(default_factory=Counter)          #: audio files seen, by format
    entries: int = 0
    written: int = 0
    skipped_uncertain: int = 0

    @property
    def errors(self) -> int:
        return sum(1 for p in self.plans if p.error) + sum(1 for r in self.renames if r.status == "error")


@lru_cache(maxsize=4096)
def _directory_name(name: str, ja_marks: bool) -> str:
    return fix_name(name, is_dir=True, ja_marks=ja_marks).fixed


#: The module that reads and plans each kind of file: ``load()`` and ``plan()``.
_FORMATS = {".mp3": tags, ".flac": flac}


def _format(name: str, only: str | None = None):
    extension = os.path.splitext(name)[1].lower()
    return _FORMATS.get(extension) if only in (None, extension[1:]) else None


def _plan_names(directory: Path, dirnames: list[str], filenames: list[str],
                options: Options) -> dict[str, Rename]:
    """Name fixes for the entries of one directory."""
    entries = [(name, True) for name in dirnames] + [(name, False) for name in filenames]
    # one name that is provably mojibake vouches for its siblings: an
    # archive is extracted with a single (wrong) code page
    routes = frozenset(filter(None, (confident_route(name, is_dir) for name, is_dir in entries)))
    return {name: Rename(directory / name,
                         fix_name(name, is_dir=is_dir, trusted_routes=routes,
                                  ja_marks=options.fix_ja_marks), is_dir)
            for name, is_dir in entries}


def _plan_tags(root: Path, directory: Path, filenames: list[str], fixes: dict[str, Rename],
               options: Options) -> list[tags.Plan]:
    """Tag plans for the audio files of one directory, decided together."""
    relative = directory.relative_to(root).parts
    ancestors = [root.name] + [_directory_name(part, options.fix_ja_marks) for part in relative]
    loaded = []
    for name in sorted(filenames):
        handler = _format(name, options.only)
        if handler is None:
            continue
        fixed = fixes[name].fix.fixed if name in fixes else name
        try:
            item = handler.load(directory / name, Hints.from_names(ancestors + [fixed]), fixed)
        except OSError as exc:
            loaded.append((handler, handler.Plan(directory / name, tags.Layout(0, 0, 0), error=str(exc))))
            continue
        if not item.error:
            tags.first_pass(item, options.force_encoding)
        loaded.append((handler, item))
    # an album is tagged by one person with one tool: let the files agree
    directory_votes: Counter = Counter()
    for _, item in loaded:
        if isinstance(item, tags.Loaded) and not item.error:
            directory_votes += tags.votes(item)
    plans = []
    for handler, item in loaded:
        if isinstance(item, tags.Loaded):
            if not item.error:
                tags.second_pass(item, directory_votes, options.force_encoding)
            item = handler.plan(item, options.fix_ja_marks)
        plans.append(item)
    return plans


def run(root: Path, options: Options, undo: UndoLog | None = None,
        progress: Callable[[Path], None] | None = None, exclude: tuple[Path, ...] = ()) -> Result:
    """Process everything below ``root`` (``root`` itself is never renamed)."""
    result = Result(root, options)
    excluded = [os.path.normcase(os.path.abspath(p)) for p in exclude]

    def is_excluded(path: Path) -> bool:
        text = os.path.normcase(os.path.abspath(path))
        return any(text == e or text.startswith(e + os.sep) for e in excluded)

    # bottom-up: a directory is renamed only after everything inside is done
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        directory = Path(dirpath)
        if is_excluded(directory):
            continue
        dirnames = [d for d in dirnames if not is_excluded(directory / d)]
        if progress:
            progress(directory)
        result.entries += len(dirnames) + len(filenames)
        fixes = _plan_names(directory, dirnames, filenames, options) if options.names else {}

        if options.tags:
            for plan in _plan_tags(root, directory, filenames, fixes, options):
                result.files[plan.format] += 1
                if options.apply and plan.needs_write and not plan.error:
                    if options.skip_uncertain and plan.uncertain:
                        result.skipped_uncertain += 1
                    else:
                        try:
                            rewrite.apply(plan, undo, options.keep_mtime)
                            result.written += 1
                        except Exception as exc:
                            plan.error = f"{type(exc).__name__}: {exc}"
                if isinstance(plan, flac.Plan):
                    plan.blocks = []  # cover art and all: not needed for the report
                if plan.needs_write or plan.error or plan.notes:
                    result.plans.append(plan)

        taken = {os.path.normcase(name) for name in dirnames + filenames}
        for name, entry in fixes.items():
            if entry.fix.suggestion:
                result.suggestions.append(entry)
            if not entry.fix.changed:
                continue
            result.renames.append(entry)
            target = os.path.normcase(entry.fix.fixed)
            if target in taken and target != os.path.normcase(name):
                entry.status, entry.error = "conflict", "name already in use"
                continue
            taken.add(target)
            if options.apply:
                try:
                    rewrite.rename(entry.path, entry.target)
                    if undo is not None:
                        undo.rename(entry.path, entry.target)
                    entry.status = "done"
                except OSError as exc:
                    entry.status, entry.error = "error", f"{type(exc).__name__}: {exc}"
    return result
