"""Start Open WebUI with loaf's terminal connection preset from the bearer key secret.

Open WebUI stores TERMINAL_SERVER_CONNECTIONS only while its database holds no terminal connections,
so a connection saved in its admin settings takes precedence over this preset.
"""

import json
import os
from pathlib import Path
import sys


KEY_FILE = Path("/run/secrets/terminal-server-key")
# No grants make a connection admin-only in Open WebUI; principal * with read permission means every account.
GRANTS = {"admins": [], "users": [{"principal_type": "user", "principal_id": "*", "permission": "read"}]}


def terminal_connections(key: str, access: str) -> str:
    return json.dumps([{
        "id": "loaf-terminal", "name": "Terminal", "enabled": True, "url": "http://terminal-server:8000",
        "path": "/openapi.json", "auth_type": "bearer", "key": key, "server_type": "terminal",
        "config": {"access_grants": GRANTS[access]},
    }])


def environment(environ: dict[str, str], key_file: Path = KEY_FILE) -> dict[str, str]:
    """Add the preset when Compose supplied the key and the caller set no connections of its own."""
    if "TERMINAL_SERVER_CONNECTIONS" in environ or not key_file.is_file():
        return environ
    key = key_file.read_bytes().rstrip(b"\r\n").decode("ascii")
    access = environ.get("LOAF_TERMINAL_WEBUI_ACCESS", "admins")
    return {**environ, "TERMINAL_SERVER_CONNECTIONS": terminal_connections(key, access)}


if __name__ == "__main__":
    os.execvpe(sys.argv[1], sys.argv[1:], environment(dict(os.environ)))
