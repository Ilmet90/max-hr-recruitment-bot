from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from app.admin_web import app
from app.db import init_db


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def main() -> None:
    init_db()
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
