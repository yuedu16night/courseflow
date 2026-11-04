"""Entry point: python run.py  (serves API + demo UI on :8000)."""
import os

import uvicorn

from app.main import create_app, load_local_env

load_local_env()        # 允许用项目根目录的 .env.local 配置 LLM 通道
# 默认服务 `demo.db` —— 与 `python demo/seed_demo.py` 造数写入的是同一个库，
# 这样「先造数、再启动」就能直接在界面上看到课程与学生；可用 COURSEFLOW_DB 覆盖。
app = create_app(os.environ.get("COURSEFLOW_DB", "demo.db"))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
