"""List and remove the Docker resources of one deployment: its containers, images, volumes and networks."""

from __future__ import annotations

from dataclasses import dataclass
import json
import shlex
import subprocess
import sys
from typing import Sequence

from . import transfer


KINDS = ("containers", "images", "volumes", "networks")
SELECTABLE = ("all", *KINDS)
# Containers hold the networks, volumes and images, so they go first.
REMOVAL_ORDER = ("containers", "networks", "volumes", "images")
COLUMNS = {
    "containers": ("SERVICE", "STATE", "NAME"),
    "images": ("IMAGE", "SIZE", "CREATED", "ID"),
    "volumes": ("CONTENT", "DRIVER", "NAME"),
    "networks": ("PURPOSE", "INTERNAL", "NAME"),
}
VOLUME_CONTENT = {"ollama": "Ollama models", "webui": "Open WebUI accounts and chats"}
TERMINAL_VOLUME_CONTENT = "terminal user folders"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
COMPOSE_NETWORK_LABEL = "com.docker.compose.network"
# Labels that the terminal provisioner sets on its own resources.
TERMINAL_CHAT_LABEL = "loaf.terminal.chat-id"
TERMINAL_HELPER_LABEL = "loaf.terminal.helper"
TERMINAL_CONNECTED_LABEL = "loaf.terminal.connected"


@dataclass(frozen=True)
class Resource:
    name: str
    row: tuple[str, ...]
    # A running Compose service container stops gracefully before removal.
    service_running: bool = False


def quantity(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def docker_output(arguments: Sequence[str]) -> str:
    # Docker's own error messages reach the terminal.
    return subprocess.run(["docker", *arguments], check=True, stdout=subprocess.PIPE, text=True).stdout


def inspect(kind: str, ids: set[str]) -> list[dict]:
    if not ids:
        return []
    # Objects removed since they were listed are missing from the output, so a nonzero exit is expected then.
    result = subprocess.run(["docker", kind, "inspect", *sorted(ids)], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    return json.loads(result.stdout or "[]")


def labeled_ids(kind: str, labels: Sequence[str]) -> set[str]:
    listing = ["container", "ls", "--all"] if kind == "container" else ["network", "ls"]
    ids = set()
    for label in labels:
        ids.update(docker_output([*listing, "--quiet", "--no-trunc", "--filter", f"label={label}"]).split())
    return ids


def owner_labels(namespace: str) -> tuple[str, ...]:
    return (f"{transfer.COMPOSE_PROJECT_LABEL}={namespace}", f"{transfer.WORKSPACE_PROJECT_LABEL}={namespace}")


def container_state(state: dict) -> str:
    text = f"exited ({state['ExitCode']})" if state["Status"] == "exited" else state["Status"]
    health = (state.get("Health") or {}).get("Status")
    return f"{text}, {health}" if health and state["Running"] else text


def containers(namespace: str) -> list[Resource]:
    """Compose service containers first, then the terminal provisioner's chat, draft and helper containers."""
    prefix = transfer.workspace_prefix(namespace)
    ids = labeled_ids("container", (*owner_labels(namespace), f"{TERMINAL_HELPER_LABEL}={namespace}"))
    found = []
    for record in inspect("container", ids):
        name, labels, state = record["Name"].removeprefix("/"), record["Config"]["Labels"] or {}, record["State"]
        service = labels.get(COMPOSE_SERVICE_LABEL) if labels.get(transfer.COMPOSE_PROJECT_LABEL) == namespace else None
        compose = service is not None
        if not compose:
            if not name.startswith(prefix):
                continue
            service = ("terminal helper" if TERMINAL_HELPER_LABEL in labels
                       else "terminal chat" if TERMINAL_CHAT_LABEL in labels else "terminal draft")
        found.append((not compose, name, Resource(name, (service, container_state(state), name),
                                                  compose and state["Running"])))
    return [resource for *_, resource in sorted(found)]


def images(namespace: str) -> list[Resource]:
    expected = transfer.image_names(namespace, include_terminal=True)
    listed = {}
    for line in docker_output(["image", "ls", "--format", "{{json .}}"]).splitlines():
        record = json.loads(line)
        name = f"{record['Repository']}:{record['Tag']}"
        if name in expected:
            listed[name] = Resource(name, (name, record["Size"], record["CreatedSince"], record["ID"].removeprefix("sha256:")[:12]))
    return [listed[name] for name in expected if name in listed]


def volumes(namespace: str) -> list[Resource]:
    expected = {name: VOLUME_CONTENT[kind] for name, kind in transfer.volume_names(namespace).items()}
    expected[transfer.terminal_volume(namespace)] = TERMINAL_VOLUME_CONTENT
    listed = {}
    for line in docker_output(["volume", "ls", "--format", "{{json .}}"]).splitlines():
        record = json.loads(line)
        if record["Name"] in expected:
            listed[record["Name"]] = Resource(record["Name"], (expected[record["Name"]], record["Driver"], record["Name"]))
    return [listed[name] for name in expected if name in listed]


def networks(namespace: str) -> list[Resource]:
    """Compose networks first, then the terminal provisioner's workspace bridges and connected network."""
    prefix = transfer.workspace_prefix(namespace)
    found = []
    for record in inspect("network", labeled_ids("network", owner_labels(namespace))):
        name, labels = record["Name"], record["Labels"] or {}
        purpose = labels.get(COMPOSE_NETWORK_LABEL) if labels.get(transfer.COMPOSE_PROJECT_LABEL) == namespace else None
        compose = purpose is not None
        if not compose:
            if not name.startswith(prefix):
                continue
            purpose = "terminal connected" if TERMINAL_CONNECTED_LABEL in labels else "terminal workspace"
        found.append((not compose, name, Resource(name, (purpose, "yes" if record["Internal"] else "no", name))))
    return [resource for *_, resource in sorted(found)]


COLLECTORS = {"containers": containers, "images": images, "volumes": volumes, "networks": networks}


def inventory(namespace: str, kinds: Sequence[str] = KINDS) -> dict[str, list[Resource]]:
    return {kind: COLLECTORS[kind](namespace) for kind in KINDS if kind in kinds}


def report(namespace: str, found: dict[str, list[Resource]]) -> str:
    """Aligned tables of the found resources; the last column is never padded."""
    lines = [f"Docker resources of deployment {namespace}"]
    for kind, resources in found.items():
        lines.append("")
        if not resources:
            lines.append(f"{kind.capitalize()}: none")
            continue
        lines.append(f"{kind.capitalize()} ({len(resources)})")
        rows = [COLUMNS[kind], *(resource.row for resource in resources)]
        widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]) - 1)]
        for row in rows:
            lines.append("  " + "".join(f"{cell:<{width}}  " for cell, width in zip(row, widths)) + row[-1])
    return "\n".join(lines)


