"""Windows 上直接執行的入口。用法見同資料夾 README.md。

預設是乾跑，不會寫檔，也不會寫資料庫。確認輸出後才加 --apply。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ch08_pipeline.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
