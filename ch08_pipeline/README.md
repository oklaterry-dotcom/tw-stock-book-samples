# 第 8 章：每日資料更新管線（簡化版）

這份程式把書上第 8 章「每日資料更新」收成可以在你電腦上試跑的五層管線。它只負責 **15:00 那一次的資料更新**，不包含盤中下單。

預設是乾跑：不連線、不寫 CSV、不寫資料庫。看完輸出，確認沒有「無法套用」，再加 `--apply`。

你的資料目前到 **2026-11-20**。第一次請明確指定這個日期。

```powershell
cd C:\Users\test\Documents\GitHub\tw-stock-book-samples
.\.venv\Scripts\python.exe .\ch08_pipeline\run_daily.py --asof 2026-11-20
```

乾跑結束後，若要真的跑：

```powershell
.\.venv\Scripts\python.exe .\ch08_pipeline\run_daily.py --asof 2026-11-20 --apply
```

`--apply` 會做這些事：股價回補（本機的 `backfill_twstockdb.py`）、必要時追加市場廣度與殖利率利差、有完整第四章資料才算因子、然後跑第五章投票與連續評分。沒有現成程式的步驟會標成「略過」，不會填假資料。

只想先更新第二層（廣度與利差），不動股價、也不跑投票：

```powershell
.\.venv\Scripts\python.exe .\ch08_pipeline\run_daily.py --asof 2026-11-20 --layer 2
.\.venv\Scripts\python.exe .\ch08_pipeline\run_daily.py --asof 2026-11-20 --layer 2 --apply
```

只跑一個步驟：

```powershell
.\.venv\Scripts\python.exe .\ch08_pipeline\run_daily.py --asof 2026-11-20 --step market_breadth
.\.venv\Scripts\python.exe .\ch08_pipeline\run_daily.py --asof 2026-11-20 --step derived_yield_spread --apply
```

看步驟清單與本機檔案：

```powershell
.\.venv\Scripts\python.exe .\ch08_pipeline\run_daily.py --list
```

不指定 `--asof` 時，日期是台北的昨天，而且必須早於今天，避免寫進還沒收盤的資料。

## 五層對照

第三層用四個 worker 同時跑。目前這一層都還沒有可追加的程式，所以會很快結束，結構仍留著。

| 層 | 步驟 | 這次 |
|---|---|---|
| 1 | 匯入股價 `import_prices` | 實作。呼叫你電腦上的 `ch02_practice/backfill_twstockdb.py` |
| 1 | 匯入外部市場、匯入大盤指數 | 略過。沒有獨立入庫程式，不寫假行情 |
| 2 | KD、MACD、RSI、布林、K 線、ATR、ADX、銅金比 | 略過。教學程式沒有接成可每日追加的寫入 |
| 2 | 殖利率曲線 `derived_yield_spread` | 實作。美國財政部公債殖利率曲線，10 年期減 2 年期 |
| 2 | 市場廣度 `market_breadth` | 實作。證交所 MI_INDEX 的上市漲跌家數 |
| 3 | 期貨選擇權、融資融券、借券當沖、注意股、三大法人、新聞、PTT、產業／概念動量、GNN | 略過。PTT 範例失敗時會改用假標題，所以這裡不呼叫 |
| 4 | 第四章因子 `factors_ch04` | 有完整 `ch04_data` 才跑 `calculate_ch04_factors.py`。輸出目錄已存在就略過 |
| 4 | 另一套總分 | 略過。能算的總分就是上一支程式的 `total_score` |
| 5 | Regime 投票、連續評分 | 實作。投票用倉庫裡的腳本；連續評分用你電腦上的腳本 |
| 5 | ML、寫入 DailyFactorScore | 略過。第六章仍是假資料示範，不寫假的 MLScore |

查詢 `dbo.ch04_factors` 時要自己帶最新的 `version_id`。這條管線**不**呼叫 `import_ch04_sql.py`，因為那支程式會更新已經存在的列。

## 本機檔案（不在 GitHub）

管線按下面的相對路徑找檔，不會把這些檔複製進倉庫。

| 路徑 | 管線會不會用 |
|---|---|
| `ch02_practice/backfill_twstockdb.py` | 會。沒有它，匯入股價會失敗 |
| `ch05_regime/ch05_voting_v1/market_regime_continuous.py` | 會 |
| `ch05_regime/ch05_voting_v1/continuous_rules.json` | 會。連續評分的設定 |
| `ch05_regime/ch05_voting_v1/input/breadth.csv` | 會。只追加新日期；沒有這個檔就停止，不會從零編 60 日 |
| `ch05_regime/ch05_voting_v1/taiex_continuous.py` | 不會。要單獨看加權指數分數時再自己執行 |
| `ch05_regime/ch05_voting_v1/test_continuous.py`、`README_CONTINUOUS.md` | 不會 |

