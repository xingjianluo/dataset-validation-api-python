"""测试日志配置：把各用例打印的「输入 + 判定依据」同时写入本地日志文件。"""

from __future__ import annotations

import logging
import os

LOG_PATH = os.path.join(os.path.dirname(__file__), "test-run.log")

_file_handler = logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
)

# 覆盖应用与全部测试模块的日志器。
for name in ("app.validation", "tests"):
    target = logging.getLogger(name)
    target.setLevel(logging.INFO)
    target.addHandler(_file_handler)
