"""Entry point: python run.py  (serves API + demo UI on :8000)."""
import uvicorn

from app.main import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
