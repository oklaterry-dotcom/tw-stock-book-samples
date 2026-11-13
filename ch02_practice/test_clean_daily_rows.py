import pandas as pd
from fetch_stock_practice import clean_daily_rows

raw = pd.DataFrame(
    [
        ["115/06/01", "105.25", "106.70", "104.50", "105.50", "135,817,122"],
        ["115/06/02", "106.00", "106.50", "105.00", "106.20", "0"],
        ["115/06/03", "106.20", "107.00", "105.50", "106.80", ""],
    ],
    columns=["日期", "開盤價", "最高價", "最低價", "收盤價", "成交股數"],
)

clean = clean_daily_rows(raw, "0050")
print(clean[["date", "volume", "stock_code"]].to_string(index=False))