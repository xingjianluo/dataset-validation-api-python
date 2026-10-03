"""每个测试用例使用全新的 TaskManager，避免并发槽位相互影响。"""

from __future__ import annotations

import pytest

from app import api as api_module
from app.manager import TaskManager


@pytest.fixture(autouse=True)
def fresh_manager():
    api_module.manager = TaskManager(max_concurrency=api_module.MAX_CONCURRENCY)
    yield
    api_module.manager = TaskManager(max_concurrency=api_module.MAX_CONCURRENCY)
