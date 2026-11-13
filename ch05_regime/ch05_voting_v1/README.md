# 第五章練習：11 位分析師淨投票 v1

本版實作資料取得 → 個別投票 → 多空差額 → 四種市場狀態。
沒有連續評分、沒有修改第四章程式或 SQL、沒有自動交易。
以提供的書籍第 70–73 頁為依據；以下未明示的門檻是教學用預設，不是書中原始公式，也尚未證明預測能力。

## 在 Windows 執行

將此資料夾放入 `C:\Users\test\Documents\GitHub\tw-stock-book-samples\ch05_regime` 下。
Python 3.10 以上，僅用標準函式庫，不需 pip 安裝。

```powershell
cd C:\Users\test\Documents\GitHub\tw-stock-book-samples
python .\ch05_regime\ch05_voting_v1\market_regime_voting.py
```

第一次下載約 120 個日曆日的市場廣度，會逐日查詢並延遲 1 秒，可能花數分鐘。
從最近日期往回查詢；連續3次失敗會停止，若不足60筆便標示缺資料。
成功回應會快取於本資料夾 `cache`。其他資料快取 6 小時；若要強制重抓請刪除 cache。
按 Ctrl+C 可中止，成功下載的快取仍保留。API 可能限流、改格式或無法連線，錯誤會列入報告。

預設截至「台北昨天」，避免混入今天未收盤資料，也能包含昨天美股收盤。
這是日終觀察，不是台股當天開盤前可取得資料的回測。
指定日期例如：

```powershell
python .\ch05_regime\ch05_voting_v1\market_regime_voting.py --asof 2026-11-12
```

輸出 `output/regime_YYYY-MM-DD.json` 與同名 CSV。JSON 包含各來源最新日期、票數、證據、設定與錯誤。
本版逐日查歷史以建指標，但不是歷史回測引擎；資料可能修訂，未建公告時間／版本資料庫。
期交所 OpenAPI 提供的歷史長度有限；較早日期可能必須自行提供 CSV。

## 淨投票規則：沒有條件重疊

`net = bull_votes - bear_votes`

| 淨票數 | 分類 |
|---|---|
| ≥ +3 | BULL |
| −1 ～ +2 | NEUTRAL |
| −2 | RISK_OFF |
| ≤ −3 | BEAR |

平手自然落在 NEUTRAL；例如多 4 空 3 → +1 → NEUTRAL，多 5 空 2 → +3 → BULL。
這是依使用者對「差距數」的解讀訂定的本版規格，不宣稱完全重現書中未提供的原始程式。
RISK_OFF 是偏空前的保守區，故差額 +2 與 −2 不對稱。
各分析師可能投不只一票，尤其加權指數可投多 2 票或空 3 票；不是每人只投一票。

## 書中規則與本版預設

| 分析師 | 本版規則 | 依據／假設 |
|---|---|---|
| 1 加權指數 | close > MA60 多1；MA20 > MA60 多1；close < MA20 空1；close < MA60×0.95 空2 | 書中門檻；四條獨立累加；「跌破」採低於價位，不要求當日穿越；不用舊範例額外的動能條件 |
| 2 VIX | <16 多1；20≤VIX≤28 空1；>28 空2；其他0 | 書中門檻；16～20不投票，20、28包含在中間區間 |
| 3 Put/Call | 成交量比 ≥1.20 多1；≤0.70 空1；其他0 | 書中只給方向；1.20／0.70為預設，資料98.17%換成0.9817，CSV也要用小數比值；不混用未平倉比 |
| 4 銅金比 | HG/GC 相對自己的60日均值 >+2% 多1；<−2% 空1 | 預設；用期貨連續近月代理，不比較不同單位的原始絕對值；轉倉可能影響訊號 |
| 5 殖利率曲線 | 美國10年−2年利差 >0 多1；<0 空1；=0不投 | 書中給方向；10年/2年是本版預設 |
| 6 VIX期限結構 | VIX3M/VIX >1.02 多1；<0.98 空1 | 預設代理：3個月與1個月隱含波動率；不是VIX期貨近遠月曲線；±2%為緩衝 |
| 7 市場廣度 | ratio-adjusted McClellan >0 多1；<0 空1 | 書中方向；公式預設為RANA=1000×(上漲−下跌)/(上漲+下跌)，EMA19−EMA39；上市股票，排除上櫃 |
| 8 S&P 500 | 收盤相對MA20 >+1% 多1；<−1% 空1 | 預設 |
| 9 費城半導體 | 同 S&P 500 | 預設 |
| 10 台灣ETF | EWT美國正常交易時段收盤日報酬 >+0.5% 多1；<−0.5% 空1 | 預設；是台灣夜間的美國交易時段，不是美股盤後報價；未調整收盤價，除息可能干擾 |
| 11 美元指數 | 收盤相對MA20 >+1% 空1；<−1% 多1 | 書中給方向；門檻為預設 |

