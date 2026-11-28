"""Read-only preflight for the selected Docker deployment."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from typing import TYPE_CHECKING

from . import config, transfer

if TYPE_CHECKING:
    from .cli import Compose


MINIMUM_COMPOSE = (2, 26, 1)
NATIVE_GPU_COMPOSE = (2, 30, 0)
SERVING_WEBUI = ("open-webui", "open-webui-online")
SERVING_OLLAMA = ("ollama-offline", "ollama-connected")


def docker_output(compose: Compose, command: list[str]) -> str:
    result = subprocess.run(
        command, cwd=compose.project_dir, env=compose.environment(compose.variables()),
        capture_output=True, encoding="utf-8", errors="replace", timeout=30, check=True,
    )
    return result.stdout.strip()


def compose_output(compose: Compose, *arguments: str) -> str:
    from .cli import PROFILES, Step

    profiles = (*PROFILES, "terminal") if compose.settings.terminal_server_enabled else PROFILES
    step = Step(profiles, arguments)
    if compose.verbose:
        compose.trace(step)
    return docker_output(compose, compose.command(step))


def probe_container(*options: str) -> list[str]:
    return ["docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only", *options]


def check_gpu(compose: Compose, image: str) -> str:
    unavailable = "Docker could not verify GPU access. Run 'loaf status --cpu' for CPU readiness."
    try:
        devices = docker_output(compose, probe_container(
            "--gpus", "all", "--entrypoint", "timeout", image, "10s", "nvidia-smi", "-L",
        ))
    except subprocess.SubprocessError as exc:
        raise ValueError(unavailable) from exc
    if not any(line.startswith("GPU ") for line in devices.splitlines()):
        raise ValueError(unavailable)
    return devices


def check_volume(compose: Compose, image: str, volume: str, *, models: bool) -> str:
    try:
        docker_output(compose, ["docker", "volume", "inspect", volume])
    except subprocess.CalledProcessError as exc:
        if "no such volume" not in (exc.stderr or "").lower():
            raise
        if models:
            raise ValueError("The model volume does not exist yet. Run loaf download or loaf restore models.") from exc
        return "will be created on first startup"
    mount = f"type=volume,source={volume},target=/check,volume-nocopy,readonly"
    script = "ls -A /check >/dev/null"
    if models:
        script = """\
set -eu
ls -A /check >/dev/null
for catalog in /check/models/manifests /check/models/manifests-v2; do
    if [ -d "$catalog" ] && [ -n "$(find -L "$catalog" -type f -print -quit)" ]; then
        exit 0
    fi
done
if [ -d /check/config ] && [ -d /check/models/blobs ] &&
    [ -n "$(find /check/config -type f -name '*.model' -print -quit)" ] &&
    [ -n "$(find /check/models/blobs -type f -name 'sha256-*' ! -name '*-partial*' -print -quit)" ]; then
    exit 0
