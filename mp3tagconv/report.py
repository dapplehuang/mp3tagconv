"""Human-readable account of a run (Traditional Chinese)."""

from __future__ import annotations

import time
from collections import Counter
from typing import TextIO

from .plausibility import GARBLED, SUSPECT
from .runner import Rename, Result
from .tags import Change, Plan

#: changes to what a field says ...
_KIND = {"decoded": "解碼", "repaired": "修復", "merged": "合併", "cleared": "清除",
         "from-filename": "回填", "fixed": "修正"}
#: ... as opposed to how it is written down (FLAC)
_FORMAT_KINDS = ("uppercased", "renamed", "reformatted", "added", "emptied", "trimmed")
_CONFIDENCE = {"high": "高", "medium": "中", "low": "低", "": ""}
_REASON = {"pua": "造字區字元還原", "windows": "Windows 問題字元清理",
           "ja-marks": "日文代用符號還原"}
_NOTE = {"dropped-frames": "無法解析而捨棄的 ID3v2 frame 數",
         "ape-skipped": "未轉入 ID3v2 的 APEv2 項目",
         "lyrics3-removed": "Lyrics3 標籤已移除",
         "date-kept": "無法辨識為日期，保留原樣",
         "number-kept": "無法辨識為數字，保留原樣",
         "malformed-comments": "格式不合規格的註解，原樣保留",
         "private-use": "含有無法判定原字的造字區字元，保留原樣，請手動修正"}


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
    if plan.format == "FLAC":
        have.append("Vorbis comment")
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


def _format_line(changes: list[Change]) -> str:
    """Every spelling change of one file on a single line."""
    parts = []
    upper = sum(1 for c in changes if c.kind == "uppercased")
    if upper:
        parts.append(f"鍵名轉大寫 ×{upper}")
    for c in changes:
        if c.kind == "renamed":
            parts.append(f"{c.before} → {c.field}")
        elif c.kind == "reformatted":
            parts.append(f"{c.field} {_one_line(c.before, 30)} → {c.after}")
        elif c.kind == "added":
            parts.append(f"+{c.field}={c.after}")
        elif c.kind == "emptied":
            parts.append(f"移除空值 {c.field}")
        elif c.kind == "trimmed":
            parts.append(f"修剪空白 {c.field}")
    return "；".join(parts)


def _evidence(change: Change, out: TextIO) -> None:
    """The raw material for overruling a doubtful decision by hand."""
    if change.raw:
        shown = change.raw[:32].hex(" ") + (" …" if len(change.raw) > 32 else "")
        print(f"         原始位元組: {shown}", file=out)
    if change.alternative and change.kind != "cleared":
        print(f"         其他讀法  : {_one_line(change.alternative, 70)}", file=out)


def _flac_summary(plans: list[Plan], out: TextIO) -> None:
    """How much each rule of the FLAC clean-up applies, across the whole run."""
    files, values, kept = Counter(), Counter(), Counter()
    for plan in plans:
        kinds = {c.kind for c in plan.changes}
        for kind in kinds & set(_FORMAT_KINDS):
            files[kind] += 1
        for change in plan.changes:
            if change.kind == "renamed":
                values[f"{change.before.upper()} → {change.field}"] += 1
            elif change.kind in ("reformatted", "added"):
                values[f"{change.kind}:{change.field}"] += 1
        if any(c.kind == "merged" for c in plan.changes) or plan.layout.id3v2 or plan.layout.apev2:
            files["foreign"] += 1
        for note in plan.notes:
            key, _, value = note.partition(":")
            if key in ("date-kept", "number-kept"):
                kept[value] += 1
    print("\n== FLAC 格式統一 ==", file=out)
    print(f"鍵名轉大寫              : {files['uppercased']} 個檔案", file=out)
    for label, count in sorted(v for v in values.items() if "→" in v[0]):
        print(f"鍵名統一 {label:<28}: {count} 個", file=out)
    for key in ("TRACKNUMBER", "DISCNUMBER", "TRACKTOTAL", "DISCTOTAL", "DATE"):
        count, added = values[f"reformatted:{key}"], values[f"added:{key}"]
        if count:
            print(f"{key} 改寫格式{'':<{15 - len(key)}}: {count} 個", file=out)
        if added:
            print(f"{key} 由 n/m 拆出{'':<{14 - len(key)}}: {added} 個", file=out)
    print(f"移除空值                : {files['emptied']} 個檔案", file=out)
    print(f"修剪前後空白            : {files['trimmed']} 個檔案", file=out)
    print(f"ID3/APE 標籤併入後移除  : {files['foreign']} 個檔案", file=out)
    if kept:
        print("保留原樣的值 (不是單純的日期或數字):", file=out)
        for value, count in kept.most_common():
            print(f"    {count:4d} × {value}", file=out)
    private = [p for p in plans if any(note.startswith("private-use:") for note in p.notes)]
    if private:
        print(f"含造字區字元、需手動修正  : {len(private)} 個檔案 (見標籤明細)", file=out)


