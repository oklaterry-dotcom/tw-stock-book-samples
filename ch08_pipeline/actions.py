"""每個實作步驟的進入點。

乾跑只檢查檔案並印出將執行的命令，不連線、不寫檔。
--apply 才下載、追加 CSV，或呼叫既有章節的程式。
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import urllib.request
from datetime import date
from pathlib import Path

from ch08_pipeline.inputs_refresh import (
    RefreshError,
    protect_existing,
    read_breadth_file,
    read_yield_file,
    refresh_breadth,
    refresh_yield_spread,
    restore_files,
)
from ch08_pipeline.models import StepContext, StepResult, StepSpec, make_result


def default_fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/csv,*/*"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8-sig")


def default_run_command(cmd: list[str], cwd: Path) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    text = proc.stdout or ""
    if proc.stderr:
        text = text + ("\n" if text else "") + proc.stderr
    return proc.returncode, text


def _hard(spec: StepSpec, ctx: StepContext, message: str) -> StepResult:
    return make_result(spec, "FAILED" if ctx.apply else "BLOCKED", message)


def _soft(spec: StepSpec, ctx: StepContext, message: str) -> StepResult:
    return make_result(spec, "SKIPPED" if ctx.apply else "PLANNED", message)


def _show(cmd: list[str]) -> str:
    return " ".join(cmd)


def _tail(text: str, limit: int = 700) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _run_preserving(ctx: StepContext, cmd: list[str], outputs: list[Path]) -> tuple[int, str]:
    """同一天的舊報告先備份。命令失敗時把原內容寫回。"""
    pairs = protect_existing(outputs, ctx.backup_dir)
    try:
        code, output = ctx.run_command(cmd, ctx.repo_root)
    except Exception:
        restore_files(pairs)
        raise
    if code != 0 and pairs:
        restore_files(pairs)
        output = (output or "") + "\n原報告已從備份還原。"
    return code, output or ""


def _already_current(output: str) -> bool:
    text = output or ""
    if "匯入失敗" in text:
        return False
    return "日期範圍不合理" in text or "指定期間沒有交易日，不需補資料" in text


def import_prices(spec: StepSpec, ctx: StepContext) -> StepResult:
    script = ctx.repo_root / "ch02_practice" / "backfill_twstockdb.py"
    cmd = [ctx.python, str(script), "--end", ctx.asof.isoformat(), "--apply"]
    if not script.is_file():
        return _hard(
            spec, ctx,
            "找不到本機檔案 ch02_practice/backfill_twstockdb.py。"
            "此檔尚未進 Git，請放在這個路徑。管線不會改用其他方式寫股價。",
        )
    if not ctx.apply:
        return make_result(
            spec, "PLANNED",
            "乾跑不執行。該腳本即使不加 --apply 也會先寫 CSV，所以這裡完全不啟動它。"
            f"套用時將執行：{_show(cmd)}",
        )
    code, output = ctx.run_command(cmd, ctx.repo_root)
    if code == 0:
        return make_result(spec, "OK", _tail(output) or "股價回補完成")
    if _already_current(output):
        return make_result(
            spec, "OK",
            "不需要補資料（資料庫已涵蓋這個 asof，或這段期間沒有交易日）。\n" + _tail(output, 400),
        )
    return make_result(spec, "FAILED", "股價回補失敗。\n" + _tail(output))


def derived_yield_spread(spec: StepSpec, ctx: StepContext) -> StepResult:
    path = ctx.input_dir / "yield_spread.csv"
    if not ctx.apply:
        if not path.is_file():
            return make_result(
                spec, "PLANNED",
                "乾跑不連線、不建檔。尚無 yield_spread.csv；套用時會向美國財政部"
                "每日公債殖利率曲線下載 10 年期減 2 年期，並新建這個檔。不使用 FRED。",
            )
        try:
            existing = read_yield_file(path)
        except RefreshError as exc:
            return _hard(spec, ctx, str(exc))
        if existing and max(existing) >= ctx.asof:
            return make_result(spec, "PLANNED", "乾跑。檔案已涵蓋 asof，套用時也不會連線或改檔。")
        return make_result(
            spec, "PLANNED",
            "乾跑不連線、不改檔。套用時只追加晚於檔內最後日期的利差，並先備份舊檔。",
        )
    try:
        message = refresh_yield_spread(
            path, ctx.asof, ctx.fetch_text, ctx.backup_dir, ctx.sleep, ctx.fetch_attempts,
        )
    except RefreshError as exc:
        return make_result(spec, "FAILED", str(exc))
    return make_result(spec, "OK", message)


def market_breadth(spec: StepSpec, ctx: StepContext) -> StepResult:
    path = ctx.input_dir / "breadth.csv"
    if not ctx.apply:
        if not path.is_file():
            return _hard(
                spec, ctx,
                f"找不到 {path}。請放上本機既有的 breadth.csv。管線只追加，不會從零建立。",
            )
        try:
            existing = read_breadth_file(path)
        except RefreshError as exc:
            return _hard(spec, ctx, str(exc))
        if not existing:
            return _hard(spec, ctx, "breadth.csv 沒有資料列，拒絕從空檔開始。")
        if max(existing) >= ctx.asof:
            return make_result(spec, "PLANNED", "乾跑。檔案已涵蓋 asof，套用時也不會連線或改檔。")
        return make_result(
            spec, "PLANNED",
            "乾跑不連線、不改檔。套用時會向證交所 MI_INDEX 追加新的交易日，並先備份。",
        )
    try:
        message = refresh_breadth(
            path, ctx.asof, ctx.fetch_text, ctx.backup_dir, ctx.sleep,
            ctx.fetch_attempts, ctx.pause_seconds,
        )
    except RefreshError as exc:
        return make_result(spec, "FAILED", str(exc))
    return make_result(spec, "OK", message)


def _read_dates(path: Path) -> list[date]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "date" not in reader.fieldnames:
            raise ValueError("trading_dates.csv 缺少 date 欄")
        days = [date.fromisoformat((row.get("date") or "").strip()) for row in reader if (row.get("date") or "").strip()]
    if len(set(days)) != len(days):
        raise ValueError("trading_dates.csv 日期重複")
    days.sort()
    return days


def _factor_folder(ctx: StepContext) -> tuple[Path | None, str | None]:
    if ctx.factor_data_dir is not None:
        return ctx.factor_data_dir, None
    root = ctx.repo_root / "ch04_factor" / "ch04_data"
    preferred = root / "20260501_20261112"
    if (preferred / "summary.json").is_file():
        return preferred, None
    if not root.is_dir():
        return None, "找不到 ch04_data，略過因子。不填假分數。"
    found = sorted(path for path in root.iterdir() if (path / "summary.json").is_file())
    if not found:
        return None, "ch04_data 裡沒有 summary.json，略過因子。不填假分數。"
    if len(found) > 1:
        names = "、".join(path.name for path in found)
        return None, f"有多個因子資料夾（{names}）。請用 --factor-data-dir 指定，管線不自行挑選。"
    return found[0], None


def factors_ch04(spec: StepSpec, ctx: StepContext) -> StepResult:
    script = ctx.repo_root / "ch04_factor" / "calculate_ch04_factors.py"
    if not script.is_file():
        return _hard(spec, ctx, "找不到 ch04_factor/calculate_ch04_factors.py")
    folder, skip = _factor_folder(ctx)
    if folder is None:
        return _soft(spec, ctx, skip or "沒有因子輸入，略過")
    summary_path = folder / "summary.json"
    if not summary_path.is_file():
        return _soft(spec, ctx, f"{folder.name} 沒有 summary.json，略過因子")
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return _hard(spec, ctx, f"summary.json 無法讀取：{exc}")
    if summary.get("status") != "COMPLETE" or summary.get("probe"):
        return _soft(spec, ctx, "因子下載不是完整資料，略過。不用探查檔計算，也不填假分數。")
    calendar = folder / "trading_dates.csv"
    if not calendar.is_file():
        return _hard(spec, ctx, "缺少 trading_dates.csv，無法安全計算因子")
    try:
        days = [day for day in _read_dates(calendar) if day <= ctx.asof]
    except (OSError, ValueError) as exc:
        return _hard(spec, ctx, f"trading_dates.csv 無法使用：{exc}")
    if len(days) < 60:
        return _soft(spec, ctx, "不晚於 asof 的交易日少於 60，不算因子，也不填假值")
    out = folder / f"factors_{days[-1]:%Y%m%d}"
    if out.exists():
        return _soft(spec, ctx, f"輸出目錄已存在，為避免覆蓋而略過：{out}")
    cmd = [ctx.python, str(script), "--data-dir", str(folder), "--as-of", ctx.asof.isoformat()]
    if not ctx.apply:
        return make_result(spec, "PLANNED", "乾跑不執行。套用時將執行：" + _show(cmd))
    code, output = ctx.run_command(cmd, ctx.repo_root)
    if code != 0:
        return make_result(spec, "FAILED", "因子計算失敗。\n" + _tail(output))
    return make_result(
        spec, "OK",
        "已寫出因子 CSV（含 total_score）。沒有呼叫 import_ch04_sql.py，"
        "所以沒有改到 ch04_factors。若日後自行匯入，查詢必須加上 version_id。\n"
        + _tail(output, 400),
    )


def regime_vote(spec: StepSpec, ctx: StepContext) -> StepResult:
    base = ctx.repo_root / "ch05_regime" / "ch05_voting_v1"
    script = base / "market_regime_voting.py"
    output_dir = base / "output"
    report = output_dir / f"regime_{ctx.asof.isoformat()}.json"
    if not script.is_file():
        return _hard(spec, ctx, f"找不到 {script}")
    cmd = [
        ctx.python, str(script), "--asof", ctx.asof.isoformat(),
        "--input-dir", str(ctx.input_dir), "--output-dir", str(output_dir),
    ]
    if not ctx.apply:
        return make_result(spec, "PLANNED", "乾跑不執行。套用時將執行：" + _show(cmd))
    code, output = _run_preserving(ctx, cmd, [report, report.with_suffix(".csv")])
    if code != 0:
        return make_result(spec, "FAILED", "投票腳本失敗。\n" + _tail(output))
    if not report.is_file():
        return make_result(spec, "FAILED", "投票腳本結束但沒有報告檔，不改用假的市場狀態。")
    try:
        payload = json.loads(report.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return make_result(spec, "FAILED", f"投票報告無法讀取：{exc}")
    note = ""
    if not payload.get("complete", False):
        note = "注意：覆蓋率未滿，正式 regime 為 INCOMPLETE，不能當成 11 位都到齊。"
    return make_result(
        spec, "OK",
        f"Regime={payload.get('regime')} coverage={payload.get('coverage')}/11。{note}\n"
        + _tail(output, 300),
    )


def regime_continuous(spec: StepSpec, ctx: StepContext) -> StepResult:
    base = ctx.repo_root / "ch05_regime" / "ch05_voting_v1"
    script = base / "market_regime_continuous.py"
    rules = base / "continuous_rules.json"
    report = base / "output" / f"regime_{ctx.asof.isoformat()}.json"
    out_dir = base / "output" / "continuous"
    target = out_dir / f"continuous_{ctx.asof.isoformat()}.json"
    if not script.is_file() or not rules.is_file():
        missing = script if not script.is_file() else rules
        try:
            shown = missing.relative_to(ctx.repo_root).as_posix()
        except ValueError:
            shown = str(missing)
        return _hard(
            spec, ctx,
            f"找不到本機檔案 {shown}。這支連續評分尚未進 Git，請放到這個路徑。管線不填假分數。",
        )
    cmd = [
        ctx.python, str(script), str(report),
        "--config", str(rules), "--output-dir", str(out_dir),
    ]
    if not ctx.apply:
        return make_result(spec, "PLANNED", "乾跑不執行。套用時將執行：" + _show(cmd))
    if not report.is_file():
        return make_result(spec, "FAILED", f"找不到投票報告 {report.name}，連續評分不使用假分數。")
    code, output = _run_preserving(ctx, cmd, [target, target.with_suffix(".csv")])
    if code != 0:
        return make_result(spec, "FAILED", "連續評分失敗。\n" + _tail(output))
    return make_result(spec, "OK", _tail(output) or "連續評分完成")


ACTIONS = {
    "import_prices": import_prices,
    "derived_yield_spread": derived_yield_spread,
    "market_breadth": market_breadth,
    "factors_ch04": factors_ch04,
    "regime_vote": regime_vote,
    "regime_continuous": regime_continuous,
}
