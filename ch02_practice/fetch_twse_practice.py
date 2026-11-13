import time
from pathlib import Path
import pandas as pd
import requests

folder = Path(__file__).resolve().parent
frames = []

for day in range(2, 7):
    date_key = f"202611{day:02d}"
    print(f"下載 TWSE {date_key}……", flush=True)

    response = requests.get(
        "https://www.twse.com.tw/exchangeReport/MI_INDEX",
        params={
            "date": date_key,
            "type": "ALLBUT0999",
            "response": "json",
        },
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("stat") != "OK" or payload.get("date") != date_key:
        raise RuntimeError(f"日期或回應異常：{payload.get('stat')}")

    columns = {
        "開盤價": "open",
        "最高價": "high",
        "最低價": "low",
        "收盤價": "close",
        "成交股數": "volume",
        "證券代號": "stock_code",
    }
    table = next(
        (t for t in payload.get("tables", [])
         if set(columns).issubset(t.get("fields", []))),
        None,
    )
    if table is None:
        raise RuntimeError("找不到個股行情表，請貼上錯誤訊息")

    raw = pd.DataFrame(table["data"], columns=table["fields"])
    raw["證券代號"] = raw["證券代號"].astype(str).str.strip()
    mask = raw["證券代號"].str.fullmatch(r"[1-9][0-9]{3}")
    daily = raw.loc[mask, list(columns)].rename(columns=columns)
    daily.insert(0, "date", f"115/11/{day:02d}")

    for col in ("open", "high", "low", "close", "volume"):
        daily[col] = pd.to_numeric(
            daily[col].astype(str).str.replace(",", "", regex=False),
            errors="coerce",
        )

    if daily.empty or daily["stock_code"].duplicated().any():
        raise RuntimeError("行情為空或股票代號重複，停止處理")

    daily.to_csv(
        folder / f"TWSE_{date_key}.csv",
        index=False, encoding="utf-8-sig",
    )
    frames.append(daily)
    print(f"已存檔，共 {len(daily):,} 筆", flush=True)
    time.sleep(3)

merged = pd.concat(frames, ignore_index=True)
merged = merged.sort_values(["stock_code", "date"])
output = folder / "TWSE_20261102_20261106.csv"
merged.to_csv(output, index=False, encoding="utf-8-sig")
print(f"下載完成，共 {len(merged):,} 筆：{output}")
