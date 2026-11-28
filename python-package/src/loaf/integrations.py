"""Render host integration files without changing the deployment."""

from collections.abc import Iterable, Sequence
import json
from pathlib import Path


TETHER_INSTRUCTIONS = """Install Open WebUI Tether

1. On your desktop computer, download the installer for your OS and CPU.
   https://github.com/open-webui/tether/releases
2. On Debian, Ubuntu or Mint, run sudo apt install ./tether_amd64.deb
   (use ./tether_arm64.deb on ARM64). On Windows, run the setup .exe.
   On macOS, open the .dmg and move Open WebUI Tether to Applications.
3. Start your WebUI deployment with loaf run offline or loaf run online. Open Tether,
   enter the WebUI address under Connect to Open WebUI (the local default
   is http://localhost:3000), and sign in. Open Terminal setup is optional.
"""


def systemd_unit(project: Path, launcher: Sequence[str], uid: int, *, cpu: bool = False) -> str:
    directory = str(project).replace("%", "%%")
    if "\n" in directory or "\r" in directory:
        raise ValueError("systemd cannot use a working directory containing a line break.")
    # WorkingDirectory reads a raw value. End with a path component if the
    # directory name would otherwise be trimmed or parsed as a continuation.
    if directory.endswith("\\") or directory.rstrip() != directory:
        directory += "/."
    # The colon disables systemd's dollar expansion in the argument list.
    args = (":" + launcher[0], *launcher[1:])
    command = " ".join(json.dumps(arg.replace("%", "%%"), ensure_ascii=False) for arg in args)
    serve = "run offline --cpu" if cpu else "run offline"
    return f"""\
[Install]
WantedBy=multi-user.target

[Unit]
Description=Local AI Ferry offline deployment
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=exec
User={uid}
WorkingDirectory={directory}
ExecStart={command} {serve} --attach
ExecStop={command} stop
StandardOutput=null
StandardError=journal
Restart=on-failure
RestartSec=5
# The terminal server may take 45 seconds to stop its chat containers.
TimeoutStopSec=90
Environment=PYTHONUNBUFFERED=1
"""


def continue_config(webui_url: str, models: Iterable[tuple[str, str]]) -> str:
    """Render Continue YAML from pairs of Ollama aliases and model references."""
    lines = [
        "# VS Code Continue integration config.",
        "# Enable API keys in Open WebUI, generate a key and replace each apiKey placeholder.",
        "#",
        "# Each model starts with Continue's default roles and its detected capabilities. Before you widen them,",
        "# check the model card:",
        "# - autocomplete needs a code model trained for fill-in-the-middle, such as a Coder model.",
        "# - embed needs an embedding model and rerank a reranker; chat models fill neither role.",
        "# - capabilities replaces Continue's detection: add tool_use only for models with tool calls",
        "#   (Agent mode needs it) and image_input only for models with a vision projector.",
        "name: Local Inference",
        "version: 1.0.0",
        "schema: v1",
        "",
        "models:",
    ]
    api_base = json.dumps(webui_url + "/api")
    for alias, source in models:
        model_id = alias or (source if ":" in source else source + ":latest")
        name = (alias or source).removesuffix(":latest")
        lines.extend([
            f"  - name: {json.dumps(name)}",
            "    provider: openai",
            f"    model: {json.dumps(model_id)}",
            f"    apiBase: {api_base}",
            "    apiKey: REPLACE_WITH_OPEN_WEBUI_API_KEY",
            "    env:",
            "      useLegacyCompletionsEndpoint: false",
            "    roles: [chat, edit, apply, summarize]",
            "    # capabilities: [tool_use, image_input]",
        ])
    lines.extend(["", "context:"])
    for provider in (
        "code", "codebase", "repo-map", "folder", "tree", "file", "currentFile",
        "os", "terminal", "open", "diff", "debugger", "problems", "docs",
        "http"
    ):
        lines.append(f"  - provider: {provider}")
    return "\n".join(lines) + "\n"
