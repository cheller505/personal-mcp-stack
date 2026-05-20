"""Load ~/.chat-mcp/config.json."""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".chat-mcp"
CONFIG_PATH = CONFIG_DIR / "config.json"


@dataclass
class LumenConfig:
    endpoint: str
    api_key: str
    model: str
    max_tokens: int = 8000


@dataclass
class BindConfig:
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class Config:
    lumen: LumenConfig
    mcp_servers: dict[str, str]
    bind: BindConfig
    allowed_users: list[str] = field(default_factory=list)
    allow_any_tailnet_user: bool = True
    hidden_tools: set = field(default_factory=set)

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            raise FileNotFoundError(
                f"Missing config at {CONFIG_PATH}. "
                "Create it with lumen creds + mcp_servers map."
            )
        raw = json.loads(CONFIG_PATH.read_text())
        lumen = LumenConfig(**raw["lumen"])
        bind = BindConfig(**raw.get("bind", {}))
        return cls(
            lumen=lumen,
            mcp_servers=dict(raw["mcp_servers"]),
            bind=bind,
            allowed_users=list(raw.get("allowed_users", [])),
            allow_any_tailnet_user=bool(raw.get("allow_any_tailnet_user", True)),
            hidden_tools=set(raw.get("hidden_tools", [])),
        )
