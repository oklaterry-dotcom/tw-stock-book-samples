"""把五層步驟跑完。第三層使用固定四個 worker 的執行緒池。"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from ch08_pipeline.actions import ACTIONS
from ch08_pipeline.catalog import LAYER_TITLES, PARALLEL_LAYER, STATUS_ZH
from ch08_pipeline.models import PipelineResult, StepContext, StepResult, StepSpec, make_result


def _grouped(specs: list[StepSpec]) -> list[tuple[int, list[StepSpec]]]:
    groups: list[tuple[int, list[StepSpec]]] = []
    for spec in specs:
        if not groups or groups[-1][0] != spec.layer:
            groups.append((spec.layer, [spec]))
        else:
            groups[-1][1].append(spec)
    return groups


def _run_one(spec: StepSpec, ctx: StepContext, actions: dict) -> StepResult:
    started_at = datetime.now(timezone.utc)
    clock = time.perf_counter()
    try:
        if spec.kind == "stub":
            result = make_result(spec, "SKIPPED", spec.summary)
        else:
            action = actions.get(spec.name)
            if action is None:
                result = make_result(spec, "FAILED", "沒有對應的實作函式")
            else:
                result = action(spec, ctx)
    except Exception as exc:
        result = make_result(spec, "FAILED", f"{type(exc).__name__}: {exc}")
    result.elapsed_ms = int((time.perf_counter() - clock) * 1000)
    result.started_at = started_at
    result.finished_at = datetime.now(timezone.utc)
    return result


def _run_parallel(specs: list[StepSpec], ctx: StepContext, actions: dict) -> list[StepResult]:
    results: list[StepResult | None] = [None] * len(specs)
    with ThreadPoolExecutor(max_workers=ctx.workers, thread_name_prefix="ch08-l3") as pool:
        futures = {
            pool.submit(_run_one, spec, ctx, actions): index
            for index, spec in enumerate(specs)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return [item for item in results if item is not None]


def _format(result: StepResult) -> str:
    label = STATUS_ZH.get(result.status, result.status)
    return f"[L{result.layer}] {result.title}（{result.name}）：{label}。{result.message}"


def run_pipeline(ctx: StepContext, specs: list[StepSpec], *, scope: str,
                 actions: dict | None = None, guard=None) -> PipelineResult:
    """scope 為 full 的 --apply 才會被 pipeline_ready() 當成每日更新。"""
    if ctx.asof >= ctx.today:
        raise ValueError(
            f"asof {ctx.asof} 必須早於台北今天 {ctx.today}，避免寫入未收盤的資料"
        )
    if ctx.workers < 1:
        raise ValueError("worker 數必須至少 1")
    actions = ACTIONS if actions is None else actions
    mode = "套用" if ctx.apply else "乾跑"
    print(
        f"每日資料更新 asof={ctx.asof} 模式={mode} 範圍={'完整' if scope == 'full' else '部分'}",
        flush=True,
    )
    closed = False
    run_id = None
    results: list[StepResult] = []
    try:
        if ctx.apply and guard is not None:
            swept = guard.sweep(ctx.now)
            if swept:
                print(f"已把 {swept} 筆超過 2 小時仍在執行的狀態補記為失敗。", flush=True)
            run_id = guard.start(ctx.asof, scope, ctx.now)
            print(f"狀態紀錄 run_id={run_id}", flush=True)
        for layer, group in _grouped(specs):
            print(LAYER_TITLES.get(layer, f"第 {layer} 層"), flush=True)
            if layer == PARALLEL_LAYER:
                done = _run_parallel(group, ctx, actions)
            else:
                done = [_run_one(spec, ctx, actions) for spec in group]
            for result in done:
                results.append(result)
                print(_format(result), flush=True)
                if ctx.apply and guard is not None:
                    guard.record(
                        result,
                        result.started_at or ctx.now,
                        result.finished_at or ctx.now,
                    )
        failed = [item for item in results if item.status == "FAILED"]
        blocked = [item for item in results if item.status == "BLOCKED"]
        if not ctx.apply:
            status = "DRY_RUN"
            message = "乾跑沒有寫入檔案或資料庫。"
            if blocked:
                message += "請先處理標成「無法套用」的步驟，否則 --apply 會失敗。"
        else:
            status = "FAILED" if failed else "COMPLETED"
            message = (
                "失敗步驟：" + "、".join(item.name for item in failed)
                if failed else "全部步驟跑完。略過的步驟沒有寫入假資料。"
            )
            if guard is not None:
                guard.finish(status, message, datetime.now(timezone.utc))
                closed = True
        print(f"結果：{STATUS_ZH.get(status, status)}。{message}", flush=True)
        return PipelineResult(status, ctx.asof, ctx.apply, scope, run_id, results, message)
    finally:
        if ctx.apply and guard is not None and run_id and not closed:
            try:
                guard.finish("FAILED", "管線中斷，未正常結束", datetime.now(timezone.utc))
            except Exception as exc:
                print(f"無法寫入中斷狀態：{exc}", file=sys.stderr)
