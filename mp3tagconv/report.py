"""Human-readable account of a run (Traditional Chinese)."""

from __future__ import annotations

import time
from collections import Counter
from typing import TextIO

from .plausibility import GARBLED, SUSPECT
from .runner import Rename, Result
from .tags import Change, Plan

_KIND = {"decoded": "解碼", "repaired": "修復", "merged": "合併", "cleared": "清除",
         "from-filename": "回填", "fixed": "修正"}
_CONFIDENCE = {"high": "高", "medium": "中", "low": "低", "": ""}
_REASON = {"pua": "造字區字元還原", "windows": "Windows 問題字元清理",
           "ja-marks": "日文代用符號還原"}
_NOTE = {"dropped-frames": "無法解析而捨棄的 ID3v2 frame 數",
         "ape-skipped": "未轉入 ID3v2 的 APEv2 項目",
         "lyrics3-removed": "Lyrics3 標籤已移除"}


def _one_line(text: str, limit: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _reasons(rename: Rename) -> str:
    parts = []
    for reason in rename.fix.reasons:
        if reason.startswith("mojibake:"):
            parts.append(f"亂碼修復 ({reason[9:]})")
        else:
            parts.append(_REASON.get(reason, reason))
    return "、".join(parts)


def _tag_summary(plan: Plan) -> str:
    layout = plan.layout
    have = []
    if layout.id3v2:
        have.append(f"ID3v2.{layout.id3v2[0]}")
    if layout.apev2:
        have.append("APEv2")
    if layout.lyrics3:
        have.append("Lyrics3")
    if layout.id3v1_at is not None:
        have.append("ID3v1")
    return " + ".join(have) or "無標籤"


def _change_line(change: Change) -> str:
    kind = _KIND[change.kind]
    if change.kind == "cleared":
        return f"  ✗ {kind} {change.field:<12} 疑似無法還原的亂碼：{_one_line(change.before, 60)}"
    if change.kind == "from-filename":
        return f"  ＋ {kind} {change.field:<12} 取自檔名：{_one_line(change.after)}"
    if change.kind == "fixed":
        return (f"  ✎ {kind} {change.field:<12} 日文符號：{_one_line(change.before, 45)}"
                f" → {_one_line(change.after, 45)}")
    detail = ""
    if change.encoding not in ("", "ascii", "unicode"):  # only when something was decided
        detail = f"{change.encoding} 信心{_CONFIDENCE[change.confidence]}"
    if change.kind == "merged":
        detail = f"來自 {change.origin}" + (f" {detail}" if detail else "")
    mark = "？" if change.confidence == "low" or change.verdict == SUSPECT else "　"
    line = f"  {mark} {kind} {change.field:<12} [{detail}] {_one_line(change.after)}"
    if change.kind == "repaired":
        line += f"   (原: {_one_line(change.before, 40)})"
    return line


def _evidence(change: Change, out: TextIO) -> None:
    """The raw material for overruling a doubtful decision by hand."""
    if change.raw:
        shown = change.raw[:32].hex(" ") + (" …" if len(change.raw) > 32 else "")
        print(f"         原始位元組: {shown}", file=out)
    if change.alternative and change.kind != "cleared":
        print(f"         其他讀法  : {_one_line(change.alternative, 70)}", file=out)


def write(result: Result, out: TextIO) -> None:
    root, options = result.root, result.options
    rel = lambda path: str(path.relative_to(root))  # noqa: E731
    write_plans = [p for p in result.plans if p.needs_write]
    changes = [c for p in write_plans for c in p.changes]
    encodings = Counter(c.encoding for c in changes if c.kind == "decoded")
    cleared = [(p, c) for p in write_plans for c in p.changes if c.kind == "cleared"]
    refilled = [(p, c) for p in write_plans for c in p.changes if c.kind == "from-filename"]
    unsure = [(p, c) for p in write_plans for c in p.changes
              if c.kind in ("decoded", "repaired", "merged")
              and (c.confidence == "low" or c.verdict in (SUSPECT, GARBLED))]
    failed = [p for p in result.plans if p.error]

    mode = "已套用 (--apply)" if options.apply else "預覽 (dry-run)，沒有修改任何檔案"
    print(f"mp3tagconv 報告  {time.strftime('%Y-%m-%d %H:%M:%S')}", file=out)
    print(f"根目錄: {root}", file=out)
    print(f"模式  : {mode}", file=out)

    print("\n== 摘要 ==", file=out)
    print(f"掃描項目          : {result.entries} (其中 MP3 {result.mp3_files})", file=out)
    print(f"需改寫標籤的 MP3  : {len(write_plans)}"
          + (f" (已寫入 {result.written})" if options.apply else ""), file=out)
    if encodings:
        listing = "、".join(f"{enc} {n}" for enc, n in encodings.most_common())
        print(f"解碼的欄位        : {sum(encodings.values())} ({listing})", file=out)
    print(f"低信心/可疑欄位   : {len(unsure)}", file=out)
    print(f"清除的亂碼欄位    : {len(cleared)} (以檔名回填標題 {len(refilled)})", file=out)
    if options.fix_ja_marks:
        print(f"修正的日文符號欄位: {sum(1 for c in changes if c.kind == 'fixed')}", file=out)
    done = sum(1 for r in result.renames if r.status in ("planned", "done"))
    print(f"檔名/目錄名變更   : {done}", file=out)
    print(f"疑似亂碼檔名(未改): {len(result.suggestions)}", file=out)
    if result.skipped_uncertain:
        print(f"因低信心而略過    : {result.skipped_uncertain}", file=out)
    print(f"錯誤              : {result.errors}", file=out)

    if failed:
        print("\n== 錯誤 (檔案未修改) ==", file=out)
        for plan in failed:
            print(f"  {rel(plan.path)}: {plan.error}", file=out)

    if cleared or unsure:
        print("\n== 請檢查 ==", file=out)
        print("-- 低信心或可疑的判定 (？) --", file=out)
        for plan, change in unsure:
            print(f"  {rel(plan.path)}", file=out)
            print("  " + _change_line(change), file=out)
            _evidence(change, out)
        print("-- 無法還原而清除的欄位 (✗) --", file=out)
        for plan, change in cleared:
            print(f"  {rel(plan.path)}", file=out)
            print("  " + _change_line(change), file=out)
            _evidence(change, out)

    if result.renames or result.suggestions:
        print("\n== 檔名與目錄名 ==", file=out)
        for rename in result.renames:
            status = {"planned": "", "done": "", "conflict": "  ※ 略過：目標名稱已存在",
                      "error": f"  ※ 失敗：{rename.error}"}[rename.status]
            kind = "目錄" if rename.is_dir else "檔案"
            print(f"  [{kind}] {rel(rename.path)}", file=out)
            print(f"      → {rename.fix.fixed}   ({_reasons(rename)}){status}", file=out)
        for rename in result.suggestions:
            print(f"  [疑似亂碼，未更名] {rel(rename.path)}", file=out)
            print(f"      可能是: {rename.fix.suggestion}   ({rename.fix.suggestion_reason})", file=out)

    print("\n== 標籤明細 ==", file=out)
    for plan in result.plans:
        if not plan.needs_write and not plan.notes:
            continue
        print(f"\n■ {rel(plan.path)}", file=out)
        if plan.needs_write:
            kept = len(plan.tags)
            target = "ID3v2.4 UTF-8" if kept else "(無可保留的欄位，移除標籤)"
            print(f"  {_tag_summary(plan)}  →  {target}", file=out)
        for change in plan.changes:
            print(_change_line(change), file=out)
        for note in plan.notes:
            key, _, value = note.partition(":")
            print(f"  ・{_NOTE.get(key, key)}" + (f": {value}" if value else ""), file=out)
        if plan.error:
            print(f"  ※ 錯誤：{plan.error}", file=out)
