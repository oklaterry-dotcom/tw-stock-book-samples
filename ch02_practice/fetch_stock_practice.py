"""
抓取證交所單一證券的每日成交資料，清理後存成 CSV。

輸入：--stock 證券代號，預設 2330。查詢月份固定為 2026 年 6 月。
輸出：目前資料夾的 <證券代號>_202606.csv，含日期、開高低收、成交量與代號。
限制：抓取時需要網路；日期保留民國格式；無法轉成數字的行情值會留空。
"""

import argparse

from html import parser
import time
import requests
import pandas as pd

# TWSE「個股日成交資訊」公開 API
TWSE_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"


def fetch_one_month(stock_code: str, year_month: str) -> pd.DataFrame:
    """抓取單一股票、單一月份的日成交資料。

    參數：
        stock_code: 股票代號，例如 "2330"
        year_month: 西元年月，格式 "YYYYMM01"，例如 "20260601"
    回傳：
        整理後的 DataFrame，欄位為日期、開、高、低、收、量
    """
    params = {"response": "json", "date": year_month, "stockNo": stock_code}

    # 加上 User-Agent，避免被當成可疑流量擋掉
    headers = {"User-Agent": "Mozilla/5.0 (book-sample/1.0)"}
    resp = requests.get(TWSE_URL, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("stat") != "OK":
        # API 沒有資料時（例如未開市的月份）會回傳非 OK 狀態
        raise RuntimeError(f"TWSE 回傳非 OK：{payload.get('stat')}")

    # data 是一個二維陣列，每一列是一天的資料
    # 欄位順序：日期, 成交股數, 成交金額, 開盤, 最高, 最低, 收盤, 漲跌價差, 成交筆數
    rows = payload["data"]
    df = pd.DataFrame(rows, columns=payload["fields"])
    if df.empty:
        raise SystemExit(f"{stock_code} 在 {year_month[:6]} 沒有日成交資料，未產生 CSV。")
    
    print("證交所原始成交股數（最後一筆）", repr(df["成交股數"].iloc[-1]))

    return clean_daily_rows(df, stock_code)


def clean_daily_rows(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    """將證交所原始資料轉成 CSV 所需的欄位與型別。"""
    
    # 清理：把帶有逗號的數字字串轉成數值
    def to_number(series: pd.Series) -> pd.Series:
        return pd.to_numeric(series.str.replace(",", "", regex=False), errors="coerce")

    clean = pd.DataFrame(
        {
            "date": df["日期"],  # 民國年格式，這裡保留原樣示範
            "open": to_number(df["開盤價"]),
            "high": to_number(df["最高價"]),
            "low": to_number(df["最低價"]),
            "close": to_number(df["收盤價"]),
            "volume": to_number(df["成交股數"]),
        }
    )
    clean["stock_code"] = stock_code
    return clean


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stock", default="2330")
    args = parser.parse_args()
    stock_code = args.stock #tewu note換成參數

    year_month = "20260601"  # 2026 年 6 月

    print(f"抓取 {stock_code} 在 {year_month[:6]} 的日成交資料……")
    df = fetch_one_month(stock_code, year_month)

    # 禮貌性延遲：避免短時間內大量請求造成對方伺服器負擔
    time.sleep(3)

    print(f"共取得 {len(df)} 筆資料\n")
    print(df.tail(10).to_string(index=False))

    # 存成 CSV，之後就能匯入資料庫或用來算技術指標
    out_path = f"{stock_code}_{year_month[:6]}.csv"
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\n已存檔：{out_path}")


if __name__ == "__main__":
    main()
