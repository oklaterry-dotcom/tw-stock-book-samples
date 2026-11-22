"""追加第五章 input CSV。

只做兩件事：市場廣度、10 年期減 2 年期公債利差。
既有日期的內容不改。寫入前先把舊檔整份複製到備份資料夾。
沒有通過檢查就不會動到原檔。
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path


class RefreshError(Exception):
    """資料檢查失敗。呼叫端應視為這個步驟失敗，且原檔應保持原樣。"""


def treasury_year_url(year: int) -> str:
    """美國財政部每日公債殖利率曲線（par yield curve）的年度 CSV。"""
    if isinstance(year, bool) or not isinstance(year, int) or not 1990 <= year <= 2100:
        raise RefreshError(f"公債資料年份不合理：{year}")
    return (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
        f"&field_tdr_date_value={year}&page&_format=csv"
    )


def breadth_day_url(day: date) -> str:
    """與第五章 market_regime_voting.breadth 相同的證交所端點。"""
    return (
        "https://www.twse.com.tw/exchangeReport/MI_INDEX"
        f"?response=json&date={day:%Y%m%d}&type=MS"
    )


def backup_file(path: Path, backup_dir: Path) -> Path:
    """複製舊檔。檔名帶時間戳，不會蓋過以前的備份。"""
    if not path.is_file():
        raise RefreshError(f"沒有可備份的檔案：{path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = backup_dir / f"{path.stem}.{stamp}{path.suffix}.bak"
    suffix = 0
    while dest.exists():
        suffix += 1
        dest = backup_dir / f"{path.stem}.{stamp}.{suffix}{path.suffix}.bak"
        if suffix > 20:
            raise RefreshError("無法建立不重複的備份檔名")
    dest.write_bytes(path.read_bytes())
    return dest


def restore_files(pairs: list[tuple[Path, Path]]) -> None:
    """把剛才備份的內容寫回。只用在本次命令失敗時。"""
    for path, backup in pairs:
        path.write_bytes(backup.read_bytes())


def protect_existing(paths: list[Path], backup_dir: Path) -> list[tuple[Path, Path]]:
    pairs = []
    for path in paths:
        if path.is_file():
            pairs.append((path, backup_file(path, backup_dir)))
    return pairs


def _fetch_with_retries(fetch_text, url: str, attempts: int, sleep) -> str:
    if attempts < 1:
        raise RefreshError("重試次數必須至少 1")
    last: Exception | None = None
    for index in range(attempts):
        try:
            return fetch_text(url)
        except RefreshError:
            raise
        except Exception as exc:
            last = exc
            if index + 1 == attempts:
                break
            sleep(1 + index)
    raise RefreshError(f"下載失敗 {url}：{type(last).__name__}: {last}") from last


def _parse_us_date(text: str) -> date:
    raw = text.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise RefreshError(f"無法辨識公債日期：{text}")


def _decimal_cell(text: str) -> Decimal | None:
    raw = (text or "").strip()
    if raw == "":
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise RefreshError(f"公債殖利率不是數字：{text}") from exc
    if not value.is_finite():
        raise RefreshError(f"公債殖利率不是有限數字：{text}")
    return value


def parse_treasury_csv(text: str, asof: date) -> dict[date, Decimal]:
    """10 年期減 2 年期，單位是百分點。0.29 代表 0.29 個百分點，不是 29%。"""
    reader = csv.DictReader(StringIO(text.lstrip("\ufeff")))
    fields = reader.fieldnames or []
    required = {"Date", "2 Yr", "10 Yr"}
    if not required <= set(fields):
        raise RefreshError(
            "美國財政部 CSV 缺少 Date、2 Yr 或 10 Yr。"
            f"實際欄位：{', '.join(fields)}"
        )
    found: dict[date, Decimal] = {}
    for line_no, row in enumerate(reader, start=2):
        if not (row.get("Date") or "").strip():
            continue
        day = _parse_us_date(row["Date"])
        if day > asof:
            continue
        two = _decimal_cell(row.get("2 Yr") or "")
        ten = _decimal_cell(row.get("10 Yr") or "")
        if two is None or ten is None:
            continue
        spread = ten - two
        if day in found and found[day] != spread:
            raise RefreshError(f"公債 CSV 第 {line_no} 列日期重複且數值不同：{day}")
        found[day] = spread
    return found


def _require_header(path: Path, expected: list[str]) -> csv.DictReader:
    # 呼叫端負責關閉檔案。這裡只檢查表頭，避免追加時欄位對不齊。
    handle = path.open(encoding="utf-8-sig", newline="")
    reader = csv.DictReader(handle)
    fields = [name.strip() for name in (reader.fieldnames or [])]
    if fields != expected:
        handle.close()
        raise RefreshError(
            f"{path.name} 的欄位必須是 {','.join(expected)}，"
            f"實際是 {','.join(fields) or '（空檔）'}"
        )
    reader._handle = handle  # type: ignore[attr-defined]
    return reader


def _close_reader(reader: csv.DictReader) -> None:
    handle = getattr(reader, "_handle", None)
    if handle is not None:
        handle.close()


def read_yield_file(path: Path) -> dict[date, Decimal]:
    reader = _require_header(path, ["date", "value"])
    found: dict[date, Decimal] = {}
    try:
        for line_no, row in enumerate(reader, start=2):
            raw_day = (row.get("date") or "").strip()
            if raw_day == "":
                continue
            day = date.fromisoformat(raw_day)
            value = _decimal_cell(row.get("value") or "")
            if value is None:
                raise RefreshError(f"{path.name}:{line_no} 利差是空的")
            if day in found:
                raise RefreshError(f"{path.name}:{line_no} 日期重複：{day}")
            found[day] = value
    except ValueError as exc:
        raise RefreshError(f"{path.name} 的日期無法讀取：{exc}") from exc
    finally:
        _close_reader(reader)
    return found


def read_breadth_file(path: Path) -> dict[date, tuple[int, int]]:
    reader = _require_header(path, ["date", "advances", "declines"])
    found: dict[date, tuple[int, int]] = {}
    try:
        for line_no, row in enumerate(reader, start=2):
            raw_day = (row.get("date") or "").strip()
            if raw_day == "":
                continue
            day = date.fromisoformat(raw_day)
            try:
                advances = int(str(row["advances"]).strip())
                declines = int(str(row["declines"]).strip())
            except (TypeError, ValueError) as exc:
                raise RefreshError(f"{path.name}:{line_no} 漲跌家數不是整數") from exc
            if min(advances, declines) < 0 or advances + declines <= 0:
                raise RefreshError(f"{path.name}:{line_no} 漲跌家數不合理")
            if day in found:
                raise RefreshError(f"{path.name}:{line_no} 日期重複：{day}")
            found[day] = (advances, declines)
    except ValueError as exc:
        raise RefreshError(f"{path.name} 的日期無法讀取：{exc}") from exc
    finally:
        _close_reader(reader)
    return found


def _ends_with_newline(path: Path) -> bool:
    raw = path.read_bytes()
    return raw.endswith(b"\n")


def _newline(path: Path) -> str:
    raw = path.read_bytes()
    if b"\r\n" in raw:
        return "\r\n"
    return "\n"


def _append_lines(path: Path, lines: list[str]) -> None:
    """只在檔尾追加。呼叫前必須已備份，而且檔案本身以換行結束。"""
    original = path.read_bytes()
    if not original.endswith(b"\n"):
        raise RefreshError(f"{path.name} 結尾沒有換行，拒絕追加")
    payload = "".join(lines).encode("utf-8")
    try:
        with path.open("ab") as handle:
            handle.write(payload)
        if not path.read_bytes().startswith(original):
            raise RefreshError("追加後檔頭與備份不一致")
    except Exception:
        path.write_bytes(original)
        raise


def _exclusive_create(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RefreshError(f"檔案已存在，拒絕覆蓋：{path}") from exc
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def refresh_yield_spread(
    path: Path,
    asof: date,
    fetch_text,
    backup_dir: Path,
    sleep,
    attempts: int = 3,
) -> str:
    """追加新的利差。檔案不存在時才新建；已有的日期一律不改。"""
    existing: dict[date, Decimal] = {}
    if path.exists():
        existing = read_yield_file(path)
        if existing and max(existing) >= asof:
            return "yield_spread.csv 已涵蓋 asof，不連線、不改檔"

    years = {asof.year}
    if existing:
        years.add(max(existing).year)
    fetched: dict[date, Decimal] = {}
    for year in sorted(years):
        text = _fetch_with_retries(fetch_text, treasury_year_url(year), attempts, sleep)
        for day, spread in parse_treasury_csv(text, asof).items():
            if day in fetched and fetched[day] != spread:
                raise RefreshError(f"{day} 在不同年度檔裡的利差不一致")
            fetched[day] = spread
    if not fetched:
        raise RefreshError("美國財政部沒有不晚於 asof 的 10 年與 2 年殖利率，不寫檔")

    conflicts = []
    for day, old in sorted(existing.items()):
        if day in fetched and fetched[day] != old:
            conflicts.append(f"{day} 檔內 {old}、財政部 {fetched[day]}")
    if conflicts:
        preview = "；".join(conflicts[:3])
        raise RefreshError(
            "既有日期與美國財政部公債殖利率曲線不一致，已停止且未改檔："
            f"{preview}。若這份檔是以前的 FRED 資料，請自行改名或移走（程式不會刪除），"
            "再執行 --apply，才會用財政部資料新建。"
        )

    new_days = sorted(day for day in fetched if day not in existing and (not existing or day > max(existing)))
    if path.exists() and not new_days:
        return "沒有晚於檔內最後日期、且不晚於 asof 的新利差，不改檔"
    if not path.exists():
        new_days = sorted(fetched)
        lines = ["date,value"]
        lines.extend(f"{day.isoformat()},{format(fetched[day], 'f')}" for day in new_days)
        _exclusive_create(path, ("\n".join(lines) + "\n").encode("utf-8-sig"))
        return (
            f"新建 yield_spread.csv，寫入 {len(new_days)} 筆。"
            "先前沒有舊檔，所以沒有備份。來源是美國財政部每日公債殖利率曲線，不是 FRED。"
        )

    if not _ends_with_newline(path):
        raise RefreshError(f"{path.name} 結尾沒有換行，拒絕追加")
    backup = backup_file(path, backup_dir)
    newline = _newline(path)
    payload = [f"{day.isoformat()},{format(fetched[day], 'f')}{newline}" for day in new_days]
    _append_lines(path, payload)
    checked = read_yield_file(path)
    for day in new_days:
        if checked.get(day) != fetched[day]:
            path.write_bytes(backup.read_bytes())
            raise RefreshError("追加後重新讀取與來源不符，已還原為備份內容")
    return f"追加 {len(new_days)} 筆利差。舊檔備份：{backup.name}"


def _voting_module():
    cached = getattr(_voting_module, "module", None)
    if cached is not None:
        return cached
    path = (
        Path(__file__).resolve().parents[1]
        / "ch05_regime" / "ch05_voting_v1" / "market_regime_voting.py"
    )
    if not path.is_file():
        raise RefreshError(f"找不到第五章解析程式：{path}")
    spec = importlib.util.spec_from_file_location("ch05_market_regime_voting", path)
    if spec is None or spec.loader is None:
        raise RefreshError(f"無法載入 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _voting_module.module = module
    return module


def _breadth_from_payload(day: date, text: str) -> tuple[int, int] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RefreshError(f"{day} 證交所回應不是 JSON") from exc
    if not isinstance(payload, dict):
        raise RefreshError(f"{day} 證交所回應不是物件")
    if payload.get("stat") != "OK":
        return None
    voting = _voting_module()
    raw_date = payload.get("date")
    if raw_date and voting.parse_date(str(raw_date)) != day:
        raise RefreshError(f"證交所回傳日期與要求不符：要求 {day}，收到 {raw_date}")
    try:
        found = voting.parse_breadth(payload)
    except (TypeError, ValueError) as exc:
        raise RefreshError(f"{day} 無法解析漲跌家數：{exc}") from exc
    advances = int(found["advances"])
    declines = int(found["declines"])
    if min(advances, declines) < 0 or advances + declines <= 0:
        raise RefreshError(f"{day} 漲跌家數不合理：{advances} / {declines}")
    return advances, declines


def refresh_breadth(
    path: Path,
    asof: date,
    fetch_text,
    backup_dir: Path,
    sleep,
    attempts: int = 3,
    pause_seconds: float = 1.0,
) -> str:
    """只追加最後一筆之後的上市股票漲跌家數。沒有既有檔就停止。"""
    if not path.is_file():
        raise RefreshError(
            f"找不到 {path}。請放上本機既有的 breadth.csv（由 TWSE MI_INDEX 匯出，"
            "至少要能涵蓋 McClellan 的 60 日）。管線只追加新日期，不會從零建立，"
            "也不用 StockDailyPrice 回推。"
        )
    existing = read_breadth_file(path)
    if not existing:
        raise RefreshError("breadth.csv 沒有任何資料列，拒絕從空檔開始編造歷史")
    last = max(existing)
    if last >= asof:
        return "breadth.csv 已涵蓋 asof，不連線、不改檔"

    wanted: list[date] = []
    day = last + timedelta(days=1)
    while day <= asof:
        if day.weekday() < 5:
            wanted.append(day)
        day += timedelta(days=1)
    if not wanted:
        return "最後日期與 asof 之間沒有平日，不連線、不改檔"

    fresh: list[tuple[date, int, int]] = []
    for index, current in enumerate(wanted):
        if index and pause_seconds:
            sleep(pause_seconds)
        text = _fetch_with_retries(fetch_text, breadth_day_url(current), attempts, sleep)
        parsed = _breadth_from_payload(current, text)
        if parsed is None:
            continue
        advances, declines = parsed
        previous = existing.get(current)
        if previous is not None and previous != (advances, declines):
            raise RefreshError(
                f"{current} 已在 breadth.csv，但證交所數值不同，拒絕改寫舊列"
            )
        if previous is None:
            fresh.append((current, advances, declines))
    if not fresh:
        return "指定區間沒有新的上市漲跌家數（休市日已略過），不改檔"

    if not _ends_with_newline(path):
        raise RefreshError(f"{path.name} 結尾沒有換行，拒絕追加")
    backup = backup_file(path, backup_dir)
    newline = _newline(path)
    payload = [
        f"{current.isoformat()},{advances},{declines}{newline}"
        for current, advances, declines in fresh
    ]
    _append_lines(path, payload)
    checked = read_breadth_file(path)
    for current, advances, declines in fresh:
        if checked.get(current) != (advances, declines):
            path.write_bytes(backup.read_bytes())
            raise RefreshError("市場廣度追加後重新讀取不符，已還原為備份內容")
    return f"追加 {len(fresh)} 個交易日的漲跌家數。舊檔備份：{backup.name}"
