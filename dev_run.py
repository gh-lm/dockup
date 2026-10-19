"""Development entry point only: mock engine, local folders, throwaway key.
The real thing runs in Docker via run.py + gunicorn."""

import os
from pathlib import Path

HERE = Path(__file__).parent
os.environ.setdefault("SECRET_KEY", "dev-secret-key-for-testing-0123456789abcdef")
os.environ.setdefault("DOCKUP_MOCK", "1")
os.environ.setdefault("DOCKUP_DATA", str(HERE / "data"))
os.environ.setdefault("DOCKUP_STACKS", str(HERE / "dev-stacks"))

from app import create_app  # noqa: E402  (env must be set before app import)

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)
