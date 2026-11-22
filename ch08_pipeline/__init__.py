"""第 8 章每日資料更新管線（簡化可執行版）。

這是為本書練習倉寫的接線程式，用來把第 2 到第 5 章已有的步驟按順序跑起來。
沒有複製其他量化框架的程式。盤中交易不在這裡。
"""

from ch08_pipeline.guard import pipeline_ready
from ch08_pipeline.runner import run_pipeline

__all__ = ["pipeline_ready", "run_pipeline"]
