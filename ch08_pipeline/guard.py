"""PipelineGuard 與 pipeline_ready()。

狀態只追加，不回寫舊列：
pipeline_runs 記一次執行的初值；最新狀態是 pipeline_steps 裡
step_name 為 __run_status__ 的最後一筆。超過兩小時仍是 RUNNING，
下次 --apply 會再追加一筆 FAILED，不會改掉原來的 RUNNING 事件。

市場資料不寫進這兩張表。SQL Server 連不上時改用本機 SQLite 或 JSON。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ch08_pipeline.catalog import PIPELINE_NAME
from ch08_pipeline.models import ReadyResult, RunView, StepResult, clip

RUN_STATUS_STEP = "__run_status__"
ZOMBIE_AFTER = timedelta(hours=2)


class StatusError(Exception):
    pass


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="seconds")


def _parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return _as_utc(parsed)


class SqliteStatusStore:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS pipeline_runs (
                    run_id TEXT PRIMARY KEY,
                    pipeline_name TEXT NOT NULL,
                    asof_date TEXT NOT NULL,
                    initial_status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    apply_mode INTEGER NOT NULL,
                    scope TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS pipeline_steps (
                    step_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    layer_no INTEGER NOT NULL,
                    step_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    elapsed_ms INTEGER,
                    message TEXT
                )"""
            )
            conn.commit()
        finally:
            conn.close()

    def start_run(self, run_id: str, pipeline_name: str, asof: date,
                  started_at: datetime, apply_mode: bool, scope: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO pipeline_runs
                   (run_id, pipeline_name, asof_date, initial_status, started_at, apply_mode, scope)
                   VALUES (?, ?, ?, 'RUNNING', ?, ?, ?)""",
                (run_id, pipeline_name, asof.isoformat(), _stamp(started_at),
                 1 if apply_mode else 0, scope),
            )
            self._insert_step(conn, run_id, 0, RUN_STATUS_STEP, "RUNNING",
                              started_at, started_at, 0, "管線開始")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_step(self, run_id: str, result: StepResult, started_at: datetime,
                 finished_at: datetime) -> None:
        conn = self._connect()
        try:
            self._insert_step(conn, run_id, result.layer, result.name, result.status,
                              started_at, finished_at, result.elapsed_ms, result.message)
            conn.commit()
        finally:
            conn.close()

    def add_run_status(self, run_id: str, status: str, message: str, when: datetime) -> None:
        conn = self._connect()
        try:
            self._insert_step(conn, run_id, 0, RUN_STATUS_STEP, status, when, when, 0, message)
            conn.commit()
        finally:
            conn.close()

    def _insert_step(self, conn, run_id, layer, name, status, started, finished, elapsed, message):
        conn.execute(
            """INSERT INTO pipeline_steps
               (run_id, layer_no, step_name, status, started_at, finished_at, elapsed_ms, message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, layer, name, status, _stamp(started), _stamp(finished),
             int(elapsed), clip(message)),
        )

    def latest_run(self, pipeline_name: str, scope: str = "full") -> RunView | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """SELECT run_id, pipeline_name, asof_date, started_at, apply_mode, scope
                   FROM pipeline_runs
                   WHERE pipeline_name = ? AND scope = ?
                   ORDER BY started_at DESC, run_id DESC
                   LIMIT 1""",
                (pipeline_name, scope),
            ).fetchone()
            if row is None:
                return None
            return self._view(conn, row)
        finally:
            conn.close()

    def iter_runs(self, pipeline_name: str) -> list[RunView]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT run_id, pipeline_name, asof_date, started_at, apply_mode, scope
                   FROM pipeline_runs WHERE pipeline_name = ?
                   ORDER BY started_at, run_id""",
                (pipeline_name,),
            ).fetchall()
            return [self._view(conn, row) for row in rows]
        finally:
            conn.close()

    def _view(self, conn, row) -> RunView:
        status_row = conn.execute(
            """SELECT status, message FROM pipeline_steps
               WHERE run_id = ? AND step_name = ?
               ORDER BY step_row_id DESC LIMIT 1""",
            (row["run_id"], RUN_STATUS_STEP),
        ).fetchone()
        if status_row is None:
            status, message = "UNKNOWN", "找不到狀態事件"
        else:
            status, message = status_row["status"], status_row["message"] or ""
        return RunView(
            run_id=row["run_id"],
            pipeline_name=row["pipeline_name"],
            asof=date.fromisoformat(row["asof_date"]),
            started_at=_parse_stamp(row["started_at"]),
            apply_mode=bool(row["apply_mode"]),
            scope=row["scope"],
            status=status,
            message=message,
        )

    def sweep_zombies(self, pipeline_name: str, now: datetime,
                      older_than: timedelta = ZOMBIE_AFTER) -> int:
        cutoff = _as_utc(now) - older_than
        stuck = [
            run for run in self.iter_runs(pipeline_name)
            if run.status == "RUNNING" and run.started_at < cutoff
        ]
        for run in stuck:
            self.add_run_status(run.run_id, "FAILED", "殭屍防護：超過 2 小時未完成", now)
        return len(stuck)


class JsonStatusStore:
    """一個執行一個檔、一筆事件一個檔，避免改寫既有 JSON。"""

    def __init__(self, root: Path):
        self.root = root

    def ensure(self) -> None:
        (self.root / "runs").mkdir(parents=True, exist_ok=True)
        (self.root / "events").mkdir(parents=True, exist_ok=True)

    def _write_new(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise StatusError(f"拒絕覆蓋既有狀態檔：{path}") from exc
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def start_run(self, run_id: str, pipeline_name: str, asof: date,
                  started_at: datetime, apply_mode: bool, scope: str) -> None:
        self._write_new(self.root / "runs" / f"{run_id}.json", {
            "run_id": run_id,
            "pipeline_name": pipeline_name,
            "asof_date": asof.isoformat(),
            "initial_status": "RUNNING",
            "started_at": _stamp(started_at),
            "apply_mode": bool(apply_mode),
            "scope": scope,
        })
        self.add_run_status(run_id, "RUNNING", "管線開始", started_at)

    def _next_event_path(self, run_id: str) -> Path:
        folder = self.root / "events" / run_id
        folder.mkdir(parents=True, exist_ok=True)
        numbers = []
        for path in folder.glob("*.json"):
            try:
                numbers.append(int(path.stem))
            except ValueError:
                continue
        return folder / f"{max(numbers, default=0) + 1:06d}.json"

    def _append_event(self, run_id: str, payload: dict) -> None:
        self._write_new(self._next_event_path(run_id), payload)

    def add_step(self, run_id: str, result: StepResult, started_at: datetime,
                 finished_at: datetime) -> None:
        self._append_event(run_id, {
            "type": "step",
            "run_id": run_id,
            "layer_no": result.layer,
            "step_name": result.name,
            "status": result.status,
            "started_at": _stamp(started_at),
            "finished_at": _stamp(finished_at),
            "elapsed_ms": int(result.elapsed_ms),
            "message": clip(result.message),
        })

    def add_run_status(self, run_id: str, status: str, message: str, when: datetime) -> None:
        self._append_event(run_id, {
            "type": "run_status",
            "run_id": run_id,
            "layer_no": 0,
            "step_name": RUN_STATUS_STEP,
            "status": status,
            "started_at": _stamp(when),
            "finished_at": _stamp(when),
            "elapsed_ms": 0,
            "message": clip(message),
        })

    def _runs(self) -> list[dict]:
        folder = self.root / "runs"
        if not folder.is_dir():
            return []
        rows = []
        for path in sorted(folder.glob("*.json")):
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        return rows

    def _events(self, run_id: str) -> list[dict]:
        folder = self.root / "events" / run_id
        if not folder.is_dir():
            return []
        rows = []
        for path in sorted(folder.glob("*.json")):
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        return rows

    def _view(self, row: dict) -> RunView:
        events = [item for item in self._events(row["run_id"]) if item.get("type") == "run_status"]
        if events:
            status = events[-1]["status"]
            message = events[-1].get("message") or ""
        else:
            status, message = "UNKNOWN", "找不到狀態事件"
        return RunView(
            run_id=row["run_id"],
            pipeline_name=row["pipeline_name"],
            asof=date.fromisoformat(row["asof_date"]),
            started_at=_parse_stamp(row["started_at"]),
            apply_mode=bool(row["apply_mode"]),
            scope=row["scope"],
            status=status,
            message=message,
        )

    def latest_run(self, pipeline_name: str, scope: str = "full") -> RunView | None:
        matched = [
            row for row in self._runs()
            if row.get("pipeline_name") == pipeline_name and row.get("scope") == scope
        ]
        if not matched:
            return None
        matched.sort(key=lambda row: (row["started_at"], row["run_id"]))
        return self._view(matched[-1])

    def iter_runs(self, pipeline_name: str) -> list[RunView]:
        rows = [row for row in self._runs() if row.get("pipeline_name") == pipeline_name]
        rows.sort(key=lambda row: (row["started_at"], row["run_id"]))
        return [self._view(row) for row in rows]

    def sweep_zombies(self, pipeline_name: str, now: datetime,
                      older_than: timedelta = ZOMBIE_AFTER) -> int:
        cutoff = _as_utc(now) - older_than
        stuck = [
            run for run in self.iter_runs(pipeline_name)
            if run.status == "RUNNING" and run.started_at < cutoff
        ]
        for run in stuck:
            self.add_run_status(run.run_id, "FAILED", "殭屍防護：超過 2 小時未完成", now)
        return len(stuck)


class MssqlStatusStore:
    """MiniQuantDemo 的 dbo.pipeline_runs / dbo.pipeline_steps。

    連線由呼叫端提供，方便測試時不要碰到真的 SQL Server。
    """

    def __init__(self, connect):
        self._connect = connect

    def _connection(self):
        return self._connect()

    def ensure(self) -> None:
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """IF OBJECT_ID(N'dbo.pipeline_runs', N'U') IS NULL
                   CREATE TABLE dbo.pipeline_runs (
                       run_id nvarchar(36) NOT NULL PRIMARY KEY,
                       pipeline_name nvarchar(64) NOT NULL,
                       asof_date date NOT NULL,
                       initial_status nvarchar(16) NOT NULL,
                       started_at datetime2 NOT NULL,
                       apply_mode bit NOT NULL,
                       scope nvarchar(16) NOT NULL
                   )"""
            )
            cursor.execute(
                """IF OBJECT_ID(N'dbo.pipeline_steps', N'U') IS NULL
                   CREATE TABLE dbo.pipeline_steps (
                       step_row_id bigint IDENTITY(1,1) NOT NULL PRIMARY KEY,
                       run_id nvarchar(36) NOT NULL,
                       layer_no int NOT NULL,
                       step_name nvarchar(64) NOT NULL,
                       status nvarchar(16) NOT NULL,
                       started_at datetime2 NOT NULL,
                       finished_at datetime2 NULL,
                       elapsed_ms int NULL,
                       message nvarchar(2000) NULL
                   )"""
            )
            conn.commit()
        finally:
            conn.close()

    def assert_ready(self) -> None:
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT OBJECT_ID(N'dbo.pipeline_runs', N'U')")
            runs = cursor.fetchone()
            cursor.execute("SELECT OBJECT_ID(N'dbo.pipeline_steps', N'U')")
            steps = cursor.fetchone()
            if runs is None or runs[0] is None or steps is None or steps[0] is None:
                raise StatusError("MiniQuantDemo 還沒有 pipeline_runs / pipeline_steps")
        finally:
            conn.close()

    def start_run(self, run_id: str, pipeline_name: str, asof: date,
                  started_at: datetime, apply_mode: bool, scope: str) -> None:
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO dbo.pipeline_runs
                   (run_id, pipeline_name, asof_date, initial_status, started_at, apply_mode, scope)
                   VALUES (?, ?, ?, 'RUNNING', ?, ?, ?)""",
                run_id, pipeline_name, asof, _as_utc(started_at).replace(tzinfo=None),
                1 if apply_mode else 0, scope,
            )
            self._insert_step(cursor, run_id, 0, RUN_STATUS_STEP, "RUNNING",
                              started_at, started_at, 0, "管線開始")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_step(self, run_id: str, result: StepResult, started_at: datetime,
                 finished_at: datetime) -> None:
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._insert_step(cursor, run_id, result.layer, result.name, result.status,
                              started_at, finished_at, result.elapsed_ms, result.message)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_run_status(self, run_id: str, status: str, message: str, when: datetime) -> None:
        conn = self._connection()
        try:
            cursor = conn.cursor()
            self._insert_step(cursor, run_id, 0, RUN_STATUS_STEP, status,
                              when, when, 0, message)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _insert_step(self, cursor, run_id, layer, name, status, started, finished, elapsed, message):
        cursor.execute(
            """INSERT INTO dbo.pipeline_steps
               (run_id, layer_no, step_name, status, started_at, finished_at, elapsed_ms, message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            run_id, layer, name, status,
            _as_utc(started).replace(tzinfo=None),
            _as_utc(finished).replace(tzinfo=None),
            int(elapsed), clip(message),
        )

    def latest_run(self, pipeline_name: str, scope: str = "full") -> RunView | None:
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT TOP 1 run_id, pipeline_name, asof_date, started_at, apply_mode, scope
                   FROM dbo.pipeline_runs
                   WHERE pipeline_name = ? AND scope = ?
                   ORDER BY started_at DESC, run_id DESC""",
                pipeline_name, scope,
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._view(cursor, row)
        finally:
            conn.close()

    def iter_runs(self, pipeline_name: str) -> list[RunView]:
        conn = self._connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT run_id, pipeline_name, asof_date, started_at, apply_mode, scope
                   FROM dbo.pipeline_runs WHERE pipeline_name = ?
                   ORDER BY started_at, run_id""",
                pipeline_name,
            )
            return [self._view(cursor, row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def _view(self, cursor, row) -> RunView:
        cursor.execute(
            """SELECT TOP 1 status, message FROM dbo.pipeline_steps
               WHERE run_id = ? AND step_name = ?
               ORDER BY step_row_id DESC""",
            row[0], RUN_STATUS_STEP,
        )
        status_row = cursor.fetchone()
        if status_row is None:
            status, message = "UNKNOWN", "找不到狀態事件"
        else:
            status, message = status_row[0], status_row[1] or ""
        started = row[3]
        if isinstance(started, str):
            started = _parse_stamp(started)
        elif started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        asof = row[2]
        if isinstance(asof, str):
            asof = date.fromisoformat(asof)
        elif isinstance(asof, datetime):
            asof = asof.date()
        return RunView(
            run_id=row[0],
            pipeline_name=row[1],
            asof=asof,
            started_at=started,
            apply_mode=bool(row[4]),
            scope=row[5],
            status=status,
            message=message,
        )

    def sweep_zombies(self, pipeline_name: str, now: datetime,
                      older_than: timedelta = ZOMBIE_AFTER) -> int:
        cutoff = _as_utc(now) - older_than
        stuck = [
            run for run in self.iter_runs(pipeline_name)
            if run.status == "RUNNING" and run.started_at < cutoff
        ]
        for run in stuck:
            self.add_run_status(run.run_id, "FAILED", "殭屍防護：超過 2 小時未完成", now)
        return len(stuck)


def connect_miniquant():
    """連到 MiniQuantDemo。驅動程式先用 17，沒有再用 18。"""
    try:
        import pyodbc
    except ImportError as exc:
        raise StatusError("這個環境沒有 pyodbc，無法連接 SQL Server") from exc
    drivers = list(pyodbc.drivers())
    preferred = [
        os.getenv("MINIQUANT_SQL_DRIVER"),
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 18 for SQL Server",
    ]
    driver = next((item for item in preferred if item and item in drivers), None)
    if driver is None:
        raise StatusError("找不到 ODBC Driver 17 或 18 for SQL Server")
    server = os.getenv("MINIQUANT_SQL_SERVER", r"localhost\SQLEXPRESS")
    database = os.getenv("MINIQUANT_SQL_DATABASE", "MiniQuantDemo")
    return pyodbc.connect(
        f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
        "Trusted_Connection=yes;TrustServerCertificate=yes;",
        timeout=5,
    )


def open_store(backend: str, sqlite_path: Path, *, connect=None, create: bool = True,
               json_path: Path | None = None):
    """apply 時建立狀態庫。auto 會先試 SQL Server，失敗再改 SQLite。"""
    backend = backend.lower()
    json_path = json_path or sqlite_path.with_name("json")
    if backend == "sqlite":
        store = SqliteStatusStore(sqlite_path)
        if create:
            store.ensure()
        elif not sqlite_path.is_file():
            raise FileNotFoundError(sqlite_path)
        return store, f"SQLite {sqlite_path}"
    if backend == "json":
        store = JsonStatusStore(json_path)
        if create:
            store.ensure()
        elif not json_path.is_dir():
            raise FileNotFoundError(json_path)
        return store, f"JSON {json_path}"
    if backend == "mssql":
        store = MssqlStatusStore(connect or connect_miniquant)
        if create:
            store.ensure()
        else:
            store.assert_ready()
        return store, "MiniQuantDemo.dbo.pipeline_runs / dbo.pipeline_steps"
    if backend != "auto":
        raise StatusError(f"不認識的狀態庫：{backend}")
    try:
        store = MssqlStatusStore(connect or connect_miniquant)
        if create:
            store.ensure()
        else:
            store.assert_ready()
        return store, "MiniQuantDemo.dbo.pipeline_runs / dbo.pipeline_steps"
    except Exception as exc:
        if not create:
            if sqlite_path.is_file():
                return SqliteStatusStore(sqlite_path), f"SQLite {sqlite_path}"
            if json_path.is_dir():
                return JsonStatusStore(json_path), f"JSON {json_path}"
            raise StatusError("沒有可讀的狀態庫") from exc
        print(f"SQL Server 狀態表無法使用，改寫本機 SQLite：{exc}", file=sys.stderr)
        store = SqliteStatusStore(sqlite_path)
        store.ensure()
        return store, f"SQLite {sqlite_path}"


def peek_store(backend: str, sqlite_path: Path, json_path: Path | None = None, connect=None):
    """--ready 使用。不建立檔案、不建立資料表。沒有狀態庫時回傳 None。"""
    json_path = json_path or sqlite_path.with_name("json")
    try:
        if backend == "auto":
            try:
                store = MssqlStatusStore(connect or connect_miniquant)
                store.assert_ready()
                return store
            except Exception:
                if sqlite_path.is_file():
                    return SqliteStatusStore(sqlite_path)
                if json_path.is_dir():
                    return JsonStatusStore(json_path)
                return None
        store, _label = open_store(
            backend, sqlite_path, connect=connect, create=False, json_path=json_path,
        )
        return store
    except FileNotFoundError:
        return None


class PipelineGuard:
    """書上的狀態機：開始為 RUNNING，結束為 COMPLETED 或 FAILED。"""

    def __init__(self, store, pipeline_name: str = PIPELINE_NAME):
        self.store = store
        self.pipeline_name = pipeline_name
        self.run_id = ""

    def sweep(self, now: datetime, older_than: timedelta = ZOMBIE_AFTER) -> int:
        return self.store.sweep_zombies(self.pipeline_name, now, older_than)

    def start(self, asof: date, scope: str, now: datetime) -> str:
        self.run_id = str(uuid.uuid4())
        self.store.start_run(self.run_id, self.pipeline_name, asof, now, True, scope)
        return self.run_id

    def record(self, result: StepResult, started_at: datetime, finished_at: datetime) -> None:
        self.store.add_step(self.run_id, result, started_at, finished_at)

    def finish(self, status: str, message: str, now: datetime) -> None:
        if status not in {"COMPLETED", "FAILED"}:
            raise StatusError("結束狀態只能是 COMPLETED 或 FAILED")
        self.store.add_run_status(self.run_id, status, message, now)


def pipeline_ready(
    store,
    pipeline_name: str = PIPELINE_NAME,
    *,
    clock=None,
    sleep=None,
    max_wait_seconds: float = 120,
    poll_seconds: float = 10,
    zombie_after: timedelta = ZOMBIE_AFTER,
    scope: str = "full",
) -> ReadyResult:
    """盤後更新還沒好時，呼叫端應放棄這一輪。

    RUNNING：每 poll_seconds 看一次，最多等 max_wait_seconds。
    時間到仍在跑就放棄。COMPLETED 可以繼續。FAILED 也可以繼續，
    但 stale 為 True，代表應沿用上次成功的資料。
    超過 zombie_after 仍是 RUNNING，視為 FAILED，而且這次檢查不寫狀態。
    """
    if clock is None:
        clock = lambda: datetime.now(timezone.utc)
    if sleep is None:
        sleep = time.sleep
    if max_wait_seconds < 0 or poll_seconds < 0:
        raise ValueError("等待時間不能是負的")
    deadline = clock() + timedelta(seconds=max_wait_seconds)
    while True:
        run = store.latest_run(pipeline_name, scope=scope)
        now = clock()
        if run is None:
            return ReadyResult(
                False, "NO_RUN", None,
                "尚無完整的每日更新紀錄，放棄本輪。",
            )
        if run.status == "RUNNING":
            if now - run.started_at >= zombie_after:
                return ReadyResult(
                    True, "FAILED", run.run_id,
                    "這次更新超過 2 小時仍是執行中，視為失敗，沿用上次成功的資料。"
                    "這次檢查沒有改狀態表。",
                    True, run.asof,
                )
            if now >= deadline:
                return ReadyResult(
                    False, "RUNNING", run.run_id,
                    "每日更新仍在執行，放棄本輪，避免讀到寫到一半的資料。",
                    False, run.asof,
                )
            sleep(poll_seconds)
            continue
        if run.status == "COMPLETED":
            return ReadyResult(
                True, "COMPLETED", run.run_id,
                f"每日更新已完成（asof {run.asof}），可以往下使用。",
                False, run.asof,
            )
        if run.status == "FAILED":
            return ReadyResult(
                True, "FAILED", run.run_id,
                f"每日更新失敗（asof {run.asof}），沿用上次成功的資料。",
                True, run.asof,
            )
        return ReadyResult(
            False, run.status, run.run_id,
            f"無法判斷的狀態：{run.status}",
            False, run.asof,
        )