`input/yield_spread.csv` 可以還沒有。第一次 `--apply` 會用財政部資料新建。若檔案已經在，而且同一天的數字和財政部不同（例如以前從 FRED 存下來的），步驟會停下來且不改檔。請自行把舊檔改名或移走，程式不會刪它。

市場廣度不要用 `StockDailyPrice` 回推。那張表目前大約只有 15 個交易日，不夠 McClellan 的 60 日。

## 資料安全

- 預設乾跑，什麼都不寫。
- 只有 `--apply` 才寫檔、寫狀態表、呼叫會改資料的腳本。
- 廣度與利差只在檔尾追加新日期。舊日期的值不同就停止。
- 追加前會把整份舊檔複製到 `ch05_regime\ch05_voting_v1\input\backups\`。備份檔名帶時間，不會蓋過舊備份。
- 股價寫入仍由 `backfill_twstockdb.py` 負責，只插入 TWStockDB 裡還沒有的列。資料庫已經涵蓋這個 asof 時，視為成功，不會再灌一次。
- 因子 CSV 的目錄若已存在就略過，避免蓋掉算好的結果。
- 同一天的第五章報告若已存在，會先備份；腳本失敗時把報告還原。投票腳本自己還可能把快取寫進 `ch05_regime\ch05_voting_v1\cache\`（原本就已被 git 忽略）。
- 狀態表只插入新列。最新狀態是 `pipeline_steps` 裡名稱為 `__run_status__` 的最後一筆，不會回去改舊列。
- 用 `--layer` 或 `--step` 的執行記成「部分」。`pipeline_ready()` 只看完整的每日更新，避免只跑了一步卻被當成整天都好了。

狀態預設寫進 MiniQuantDemo 的 `dbo.pipeline_runs` 與 `dbo.pipeline_steps`（Windows 驗證、ODBC Driver 17，沒有 17 才用 18）。SQL Server 連不上時改寫 `ch08_pipeline\state\pipeline.sqlite`。也可以自己指定：

```powershell
$env:MINIQUANT_SQL_SERVER = "localhost\SQLEXPRESS"
$env:MINIQUANT_SQL_DATABASE = "MiniQuantDemo"
.\.venv\Scripts\python.exe .\ch08_pipeline\run_daily.py --asof 2026-11-20 --apply --status-backend sqlite --status-path .\ch08_pipeline\state\pipeline.sqlite
```

## pipeline_ready()

書上的規則是：狀態為執行中就每 10 秒看一次，最多等 2 分鐘；還在跑就放棄這一輪。完成就可以用。失敗則沿用上次成功的資料。超過 2 小時仍是執行中，視為失敗。

只查狀態、不寫入：

```powershell
.\.venv\Scripts\python.exe .\ch08_pipeline\run_daily.py --ready
```

結束代碼 0 代表可以繼續；2 代表放棄這一輪。就算加上 `--apply`，`--ready` 仍然不寫狀態。超過 2 小時的執行中紀錄，要等下一次真正的 `--apply` 才會補上一筆失敗事件。

## 在這裡測試

不需要 SQL Server，也不需要網路：

```powershell
.\.venv\Scripts\python.exe -m unittest ch08_pipeline.test_pipeline
```

## 以後若要排程

這支程式**不會**建立 Windows 工作排程器的工作。請先把上面的乾跑和 `--apply` 都跑通，再自己加。下面只是日後要設「每個工作日 15:00」時的範例。若已經有同名工作，請先在工作排程器裡看過，不要直接蓋掉。

```powershell
schtasks /Create /TN "TwStockDailyUpdate" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 15:00 /TR "C:\Users\test\Documents\GitHub\tw-stock-book-samples\.venv\Scripts\python.exe C:\Users\test\Documents\GitHub\tw-stock-book-samples\ch08_pipeline\run_daily.py --apply"
```

排程沒有人看畫面時，結果在狀態表裡。盤中那四步驟（報價、退場、進場、下單通知）不在這次的範圍。

## 套件

沒有新增 pip 套件。沿用倉庫裡的 `requirements.txt`（標準函式庫加上既有的 pandas、pyodbc 等）。狀態庫的 SQLite 與 JSON 都只用標準函式庫；SQL Server 才需要已經列在需求裡的 pyodbc。
