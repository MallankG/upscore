"""Upscore: personal credit-coach agent. Loads ./.env (KEY=value lines) before modules read config."""
import os
from pathlib import Path

_env = Path(os.environ.get("UPSCORE_ENV", Path(__file__).resolve().parent.parent / ".env"))
if _env.is_file():
    for line in _env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
