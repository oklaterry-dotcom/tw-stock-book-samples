"""命令列。預設乾跑；只有 --apply 會寫入。"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ch08_pipeline.actions import default_fetch_text, default_run_command
from ch08_pipeline.catalog import (
    LAYER_TITLES,
    LOCAL_FILES,
    PARALLEL_WORKERS,
    STATUS_ZH,
    STEPS,
    scope_for,
    select_steps,
)
from ch08_pipeline.guard import open_store, peek_store, pipeline_ready, PipelineGuard
from ch08_pipeline.models import StepContext
from ch08_pipeline.runner import run_pipeline

TW = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]


def taipei_today(now: datetime | None = None) -> date:
    current = now or datetime.now(TW)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TW)
    return current.astimezone(TW).date()


def resolve_asof(value: date | None, today: date) -> date:
    asof = value or (today - timedelta(days=1))
    if asof >= today:
        raise ValueError(f"asof {asof} 必須早於台北今天 {today}，避免寫入未收盤的資料")
    return asof


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="第 8 章每日資料更新管線（簡化版）。預設乾跑，不加 --apply 不會寫入。",
    )
    parser.add_argument("--apply", action="store_true", help="真正寫入。預設只做乾跑")
    parser.add_argument("--asof", type=date.fromisoformat, help="資料日期 YYYY-MM-DD，預設台北昨天")
    parser.add_argument("--layer", type=int, choices=sorted(LAYER_TITLES), help="只跑這一層")
    parser.add_argument("--step", action="append", default=None, help="只跑這個步驟，可重複指定")
    parser.add_argument("--workers", type=int, default=PARALLEL_WORKERS, help="第三層的 worker 數，預設 4")
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--input-dir", type=Path, default=None)
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument("--factor-data-dir", type=Path, default=None)
    parser.add_argument(
        "--status-backend", choices=["auto", "mssql", "sqlite", "json"], default="auto",
        help="狀態存放位置。auto 先試 MiniQuantDemo，不行再用 SQLite",
    )
    parser.add_argument("--status-path", type=Path, default=None, help="SQLite 檔或 JSON 目錄的位置")
    parser.add_argument("--ready", action="store_true", help="只檢查 pipeline_ready()，不跑管線、不寫入")
    parser.add_argument("--ready-wait", type=float, default=120, help="RUNNING 時最多再等幾秒，預設 120")
    parser.add_argument("--ready-poll", type=float, default=10, help="RUNNING 時幾秒看一次，預設 10")
    parser.add_argument("--list", action="store_true", help="列出步驟與本機檔案後結束")
    return parser


def _status_paths(args) -> tuple[Path, Path]:
    sqlite_path = args.status_path or (args.repo_root / "ch08_pipeline" / "state" / "pipeline.sqlite")
    if args.status_backend == "json" and args.status_path is not None:
        json_path = args.status_path
    else:
        json_path = sqlite_path.with_name("json")
    return sqlite_path, json_path


def command_list() -> int:
    print("步驟（實作會真的跑；略過實作不會製造資料）")
    for step in STEPS:
        kind = "實作" if step.kind == "real" else "略過"
        print(f"[L{step.layer}] {step.title}（{step.name}）{kind}")
        print(f"    {step.summary}")
    print("\n本機檔案（不在 Git 上的不需提交；管線按這個相對路徑尋找）")
    for item in LOCAL_FILES:
        mark = "需要" if item.needed else "不呼叫"
        print(f"- {item.relative_path}：{mark}。{item.purpose}")
    return 0


def command_ready(args) -> int:
    sqlite_path, json_path = _status_paths(args)
    if args.apply:
        print("--ready 只讀狀態，不會因為同時加上 --apply 而寫入。", flush=True)
    try:
        store = peek_store(args.status_backend, sqlite_path, json_path)
    except Exception as exc:
        print(f"無法讀取狀態：{exc}", file=sys.stderr)
        return 1
    if store is None:
        print("尚無狀態庫，視為還沒跑過完整的每日更新。", flush=True)
        return 2
    result = pipeline_ready(
        store,
        max_wait_seconds=args.ready_wait,
        poll_seconds=args.ready_poll,
    )
    label = STATUS_ZH.get(result.status, result.status)
    print(f"{label}：{result.message}", flush=True)
    return 0 if result.ready else 2


def build_context(args, asof: date, today: date) -> StepContext:
    root = args.repo_root.resolve()
    input_dir = args.input_dir or (root / "ch05_regime" / "ch05_voting_v1" / "input")
    backup_dir = args.backup_dir or (input_dir / "backups")
    return StepContext(
        repo_root=root,
        asof=asof,
        today=today,
        apply=bool(args.apply),
        python=sys.executable,
        input_dir=input_dir,
        backup_dir=backup_dir,
        factor_data_dir=args.factor_data_dir,
        fetch_text=default_fetch_text,
        sleep=time.sleep,
        run_command=default_run_command,
        now=datetime.now(timezone.utc),
        workers=args.workers,
    )


def command_run(args) -> int:
    today = taipei_today()
    try:
        if args.workers < 1:
            raise ValueError("worker 數必須至少 1")
        asof = resolve_asof(args.asof, today)
        specs = select_steps(args.layer, args.step)
    except (ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    scope = scope_for(args.layer, args.step)
    ctx = build_context(args, asof, today)
    guard = None
    if args.apply:
        sqlite_path, json_path = _status_paths(args)
        try:
            store, label = open_store(
                args.status_backend, sqlite_path, create=True, json_path=json_path,
            )
        except Exception as exc:
            print(f"無法開啟狀態庫，尚未開始寫市場資料：{exc}", file=sys.stderr)
            return 1
        print(f"狀態庫：{label}", flush=True)
        guard = PipelineGuard(store)
    result = run_pipeline(ctx, specs, scope=scope, guard=guard)
    return 0 if result.status in {"DRY_RUN", "COMPLETED"} else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list:
        return command_list()
    if args.ready:
        return command_ready(args)
    return command_run(args)
