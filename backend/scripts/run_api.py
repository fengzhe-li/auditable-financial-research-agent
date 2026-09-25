"""Serves afra.api.app for local development / demo use.

    python3 -m pip install -e ".[api]"
    python3 scripts/run_api.py                 # http://127.0.0.1:8000
    AFRA_DB_PATH=./afra_demo.db python3 scripts/seed_demo_data.py   # populate it first

Host/port default to 127.0.0.1:8000 (safe for local dev - not exposed
outside the machine). AFRA_API_HOST/AFRA_API_PORT override both -
docker/backend-entrypoint.sh sets AFRA_API_HOST=0.0.0.0 so the API is
reachable from other containers/the host's published port; nothing else
about this module changes between local and containerized use.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn


def main() -> None:
    host = os.environ.get("AFRA_API_HOST", "127.0.0.1")
    port = int(os.environ.get("AFRA_API_PORT", "8000"))
    uvicorn.run("afra.api.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
