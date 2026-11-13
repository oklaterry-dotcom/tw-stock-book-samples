import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyodbc

folder = Path(__file__).resolve().parent
sources = [
    ("TWSE", folder / "TWSE_20261102_20261106.csv"),
    ("TPEX", folder / "TPEX_202611.csv"),
]
expected_dates = {date(2026, 11, day) for day in range(2, 7)}
rows = []
keys = set()

def number(value):
    text = (value or "").strip().replace(",", "")
    if text in ("", "--", "---"):
        return None
    result = Decimal(text)
    if not result.is_finite():
        raise ValueError(f"無效數字：{text}")
    return result

# 先檢查兩份檔案，全部通過後才連線寫入
for market, path in sources:
    if not path.is_file():
        raise FileNotFoundError(f"找不到檔案：{path}")

    dates = set()
    count = 0
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"date", "stock_code", "open", "high",
                    "low", "close", "volume"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"{path.name} 缺少必要欄位")

        for line, row in enumerate(reader, start=2):
            year, month, day = map(int, row["date"].strip().split("/"))
            trade_date = date(year + 1911, month, day)
            if trade_date not in expected_dates:
                raise ValueError(f"{path.name}:{line} 日期超出指定期間")

            code = row["stock_code"].strip()
            if not code:
                raise ValueError(f"{path.name}:{line} 股票代號為空")
            key = (trade_date, code, market)
            if key in keys:
                raise ValueError(f"CSV 有重複資料：{key}")
            keys.add(key)

            prices = [number(row[c]) for c in
                      ("open", "high", "low", "close")]
            volume = number(row["volume"])
            if volume is not None:
                if volume < 0 or volume != volume.to_integral_value():
                    raise ValueError(f"{path.name}:{line} 成交量異常")
                volume = int(volume)

            rows.append(key + tuple(prices) + (volume,))
            dates.add(trade_date)
            count += 1

    if dates != expected_dates:
        raise ValueError(f"{path.name} 沒有完整涵蓋 11/2～11/6")
    print(f"{market} CSV：{count:,} 筆，五天日期檢查通過")

connection_string = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    r"SERVER=localhost\SQLEXPRESS;"
    "DATABASE=TWStockDB;"
    "Trusted_Connection=yes;"
)

sql = """
INSERT INTO dbo.StockDailyPrice
(trade_date, stock_code, market,
 open_price, high_price, low_price, close_price, volume)
SELECT ?, ?, ?, ?, ?, ?, ?, ?
WHERE NOT EXISTS (
    SELECT 1 FROM dbo.StockDailyPrice WITH (UPDLOCK, HOLDLOCK)
    WHERE trade_date = ? AND stock_code = ? AND market = ?
)
"""

conn = pyodbc.connect(connection_string, timeout=10, autocommit=False)
try:
    cursor = conn.cursor()

    def count_market(market):
        return cursor.execute("""
            SELECT COUNT(*) FROM dbo.StockDailyPrice
            WHERE market = ? AND trade_date BETWEEN ? AND ?
        """, market, date(2026, 11, 2), date(2026, 11, 6)).fetchone()[0]

    before = {market: count_market(market) for market, _ in sources}
    cursor.executemany(sql, [row + row[:3] for row in rows])

    # 逐筆確認來源資料的鍵都已存在
    for row in rows:
        count = cursor.execute("""
            SELECT COUNT(*) FROM dbo.StockDailyPrice
            WHERE trade_date = ? AND stock_code = ? AND market = ?
        """, *row[:3]).fetchone()[0]
        if count != 1:
            raise RuntimeError(f"SQL 資料缺漏或重複：{row[:3]}")

    after = {market: count_market(market) for market, _ in sources}
    conn.commit()

    for market, _ in sources:
        print(
            f"{market}：期間原有 {before[market]:,} 筆，"
            f"新增 {after[market] - before[market]:,} 筆，"
            f"期間合計 {after[market]:,} 筆"
        )
    print("匯入完成，兩市場來源資料皆已逐筆確認存在。")
except Exception:
    conn.rollback()
    print("匯入失敗，本次寫入已回復。")
    raise
finally:
    conn.close()
