from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_CF_CLEARANCE = "cf_clearance"


def cookie_domain_from_url(url: str) -> str:
    host = urlparse(url).hostname
    if not host:
        raise ValueError("target URL has no hostname")
    return host


def playwright_cookie(*, name: str, value: str, domain: str) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "httpOnly": True,
        "secure": True,
        "sameSite": "None",
    }


def parse_cookie_pairs(raw: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    normalized = raw.replace(";", "\n")
    for line in normalized.splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if "=" not in item:
            raise ValueError("invalid cookie line: missing '='")
        name, value = item.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if not name:
            raise ValueError("invalid cookie line: empty name")
        if not value:
            raise ValueError(f"invalid cookie line: empty value for {name}")
        pairs.append((name, value))
    return pairs


def build_storage_state(
    *,
    cf_clearance: str,
    extra_pairs: list[tuple[str, str]],
    domain: str,
) -> dict[str, Any]:
    clearance = cf_clearance.strip()
    if not clearance:
        raise ValueError("cf_clearance is required")
    if f"{_CF_CLEARANCE}=" in clearance:
        pairs = parse_cookie_pairs(clearance)
        found = next((value for name, value in pairs if name == _CF_CLEARANCE), "")
        extras = [(name, value) for name, value in pairs if name != _CF_CLEARANCE]
        if not found:
            raise ValueError("cf_clearance is required")
        clearance = found
        extra_pairs = extras + extra_pairs

    cookies = [playwright_cookie(name=_CF_CLEARANCE, value=clearance, domain=domain)]
    seen = {_CF_CLEARANCE}
    for name, value in extra_pairs:
        if name in seen:
            continue
        seen.add(name)
        cookies.append(playwright_cookie(name=name, value=value, domain=domain))
    return {"cookies": cookies, "origins": []}


def is_usable_storage_state(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning(
            "storage_state_unreadable",
            extra={"path": str(path), "error": str(exc)},
        )
        return False
    if not isinstance(data, dict):
        logger.warning("storage_state_invalid", extra={"path": str(path), "reason": "not_object"})
        return False
    cookies = data.get("cookies")
    if not isinstance(cookies, list):
        logger.warning("storage_state_invalid", extra={"path": str(path), "reason": "cookies_not_list"})
        return False
    for item in cookies:
        if not isinstance(item, dict) or not item.get("name") or "value" not in item:
            logger.warning(
                "storage_state_invalid",
                extra={"path": str(path), "reason": "cookie_missing_name_or_value"},
            )
            return False
    return True


def write_storage_state_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(encoded, encoding="utf-8")
    tmp.replace(path)
