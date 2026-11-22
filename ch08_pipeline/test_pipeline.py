"""每日更新管線的測試。不連 SQL Server，也不連外網。"""

from __future__ import annotations

import inspect
import io
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ch08_pipeline.actions import (  # noqa: E402
    factors_ch04,
    import_prices,
    market_breadth,
    regime_continuous,
    regime_vote,
)
from ch08_pipeline.catalog import (  # noqa: E402
    LOCAL_FILES,
    PARALLEL_WORKERS,
    STEPS,
    scope_for,
    select_steps,
)
from ch08_pipeline.cli import build_parser, main, taipei_today  # noqa: E402
from ch08_pipeline.guard import (  # noqa: E402
    JsonStatusStore,
    MssqlStatusStore,
    PipelineGuard,
    SqliteStatusStore,
    StatusError,
    open_store,
    pipeline_ready,
)
from ch08_pipeline.inputs_refresh import (  # noqa: E402
    RefreshError,
    breadth_day_url,
    parse_treasury_csv,
    refresh_breadth,
    refresh_yield_spread,
    treasury_year_url,
)
from ch08_pipeline.models import StepContext, StepResult, StepSpec, make_result  # noqa: E402
from ch08_pipeline.runner import run_pipeline  # noqa: E402

UTC = timezone.utc
ASOF = date(2026, 11, 20)
NOW = datetime(2026, 11, 22, 1, 0, tzinfo=UTC)


def safe_asof() -> date:
    today = taipei_today()
    if ASOF < today:
        return ASOF
    return today - timedelta(days=1)


def forbid_fetch(url: str) -> str:
    raise AssertionError("不該連線 " + url)


def forbid_sleep(seconds: float) -> None:
    raise AssertionError("不該等待")


def forbid_command(cmd, cwd):
    raise AssertionError(cmd)


def make_ctx(tmp: Path, **kwargs) -> StepContext:
    input_dir = kwargs.pop("input_dir", tmp / "input")
    input_dir.mkdir(parents=True, exist_ok=True)
    values = dict(
        repo_root=tmp,
        asof=ASOF,
        today=date(2026, 11, 22),
        apply=False,
        python=sys.executable,
        input_dir=input_dir,
        backup_dir=input_dir / "backups",
        factor_data_dir=None,
        fetch_text=forbid_fetch,
        sleep=forbid_sleep,
        run_command=forbid_command,
        now=NOW,
        workers=4,
        pause_seconds=0,
        fetch_attempts=1,
    )
    values.update(kwargs)
    return StepContext(**values)


