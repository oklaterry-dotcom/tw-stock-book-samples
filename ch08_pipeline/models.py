"""管線共用的資料結構。給使用者看的說明放在各步驟的 title 與 message。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional


@dataclass(frozen=True)
class StepSpec:
    """一個管線步驟。kind 為 real 才會真的跑；stub 只說明為何略過。"""

    name: str
    layer: int
    title: str
    kind: str
    summary: str

    @property
    def parallel(self) -> bool:
        return self.layer == 3


@dataclass(frozen=True)
class LocalFile:
    """使用者電腦上、尚未進 Git 的檔案。needed 為 False 表示管線不呼叫它。"""

    relative_path: str
    needed: bool
    purpose: str


@dataclass
class StepResult:
    name: str
    layer: int
    title: str
    kind: str
    status: str
    message: str
    elapsed_ms: int = 0
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


@dataclass(frozen=True)
class RunView:
    run_id: str
    pipeline_name: str
    asof: date
    started_at: datetime
    apply_mode: bool
    scope: str
    status: str
    message: str


@dataclass(frozen=True)
class ReadyResult:
    """pipeline_ready() 的結果。ready 為 False 代表這一輪應放棄。"""

    ready: bool
    status: str
    run_id: str | None
    message: str
    stale: bool = False
    asof: date | None = None


@dataclass
class PipelineResult:
    status: str
    asof: date
    apply: bool
    scope: str
    run_id: str | None
    steps: list[StepResult]
    message: str


@dataclass(frozen=True)
class StepContext:
    """單次執行的參數。fetch_text 與 run_command 在測試裡換成假的。"""

    repo_root: Path
    asof: date
    today: date
    apply: bool
    python: str
    input_dir: Path
    backup_dir: Path
    factor_data_dir: Path | None
    fetch_text: Callable[[str], str]
    sleep: Callable[[float], None]
    run_command: Callable[[list[str], Path], tuple[int, str]]
    now: datetime
    workers: int = 4
    pause_seconds: float = 1.0
    fetch_attempts: int = 3


def clip(text: str, limit: int = 1800) -> str:
    text = "" if text is None else str(text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def make_result(spec: StepSpec, status: str, message: str) -> StepResult:
    return StepResult(
        name=spec.name,
        layer=spec.layer,
        title=spec.title,
        kind=spec.kind,
        status=status,
        message=clip(message),
    )
