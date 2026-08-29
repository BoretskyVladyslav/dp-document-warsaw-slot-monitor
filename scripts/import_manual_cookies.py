from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.config import get_settings
from src.services.scraper import has_cf_clearance_cookie, resolve_repo_path
from src.services.storage_state import (
    build_storage_state,
    cookie_domain_from_url,
    parse_cookie_pairs,
    write_storage_state_file,
)

CF_PROMPT = "Paste cf_clearance cookie value (or cf_clearance=<value>): "
EXTRA_PROMPT = (
    "Paste optional extra cookies (name=value; ... or one per line). "
    "Press Enter to skip: "
)


async def _read_line(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def main() -> int:
    settings = get_settings()
    storage_path = resolve_repo_path(settings.storage_state_path)
    domain = cookie_domain_from_url(str(settings.target_url))
    try:
        cf_clearance = (await _read_line(CF_PROMPT)).strip()
        extra_raw = (await _read_line(EXTRA_PROMPT)).strip()
    except EOFError:
        print("cancelled: stdin closed", flush=True)
        return 130
    except KeyboardInterrupt:
        print("cancelled", flush=True)
        return 130

    try:
        extra_pairs = parse_cookie_pairs(extra_raw) if extra_raw else []
        payload = build_storage_state(
            cf_clearance=cf_clearance,
            extra_pairs=extra_pairs,
            domain=domain,
        )
    except ValueError as exc:
        print(f"import_failed: {exc}", flush=True)
        return 1

    await asyncio.to_thread(write_storage_state_file, storage_path, payload)
    cookies = payload["cookies"]
    names = [str(item["name"]) for item in cookies]
    print(f"storage_state={storage_path}", flush=True)
    print(f"domain={domain}", flush=True)
    print(f"cookie_names={','.join(names)}", flush=True)
    print(f"cookie_count={len(cookies)}", flush=True)
    print(f"cf_clearance={has_cf_clearance_cookie(cookies)}", flush=True)
    print(f"storage_saved={storage_path.is_file()}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
