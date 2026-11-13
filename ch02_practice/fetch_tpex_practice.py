import argparse

import pandas as pd
import requests

parser = argparse.ArgumentParser()
parser.add_argument("--date", default="2026/11/06", help="西元日期 YYYY/MM/DD")
args = parser.parse_args()
trade_date = args.date

url = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"

response = requests.get(
    url,
    params={"date": trade_date, "id": "", "response": "json"},
    timeout=45,
)
response.raise_for_status()
payload = response.json()

date_key = trade_date.replace("/", "")
if payload.get("stat") != "ok" or payload.get("date") != date_key:
    raise SystemExit(f"櫃買中心未回傳 {trade_date} 的有效行情。")

table = next(t for t in payload["tables"] if t["title"] == "上櫃股票行情")
raw = pd.DataFrame(table["data"], columns=table["fields"])

columns = {
    "開盤": "open",
    "最高": "high",
    "最低": "low",
    "收盤": "close",
    "成交股數": "volume",
    "代號": "stock_code",
}
mask = raw["代號"].str.fullmatch(r"[1-9][0-9]{3}")
daily = raw.loc[mask, list(columns)].rename(columns=columns)
daily.insert(0, "date", table["date"])

for column in ("open", "high", "low", "close", "volume"):
    daily[column] = pd.to_numeric(
        daily[column].str.replace(",", "", regex=False), errors="coerce"
    )

output = f"TPEX_{date_key}.csv"
daily.to_csv(output, index=False, encoding="utf-8-sig")
print(f"已存檔：{output}，共 {len(daily):,} 筆，收盤價缺值 {daily['close'].isna().sum()} 筆")