"""Command line interface."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__, charsets, report, rewrite
from .runner import Options, run
from .undo import UndoLog, undo


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mp3tagconv",
        description="將 MP3 標籤轉為 ID3v2.4 UTF-8、統一 FLAC 標籤的格式，"
                    "並修復檔名/目錄名的編碼問題。")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    convert = commands.add_parser(
        "convert", help="掃描目錄並轉換 (預設只預覽，不修改檔案)",
        description="遞迴掃描 ROOT。預設只產生報告；確認報告內容後加上 --apply 才會寫入。")
    convert.add_argument("root", type=Path, metavar="ROOT", help="要處理的目錄")
    convert.add_argument("--apply", action="store_true", help="實際寫入標籤並更名")
    convert.add_argument("--report", type=Path, metavar="FILE",
                         help="報告輸出路徑 (預設: 目前目錄下的 mp3tagconv-<時間>-<模式>.txt)")
    convert.add_argument("--undo-dir", type=Path, metavar="DIR",
                         help="還原資料存放目錄 (預設: 目前目錄下的 mp3tagconv-undo-<時間>)")
    convert.add_argument("--no-undo", action="store_true", help="不保存還原資料")
    convert.add_argument("--no-tags", action="store_true", help="不處理標籤，只檢查檔名")
    convert.add_argument("--no-rename", action="store_true", help="不檢查檔名，只處理標籤")
    convert.add_argument("--only", choices=("mp3", "flac"),
                         help="只處理這種格式的標籤 (檔名檢查不受影響)")
    convert.add_argument("--force-encoding", metavar="ENC", choices=charsets.TAG_ENCODINGS,
                         help="舊編碼文字一律先以此編碼解讀，解不開才自動偵測 "
                              f"({', '.join(charsets.TAG_ENCODINGS)})")
    convert.add_argument("--fix-ja-marks", action="store_true",
                         help="還原標籤與檔名/目錄名中，當年因 Big5 打不出來而代用的日文符號："
                              "片假名後的「一」「－」→「ー」，日文中的「‧」→「・」")
    convert.add_argument("--skip-uncertain", action="store_true",
                         help="含有低信心判定的檔案先不寫入")
    convert.add_argument("--keep-mtime", action="store_true", help="保留檔案原本的修改時間")

    restore = commands.add_parser("undo", help="依還原資料把檔案恢復原狀")
    restore.add_argument("undo_dir", type=Path, metavar="UNDO_DIR", help="convert --apply 產生的還原目錄")
    restore.add_argument("--root", type=Path, metavar="DIR", help="音樂目錄已搬移時，指定新位置")
    return parser


def _convert(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    if not root.is_dir():
        print(f"找不到目錄: {args.root}", file=sys.stderr)
        return 2
    stamp = time.strftime("%Y%m%d-%H%M%S")
    options = Options(apply=args.apply, tags=not args.no_tags, names=not args.no_rename,
                      force_encoding=args.force_encoding, skip_uncertain=args.skip_uncertain,
                      keep_mtime=args.keep_mtime, fix_ja_marks=args.fix_ja_marks, only=args.only)
    report_path = args.report or Path(f"mp3tagconv-{stamp}-{'applied' if args.apply else 'preview'}.txt")
    log = None
    exclude: tuple[Path, ...] = ()
    if args.apply and not args.no_undo:
        undo_dir = (args.undo_dir or Path(f"mp3tagconv-undo-{stamp}")).resolve()
        log = UndoLog(undo_dir, root)
        exclude = (undo_dir,)

    seen = 0

    def progress(directory: Path) -> None:
        nonlocal seen
        seen += 1
        print(f"\r掃描中… 已處理 {seen} 個目錄", end="", file=sys.stderr, flush=True)

    try:
        result = run(root, options, log, progress, exclude)
    finally:
        if log is not None:
            log.close()
        print(file=sys.stderr)

    # utf-8-sig: Notepad and Excel on a Big5 system need the BOM to pick UTF-8
    with open(report_path, "w", encoding="utf-8-sig", newline="\r\n") as out:
        report.write(result, out)

    writes = sum(1 for p in result.plans if p.needs_write)
    renames = sum(1 for r in result.renames if r.status in ("planned", "done"))
    if args.apply:
        print(f"完成：寫入 {result.written} 個檔案的標籤，更名 {renames} 個項目，錯誤 {result.errors}。")
        if log is not None:
            print(f"還原資料：{log.directory}")
            print(f"  要復原請執行: mp3tagconv undo \"{log.directory}\"")
    else:
        seen = "、".join(f"{n} 個 {fmt}" for fmt, n in sorted(result.files.items())) or "0 個音訊檔"
        print(f"預覽：{seen} 中有 {writes} 個需要改寫標籤，"
              f"{renames} 個檔名/目錄名需要變更。沒有修改任何檔案。")
        print("  確認報告後，加上 --apply 重新執行即可寫入。")
    print(f"報告：{report_path.resolve()}")
    return 1 if result.errors else 0


def _undo(args: argparse.Namespace) -> int:
    try:
        undone, failed = undo(args.undo_dir, args.root, rewrite.restore, rewrite.rename,
                              lambda message: print(f"  失敗 {message}", file=sys.stderr),
                              rewrite.unpatch)
    except (OSError, ValueError) as exc:
        print(f"無法讀取還原資料: {exc}", file=sys.stderr)
        return 2
    print(f"已復原 {undone} 個操作，失敗 {failed} 個。")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    # paths with kana must not crash a console or pipe that is still cp950
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = _parser().parse_args(argv)
    return _convert(args) if args.command == "convert" else _undo(args)


if __name__ == "__main__":
    sys.exit(main())
