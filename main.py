"""服务入口：uvicorn 通过 ``main:app`` 加载应用。"""

from app.api import app

__all__ = ["app"]
