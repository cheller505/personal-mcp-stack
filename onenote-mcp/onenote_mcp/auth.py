"""MSAL device code flow authentication with persistent token cache."""

import logging
import os
from pathlib import Path

import msal

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".onenote-mcp"
TOKEN_CACHE_PATH = CONFIG_DIR / "token_cache.json"


# NOTE: do NOT add "offline_access" — MSAL hard-rejects it as a reserved scope.
SCOPES = ["Notes.Read.All", "Notes.ReadWrite", "Notes.Create"]


def get_client_id() -> str:
    cid = os.environ.get("ONENOTE_MCP_CLIENT_ID", "").strip()
    if not cid:
        raise ValueError(
            "ONENOTE_MCP_CLIENT_ID is not set. Register an Azure app and "
            "export ONENOTE_MCP_CLIENT_ID and ONENOTE_MCP_TENANT_ID. "
            "Same Azure app can serve both email-mcp and onenote-mcp. See SETUP.md."
        )
    return cid


def get_tenant_id() -> str:
    tid = os.environ.get("ONENOTE_MCP_TENANT_ID", "").strip()
    if not tid:
        raise ValueError(
            "ONENOTE_MCP_TENANT_ID is not set. Use your Azure directory "
            "(tenant) ID, or 'organizations' for any org account. See SETUP.md."
        )
    return tid


def load_token_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_PATH.exists():
        cache.deserialize(TOKEN_CACHE_PATH.read_text())
    return cache


def save_token_cache(cache: msal.SerializableTokenCache) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE_PATH.write_text(cache.serialize())
    TOKEN_CACHE_PATH.chmod(0o600)


def build_app(cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    return msal.PublicClientApplication(
        get_client_id(),
        authority=f"https://login.microsoftonline.com/{get_tenant_id()}",
        token_cache=cache,
    )


def get_token_silent(cache: msal.SerializableTokenCache) -> dict | None:
    app = build_app(cache)
    accounts = app.get_accounts()
    if not accounts:
        return None
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if result and cache.has_state_changed:
        save_token_cache(cache)
    return result


def acquire_token_device_code(cache: msal.SerializableTokenCache) -> dict:
    app = build_app(cache)
    flow = app.initiate_device_flow(scopes=SCOPES)

    if "user_code" not in flow:
        raise RuntimeError(
            f"Failed to initiate device flow: {flow.get('error_description', flow.get('error', 'unknown'))}"
        )

    user_code = flow["user_code"]
    verify_uri = flow.get("verification_uri", "https://microsoft.com/devicelogin")

    border = "=" * 64
    print(f"\n{border}")
    print("  MICROSOFT AUTHENTICATION REQUIRED (OneNote MCP)")
    print(border)
    print(f"\n  Step 1 — Open this URL in any browser:")
    print(f"\n      {verify_uri}")
    print(f"\n  Step 2 — Enter this code when prompted:")
    print(f"\n      {user_code}")
    print(f"\n  The code expires in approximately 15 minutes.")
    print(f"  If this machine has no browser, complete the sign-in on a phone or laptop.")
    print(f"  Sign in with your Microsoft account to grant OneNote access.")
    print(f"{border}\n")
    print("Waiting for you to complete authentication...\n")

    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        error = result.get("error", "")
        desc = result.get("error_description", "unknown error")
        if error == "code_expired":
            raise TimeoutError(
                "Device code expired before authentication completed. "
                "Restart the server to get a new code."
            )
        raise RuntimeError(f"Authentication failed: {desc}")

    if cache.has_state_changed:
        save_token_cache(cache)

    print("Authentication successful!\n")
    return result


def ensure_authenticated() -> tuple[msal.SerializableTokenCache, str]:
    """Returns (cache, access_token). Runs device code flow if no valid token exists."""
    cache = load_token_cache()

    result = get_token_silent(cache)
    if result and "access_token" in result:
        logger.info("Token loaded from cache (silent auth)")
        return cache, result["access_token"]

    result = acquire_token_device_code(cache)
    return cache, result["access_token"]


def get_fresh_token(cache: msal.SerializableTokenCache) -> str:
    """Get a current access token, refreshing silently if needed. Raises on failure."""
    result = get_token_silent(cache)
    if result and "access_token" in result:
        return result["access_token"]
    raise RuntimeError(
        "Token refresh failed — all accounts may have been removed. "
        "Delete ~/.onenote-mcp/token_cache.json and restart to re-authenticate."
    )