def plan(namespace: str, kinds: Sequence[str]) -> list[str]:
    """Describe a removal without Docker. Container and network names exist only at runtime."""
    steps = {
        "containers": f"Stop the running services of deployment {namespace}, then remove all of its containers",
        "networks": "Remove its Compose networks and terminal workspace networks",
        "volumes": "Remove the volumes " + ", ".join([*transfer.volume_names(namespace), transfer.terminal_volume(namespace)]),
        "images": "Remove the images " + ", ".join(transfer.image_names(namespace, include_terminal=True)),
    }
    check = [] if "containers" in kinds else [f"Require that deployment {namespace} has no containers"]
    return check + [steps[kind] for kind in REMOVAL_ORDER if kind in kinds]


def run(arguments: Sequence[str], *, verbose: bool) -> None:
    command = ["docker", *arguments]
    if verbose:
        print("+ " + shlex.join(command), flush=True)
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)


def remove(namespace: str, kinds: Sequence[str], *, verbose: bool) -> int:
    """Remove the selected kinds in dependency order and return the number of removed resources.

    Each kind is listed again right before its removal: stopping the terminal provisioner
    removes the containers of temporary chats and the networks of stopped workspaces.
    """
    removed = 0
    for kind in REMOVAL_ORDER:
        if kind not in kinds:
            continue
        if kind == "containers":
            # Services stop within their configured grace periods; the provisioner stops its workspaces.
            running = [resource.name for resource in containers(namespace) if resource.service_running]
            if running:
                print(f"Stopping {quantity(len(running), 'service container')}…", file=sys.stderr, flush=True)
                run(["container", "stop", *running], verbose=verbose)
        resources = COLLECTORS[kind](namespace)
        if not resources:
            continue
        noun = kind.removesuffix("s")
        print(f"Removing {quantity(len(resources), noun)}…", file=sys.stderr, flush=True)
        names = [resource.name for resource in resources]
        run([noun, "rm", *(("--force",) if kind == "containers" else ()), *names], verbose=verbose)
        removed += len(resources)
    return removed
