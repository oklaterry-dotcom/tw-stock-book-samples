"""第 8 章圖 8-2 的五層步驟清單。

第三層彼此獨立，執行時用四個 worker。沒有現成程式的步驟是 stub，
只回報略過，不產生任何假資料。
"""

from __future__ import annotations

from ch08_pipeline.models import LocalFile, StepSpec

PIPELINE_NAME = "daily_update"
PARALLEL_LAYER = 3
PARALLEL_WORKERS = 4

LAYER_TITLES = {
    1: "第一層 基礎資料（串行）",
    2: "第二層 技術指標與市場序列（串行）",
    3: "第三層 外部數據（四個 worker 並行）",
    4: "第四層 因子（串行）",
    5: "第五層 Regime 與 ML（串行）",
}

LOCAL_FILES: tuple[LocalFile, ...] = (
    LocalFile(
        "ch02_practice/backfill_twstockdb.py",
        True,
        "第一層匯入股價。此檔在你的電腦上，尚未進 Git。",
    ),
    LocalFile(
        "ch05_regime/ch05_voting_v1/market_regime_continuous.py",
        True,
        "第五層連續評分。管線只在 --apply 時呼叫。",
    ),
    LocalFile(
        "ch05_regime/ch05_voting_v1/continuous_rules.json",
        True,
        "連續評分的設定，由上一支程式預設讀取。",
    ),
    LocalFile(
        "ch05_regime/ch05_voting_v1/input/breadth.csv",
        True,
        "市場廣度的起點。管線只追加新日期，沒有這個檔就停止。",
    ),
    LocalFile(
        "ch05_regime/ch05_voting_v1/taiex_continuous.py",
        False,
        "只算加權指數的小工具。管線不呼叫；連續評分主程式已含加權指數。",
    ),
    LocalFile(
        "ch05_regime/ch05_voting_v1/test_continuous.py",
        False,
        "連續評分的單元測試。管線不呼叫。",
    ),
    LocalFile(
        "ch05_regime/ch05_voting_v1/README_CONTINUOUS.md",
        False,
        "連續評分說明。管線不呼叫。",
    ),
)