def write(result: Result, out: TextIO) -> None:
    root, options = result.root, result.options
    rel = lambda path: str(path.relative_to(root))  # noqa: E731
    write_plans = [p for p in result.plans if p.needs_write]
    changes = [c for p in write_plans for c in p.changes]
    encodings = Counter(c.encoding for c in changes if c.kind in ("decoded", "repaired"))
    cleared = [(p, c) for p in write_plans for c in p.changes if c.kind == "cleared"]
    refilled = [(p, c) for p in write_plans for c in p.changes if c.kind == "from-filename"]
    unsure = [(p, c) for p in write_plans for c in p.changes
              if c.kind in ("decoded", "repaired", "merged")
              and (c.confidence == "low" or c.verdict in (SUSPECT, GARBLED))]
    failed = [p for p in result.plans if p.error]
    by_format = Counter(p.format for p in write_plans)

    mode = "已套用 (--apply)" if options.apply else "預覽 (dry-run)，沒有修改任何檔案"
    print(f"mp3tagconv 報告  {time.strftime('%Y-%m-%d %H:%M:%S')}", file=out)
    print(f"根目錄: {root}", file=out)
    print(f"模式  : {mode}", file=out)

    print("\n== 摘要 ==", file=out)
    seen = "、".join(f"{fmt} {n}" for fmt, n in sorted(result.files.items())) or "沒有音訊檔"
    print(f"掃描項目          : {result.entries} (其中 {seen})", file=out)
    need = "、".join(f"{fmt} {n}" for fmt, n in sorted(by_format.items())) or "0"
    print(f"需改寫標籤的檔案  : {need}"
          + (f" (已寫入 {result.written})" if options.apply else ""), file=out)
    if encodings:
        listing = "、".join(f"{enc} {n}" for enc, n in encodings.most_common())
        print(f"解碼/修復的欄位   : {sum(encodings.values())} ({listing})", file=out)
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

    flac_plans = [p for p in write_plans if p.format == "FLAC"]
    if flac_plans:
        _flac_summary(flac_plans, out)

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
    unlisted = 0
    for plan in result.plans:
        if not plan.needs_write and not plan.notes:
            continue
        content = [c for c in plan.changes if c.kind in _KIND]
        spelling = [c for c in plan.changes if c.kind in _FORMAT_KINDS]
        if (plan.format == "FLAC" and not content and not plan.notes and not plan.error
                and all(c.kind == "uppercased" for c in spelling)
                and not (plan.layout.id3v2 or plan.layout.apev2 or plan.layout.id3v1_at is not None)):
            unlisted += 1  # nothing but key capitalisation: the summary has the count
            continue
        print(f"\n■ {rel(plan.path)}", file=out)
        outside = plan.layout.id3v2 or plan.layout.apev2 or plan.layout.id3v1_at is not None
        if plan.needs_write and (plan.format != "FLAC" or outside):
            # a FLAC whose tags are where they belong changes spelling, not format
            target = plan.target if plan.kept else "(無可保留的欄位，移除標籤)"
            print(f"  {_tag_summary(plan)}  →  {target}", file=out)
        for change in content:
            print(_change_line(change), file=out)
        if spelling:
            print(f"  　 格式 {_format_line(spelling)}", file=out)
        for note in plan.notes:
            key, _, value = note.partition(":")
            print(f"  ・{_NOTE.get(key, key)}" + (f": {value}" if value else ""), file=out)
        if plan.error:
            print(f"  ※ 錯誤：{plan.error}", file=out)
    if unlisted:
        print(f"\n(另有 {unlisted} 個 FLAC 只有鍵名大小寫需要調整，不逐一列出)", file=out)
