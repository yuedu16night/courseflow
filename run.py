"""Entry point: python run.py  (serves API + demo UI on :8000)."""
import uvicorn

from app.main import create_app, load_local_env

load_local_env()        # 允许用项目根目录的 .env.local 配置 LLM 通道
app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
