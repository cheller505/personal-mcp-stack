"""Granola Enterprise API key storage and validation."""

import json
import logging
import sys
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".granola-mcp"
CONFIG_PATH = CONFIG_DIR / "config.json"

API_BASE = "https://public-api.granola.ai/v1"


class InvalidAPIKeyError(RuntimeError):
    """Raised when Granola rejects our API key (401)."""


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    CONFIG_PATH.chmod(0o600)


def _prompt_for_key() -> str:
    border = "=" * 64
    print(f"\n{border}")
    print("  GRANOLA ENTERPRISE API KEY REQUIRED")
    print(border)
    print("")
    print("  Granola Enterprise plan required.")
    print("")
    print("  1. Open Granola in a browser")
    print("  2. Settings → Workspaces → API tab")
    print("  3. Click \"Generate API Key\"")
    print("  4. Copy the key")
    print("  5. Paste it below, then press Enter")
    print("")
    print(f"{border}\n", flush=True)
    key = input("Paste API key: ").strip()
    if not key:
        print("No key entered. Exiting.", flush=True)
        sys.exit(1)
    return key


def _validate_key(key: str) -> int | None:
    """Calls /notes?page_size=1. Returns note count if available, else None."""
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{API_BASE}/notes", headers=headers, params={"page_size": 1})
        if resp.status_code == 401:
            raise InvalidAPIKeyError("API key rejected by Granola (401)")
        resp.raise_for_status()
        data = resp.json() or {}
        # The API doesn't return a total; just report whether notes are accessible
        notes = data.get("notes") or []
        if data.get("hasMore"):
            return None  # unknown total, but accessible
        return len(notes)


def ensure_authenticated() -> str:
    """Load key from config, prompt if missing, validate. Returns key."""
    cfg = _load_config()
    key = (cfg.get("api_key") or "").strip()

    if not key:
        key = _prompt_for_key()
        _save_config({"api_key": key})

    try:
        count = _validate_key(key)
    except InvalidAPIKeyError:
        print(
            "\nAPI key is invalid — delete ~/.granola-mcp/config.json and restart.",
            flush=True,
        )
        sys.exit(1)

    if count is None:
        print("Granola API key validated. Notes accessible (paginated).", flush=True)
    else:
        print(f"Granola API key validated. {count} notes accessible.", flush=True)
    return key


def get_api_key() -> str:
    cfg = _load_config()
    key = (cfg.get("api_key") or "").strip()
    if not key:
        raise InvalidAPIKeyError(
            "No Granola API key configured — delete ~/.granola-mcp/config.json and restart."
        )
    return key
