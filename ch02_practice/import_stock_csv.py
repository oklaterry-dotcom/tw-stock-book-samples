import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
import pyodbc

csv_path = Path("TPEX_202611.csv")

def convert_date(value):
    year, month, day = map(int, value.strip().split("/"))
    return date(year + 1911, month, day)

def convert_price(value):
    value = (value or "").strip().replace(",", "")
    if value in ("", "--", "---"):
        return None
    return Decimal(value)

def convert_volume(value):
    value = (value or "").strip().replace(",", "")
    return int(value) if value else None

connection_string = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    r"SERVER=localhost\SQLEXPRESS;"
    "DATABASE=TWStockDB;"
    "Trusted_Connection=yes;"
)

rows = []

with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
    reader = csv.DictReader(f)

    for row in reader:
        rows.append((
            convert_date(row["date"]),
            row["stock_code"].strip(),
            "TPEX",
            convert_price(row["open"]),
            convert_price(row["high"]),
            convert_price(row["low"]),
            convert_price(row["close"]),
            convert_volume(row["volume"])
        ))

print(f"CSV rows: {len(rows)}")

with pyodbc.connect(connection_string, timeout=10) as conn:
    cursor = conn.cursor()

    cursor.executemany("""
        INSERT INTO dbo.StockDailyPrice
        (trade_date, stock_code, market,
         open_price, high_price, low_price, close_price, volume)
        SELECT ?, ?, ?, ?, ?, ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1
            FROM dbo.StockDailyPrice
            WHERE trade_date = ?
              AND stock_code = ?
              AND market = ?
        )
    """, [
        row + row[:3]
        for row in rows
    ])

    conn.commit()

    cursor.execute("""
        SELECT COUNT(*)
        FROM dbo.StockDailyPrice
        WHERE market = 'TPEX'
    """)

    print(f"TPEX rows in SQL Server: {cursor.fetchone()[0]}")
    print("Import completed.")