除表中明確的 ≥／≤ 外，門檻相等不投票。所有可調參數在 `rules.json`。
McClellan 至少60筆、EMA由首筆值初始化，預設抓120日曆日暖機，和長期完整歷史可能有微小差異。
書中市場廣度也提到指數與多數股票背離；本版採 McClellan 的正負號投票，沒有額外疊加背離票。

## 資料來源

| 本地名稱 | 自動來源 |
|---|---|
| taiex | Yahoo Finance `^TWII`（可由既有加權指數CSV覆蓋） |
| vix / vix3m | Yahoo `^VIX` / `^VIX3M` |
| pcr | 期交所 OpenAPI `PutCallRatio`，`PutCallVolumeRatio%` |
| copper / gold | Yahoo `HG=F` / `GC=F` |
| yield_spread | FRED `T10Y2Y` |
| breadth | 證交所 MI_INDEX 大盤統計「漲跌證券數合計」的「股票」欄 |
| sp500 / sox / ewt / dxy | Yahoo `^GSPC` / `^SOX` / `EWT` / `DX-Y.NYB` |

參考：
- https://www.twse.com.tw/zh/trading/historical/mi-index.html
- https://openapi.taifex.com.tw/ （官方API規格）
- https://openapi.taifex.com.tw/v1/PutCallRatio
- https://fred.stlouisfed.org/series/T10Y2Y
- https://www.cboe.com/tradable_products/vix/ （VIX相關產品說明）
- https://finance.yahoo.com/ （價格使用公開chart端點，無服務保證）

原始範例只實作均線與動能的三分類；本版另建獨立檔案保留原始教材。

## 既有加權指數如何沿用？

不用重建SQL表。將現有指數資料匯出為 `input/taiex.csv`，程式會優先讀它，不再抓該來源。
至少60個交易日，由舊到新排列（程式也會排序）。

```csv
date,close
2026-11-11,100
2026-11-12,101
```

上面兩列是虛構數字，只是欄位示例，不足以算60日均線。務必提供你現有的完整歷史。
其他來源也能用同名CSV覆蓋，自動抓取與本地CSV可混搭：
- 價格類：`date,close` 或 `date,value`
- `pcr.csv`：`date,value`（例如0.9817，非98.17）
- `yield_spread.csv`：`date,value`（百分點，可為負）
- `breadth.csv`：`date,advances,declines`（至少60筆）

`--input-dir` 可改來源資料夾；`--offline` 僅讀CSV、不連網。
本版不直接連你的SQL，因為附件沒有目前的連線與資料表規格；使用CSV介面避免猜表名或改動資料庫。

## 缺資料處理

缺檔、欄位錯誤、歷史不足或最新觀察超過7個日曆日，該分析師標示MISSING。
其他分析師照算，輸出可供排錯的 `provisional_regime`；正式 `regime` 為 INCOMPLETE。
這避免少幾個分析師卻被當作完整11人判斷。0票與缺資料會分開呈現。
比值以共同日期對齊；不同市場假日可能使各來源日期略異，JSON列出日期供核對。

## 驗證

```powershell
python .\ch05_regime\ch05_voting_v1\test_voting.py
```

包含淨票邊界、重疊案例、全部訊號、缺資料、過期資料、日期篩選、百分比解析與市場廣度欄位解析。
實際連線測試與本次結果見 `VALIDATION.md`。訊號彼此相關（例如VIX與VIX期限代理），票數不能視為獨立機率。