STEPS: tuple[StepSpec, ...] = (
    StepSpec(
        "import_prices", 1, "匯入股價", "real",
        "呼叫本機 backfill_twstockdb.py，把上市與上櫃日成交追加進 "
        "TWStockDB.dbo.StockDailyPrice。該腳本只插入尚不存在的列。",
    ),
    StepSpec(
        "import_external_markets", 1, "匯入外部市場", "stub",
        "本倉庫沒有獨立的美股、美元指數、VIX 或原油入庫程式。"
        "這些序列由第五章投票讀 input CSV，缺檔才連線。這裡不另抓，也不寫假行情。",
    ),
    StepSpec(
        "import_taiex", 1, "匯入大盤指數", "stub",
        "本倉庫沒有把加權指數寫進資料庫的每日步驟。請沿用 input/taiex.csv；"
        "沒有檔時交給第五章投票腳本。這裡不產生假指數。",
    ),
    StepSpec(
        "indicators_basic", 2, "基礎技術指標", "stub",
        "教學用的 RSI 與均線沒有接成可每日追加的 KD、MACD、RSI、布林寫入。"
        "StockDailyPrice 目前大約只有 15 個交易日，不夠長週期指標。不算，也不填假值。",
    ),
    StepSpec(
        "indicators_advanced", 2, "進階技術指標", "stub",
        "K 線範例使用內建假資料，沒有 ATR、ADX 的每日寫入程式。略過，不把假型態寫進結果。",
    ),
    StepSpec(
        "derived_copper_gold", 2, "銅金比", "stub",
        "銅金比在第五章由 input/copper.csv 與 input/gold.csv 對齊後計算。"
        "這裡沒有可追加的獨立產出，也不用假價格代替。",
    ),
    StepSpec(
        "derived_yield_spread", 2, "殖利率曲線", "real",
        "從美國財政部每日公債殖利率曲線取 10 年期減 2 年期（百分點），"
        "追加到 input/yield_spread.csv。不使用 FRED。",
    ),
    StepSpec(
        "market_breadth", 2, "市場廣度", "real",
        "從證交所 MI_INDEX 的上市股票漲跌家數，追加 input/breadth.csv 還沒有的交易日。"
        "不用 StockDailyPrice 回推，因為歷史不夠 McClellan 的 60 日。",
    ),
    StepSpec(
        "futures_options", 3, "期貨與選擇權", "stub",
        "沒有期交所期貨與選擇權的每日入庫程式。Put/Call 由第五章投票直接讀。略過。",
    ),
    StepSpec(
        "margin_trading", 3, "融資融券", "stub",
        "沒有融資融券每日入庫程式。略過。",
    ),
    StepSpec(
        "securities_lending", 3, "借券與當沖", "stub",
        "沒有借券與當沖每日入庫程式。略過。",
    ),
    StepSpec(
        "attention_stocks", 3, "注意股", "stub",
        "沒有注意股每日入庫程式。略過。",
    ),
    StepSpec(
        "institutions", 3, "三大法人", "stub",
        "ch04_factor/fetch_ch04_data.py 是歷史批次下載，不是只追加一天的步驟。"
        "管線不自動呼叫，避免整批重抓。略過。",
    ),
    StepSpec(
        "news_market", 3, "大盤新聞情緒", "stub",
        "沒有鉅亨網或 Google News 的大盤情緒程式。略過，不填假情緒分數。",
    ),
    StepSpec(
        "news_stock", 3, "個股新聞情緒", "stub",
        "沒有個股新聞情緒程式。略過。",
    ),
    StepSpec(
        "ptt_sentiment", 3, "PTT 情緒", "stub",
        "ch09_ptt/ptt_sentiment.py 在抓取失敗時會改用內建範例標題。"
        "管線不呼叫它，避免假情緒進入結果。",
    ),
    StepSpec(
        "industry_momentum", 3, "產業動量", "stub",
        "沒有產業動量計算。略過。",
    ),
    StepSpec(
        "concept_momentum", 3, "概念動量", "stub",
        "沒有概念動量計算。略過。",
    ),
    StepSpec(
        "gnn_supply_chain", 3, "GNN 供應鏈", "stub",
        "沒有 GNN 供應鏈計算。略過。",
    ),
    StepSpec(
        "factors_ch04", 4, "計算第四章因子", "real",
        "若已有完整的 ch04_data，呼叫 calculate_ch04_factors.py 寫出因子 CSV"
        "（含 total_score）。輸出目錄已存在就略過，避免覆蓋。不呼叫 import_ch04_sql.py。",
    ),
    StepSpec(
        "factors_aggregate", 4, "聚合因子總分", "stub",
        "本倉庫能做的加總就是 calculate_ch04_factors.py 的 total_score。"
        "沒有另一套擴展因子總分，也不寫假的 TotalScore。",
    ),
    StepSpec(
        "regime_vote", 5, "Regime 投票", "real",
        "呼叫 market_regime_voting.py。同一天的舊報告會先備份；腳本失敗時把報告還原。",
    ),
    StepSpec(
        "regime_continuous", 5, "Regime 連續評分", "real",
        "呼叫本機 market_regime_continuous.py，只轉換剛產生的投票報告，不重新下載行情。",
    ),
    StepSpec(
        "ml_predict", 5, "ML 預測", "stub",
        "第六章目前用假資料示範隨機森林。管線不呼叫，避免寫入假的 MLScore。",
    ),
    StepSpec(
        "write_daily_factor_score", 5, "寫入 DailyFactorScore", "stub",
        "沒有只追加、不覆蓋的 DailyFactorScore 寫入程式。不建立含假分數的資料表。",
    ),
)

STATUS_ZH = {
    "PLANNED": "預計",
    "BLOCKED": "無法套用",
    "SKIPPED": "略過",
    "OK": "成功",
    "FAILED": "失敗",
    "DRY_RUN": "乾跑完成",
    "COMPLETED": "完成",
    "RUNNING": "執行中",
    "NO_RUN": "尚無紀錄",
}


def step_names() -> set[str]:
    return {step.name for step in STEPS}


def select_steps(layer: int | None = None, names: list[str] | None = None) -> list[StepSpec]:
    """依目錄順序篩步驟。層與名稱都沒指定時就是完整的每日更新。"""
    rows = list(STEPS)
    if names:
        unknown = [name for name in names if name not in step_names()]
        if unknown:
            raise KeyError("沒有這個步驟：" + "、".join(unknown))
        wanted = set(names)
        rows = [step for step in rows if step.name in wanted]
    if layer is not None:
        rows = [step for step in rows if step.layer == layer]
    if not rows:
        raise KeyError("沒有符合的步驟")
    return rows


def scope_for(layer: int | None, names: list[str] | None) -> str:
    """只有完全沒有篩選的 --apply 才算完整每日更新，pipeline_ready 會看它。"""
    if layer is None and not names:
        return "full"
    return "partial"
