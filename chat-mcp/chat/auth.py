"""Auth dependency — permissive.

This deployment is reachable only on the LAN and via Tailscale (no public
endpoint), so network reachability is the access control. The dependency is
left as a stub so future tightening only needs to swap this file.
"""

import logging

from .config import Config

logger = logging.getLogger(__name__)


def make_dependency(cfg: Config):
    async def allow_all() -> str:
        return "local"
    return allow_all
