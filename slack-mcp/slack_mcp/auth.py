"""Slack User OAuth Token storage and validation."""

import json
import logging
import sys
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".slack-mcp"
CONFIG_PATH = CONFIG_DIR / "config.json"

API_BASE = "https://slack.com/api"


class SlackAuthError(RuntimeError):
    """Raised on invalid_auth / not_authed / token_revoked / account_inactive."""


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
    print("  SLACK USER OAUTH TOKEN REQUIRED")
    print(border)
    print("")
    print("  You need a Slack User OAuth Token (starts with \"xoxp-\").")
    print("")
    print("  1. Go to https://api.slack.com/apps → \"Create New App\"")
    print("     → \"From scratch\", name it (e.g. \"Personal Archive\")")
    print("     → pick your workspace")
    print("  2. In the new app, go to \"OAuth & Permissions\"")
    print("  3. Under \"User Token Scopes\" (NOT Bot Token Scopes), add:")
    print("       channels:read, channels:history,")
    print("       groups:read, groups:history,")
    print("       im:read, im:history,")
    print("       mpim:read, mpim:history,")
    print("       users:read, users:read.email,")
    print("       files:read, team:read, reactions:read")
    print("  4. Scroll up, click \"Install to Workspace\" and approve.")
    print("     (Workspace admin approval may be required.)")
    print("  5. Copy the \"User OAuth Token\" (starts with xoxp-).")
    print("  6. Paste it below, then press Enter.")
    print("")
    print(f"{border}\n", flush=True)
    token = input("Paste user OAuth token: ").strip()
    if not token:
        print("No token entered. Exiting.", flush=True)
        sys.exit(1)
    if not token.startswith("xoxp-"):
        print("Warning: token does not start with 'xoxp-' — continuing anyway.", flush=True)
    return token


def _validate_token(token: str) -> dict:
    """Calls auth.test. Returns the response dict on success, raises on failure."""
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{API_BASE}/auth.test", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            err = data.get("error", "unknown")
            if err in ("invalid_auth", "not_authed", "token_revoked", "account_inactive"):
                raise SlackAuthError(err)
            raise RuntimeError(f"auth.test failed: {err}")
        return data


def _team_info(token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{API_BASE}/team.info", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            return {}
        return data.get("team", {}) or {}


def ensure_authenticated() -> str:
    """Load token from config, prompt if missing, validate. Returns token."""
    cfg = _load_config()
    token = cfg.get("token", "").strip()

    if not token:
        token = _prompt_for_token()
        _save_config({"token": token})

    try:
        info = _validate_token(token)
    except SlackAuthError as exc:
        print(
            f"\nToken is invalid ({exc}) — delete ~/.slack-mcp/config.json and restart.",
            flush=True,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"\nCould not validate token: {exc}", flush=True)
        sys.exit(1)

    user = info.get("user") or "unknown"
    team_name = info.get("team") or "unknown"
    team_id = info.get("team_id") or "unknown"

    # team.info gives richer name/domain if available
    try:
        t = _team_info(token)
        if t.get("name"):
            team_name = t["name"]
    except Exception as exc:
        logger.warning("team.info call failed: %s", exc)

    print(
        f"Authenticated as {user} on team {team_name} (team_id={team_id})",
        flush=True,
    )
    return token


def get_token() -> str:
    cfg = _load_config()
    token = cfg.get("token", "").strip()
    if not token:
        raise RuntimeError(
            "No Slack token configured — delete ~/.slack-mcp/config.json and restart."
        )
    return token