def run_quiet(ctx, specs, **kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = run_pipeline(ctx, specs, **kwargs)
    return result, buf.getvalue()


def breadth_payload(day: date, advances: int, declines: int) -> str:
    return json.dumps({
        "stat": "OK",
        "date": f"{day:%Y%m%d}",
        "tables": [{
            "fields": ["項目", "股票"],
            "data": [
                ["上漲證券", f"{advances}(1)"],
                ["下跌證券", f"{declines}(2)"],
            ],
        }],
    }, ensure_ascii=False)


def write_breadth(path: Path, rows: list[tuple[date, int, int]], newline: str = "\n") -> None:
    body = "date,advances,declines" + newline
    body += "".join(f"{day.isoformat()},{adv},{dec}{newline}" for day, adv, dec in rows)
    path.write_bytes(body.encode("utf-8"))


def write_yield(path: Path, rows: list[tuple[date, str]]) -> None:
    lines = ["date,value"] + [f"{day.isoformat()},{value}" for day, value in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


TREASURY = """Date,"1 Mo","2 Yr","10 Yr"
11/21/2026,4.19,4.50,4.80
11/20/2026,4.19,4.63,4.92
11/19/2026,4.20,4.76,5.04
"""


class CatalogTests(unittest.TestCase):
    def test_layers_and_real_steps(self):
        names = [step.name for step in STEPS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(range(1, 6)), {step.layer for step in STEPS})
        layer3 = [step for step in STEPS if step.layer == 3]
        self.assertEqual(len(layer3), 11)
        self.assertTrue(all(step.kind == "stub" and step.parallel for step in layer3))
        real = {step.name for step in STEPS if step.kind == "real"}
        self.assertEqual(real, {
            "import_prices", "derived_yield_spread", "market_breadth",
            "factors_ch04", "regime_vote", "regime_continuous",
        })
        self.assertEqual(PARALLEL_WORKERS, 4)
        self.assertTrue(any(item.relative_path.endswith("backfill_twstockdb.py") and item.needed
                            for item in LOCAL_FILES))
        self.assertTrue(any(item.relative_path.endswith("taiex_continuous.py") and not item.needed
                            for item in LOCAL_FILES))

    def test_select_and_scope(self):
        self.assertEqual(scope_for(None, None), "full")
        self.assertEqual(scope_for(2, None), "partial")
        chosen = select_steps(2, ["market_breadth", "regime_vote"])
        self.assertEqual([step.name for step in chosen], ["market_breadth"])
        with self.assertRaises(KeyError):
            select_steps(names=["not_a_step"])

    def test_defaults_match_the_book(self):
        ready = inspect.signature(pipeline_ready)
        self.assertEqual(ready.parameters["max_wait_seconds"].default, 120)
        self.assertEqual(ready.parameters["poll_seconds"].default, 10)
        self.assertEqual(ready.parameters["zombie_after"].default, timedelta(hours=2))
        args = build_parser().parse_args([])
        self.assertFalse(args.apply)
        self.assertEqual(args.workers, 4)
        self.assertEqual(args.ready_wait, 120)
        self.assertEqual(args.ready_poll, 10)
        self.assertEqual(args.status_backend, "auto")

    def test_sources_do_not_destroy_data_or_call_fred(self):
        folder = Path(__file__).resolve().parent
        for path in folder.glob("*.py"):
            if path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(r"\b(DELETE|DROP|TRUNCATE|UPDATE)\b", text, re.I),
                path.name,
            )
            self.assertNotIn("fred.stlouisfed.org", text.lower(), path.name)
        self.assertIn("daily_treasury_yield_curve", treasury_year_url(2026))
        self.assertNotIn("fred", treasury_year_url(2026).lower())
        self.assertIn("MI_INDEX", breadth_day_url(ASOF))


class RefreshTests(unittest.TestCase):
    def test_treasury_parser_uses_ten_minus_two(self):
        found = parse_treasury_csv(TREASURY, ASOF)
        self.assertEqual(found[date(2026, 11, 20)], __import__("decimal").Decimal("0.29"))
        self.assertEqual(found[date(2026, 11, 19)], __import__("decimal").Decimal("0.28"))
        self.assertNotIn(date(2026, 11, 21), found)

    def test_yield_creates_file_without_backup(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            target = tmp / "yield_spread.csv"
            seen = []

            def fetch(url):
                seen.append(url)
                return TREASURY

            message = refresh_yield_spread(target, ASOF, fetch, tmp / "backups", forbid_sleep, 1)
            self.assertIn("新建", message)
            self.assertIn("daily_treasury_yield_curve", seen[0])
            text = target.read_text(encoding="utf-8-sig")
            self.assertIn("2026-11-20,0.29", text)
            self.assertIn("2026-11-19,0.28", text)
            self.assertNotIn("2026-11-21", text)
            self.assertFalse((tmp / "backups").exists())

    def test_yield_appends_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            target = tmp / "yield_spread.csv"
            write_yield(target, [(date(2026, 11, 19), "0.28")])
            original = target.read_bytes()

            def fetch(url):
                return TREASURY

            message = refresh_yield_spread(target, ASOF, fetch, tmp / "backups", forbid_sleep, 1)
            self.assertIn("追加 1", message)
            self.assertTrue(target.read_bytes().startswith(original))
            self.assertIn("2026-11-20,0.29", target.read_text(encoding="utf-8"))
            backups = list((tmp / "backups").iterdir())
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            again = target.read_bytes()
            message = refresh_yield_spread(target, ASOF, forbid_fetch, tmp / "backups", forbid_sleep, 1)
            self.assertIn("已涵蓋", message)
            self.assertEqual(target.read_bytes(), again)

    def test_yield_conflict_does_not_touch_file(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            target = tmp / "yield_spread.csv"
            write_yield(target, [(date(2026, 11, 19), "0.99")])
            original = target.read_bytes()
            with self.assertRaises(RefreshError):
                refresh_yield_spread(target, ASOF, lambda url: TREASURY, tmp / "backups", forbid_sleep, 1)
            self.assertEqual(target.read_bytes(), original)
            self.assertFalse((tmp / "backups").exists())

    def test_yield_retries_then_succeeds(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            target = tmp / "yield_spread.csv"
            calls = {"n": 0, "sleeps": []}

            def fetch(url):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise URLError("timeout")
                return TREASURY

            def sleep(seconds):
                calls["sleeps"].append(seconds)

            refresh_yield_spread(target, ASOF, fetch, tmp / "backups", sleep, attempts=2)
            self.assertEqual(calls["n"], 2)
            self.assertEqual(calls["sleeps"], [1])
            self.assertTrue(target.is_file())

    def test_breadth_appends_weekdays_and_skips_holiday(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            target = tmp / "breadth.csv"
            write_breadth(target, [(date(2026, 11, 18), 10, 8)], newline="\r\n")
            original = target.read_bytes()
            calls = []

            def fetch(url):
                calls.append(url)
                if "20261119" in url:
                    return json.dumps({"stat": "no data"})
                if "20261120" in url:
                    return breadth_payload(date(2026, 11, 20), 277, 438)
                raise AssertionError(url)

            message = refresh_breadth(
                target, ASOF, fetch, tmp / "backups", forbid_sleep, attempts=1, pause_seconds=0,
            )
            self.assertIn("追加 1", message)
            self.assertEqual(len(calls), 2)
            updated = target.read_bytes()
            self.assertTrue(updated.startswith(original))
            self.assertTrue(original.endswith(b"\r\n"))
            self.assertTrue(updated.endswith(b"2026-11-20,277,438\r\n"))
            self.assertNotIn(b"2026-11-19", updated)
            self.assertEqual((tmp / "backups").joinpath(next((tmp / "backups").iterdir()).name).read_bytes(), original)

    def test_breadth_rejects_date_mismatch_and_missing_file(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            target = tmp / "breadth.csv"
            write_breadth(target, [(date(2026, 11, 19), 10, 8)])
            original = target.read_bytes()
            bad = breadth_payload(date(2026, 11, 19), 1, 2).replace("20261119", "20261118")
            with self.assertRaises(RefreshError):
                refresh_breadth(target, ASOF, lambda url: bad, tmp / "backups", forbid_sleep, 1, 0)
            self.assertEqual(target.read_bytes(), original)
            self.assertFalse((tmp / "backups").exists())
            missing = tmp / "missing.csv"
            with self.assertRaises(RefreshError):
                refresh_breadth(missing, ASOF, forbid_fetch, tmp / "backups", forbid_sleep, 1, 0)
            self.assertFalse(missing.exists())

    def test_breadth_weekend_does_not_call_network(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            target = tmp / "breadth.csv"
            write_breadth(target, [(date(2026, 11, 20), 10, 8)])
            message = refresh_breadth(
                target, date(2026, 11, 22), forbid_fetch, tmp / "backups", forbid_sleep, 1, 0,
            )
            self.assertIn("沒有平日", message)


class GuardTests(unittest.TestCase):
    def _exercise(self, store):
        guard = PipelineGuard(store)
        guard.start(ASOF, "partial", NOW)
        guard.finish("COMPLETED", "部分步驟", NOW)
        self.assertIsNone(store.latest_run("daily_update", "full"))
        ready = pipeline_ready(store, clock=lambda: NOW, sleep=forbid_sleep, max_wait_seconds=0)
        self.assertFalse(ready.ready)
        self.assertEqual(ready.status, "NO_RUN")

        full = PipelineGuard(store)
        full.start(ASOF, "full", NOW)
        self.assertEqual(store.latest_run("daily_update").status, "RUNNING")
        running = pipeline_ready(store, clock=lambda: NOW, sleep=forbid_sleep, max_wait_seconds=0)
        self.assertFalse(running.ready)
        self.assertEqual(running.status, "RUNNING")
        full.finish("COMPLETED", "完成", NOW)
        done = pipeline_ready(store, clock=lambda: NOW, sleep=forbid_sleep)
        self.assertTrue(done.ready)
        self.assertFalse(done.stale)
        self.assertEqual(done.status, "COMPLETED")

        failed = PipelineGuard(store)
        later = NOW + timedelta(minutes=5)
        failed.start(ASOF, "full", later)
        failed.finish("FAILED", "有步驟失敗", later)
        stale = pipeline_ready(store, clock=lambda: later, sleep=forbid_sleep)
        self.assertTrue(stale.ready)
        self.assertTrue(stale.stale)
        self.assertEqual(stale.run_id, failed.run_id)

    def test_sqlite_and_json_round_trip(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            sqlite_store = SqliteStatusStore(tmp / "pipeline.sqlite")
            sqlite_store.ensure()
            self._exercise(sqlite_store)
            json_store = JsonStatusStore(tmp / "json")
            json_store.ensure()
            self._exercise(json_store)

    def test_zombie_is_appended_not_rewritten(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            store = SqliteStatusStore(tmp / "pipeline.sqlite")
            store.ensure()
            guard = PipelineGuard(store)
            started = NOW - timedelta(hours=3)
            guard.start(ASOF, "full", started)

            def statuses():
                conn = sqlite3.connect(store.path)
                try:
                    rows = conn.execute(
                        "SELECT status FROM pipeline_steps WHERE step_name = ? ORDER BY step_row_id",
                        ("__run_status__",),
                    ).fetchall()
                finally:
                    conn.close()
                return [row[0] for row in rows]

            self.assertEqual(statuses(), ["RUNNING"])
            ready = pipeline_ready(store, clock=lambda: NOW, sleep=forbid_sleep)
            self.assertTrue(ready.stale)
            self.assertEqual(store.latest_run("daily_update").status, "RUNNING")
            self.assertEqual(guard.sweep(NOW), 1)
            self.assertEqual(guard.sweep(NOW), 0)
            self.assertEqual(statuses(), ["RUNNING", "FAILED"])
            self.assertEqual(store.latest_run("daily_update").status, "FAILED")

    def test_ready_waits_then_sees_completion_or_gives_up(self):
        with tempfile.TemporaryDirectory() as raw:
            store = SqliteStatusStore(Path(raw) / "pipeline.sqlite")
            store.ensure()
            guard = PipelineGuard(store)
            guard.start(ASOF, "full", NOW)
            state = {"t": NOW, "n": 0}

            def clock():
                return state["t"]

            def sleep(seconds):
                state["n"] += 1
                state["t"] += timedelta(seconds=seconds)
                if state["n"] == 1:
                    store.add_run_status(guard.run_id, "COMPLETED", "好了", state["t"])

            done = pipeline_ready(store, clock=clock, sleep=sleep, max_wait_seconds=50, poll_seconds=10)
            self.assertTrue(done.ready)
            self.assertEqual(done.status, "COMPLETED")
            self.assertEqual(state["n"], 1)

            guard2 = PipelineGuard(store)
            start = NOW + timedelta(hours=1)
            guard2.start(ASOF, "full", start)
            state = {"t": start, "n": 0}

            def clock2():
                return state["t"]

            def sleep2(seconds):
                state["n"] += 1
                state["t"] += timedelta(seconds=seconds)

            abandoned = pipeline_ready(
                store, clock=clock2, sleep=sleep2, max_wait_seconds=25, poll_seconds=10,
            )
            self.assertFalse(abandoned.ready)
            self.assertEqual(abandoned.status, "RUNNING")
            self.assertEqual(state["n"], 3)

    def test_auto_backend_falls_back_to_sqlite(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "pipeline.sqlite"

            def connect():
                raise StatusError("沒有 SQL Server")

            store, label = open_store("auto", path, connect=connect, create=True)
            self.assertIsInstance(store, SqliteStatusStore)
            self.assertIn("SQLite", label)
            self.assertTrue(path.is_file())

    def test_mssql_statements_only_insert(self):
        class Cursor:
            def __init__(self):
                self.executed = []

            def execute(self, sql, *params):
                self.executed.append(sql)
                return self

            def fetchone(self):
                return None

            def fetchall(self):
                return []

        class Conn:
            def __init__(self):
                self.cursor_obj = Cursor()
                self.commits = 0

            def cursor(self):
                return self.cursor_obj

            def commit(self):
                self.commits += 1

            def close(self):
                pass

        conn = Conn()
        store = MssqlStatusStore(lambda: conn)
        store.ensure()
        store.start_run("run-1", "daily_update", ASOF, NOW, True, "full")
        store.add_step("run-1", make_result(
            StepSpec("market_breadth", 2, "市場廣度", "real", ""), "OK", "ok",
        ), NOW, NOW)
        store.add_run_status("run-1", "COMPLETED", "完成", NOW)
        blob = "\n".join(conn.cursor_obj.executed)
        self.assertIn("CREATE TABLE", blob)
        self.assertIn("INSERT INTO", blob)
        self.assertIn("pipeline_runs", blob)
        self.assertIn("pipeline_steps", blob)
        self.assertIsNone(re.search(r"\b(DELETE|DROP|TRUNCATE|UPDATE)\b", blob, re.I))
        self.assertGreaterEqual(conn.commits, 1)


class RunnerTests(unittest.TestCase):
    def test_dry_run_does_not_call_network_or_commands(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            self._write_inputs(tmp)
            ctx = make_ctx(tmp, apply=False)
            result, text = run_quiet(ctx, select_steps(), scope="full")
            self.assertEqual(result.status, "DRY_RUN")
            self.assertIn("乾跑", text)
            self.assertTrue(all(step.status in {"PLANNED", "SKIPPED"} for step in result.steps))
            self.assertFalse((tmp / "input" / "backups").exists())

    def test_missing_backfill_blocks_dry_run_and_fails_apply(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            dry, _ = run_quiet(make_ctx(tmp), [select_steps(names=["import_prices"])[0]], scope="partial")
            self.assertEqual(dry.steps[0].status, "BLOCKED")
            self.assertIn("backfill_twstockdb.py", dry.steps[0].message)
            applied, _ = run_quiet(
                make_ctx(tmp, apply=True), [select_steps(names=["import_prices"])[0]], scope="partial",
            )
            self.assertEqual(applied.status, "FAILED")

    def test_backfill_command_and_already_current(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            script = tmp / "ch02_practice" / "backfill_twstockdb.py"
            script.parent.mkdir(parents=True)
            script.write_text("# local\n", encoding="utf-8")
            calls = []

            def run_command(cmd, cwd):
                calls.append(cmd)
                return 1, "日期範圍不合理：2026-11-21～2026-11-20"

            ctx = make_ctx(tmp, apply=True, run_command=run_command)
            result = import_prices(select_steps(names=["import_prices"])[0], ctx)
            self.assertEqual(result.status, "OK")
            self.assertIn("--apply", calls[0])
            self.assertIn("--end", calls[0])
            self.assertIn("2026-11-20", calls[0])

            def failed(cmd, cwd):
                return 1, "匯入失敗，本次寫入已回復。日期範圍不合理"

            result = import_prices(
                select_steps(names=["import_prices"])[0],
                make_ctx(tmp, apply=True, run_command=failed),
            )
            self.assertEqual(result.status, "FAILED")

    def test_breadth_action_dry_run_does_not_fetch(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = tmp / "input" / "breadth.csv"
            path.parent.mkdir(parents=True)
            write_breadth(path, [(date(2026, 11, 18), 10, 8)])
            spec = select_steps(names=["market_breadth"])[0]
            dry = market_breadth(spec, make_ctx(tmp, input_dir=path.parent))
            self.assertEqual(dry.status, "PLANNED")
            applied = market_breadth(spec, make_ctx(
                tmp, input_dir=path.parent, apply=True,
                fetch_text=lambda url: breadth_payload(date(2026, 11, 20), 3, 4)
                if "20261120" in url else json.dumps({"stat": "no data"}),
                sleep=lambda seconds: None,
            ))
            self.assertEqual(applied.status, "OK")
            self.assertIn("2026-11-20,3,4", path.read_text(encoding="utf-8"))

    def test_factor_skips_existing_output_and_runs_when_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            folder = self._factor_dir(tmp)
            script = tmp / "ch04_factor" / "calculate_ch04_factors.py"
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("# calculator\n", encoding="utf-8")
            latest = max(self._dates())
            out = folder / f"factors_{latest:%Y%m%d}"
            out.mkdir()
            spec = select_steps(names=["factors_ch04"])[0]
            skipped = factors_ch04(spec, make_ctx(tmp, apply=True, factor_data_dir=folder))
            self.assertEqual(skipped.status, "SKIPPED")
            self.assertIn("避免覆蓋", skipped.message)
            out.rmdir()
            calls = []

            def run_command(cmd, cwd):
                calls.append(cmd)
                return 0, "評分日期"

            done = factors_ch04(spec, make_ctx(
                tmp, apply=True, factor_data_dir=folder, run_command=run_command,
            ))
            self.assertEqual(done.status, "OK")
            self.assertIn("--as-of", calls[0])
            self.assertNotIn("import_ch04_sql.py", " ".join(calls[0]))

    def test_vote_restores_previous_report(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            base = tmp / "ch05_regime" / "ch05_voting_v1"
            (base / "output").mkdir(parents=True)
            (base / "market_regime_voting.py").write_text("# vote\n", encoding="utf-8")
            report = base / "output" / "regime_2026-11-20.json"
            report.write_text('{"old": true}\n', encoding="utf-8")
            spec = select_steps(names=["regime_vote"])[0]

            def fail_command(cmd, cwd):
                report.write_text('{"new": true}\n', encoding="utf-8")
                return 1, "boom"

            failed = regime_vote(spec, make_ctx(tmp, apply=True, run_command=fail_command))
            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(report.read_text(encoding="utf-8"), '{"old": true}\n')
            self.assertTrue(any(path.read_text(encoding="utf-8") == '{"old": true}\n'
                                for path in (tmp / "input" / "backups").iterdir()))

            def ok_command(cmd, cwd):
                out = Path(cmd[cmd.index("--output-dir") + 1])
                out.mkdir(parents=True, exist_ok=True)
                asof = cmd[cmd.index("--asof") + 1]
                payload = {"asof": asof, "regime": "NEUTRAL", "coverage": 11, "complete": True}
                (out / f"regime_{asof}.json").write_text(json.dumps(payload), encoding="utf-8")
                return 0, "Regime=NEUTRAL"

            done = regime_vote(spec, make_ctx(tmp, apply=True, run_command=ok_command))
            self.assertEqual(done.status, "OK")
            self.assertIn("NEUTRAL", report.read_text(encoding="utf-8"))

    def test_continuous_missing_script_and_missing_report(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            spec = select_steps(names=["regime_continuous"])[0]
            blocked = regime_continuous(spec, make_ctx(tmp))
            self.assertEqual(blocked.status, "BLOCKED")
            base = tmp / "ch05_regime" / "ch05_voting_v1"
            base.mkdir(parents=True)
            (base / "market_regime_continuous.py").write_text("# c\n", encoding="utf-8")
            (base / "continuous_rules.json").write_text("{}\n", encoding="utf-8")
            failed = regime_continuous(spec, make_ctx(tmp, apply=True))
            self.assertEqual(failed.status, "FAILED")
            self.assertIn("投票報告", failed.message)

    def test_layer3_pool_keeps_order_and_limit(self):
        state = {"current": 0, "peak": 0}
        lock = threading.Lock()

        def slow(spec, ctx):
            with lock:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
            time.sleep(0.08 if spec.name == "s0" else 0.02)
            with lock:
                state["current"] -= 1
            return make_result(spec, "OK", "done")

        specs = [StepSpec(f"s{i}", 3, "外部", "real", "") for i in range(8)]
        actions = {spec.name: slow for spec in specs}
        with tempfile.TemporaryDirectory() as raw:
            ctx = make_ctx(Path(raw), workers=4)
            result, _ = run_quiet(ctx, specs, scope="partial", actions=actions)
        self.assertEqual([step.name for step in result.steps], [f"s{i}" for i in range(8)])
        self.assertGreaterEqual(state["peak"], 2)
        self.assertLessEqual(state["peak"], 4)

    def test_apply_full_pipeline_with_fakes(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            self._write_inputs(tmp)
            breadth = (tmp / "input" / "breadth.csv").read_bytes()
            yield_file = (tmp / "input" / "yield_spread.csv").read_bytes()
            calls = []

            def run_command(cmd, cwd):
                calls.append(list(cmd))
                joined = " ".join(cmd)
                if "backfill_twstockdb.py" in joined:
                    return 1, "日期範圍不合理：已涵蓋"
                if "calculate_ch04_factors.py" in joined:
                    return 0, "評分日期"
                if "market_regime_voting.py" in joined:
                    out = Path(cmd[cmd.index("--output-dir") + 1])
                    out.mkdir(parents=True, exist_ok=True)
                    asof = cmd[cmd.index("--asof") + 1]
                    (out / f"regime_{asof}.json").write_text(json.dumps({
                        "asof": asof, "regime": "BULL", "coverage": 11, "complete": True,
                    }), encoding="utf-8")
                    return 0, "Regime=BULL"
                if "market_regime_continuous.py" in joined:
                    return 0, "Total continuous score: +1.00"
                raise AssertionError(joined)

            store = SqliteStatusStore(tmp / "pipeline.sqlite")
            store.ensure()
            guard = PipelineGuard(store)
            ctx = make_ctx(tmp, apply=True, run_command=run_command, factor_data_dir=self._factor_dir(tmp))
            result, _ = run_quiet(ctx, select_steps(), scope="full", guard=guard)
            self.assertEqual(result.status, "COMPLETED", result.message)
            self.assertTrue(all(step.status != "FAILED" for step in result.steps))
            self.assertEqual(store.latest_run("daily_update").status, "COMPLETED")
            self.assertEqual((tmp / "input" / "breadth.csv").read_bytes(), breadth)
            self.assertEqual((tmp / "input" / "yield_spread.csv").read_bytes(), yield_file)
            joined = [" ".join(cmd) for cmd in calls]
            self.assertTrue(any("--apply" in cmd and "backfill_twstockdb.py" in cmd for cmd in joined))
            self.assertTrue(any("--as-of" in cmd and "calculate_ch04_factors.py" in cmd for cmd in joined))
            self.assertFalse(any("import_ch04_sql.py" in cmd for cmd in joined))
            self.assertTrue(any("market_regime_continuous.py" in cmd for cmd in joined))
            self.assertTrue(any(step.name == "ml_predict" and step.status == "SKIPPED" for step in result.steps))
            self.assertTrue(any(step.name == "ptt_sentiment" and step.status == "SKIPPED" for step in result.steps))

    def test_failed_step_does_not_stop_later_layers(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            breadth = tmp / "input" / "breadth.csv"
            breadth.parent.mkdir(parents=True)
            write_breadth(breadth, [(ASOF, 10, 8)])
            specs = select_steps(names=["import_prices", "market_breadth"])
            result, _ = run_quiet(make_ctx(tmp, apply=True), specs, scope="partial")
            self.assertEqual([step.status for step in result.steps], ["FAILED", "OK"])
            self.assertEqual(result.status, "FAILED")

    def test_future_asof_is_rejected_before_work(self):
        with tempfile.TemporaryDirectory() as raw:
            ctx = make_ctx(Path(raw), asof=date(2999, 1, 1), today=date(2026, 11, 22))
            with self.assertRaises(ValueError):
                run_pipeline(ctx, select_steps(names=["news_market"]), scope="partial")

    def _dates(self):
        days = []
        cursor = date(2026, 1, 5)
        while len(days) < 60:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor += timedelta(days=1)
        return days

    def _factor_dir(self, tmp: Path) -> Path:
        folder = tmp / "ch04_data"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "summary.json").write_text(
            json.dumps({"status": "COMPLETE", "probe": False}), encoding="utf-8",
        )
        lines = ["date"] + [day.isoformat() for day in self._dates()]
        (folder / "trading_dates.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return folder

    def _write_inputs(self, tmp: Path):
        (tmp / "ch02_practice").mkdir(parents=True)
        (tmp / "ch02_practice" / "backfill_twstockdb.py").write_text("# local\n", encoding="utf-8")
        (tmp / "ch04_factor").mkdir(parents=True)
        (tmp / "ch04_factor" / "calculate_ch04_factors.py").write_text("# calc\n", encoding="utf-8")
        base = tmp / "ch05_regime" / "ch05_voting_v1"
        base.mkdir(parents=True)
        (base / "market_regime_voting.py").write_text("# vote\n", encoding="utf-8")
        (base / "market_regime_continuous.py").write_text("# score\n", encoding="utf-8")
        (base / "continuous_rules.json").write_text("{}\n", encoding="utf-8")
        input_dir = tmp / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        write_breadth(input_dir / "breadth.csv", [(ASOF, 10, 8)])
        write_yield(input_dir / "yield_spread.csv", [(ASOF, "0.29")])
        self._factor_dir(tmp)


class CliTests(unittest.TestCase):
    def test_list_and_dry_run_write_nothing(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "ch08_pipeline" / "run_daily.py"), "--list"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("市場廣度", proc.stdout)
        self.assertIn("backfill_twstockdb.py", proc.stdout)

        before = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--asof", safe_asof().isoformat(), "--status-backend", "sqlite",
                         "--status-path", str(ROOT / "ch08_pipeline" / "state" / "should-not-exist.sqlite")])
        after = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)
        self.assertEqual(code, 0)
        self.assertEqual(before, after)
        self.assertIn("乾跑", buf.getvalue())
        self.assertFalse((ROOT / "ch08_pipeline" / "state" / "should-not-exist.sqlite").exists())

    def test_future_asof_does_not_create_status_db(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "pipeline.sqlite"
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["--apply", "--asof", "2999-01-01", "--status-backend", "sqlite",
                             "--status-path", str(path), "--repo-root", raw])
            self.assertEqual(code, 2)
            self.assertFalse(path.exists())

    def test_ready_reads_sqlite_without_writing(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "pipeline.sqlite"
            store = SqliteStatusStore(path)
            store.ensure()
            guard = PipelineGuard(store)
            guard.start(safe_asof(), "full", NOW)
            guard.finish("COMPLETED", "完成", NOW)
            def count_steps():
                conn = sqlite3.connect(path)
                try:
                    return conn.execute("SELECT COUNT(*) FROM pipeline_steps").fetchone()[0]
                finally:
                    conn.close()

            before = count_steps()
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["--ready", "--apply", "--status-backend", "sqlite",
                             "--status-path", str(path), "--ready-wait", "0"])
            after = count_steps()
            self.assertEqual(code, 0)
            self.assertEqual(before, after)
            self.assertIn("可以往下使用", buf.getvalue())

            empty = Path(raw) / "missing.sqlite"
            with redirect_stdout(io.StringIO()):
                missing = main(["--ready", "--status-backend", "sqlite", "--status-path", str(empty)])
            self.assertEqual(missing, 2)
            self.assertFalse(empty.exists())

    def test_single_step_apply_is_partial(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = tmp / "pipeline.sqlite"
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main([
                    "--apply", "--asof", safe_asof().isoformat(), "--step", "news_market",
                    "--repo-root", str(tmp), "--status-backend", "sqlite", "--status-path", str(path),
                    "--input-dir", str(tmp / "input"),
                ])
            self.assertEqual(code, 0, buf.getvalue())
            store = SqliteStatusStore(path)
            self.assertIsNone(store.latest_run("daily_update", "full"))
            self.assertEqual(store.latest_run("daily_update", "partial").status, "COMPLETED")


if __name__ == "__main__":
    unittest.main()
