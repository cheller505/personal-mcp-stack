"""ClickUp Personal API Token storage and validation."""

import json
import logging
import sys
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".clickup-mcp"
CONFIG_PATH = CONFIG_DIR / "config.json"

API_BASE = "https://api.clickup.com/api/v2"


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


def _prompt_for_token() -> str:
    border = "=" * 64
    print(f"\n{border}")
    print("  CLICKUP API TOKEN REQUIRED")
    print(border)
    print("")
    print("  1. Open ClickUp in a browser")
    print("  2. Click your avatar (bottom-left) → Settings → Apps")
    print("  3. Under \"API Token\", click \"Generate\" (or copy existing token)")
    print("  4. The token starts with \"pk_...\"")
    print("  5. Paste it below, then press Enter")
    print("")
    print(f"{border}\n", flush=True)
    token = input("Paste API token: ").strip()
    if not token:
        print("No token entered. Exiting.", flush=True)
        sys.exit(1)
    return token


def _validate_token(token: str) -> dict:
    """Calls /user. Returns the user dict on success, raises on failure."""
    headers = {"Authorization": token}
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{API_BASE}/user", headers=headers)
        if resp.status_code == 401:
            raise RuntimeError("Token rejected by ClickUp (401)")
        resp.raise_for_status()
        return resp.json().get("user", {})


def _list_workspaces(token: str) -> list[dict]:
    headers = {"Authorization": token}
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{API_BASE}/team", headers=headers)
        resp.raise_for_status()
        return resp.json().get("teams", [])


def ensure_authenticated() -> str:
    """Load token from config, prompt if missing, validate. Returns token."""
    cfg = _load_config()
    token = cfg.get("token", "").strip()

    if not token:
        token = _prompt_for_token()
        _save_config({"token": token})

    try:
        user = _validate_token(token)
    except RuntimeError:
        print(
            "\nToken is invalid — delete ~/.clickup-mcp/config.json and restart.",
            flush=True,
        )
        sys.exit(1)

    username = user.get("username") or user.get("email") or "unknown"
    print(f"Authenticated as {username}", flush=True)

    try:
        teams = _list_workspaces(token)
        if teams:
            print("Workspaces:", flush=True)
            for t in teams:
                print(f"  • {t.get('name')} (id: {t.get('id')})", flush=True)
    except Exception as exc:
        logger.warning("Could not list workspaces: %s", exc)

    return token


def get_token() -> str:
    cfg = _load_config()
    token = cfg.get("token", "").strip()
    if not token:
        raise RuntimeError(
            "No ClickUp token configured — delete ~/.clickup-mcp/config.json and restart."
        )
    return token