fi
echo 'No local model manifests or saved definitions with blobs. Run loaf download or loaf restore models.' >&2
exit 1
"""
    docker_output(compose, probe_container(
        "--env", "NVIDIA_VISIBLE_DEVICES=void", "--mount", mount, "--entrypoint", "/bin/sh", image, "-c", script,
    ))
    return "readable; local model data found" if models else "readable"


def compose_containers(compose: Compose, *services: str) -> list[dict]:
    output = compose_output(compose, "ps", "--format", "json", *services)
    # Compose versions emit either newline-delimited objects or a JSON array.
    return json.loads(output) if output.startswith("[") else [json.loads(line) for line in output.splitlines()]


def check_ports(compose: Compose, services: dict) -> str:
    host = os.environ.get("DOCKER_HOST") if not os.environ.get("DOCKER_CONTEXT") else None
    if not host:
        host = docker_output(compose, ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"])
    if not host.startswith("unix://"):
        raise ValueError("Cannot test host ports from this Docker endpoint. Run status on the Docker host.")

    containers = compose_containers(compose, *SERVING_WEBUI)
    published = [
        port for container in containers if container["State"] == "running" for port in (container["Publishers"] or ())
    ]
    ports = {
        (port.get("host_ip") or "0.0.0.0", int(port["published"]), port.get("protocol", "tcp"))
        for service in SERVING_WEBUI for port in services[service].get("ports", ())
    }
    results = []
    for address, number, protocol in sorted(ports):
        endpoint = f"[{address}]:{number}" if ":" in address else f"{address}:{number}"
        wildcard = "::" if ":" in address else "0.0.0.0"
        if any(
            item["PublishedPort"] == number and item["Protocol"] == protocol and item["URL"] in (address, wildcard)
            for item in published
        ):
            results.append(f"{endpoint}/{protocol} belongs to this deployment")
            continue
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        kind = socket.SOCK_DGRAM if protocol == "udp" else socket.SOCK_STREAM
        try:
            with socket.socket(family, kind) as probe:
                if os.name != "nt":
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind((address, number))
                if kind == socket.SOCK_STREAM:
                    probe.listen()
        except OSError as exc:
            raise ValueError(f"Cannot bind {endpoint}/{protocol}. {exc.strerror}") from exc
        results.append(f"{endpoint}/{protocol} available")
    return ", ".join(results) or "no configured host ports"


def print_result(status: str, label: str, detail: str = "") -> None:
    lines = detail.strip().splitlines()
    prefix = f"  {status:<4}  {label}"
    print(prefix + (f" — {lines[0]}" if lines else ""), flush=True)
    for line in lines[1:]:
        print(f"        {line}", flush=True)


def check(compose: Compose, *, cpu: bool = False) -> bool:
    failed = False

    def report(label, check_item, *, status="OK"):
        nonlocal failed
        try:
            value = check_item()
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            failed = True
            details = getattr(exc, "stderr", None) or str(exc)
            print_result("FAIL", label, details)
            return None
        if status is not None:
            print_result(status, label, value if isinstance(value, str) else "")
        return value

    def compose_version():
        version = docker_output(compose, ["docker", "compose", "version", "--short"])
        match = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?(?=$|[-+])", version)
        if not match:
            raise ValueError(f"Unrecognized Docker Compose version {version!r}.")
        release = tuple(int(part or 0) for part in match.groups())
        if release < MINIMUM_COMPOSE:
            raise ValueError(f"Found {version!r}. Compose 2.26.1 or newer is required.")
        return release, version

    def compose_services():
        return json.loads(compose_output(compose, "config", "--format", "json"))["services"]

    def engine_version():
        version = docker_output(compose, ["docker", "version", "--format", "{{.Server.Version}}"])
        match = re.match(r"(\d+)\.", version)
        if not match:
            raise ValueError(f"Unrecognized Docker Engine version {version!r}.")
        major = int(match[1])
        if major < 26 and compose.settings.terminal_server_enabled:
            raise ValueError(f"Found {version!r}. Open Terminal requires Docker Engine 26 or newer.")
        return major, version

    print("\nDocker and Compose", flush=True)
    engine = report("Docker Engine version check", engine_version, status=None)
    if engine is not None:
        major, version = engine
        # Only an enabled terminal server uses the bridge isolation.
        warning = 26 <= major <= 27 and compose.settings.terminal_server_enabled
        detail = f"{version} — Open Terminal uses legacy bridge isolation" if warning else version
        print_result("WARN" if warning else "OK", "Docker Engine", detail)
    compose_release = report("Docker Compose", compose_version, status=None)
    if compose_release is not None:
        release, version = compose_release
        if release < NATIVE_GPU_COMPOSE and not cpu:
            print_result("WARN", "Docker Compose", f"{version} — uses NVIDIA device reservations for GPU access")
        else:
            print_result("OK", "Docker Compose", version)
    print("\nConfiguration", flush=True)
    print_result("OK", "Launcher configuration")
    if cpu:
        print_result("OK", "Inference mode", "CPU")
    services = report("Compose configuration", compose_services)
    if compose.settings.terminal_server_enabled:
        def terminal_secret():
            config.read_terminal_secret(compose.terminal_secret_file())
            return "valid"

        report("Terminal server key file", terminal_secret)
        if compose.settings.terminal_server_apt_source_file or compose.settings.terminal_server_apt_repository_dir:
            def terminal_apt():
                source, repository = compose.terminal_apt_inputs(serving=True)
                if repository is not None:
                    compose.repository_source(repository, source)
                return "valid"

            report("Terminal server APT configuration", terminal_apt)
    if services is None:
        return False
    try:
        backend_images = {services[service]["image"] for service in SERVING_OLLAMA}
        images = backend_images | {services[service]["image"] for service in (*SERVING_WEBUI, "ollama-online")}
        images.add(transfer.image_names(compose.namespace)[0])
        if compose.settings.terminal_server_enabled:
            images.add(services["terminal-server"]["image"])
    except KeyError as exc:
        print_result("FAIL", "Serving configuration", f"Missing service or image {exc}")
        return False
    print("\nLocal images" if cpu else "\nLocal images and GPU", flush=True)
    available = set()
    def local_image(image):
        try:
            return docker_output(compose, ["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image])
        except subprocess.CalledProcessError as exc:
            problem = (exc.stderr or "").strip() or str(exc)
            raise ValueError(f"{problem} Run loaf build, or loaf restore images on an offline host.") from exc

    for image in sorted(images):
        platform = report(f"Local image {image}", lambda: local_image(image))
        if platform is not None:
            available.add(image)
    if not cpu:
        for image in sorted(backend_images & available):
            report(f"NVIDIA GPU access for {image}", lambda: check_gpu(compose, image))

    print("\nStorage and networking", flush=True)
    helper = next(iter(sorted(backend_images & available)), None)
    if helper is not None:
        for volume, role in transfer.volume_names(compose.namespace).items():
            report(f"Volume {volume}", lambda: check_volume(compose, helper, volume, models=role == "ollama"))
    else:
        print_result("SKIP", "Volume mount checks", "Requires a local Ollama serving image")
    report("WebUI host ports", lambda: check_ports(compose, services))
    return not failed
