from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent
for _path in (str(_SCRIPTS), str(_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from solve_session import main


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
