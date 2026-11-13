from pathlib import Path

import pandas as pd
import pyodbc

# 沿用書中的判斷邏輯，不執行原檔的假資料 main()
from detect_kline import scan

connection_string = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    r"SERVER=localhost\SQLEXPRESS;"
    "DATABASE=TWStockDB;"
    "Trusted_Connection=yes;"
)

query = """
SELECT
    trade_date AS date,
    stock_code,
    market,
    open_price AS [open],
    high_price AS high,
    low_price AS low,
    close_price AS [close],
    volume
FROM dbo.StockDailyPrice
WHERE trade_date BETWEEN ? AND ?
  AND market IN ('TWSE', 'TPEX')
ORDER BY market, stock_code, trade_date
"""

# 新增功能：從 SQL 取出資料
with pyodbc.connect(connection_string, timeout=10) as conn:
    cursor = conn.cursor()
    cursor.execute(query, "20261102", "20261106")
    columns = [column[0] for column in cursor.description]
    df = pd.DataFrame.from_records(
        [tuple(row) for row in cursor.fetchall()],
        columns=columns,
    )

if df.empty:
    raise RuntimeError("SQL 查無指定期間資料")

if df.duplicated(["market", "stock_code", "date"]).any():
    raise RuntimeError("SQL 有重複日期與股票，請先檢查")

prices = ["open", "high", "low", "close"]
for column in prices:
    df[column] = pd.to_numeric(df[column], errors="raise")

# 缺值或不合理價格不做判斷，並保留該列避免跨過它比較
valid = (
    df[prices].notna().all(axis=1)
    & df[prices].gt(0).all(axis=1)
    & (df["high"] >= df[["open", "close"]].max(axis=1))
    & (df["low"] <= df[["open", "close"]].min(axis=1))
    & (df["high"] >= df["low"])
)
df["_valid"] = valid

# 新增功能：每個市場、每支股票分開掃描
results = []
for _, group in df.groupby(["market", "stock_code"], sort=False):
    group = group.sort_values("date").reset_index(drop=True)
    working = group.copy()
    working.loc[~working["_valid"], prices] = float("nan")

    # 書中原本的掃描函式
    scanned = scan(working)

    group["pattern"] = scanned["pattern"]
    group.loc[~group["_valid"], "pattern"] = "資料不足或價格異常"
    results.append(group)

result = pd.concat(results, ignore_index=True)
hits = result[
    result["_valid"] & result["pattern"].ne("—")
].copy()

folder = Path(__file__).resolve().parent
result.drop(columns="_valid").to_csv(
    folder / "kline_sql_all.csv", index=False, encoding="utf-8-sig"
)
hits.drop(columns="_valid").to_csv(
    folder / "kline_sql_hits.csv", index=False, encoding="utf-8-sig"
)

print(f"SQL 讀取：{len(result):,} 筆")
print(f"股票數（依市場區分）：{df.groupby(['market', 'stock_code']).ngroups:,}")
print(f"資料不足或價格異常：{(~result['_valid']).sum():,} 筆")
print(f"命中至少一種型態：{len(hits):,} 筆")

if not hits.empty:
    print("\n各市場型態統計（同一天可能命中多種）：")
    counts = (
        hits.assign(pattern=hits["pattern"].str.split("、"))
        .explode("pattern")
        .groupby(["market", "pattern"])
        .size()
        .rename("count")
    )
    print(counts.to_string())
    print("\n前 20 筆命中資料：")
    print(hits.drop(columns="_valid").head(20).to_string(index=False))

print(f"\n全部結果：{folder / 'kline_sql_all.csv'}")
print(f"命中結果：{folder / 'kline_sql_hits.csv'}")
