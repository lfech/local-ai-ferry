"""Route one Open Terminal connection to deployment-scoped Docker workspaces."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager, closing, suppress
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, TraceConfig, UnixConnector, WSMsgType, web
from yarl import URL


LOG = logging.getLogger(__name__)
# Workspace ownership is persisted in Docker labels.
PROJECT = "loaf.tool-server.project"
OWNER = "loaf.tool-server.workspace"
CHAT_PROJECT = "loaf.terminal.chat-project"
CHAT_ID = "loaf.terminal.chat-id"
CHAT_OWNER = "loaf.terminal.chat-owner"
RUNTIME_MOUNTS = "loaf.terminal.runtime-mounts"
# Each workspace has a runner for the account's own requests and one for model tool calls.
ROLE = "loaf.terminal.role"
USER_ROLE = "user"
AGENT_ROLE = "agent"
ROLES = (USER_ROLE, AGENT_ROLE)
# The published tool schema places every model tool under this prefix.
AGENT_PREFIX = "/agent"
# Model tools that reach the account's interactive shell. Reading is routed to it; typing is not offered.
AGENT_READS_USER_SHELL = "/api/terminals/user/output"
AGENT_HIDDEN_TOOLS = {"/api/terminals/user/input"}
APT_PROJECT = "loaf.terminal.apt-project"
APT_WORKSPACE = "loaf.terminal.apt-workspace"
APT_TEMPORARY = "loaf.terminal.apt-temporary"
APT_PACKAGES = "loaf.terminal.apt-packages"
APT_IMAGE_REPOSITORY = "local/loaf-terminal-chat"
APT_REPOSITORY = "/run/loaf-apt/repository"
APT_INSTALL_PATH = "/packages/install"
APT_REMOVE_PATH = "/packages/remove"
APT_MAX_PACKAGES = 32
# A trailing "-" would ask APT to remove the package.
APT_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]*[a-z0-9+.](?::[a-z0-9-]+)?(?:=[a-zA-Z0-9.+:~\-]+)?")
APT_COMMANDS = {"apt", "apt-get", "/usr/bin/apt", "/usr/bin/apt-get"}
# Subcommands handled for a chat; None marks the ones that need no change.
APT_ACTIONS = {"install": "install", "reinstall": "install", "remove": "remove", "purge": "remove",
               "update": None, "autoremove": None, "clean": None, "autoclean": None}
APT_OPTIONS = {"-y", "--yes", "--assume-yes", "-q", "-qq", "--quiet", "--no-install-recommends", "--purge",
               "--auto-remove", "--autoremove"}
APT_WORD = re.compile(r"[A-Za-z0-9_.:/=+~-]+")
ENVIRONMENT_ASSIGNMENT = re.compile(r"[A-Z_][A-Z0-9_]*=[A-Za-z0-9_.:/-]*")
USER_PROCESS_HISTORY = 64
GATEWAY = "com.docker.network.bridge.gateway_mode_ipv4"
INHIBIT_IPV4 = "com.docker.network.bridge.inhibit_ipv4"
BRIDGE_NAME = "com.docker.network.bridge.name"
KEY_FILE = Path("/run/secrets/terminal-server-key")
CHAT_DB = Path("/var/lib/loaf-webui/webui.db")
RUNTIME_RUNNER = "/run/loaf-terminal/runner.py"
WORKSPACE_ROOT = "/home"
WORKSPACE_HOME = "/home/user"
CHAT_HOME = "/home/chat"
WORKING_DIRECTORY_RECORDS = {
    hashlib.sha256(path.encode()).hexdigest() + ".json": json.dumps({"path": path})
    for path in (WORKSPACE_HOME, CHAT_HOME)
}
FOLDER_HISTORY_MAX_BYTES = 1024 * 1024
NON_SAVED_PREFIXES = ("temporary:", "local:", "channel:")
PORT = 8000
PROXY_ROOTS = {"api", "events", "execute", "files", "git", "info", "skills", "system"}
REQUEST_HEADERS = ("Accept", "Content-Type", "If-Modified-Since", "If-None-Match", "Range", "X-Session-Id")
RESPONSE_HEADERS = (
    "Accept-Ranges", "Cache-Control", "Content-Disposition", "Content-Encoding", "Content-Length",
    "Content-Range", "Content-Type", "ETag", "Last-Modified",
)


class DockerError(RuntimeError):
    def __init__(self, status, operation):
        super().__init__(f"Docker {operation} returned HTTP {status}")
        self.status = status


class Docker:
    def __init__(self, session):
        self.session = session

    async def call(self, method, path, *, body=None, params=None, missing=False, timeout=None, text=False):
        options = {} if timeout is None else {"timeout": ClientTimeout(total=timeout)}
        async with self.session.request(method, "http://docker" + path, json=body, params=params, **options) as response:
            if response.status == 404 and missing:
                return None
            if response.status >= 300:
                # Docker's error body can contain private configuration.
                parts = path.split("/")
                operation = f"{method} {parts[1]}" + (f" {parts[-1]}" if len(parts) > 3 else "")
                raise DockerError(response.status, operation)
            if text:
                return (await response.content.read(8192)).decode(errors="replace").strip()
            payload = await response.read()
            if payload:
                return json.loads(payload)


class Chats:
    """Look up saved chats in the deployment's read-only SQLite mount."""

    def __init__(self, path):
        self.uri = path.as_uri() + "?mode=ro"

    async def owners(self, chat_ids):
        return await asyncio.to_thread(self.read_owners, chat_ids)

    def read_owners(self, chat_ids):
        with closing(sqlite3.connect(self.uri, uri=True, timeout=1)) as database:
            database.execute("BEGIN")
            owners = {}
            for chat in chat_ids:
                row = database.execute("SELECT user_id FROM chat WHERE id = ?", (chat,)).fetchone()
                if row is not None:
                    owners[chat] = row[0]
            return owners


def workspace_key(owner_key, session):
    return hashlib.sha256(f"{owner_key}/{session}".encode()).hexdigest() if session else owner_key


def package_name(specification):
    return re.split(r"[:=]", specification, maxsplit=1)[0]


def plan_packages(current, operations):
    """Apply install and remove operations to the package list that defines a chat image."""
    packages = {package_name(specification): specification for specification in current}
    for action, names in operations:
        for name in names:
            if action == "install":
                packages[package_name(name)] = name
            elif packages.pop(package_name(name), None) is None:
                raise web.HTTPUnprocessableEntity(
                    text=f"{package_name(name)} was not installed for this chat. Only packages installed for this "
                         "chat can be removed; the shared terminal image keeps its own packages.")
    return sorted(packages.values())


def parse_apt_command(command):
    """Return the package operations of a plain APT command line, or None for any other command.

    Accepts APT commands joined by && or ;, optionally with sudo and environment assignments.
    Anything else, such as pipes, substitutions or other programs, runs unchanged in the runner.
    """
    command = command.strip() if isinstance(command, str) else ""
    if not command or "\n" in command or len(command) > 4096:
        return None
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments, current = [], []
    for token in tokens:
        if token in ("&&", ";"):
            if not current:
                return None
            segments.append(current)
            current = []
        elif APT_WORD.fullmatch(token):
            current.append(token)
        else:
            return None
    if current:
        segments.append(current)
    elif not tokens or tokens[-1] != ";":
        return None
    operations = []
    for words in segments:
        while words and ENVIRONMENT_ASSIGNMENT.fullmatch(words[0]):
            words = words[1:]
        if words[:1] == ["sudo"]:
            words = words[1:]
            while words and words[0] in ("-E", "-H", "-n"):
                words = words[1:]
        if not words or words[0] not in APT_COMMANDS:
            return None
        subcommand, packages = None, []
        for word in words[1:]:
            if word.startswith("-"):
                if word not in APT_OPTIONS and not re.fullmatch(r"-[yq]+", word):
                    return None
            elif subcommand is None:
                if word not in APT_ACTIONS:
                    return None
                subcommand = word
            elif APT_PACKAGE.fullmatch(word):
                packages.append(word)
            else:
                return None
        if subcommand is None or bool(packages) != (APT_ACTIONS[subcommand] is not None):
            return None
        if packages:
            operations.append((APT_ACTIONS[subcommand], packages))
    if sum(len(names) for _, names in operations) > APT_MAX_PACKAGES:
        return None
    return operations


@dataclass(frozen=True)
class Policy:
    project: str
    cpus: int
    memory_mib: int
    maximum: int
    idle_seconds: int
    command_seconds: int
    volume_driver: str
    volume_options: dict
    bridge_options: dict[str, str] = field(default_factory=lambda: {GATEWAY: "isolated"})

    @classmethod
    def load(cls, project, env, *, bridge_options):
        numbers = [
            int(env["LOAF_TERMINAL_" + name]) for name in
            ("CPUS", "MEMORY_MIB", "MAX_WORKSPACES", "IDLE_SECONDS", "COMMAND_SECONDS")
        ]
        if min(numbers) < 1:
            raise ValueError("Terminal server limits must be positive.")
        options = json.loads(env.get("LOAF_TERMINAL_VOLUME_OPTIONS") or "{}")
        if not isinstance(options, dict) or any(not isinstance(value, str) for value in options.values()):
            raise ValueError("Terminal server volume options must be an object of strings.")
        return cls(project, *numbers, env["LOAF_TERMINAL_VOLUME_DRIVER"], options, bridge_options)

    def asset_name(self, key):
        project = hashlib.sha256(self.project.encode()).hexdigest()[:16]
        return f"loaf-tools-{project}-{key}"

    def labels(self, key):
        return {PROJECT: self.project, OWNER: key}

    def runner_name(self, key, role):
        return self.asset_name(key) + ("" if role == USER_ROLE else "-" + role)

    def runner_labels(self, key, role):
        return {**self.labels(key), ROLE: role}

    def runner_identity(self, labels, name):
        """Return the workspace key and role of a runner network, or None for a foreign network."""
        key = labels.get(OWNER, "")
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            return None
        role = labels.get(ROLE)
        if role is None and labels == self.labels(key) and name == self.asset_name(key):
            return key, None  # A runner network from an earlier release.
        if role in ROLES and labels == self.runner_labels(key, role) and name == self.runner_name(key, role):
            return key, role
        return None

    def chat_volume(self, key):
        return f"{self.asset_name(key)}-chat"

    def chat_labels(self, chat, owner_key):
        return {CHAT_PROJECT: self.project, CHAT_ID: chat, CHAT_OWNER: owner_key}


class ChatPackages:
    def __init__(self, policy, docker, source=None, repository=None):
        self.policy = policy
        self.docker = docker
        self.source = source
        self.repository = repository

    def tag(self, key):
        return self.policy.asset_name(key)

    def labels(self, key):
        return {APT_PROJECT: self.policy.project, APT_WORKSPACE: key}

    async def image(self, key):
        name = f"{APT_IMAGE_REPOSITORY}:{self.tag(key)}"
        details = await self.docker.call("GET", f"/images/{name}/json", missing=True)
        if details is not None:
            labels = details["Config"].get("Labels") or {}
            if any(labels.get(field) != value for field, value in self.labels(key).items()):
                raise RuntimeError("Chat package image ownership mismatch")
        return details

    async def requested(self, key):
        """Return the package list that defines the chat image, or None for an image without one."""
        details = await self.image(key)
        if details is None:
            return []
        try:
            packages = json.loads((details["Config"].get("Labels") or {}).get(APT_PACKAGES, ""))
        except ValueError:
            return None  # Chat images from earlier releases do not record their packages.
        if not isinstance(packages, list) or any(
            not isinstance(name, str) or not APT_PACKAGE.fullmatch(name) for name in packages
        ):
            return None
        return packages

    async def remove(self, key):
        if await self.image(key):
            name = f"{APT_IMAGE_REPOSITORY}:{self.tag(key)}"
            await self.docker.call("DELETE", f"/images/{name}", missing=True)

    async def clear_installers(self):
        filters = json.dumps({"label": [f"{APT_PROJECT}={self.policy.project}"]})
        containers = await self.docker.call("GET", "/containers/json", params={"all": "true", "filters": filters})
        for container in containers:
            key = container["Labels"].get(APT_WORKSPACE, "")
            if re.fullmatch(r"[0-9a-f]{64}", key) and f"/{self.tag(key)}-apt" in container["Names"]:
                await self.docker.call("DELETE", f"/containers/{container['Id']}", params={"force": "true"}, missing=True)

    async def temporary_images(self):
        filters = json.dumps({"label": [f"{APT_PROJECT}={self.policy.project}", f"{APT_TEMPORARY}=true"]})
        images = await self.docker.call("GET", "/images/json", params={"filters": filters})
        for image in images:
            key = (image["Labels"] or {}).get(APT_WORKSPACE, "")
            if re.fullmatch(r"[0-9a-f]{64}", key):
                if f"{APT_IMAGE_REPOSITORY}:{self.tag(key)}" in (image["RepoTags"] or ()):
                    yield key

    @asynccontextmanager
    async def install(self, key, image, packages):
        mounts = [self.source]
        if self.repository:
            mounts.append({"Type": "bind", "Source": self.repository, "Target": APT_REPOSITORY, "ReadOnly": True})
        # The image's apt and apt-get commands on PATH only explain how chat packages work.
        script = """\
export DEBIAN_FRONTEND=noninteractive
source_file=$1
shift
apt_local() {
    /usr/bin/apt-get -o Dir::Etc::sourcelist="$source_file" -o Dir::Etc::sourceparts=- "$@"
}
apt_local -o APT::Update::Error-Mode=any update
apt_local -y --no-remove install -- "$@"
apt_local clean
rm -rf /var/lib/apt/lists/*
"""
        created = await self.docker.call("POST", "/containers/create", params={"name": self.tag(key) + "-apt"}, body={
            "Image": image, "Labels": self.labels(key), "User": "0:0", "WorkingDir": "/",
            "Entrypoint": ["/bin/sh", "-ec", script, "loaf-apt"], "Cmd": [self.source["Target"], *packages],
            "Tty": True, "Healthcheck": {"Test": ["NONE"]},
            "HostConfig": {
                "NetworkMode": "none" if self.repository else "bridge", "Mounts": mounts,
                "SecurityOpt": ["no-new-privileges:true"], "PidsLimit": 256,
                "NanoCpus": self.policy.cpus * 1_000_000_000,
                "Memory": self.policy.memory_mib * 1024 * 1024,
                "MemorySwap": self.policy.memory_mib * 1024 * 1024,
                "LogConfig": {"Type": "local", "Config": {"max-size": "1m", "max-file": "2"}},
            },
        })
        container = created["Id"]
        path = f"/containers/{container}"
        try:
            await self.docker.call("POST", f"{path}/start")
            result = await self.docker.call("POST", f"{path}/wait", timeout=self.policy.command_seconds)
            if result["StatusCode"]:
                log = await self.docker.call("GET", f"{path}/logs", text=True,
                                             params={"stdout": "true", "stderr": "true", "tail": "20"})
                raise web.HTTPUnprocessableEntity(text=log or "APT failed against the configured mirror.")
            yield container
        finally:
            await self.docker.call("DELETE", path, params={"force": "true"}, missing=True)

    async def publish(self, key, container, base_image, packages, *, saved):
        base = await self.docker.call("GET", f"/images/{base_image}/json")
        config = base["Config"]
        config["Labels"] = {**(config.get("Labels") or {}), **self.labels(key), APT_TEMPORARY: str(not saved).lower(),
                            APT_PACKAGES: json.dumps(packages)}
        await self.docker.call("POST", "/commit", body=config, timeout=self.policy.command_seconds, params={
            "container": container, "repo": APT_IMAGE_REPOSITORY, "tag": self.tag(key), "pause": "false",
        })


@dataclass
class Workspace:
    container: str
    network: str
    address: str
    credential: str
    active: int = 0
    used: float = 0
    # The chat's working directory as last applied to this runner.
    directory: str | None = None

    def url(self, path, query=None):
        return URL.build(scheme="http", host=self.address, port=PORT, path=path, query=query)

    def headers(self, user, headers):
        forwarded = {name: headers[name] for name in REQUEST_HEADERS if name in headers}
        return {**forwarded, "X-User-Id": user, "Authorization": "Bearer " + self.credential}


def account_workspace_key(user, session):
    return workspace_key(hashlib.sha256(user.encode()).hexdigest(), session)


class Workspaces:
    def __init__(self, policy, docker, client, provisioner, image_id, chats, *, runtime_mounts=(), runtime_revision=None,
                 packages=None):
        self.policy = policy
        self.docker = docker
        self.client = client
        self.provisioner = provisioner
        self.image_id = image_id
        self.chats = chats
        self.runtime_mounts = runtime_mounts
        self.runtime_revision = runtime_revision
        self.packages = packages
        self.installing = set()
        self.temporary_packages = {}
        # Temporary chats share one chat volume between their runners until they go idle.
        self.temporary_chats = {}
        # The working directory selected for each workspace, applied to both of its runners.
        self.directories = {}
        # Commands the account started itself run in its user runner, all others in the agent runner.
        self.user_processes = {}
        # Runners by (workspace key, role). A workspace occupies one slot for up to two runners.
        self.active = {}
        self.lock = asyncio.Lock()

    def keys(self):
        return {key for key, _ in self.active}

    def has_slot(self, key):
        keys = self.keys()
        return key in keys or len(keys) < self.policy.maximum

    async def initialize(self):
        if self.packages:
            await self.packages.clear_installers()
            self.temporary_packages.update({key: monotonic() async for key in self.packages.temporary_images()})
        filters = json.dumps({"label": [f"{PROJECT}={self.policy.project}"]})
        records = await self.docker.call("GET", "/containers/json", params={
            "all": "true", "filters": filters,
        })
        for record in records:
            key, role = record["Labels"].get(OWNER, ""), record["Labels"].get(ROLE)
            if not re.fullmatch(r"[0-9a-f]{64}", key):
                continue
            if role not in ROLES:
                # Runners from earlier releases ran model commands with a writable user folder.
                if "/" + self.policy.asset_name(key) in record["Names"]:
                    await self.remove(record["Id"], self.policy.asset_name(key))
                continue
            name = self.policy.runner_name(key, role)
            if "/" + name not in record["Names"]:
                continue
            container = await self.docker.call("GET", f"/containers/{record['Id']}/json")
            image = await self.workspace_image(key)
            if (container["State"]["Running"] and container["Image"] == image
                    and container["Config"]["Labels"].get(RUNTIME_MOUNTS) == self.runtime_revision
                    and self.has_slot(key)):
                self.active[(key, role)] = await self.recover(key, role, container)
            else:
                await self.remove(record["Id"], name)
        networks = await self.docker.call("GET", "/networks", params={"filters": filters})
        for network in networks:
            identity = self.policy.runner_identity(network["Labels"] or {}, network["Name"])
            if identity is not None and identity not in self.active:
                await self.remove(None, network["Name"])
        chat_filters = json.dumps({"label": [f"{CHAT_PROJECT}={self.policy.project}"]})
        listing = await self.docker.call("GET", "/volumes", params={"filters": chat_filters})
        for volume in listing.get("Volumes") or ():
            labels = volume["Labels"] or {}
            chat, owner_key = labels.get(CHAT_ID, ""), labels.get(CHAT_OWNER, "")
            if (valid_session(chat) and chat.startswith(NON_SAVED_PREFIXES) and isinstance(owner_key, str)
                    and re.fullmatch(r"[0-9a-f]{64}", owner_key) and labels == self.policy.chat_labels(chat, owner_key)
                    and volume["Name"] == self.policy.chat_volume(workspace_key(owner_key, chat))):
                self.temporary_chats[workspace_key(owner_key, chat)] = monotonic()

    async def connect(self, network, labels):
        details = await self.docker.call("GET", f"/networks/{network}")
        if details["Labels"] != labels:
            raise RuntimeError("Workspace network ownership mismatch")
        options = details["Options"] or {}
        if (
            details["Driver"] != "bridge" or not details["Internal"] or details["EnableIPv6"]
            or options.get(BRIDGE_NAME)
            or any(options.get(key) != value for key, value in self.policy.bridge_options.items())
        ):
            raise RuntimeError("Workspace network lacks the required isolation")
        if self.provisioner not in details.get("Containers", {}):
            await self.docker.call("POST", f"/networks/{network}/connect", body={"Container": self.provisioner})
        return details

    async def recover(self, key, role, container):
        network = self.policy.runner_name(key, role)
        endpoints = container["NetworkSettings"]["Networks"]
        if set(endpoints) != {network}:
            raise RuntimeError("Workspace is attached to another network")
        await self.connect(network, self.policy.runner_labels(key, role))
        credential = next(value.split("=", 1)[1] for value in container["Config"]["Env"]
                          if value.startswith("OPEN_TERMINAL_API_KEY="))
        return Workspace(container["Id"], network, str(ipaddress.IPv4Address(endpoints[network]["IPAddress"])),
                         credential, used=monotonic())

    async def ensure_volume(self, name, labels):
        volume = await self.docker.call("POST", "/volumes/create", body={
            "Name": name, "Labels": labels, "Driver": self.policy.volume_driver, "DriverOpts": self.policy.volume_options,
        })
        if (volume["Labels"] != labels or volume["Driver"] != self.policy.volume_driver
                or (volume["Options"] or {}) != self.policy.volume_options):
            raise RuntimeError("Workspace volume ownership or storage policy mismatch")

    async def workspace_image(self, key):
        if self.packages and (image := await self.packages.image(key)):
            return image["Id"]
        return self.image_id

    async def create(self, key, role, owner_key, session, *, saved):
        name = self.policy.runner_name(key, role)
        labels = self.policy.runner_labels(key, role)
        home_volume = self.policy.asset_name(owner_key)
        await self.ensure_volume(home_volume, self.policy.labels(owner_key))
        chat_volume = None
        if session:
            chat_volume = self.policy.chat_volume(key)
            await self.ensure_volume(chat_volume, self.policy.chat_labels(session, owner_key))
            if not saved:
                self.temporary_chats[key] = monotonic()
        params = {"Name": name, "Driver": "bridge", "Internal": True, "EnableIPv6": False,
                  "Options": self.policy.bridge_options, "Labels": labels}
        network = await self.docker.call("GET", f"/networks/{name}", missing=True)
        if network and network["Labels"] != labels:
            raise RuntimeError("Workspace network ownership mismatch")
        container = None
        try:
            if not network:
                await self.docker.call("POST", "/networks/create", body=params)
            await self.connect(name, labels)
            credential = secrets.token_urlsafe(32)
            created = await self.docker.call(
                "POST", "/containers/create", params={"name": name},
                body=self.container_spec(name, labels, credential, home_volume, role=role, chat_volume=chat_volume,
                                         image=await self.workspace_image(key)),
            )
            container = created["Id"]
            await self.docker.call("POST", f"/containers/{container}/start")
            info = await self.docker.call("GET", f"/containers/{container}/json")
            endpoint = info["NetworkSettings"]["Networks"][name]
            return Workspace(container, name, str(ipaddress.IPv4Address(endpoint["IPAddress"])),
                             credential, used=monotonic())
        except (Exception, asyncio.CancelledError):
            await self.remove(container, name)
            raise

    def container_spec(self, name, labels, credential, home_volume, *, role=USER_ROLE, chat_volume=None, image=None):
        agent = role == AGENT_ROLE
        # Model tool calls can read the account's folder but write only to the chat folder and /tmp.
        mounts = [{"Type": "volume", "Source": home_volume, "Target": WORKSPACE_HOME, "ReadOnly": agent}]
        runner = "/opt/loaf-terminal/runner.py"
        if self.runtime_mounts:
            mounts.extend(self.runtime_mounts)
            labels = {**labels, RUNTIME_MOUNTS: self.runtime_revision}
            runner = RUNTIME_RUNNER
        tmpfs = {"/tmp": "rw,nosuid,nodev,mode=1777,size=256m"}
        if chat_volume:
            mounts.append({"Type": "volume", "Source": chat_volume, "Target": CHAT_HOME})
        elif agent:
            # Tool calls without a chat get scratch space. A draft's user runner keeps the empty, read-only folder.
            tmpfs[CHAT_HOME] = "rw,nosuid,nodev,mode=0700,uid=1000,gid=1000,size=256m"
        # Open Terminal confines its start directory to the home. A chat's default directory is set by lease().
        home = CHAT_HOME if agent else WORKSPACE_HOME
        return {
            # Open Terminal uses the process working directory as its single-user home.
            "Image": image or self.image_id, "Labels": labels, "User": "1000:1000", "WorkingDir": home,
            "Entrypoint": ["/usr/local/bin/python", runner],
            "Cmd": [],
            "Env": [
                f"HOME={home}", f"OPEN_TERMINAL_START_DIR={home}",
                # A draft must be able to select /home/chat before WebUI creates the chat.
                f"OPEN_TERMINAL_FILE_BROWSER_ROOT={WORKSPACE_ROOT}", "OPEN_TERMINAL_API_KEY=" + credential,
                f"OPEN_TERMINAL_COMMAND_SECONDS={self.policy.command_seconds}",
                "OPEN_TERMINAL_MAX_LOG_SIZE=4194304",
            ],
            "HostConfig": {
                "Init": True, "NetworkMode": name, "ReadonlyRootfs": True,
                "SecurityOpt": ["no-new-privileges:true"], "CapDrop": ["ALL"], "PidsLimit": 256,
                "NanoCpus": self.policy.cpus * 1_000_000_000,
                "Memory": self.policy.memory_mib * 1024 * 1024,
                "MemorySwap": self.policy.memory_mib * 1024 * 1024,
                "Dns": ["127.0.0.1"],
                # Docker's network flag controls IPv6 allocation. Also suppress kernel link-local addresses.
                "Sysctls": {"net.ipv6.conf.all.disable_ipv6": "1", "net.ipv6.conf.default.disable_ipv6": "1"}
                if Path("/proc/sys/net/ipv6/conf/all/disable_ipv6").is_file() else {},
                "Tmpfs": tmpfs, "Mounts": mounts,
                "LogConfig": {"Type": "local", "Config": {"max-size": "5m", "max-file": "2"}},
            },
        }

    async def remove(self, container, network):
        if container:
            await self.docker.call("DELETE", f"/containers/{container}", params={"force": "true"}, missing=True)
        # The provisioner may have been replaced while a worker was stopped.
        details = await self.docker.call("GET", f"/networks/{network}", missing=True)
        if details and self.policy.runner_identity(details["Labels"] or {}, network) is None:
            raise RuntimeError("Workspace network ownership mismatch")
        if details and self.provisioner in details.get("Containers", {}):
            await self.docker.call("POST", f"/networks/{network}/disconnect",
                                   body={"Container": self.provisioner})
        if details:
            await self.docker.call("DELETE", f"/networks/{network}", missing=True)

    async def retire(self, key, user, session):
        # The caller holds the lock. The next request starts runners from the changed image.
        for role in ROLES:
            workspace = self.active.get((key, role))
            if workspace is None:
                continue
            if role == USER_ROLE and key not in self.directories:
                await self.remember_directory(key, workspace, user, session)
            await self.remove(workspace.container, workspace.network)
            del self.active[(key, role)]

    async def check_chat(self, user, session, saved):
        if saved and (await self.chats.owners((session,))).get(session) != user:
            raise web.HTTPNotFound(text="Chat not found.")

    async def change_packages(self, user, session, operations):
        """Rebuild the chat image from the shared image with the changed package list."""
        if not session or session.startswith("channel:"):
            raise web.HTTPBadRequest(text="Open a chat before changing its packages.")
        saved = not session.startswith(NON_SAVED_PREFIXES)
        owner_key = hashlib.sha256(user.encode()).hexdigest()
        key = workspace_key(owner_key, session)
        async with self.lock:
            await self.check_chat(user, session, saved)
            if key in self.installing or len(self.installing) >= self.policy.maximum:
                raise web.HTTPConflict(text="Package installation is busy. Try again later.")
            current = await self.packages.requested(key)
            packages = plan_packages(current or [], operations)
            if packages == current:
                return {"packages": packages, "changed": False}
            await self.ensure_volume(self.policy.chat_volume(key), self.policy.chat_labels(session, owner_key))
            if not saved:
                self.temporary_chats[key] = monotonic()
            self.installing.add(key)
        try:
            if packages:
                async with self.packages.install(key, self.image_id, packages) as installer:
                    async with self.lock:
                        await self.check_chat(user, session, saved)
                        await self.retire(key, user, session)
                    await self.packages.publish(key, installer, self.image_id, packages, saved=saved)
                if not saved:
                    self.temporary_packages[key] = monotonic()
            else:
                async with self.lock:
                    await self.check_chat(user, session, saved)
                    await self.retire(key, user, session)
                await self.packages.remove(key)
                self.temporary_packages.pop(key, None)
        finally:
            self.installing.discard(key)
            if key in self.temporary_chats:
                self.temporary_chats[key] = monotonic()
        # An image from an earlier release did not record its packages and was rebuilt without them.
        return {"packages": packages, "changed": True, "untracked_reset": current is None}

    async def remember_directory(self, key, workspace, user, session):
        headers = workspace.headers(user, {"X-Session-Id": session})
        with suppress(ClientError, TimeoutError, ValueError):
            async with self.client.get(workspace.url("/files/cwd"), headers=headers, timeout=ClientTimeout(total=3),
                                       allow_redirects=False) as response:
                if response.status == 200:
                    directory = (await response.json()).get("cwd")
                    if isinstance(directory, str):
                        self.directories[key] = directory

    def record_directory(self, user, session, workspace, directory):
        self.directories[account_workspace_key(user, session)] = workspace.directory = directory

    def record_process(self, user, session, process):
        history = self.user_processes.setdefault(account_workspace_key(user, session),
                                                 deque(maxlen=USER_PROCESS_HISTORY))
        history.append(process)

    def command_role(self, user, session, path):
        """Route a command's status, input and removal to the runner that started it."""
        parts = path.split("/")
        processes = self.user_processes.get(account_workspace_key(user, session), ())
        return USER_ROLE if len(parts) > 2 and parts[2] in processes else AGENT_ROLE

    async def apply_directory(self, key, workspace, user, session):
        # Open Terminal changes a session's directory only through this request.
        directory = self.directories.get(key)
        if directory is None or workspace.directory == directory:
            return
        workspace.directory = directory
        headers = workspace.headers(user, {"X-Session-Id": session})
        with suppress(ClientError, TimeoutError):
            async with self.client.post(workspace.url("/files/cwd"), headers=headers, json={"path": directory},
                                        timeout=ClientTimeout(total=3), allow_redirects=False):
                pass

    @asynccontextmanager
    async def lease(self, user, session="", role=USER_ROLE):
        owner_key = hashlib.sha256(user.encode()).hexdigest()
        key = workspace_key(owner_key, session)
        slot = (key, role)
        saved = bool(session) and not session.startswith(NON_SAVED_PREFIXES)
        async with self.lock:
            await self.check_chat(user, session, saved)
            if key in self.installing:
                raise web.HTTPServiceUnavailable(text="The chat is changing its packages. Try again shortly.")
            workspace = self.active.get(slot)
            if workspace is None:
                if not self.has_slot(key):
                    await self.reclaim_idle(one=True)
                if not self.has_slot(key):
                    raise web.HTTPServiceUnavailable(text="All terminal workspace slots are in use. Try again later.")
                workspace = self.active[slot] = await self.create(key, role, owner_key, session, saved=saved)
                if session:
                    # Both runners of a chat start in its folder unless the account selected another one.
                    self.directories.setdefault(key, CHAT_HOME)
            workspace.active += 1
        ready = False
        try:
            deadline = monotonic() + 30
            while monotonic() < deadline:
                try:
                    async with self.client.get(workspace.url("/health"), timeout=ClientTimeout(total=1),
                                               allow_redirects=False) as health:
                        if health.status == 200:
                            ready = True
                            break
                except (ClientError, TimeoutError):
                    pass
                await asyncio.sleep(0.25)
            else:
                raise web.HTTPServiceUnavailable(text="Workspace is unavailable. Try again.")
            await self.apply_directory(key, workspace, user, session)
            yield workspace
        finally:
            workspace.active -= 1
            workspace.used = monotonic()
            if key in self.temporary_chats:
                self.temporary_chats[key] = workspace.used
            if not ready and not workspace.active:
                async with self.lock:
                    if not workspace.active and self.active.get(slot) is workspace:
                        await self.remove(workspace.container, workspace.network)
                        del self.active[slot]

    async def reap_idle(self):
        async with self.lock:
            await self.reclaim_idle()
            keys = self.keys()
            for key, installed in list(self.temporary_packages.items()):
                if key in keys or key in self.installing or monotonic() - installed < self.policy.idle_seconds:
                    continue
                await self.packages.remove(key)
                del self.temporary_packages[key]
            for key, used in list(self.temporary_chats.items()):
                if key in keys or key in self.installing or monotonic() - used < self.policy.idle_seconds:
                    continue
                await self.discard_temporary_chat(key)

    async def discard_temporary_chat(self, key):
        await self.docker.call("DELETE", f"/volumes/{self.policy.chat_volume(key)}", missing=True)
        del self.temporary_chats[key]
        self.directories.pop(key, None)
        self.user_processes.pop(key, None)

    async def reap_deleted_chats(self):
        async with self.lock:
            filters = json.dumps({"label": [f"{CHAT_PROJECT}={self.policy.project}"]})
            listing = await self.docker.call("GET", "/volumes", params={"filters": filters})
            chat_volumes = []
            for volume in listing.get("Volumes") or ():
                labels = volume["Labels"] or {}
                chat, owner_key = labels.get(CHAT_ID, ""), labels.get(CHAT_OWNER, "")
                if (not valid_session(chat) or chat.startswith(NON_SAVED_PREFIXES)
                        or not isinstance(owner_key, str) or not re.fullmatch(r"[0-9a-f]{64}", owner_key)
                        or labels != self.policy.chat_labels(chat, owner_key)):
                    continue
                key = workspace_key(owner_key, chat)
                if volume["Name"] == self.policy.chat_volume(key):
                    chat_volumes.append((chat, key, volume["Name"]))
            if not chat_volumes:
                return
            owners = await self.chats.owners({chat for chat, _, _ in chat_volumes})
            for chat, key, volume in chat_volumes:
                if chat in owners or key in self.installing:
                    continue
                try:
                    for role in ROLES:
                        workspace = self.active.get((key, role))
                        if workspace:
                            await self.remove(workspace.container, workspace.network)
                            del self.active[(key, role)]
                    if self.packages:
                        await self.packages.remove(key)
                    self.directories.pop(key, None)
                    self.user_processes.pop(key, None)
                    await self.docker.call("DELETE", f"/volumes/{volume}", missing=True)
                except (RuntimeError, ClientError, TimeoutError):
                    LOG.warning("Could not remove a deleted chat's terminal workspace")

    async def idle(self, key, workspace):
        if key in self.installing or workspace.active or monotonic() - workspace.used < self.policy.idle_seconds:
            return False
        processes = await self.docker.call("GET", f"/containers/{workspace.container}/top",
                                           params={"ps_args": "-eo pid"}, missing=True)
        # The init process and Open Terminal itself are the only idle processes.
        return not (processes and len(processes["Processes"] or []) > 2)

    async def reclaim_idle(self, *, one=False):
        # The caller holds the lock so a runner cannot be leased during removal.
        for key in list(dict.fromkeys(key for key, _ in self.active)):
            runners = [(slot, workspace) for slot, workspace in self.active.items() if slot[0] == key]
            idle = [(slot, workspace) for slot, workspace in runners if await self.idle(key, workspace)]
            if one and len(idle) < len(runners):
                continue  # Only a workspace without busy runners frees its slot.
            for slot, workspace in idle:
                await self.remove(workspace.container, workspace.network)
                del self.active[slot]
            if one and idle:
                return

    async def sweep(self):
        while True:
            await asyncio.sleep(min(5, self.policy.idle_seconds))
            try:
                await self.reap_idle()
            except (RuntimeError, ClientError, TimeoutError):
                LOG.warning("Could not clean up idle terminal workspaces")
            try:
                await self.reap_deleted_chats()
            except (DockerError, ClientError, TimeoutError, sqlite3.Error):
                LOG.warning("Could not check for deleted Open WebUI chats")

    async def close(self):
        results = await asyncio.gather(
            *(self.remove(item.container, item.network) for item in self.active.values()),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results):
            LOG.warning("Some terminal workspaces will need reconciliation after restart")
        results = await asyncio.gather(
            *(self.docker.call("DELETE", f"/volumes/{self.policy.chat_volume(key)}", missing=True)
              for key in self.temporary_chats),
            *(self.packages.remove(key) for key in self.temporary_packages),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results):
            LOG.warning("Some temporary chat folders or package images will need cleanup after restart")


def same_secret(received, secret):
    return isinstance(received, str) and secrets.compare_digest(received.encode(), secret.encode())


def owner(request):
    user = request.headers.get("X-User-Id", "")
    if len(request.headers.getall("X-User-Id", [])) != 1 or not re.fullmatch(r"[!-~]{1,128}", user):
        raise web.HTTPForbidden(text="A verified Open WebUI account is required.")
    return user


def valid_session(value):
    return isinstance(value, str) and re.fullmatch(r"[!-~]{1,128}", value) is not None


def terminal_session(request):
    sessions = request.headers.getall("X-Session-Id", [])
    if len(sessions) > 1 or (sessions and sessions[0] and not valid_session(sessions[0])):
        raise web.HTTPBadRequest(text="Invalid terminal session.")
    return sessions[0] if sessions else ""


def authenticate_http(request, secret):
    headers = request.headers.getall("Authorization", [])
    if len(headers) != 1 or not same_secret(headers[0], "Bearer " + secret):
        raise web.HTTPUnauthorized()


def permitted_path(path):
    parts = path.split("/")[1:]
    if any(part in (".", "..") for part in parts) or "\\" in path or "%" in path:
        return False
    if not parts or parts[0] not in PROXY_ROOTS:
        return False
    return parts[0] != "api" or path == "/api/terminals" or path.startswith("/api/terminals/")


async def working_directory_listing(response, directory):
    if response.status == 404:
        listing = {"dir": directory, "writable": True, "entries": []}
    elif (response.status == 200 and response.content_type == "application/json"
          and response.content_length is not None and response.content_length <= FOLDER_HISTORY_MAX_BYTES):
        listing = await response.json()
    else:
        return None

    entries = listing["entries"]
    # WebUI takes the eight newest records. Keep the workspace choices first.
    newest = max((entry.get("modified") or 0 for entry in entries), default=0)
    choices = [
        {"name": name, "type": "file", "size": len(content), "modified": newest}
        for name, content in WORKING_DIRECTORY_RECORDS.items()
    ]
    listing["entries"] = choices + [entry for entry in entries if entry["name"] not in WORKING_DIRECTORY_RECORDS]
    return web.json_response(listing)


def agent_path(path):
    """Return the runner path of a published model tool, or None for any other path."""
    inner = path[len(AGENT_PREFIX):]
    if not path.startswith(AGENT_PREFIX + "/") or not permitted_path(inner) or inner in AGENT_HIDDEN_TOOLS:
        return None
    # Model tools cannot open interactive shells; they may only read the account's shell output.
    if inner.startswith("/api/") and inner != AGENT_READS_USER_SHELL:
        return None
    return inner


def package_operation(operation_id, summary, description):
    return {"post": {
        "operationId": operation_id, "summary": summary, "description": description,
        "requestBody": {"required": True, "content": {"application/json": {"schema": {
            "type": "object", "required": ["packages"], "properties": {"packages": {
                "type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": APT_MAX_PACKAGES,
                "description": "Debian package names, optionally with an architecture or version.",
            }},
        }}}},
        "responses": {"200": {"description": "The packages installed for this chat"}},
    }}


def agent_schema(paths, install_enabled):
    """Publish the model tools under the agent prefix, which routes them to the agent runner."""
    tools = {
        AGENT_PREFIX + path: operations for path, operations in paths.items()
        if permitted_path(path) and path not in AGENT_HIDDEN_TOOLS
    }
    note = "\n\nCommands run for the assistant: /home/user is read-only, so write files to /home/chat or /tmp."
    if install_enabled:
        note += (" A plain `apt-get install|remove|purge PACKAGE...` command changes the APT packages of this chat."
                 " The chat's terminal then restarts with them.")
        tools[AGENT_PREFIX + APT_INSTALL_PATH] = package_operation(
            "install_chat_packages", "Install APT packages for the current chat",
            "Install packages from the configured APT source for this chat only. Use this before running the "
            "installed tools. The chat's terminal restarts with the packages; its files are kept.")
        tools[AGENT_PREFIX + APT_REMOVE_PATH] = package_operation(
            "remove_chat_packages", "Remove APT packages from the current chat",
            "Remove packages that were installed for this chat. Packages of the shared terminal image stay. "
            "The chat's terminal restarts without them; its files are kept.")
    run = tools.get(AGENT_PREFIX + "/execute", {}).get("post")
    if isinstance(run, dict):
        run["description"] = run.get("description", "") + note
    return tools


async def proxy_http(request, workspace, user, client, path, *, body=None):
    # Open WebUI reads the picker options as per-user, path-hashed history files.
    account = quote(user, safe="!~*'()")
    history = f"{WORKSPACE_HOME}/.open-webui/users/{account}/folders"
    history_request = request.method == "GET" and len(request.query) == 1
    if history_request and path == "/files/read":
        target = request.query.get("path", "")
        directory, _, name = target.rpartition("/")
        if directory == history and name in WORKING_DIRECTORY_RECORDS:
            return web.json_response({"path": target, "content": WORKING_DIRECTORY_RECORDS[name], "total_lines": 1})

    upstream = workspace.url(path, request.query)
    data = body if body is not None else request.content if request.can_read_body else None
    async with client.request(
        request.method, upstream, headers=workspace.headers(user, request.headers), data=data, allow_redirects=False,
    ) as response:
        if history_request and path == "/files/list" and request.query.get("directory") == history:
            listing = await working_directory_listing(response, history)
            if listing is not None:
                return listing
        headers = {name: response.headers[name] for name in RESPONSE_HEADERS if name in response.headers}
        if response.content_type in ("text/html", "image/svg+xml", "application/xhtml+xml"):
            headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        outgoing = web.StreamResponse(status=response.status, headers=headers)
        await outgoing.prepare(request)
        async for chunk in response.content.iter_chunked(64 * 1024):
            await outgoing.write(chunk)
        return outgoing


async def proxy_recorded(request, workspace, user, session, workspaces, path):
    """Forward a request of the account that selects the chat's directory or starts a command."""
    headers = {**workspace.headers(user, request.headers), "Accept-Encoding": "identity"}
    async with workspaces.client.request(
        request.method, workspace.url(path, request.query), headers=headers,
        data=request.content if request.can_read_body else None, allow_redirects=False,
    ) as response:
        payload = await response.read()
        result = None
        if response.status == 200 and response.content_type == "application/json":
            with suppress(ValueError):
                result = json.loads(payload)
        if isinstance(result, dict) and path == "/files/cwd" and isinstance(result.get("cwd"), str):
            workspaces.record_directory(user, session, workspace, result["cwd"])
        elif isinstance(result, dict) and path == "/execute" and isinstance(result.get("id"), str):
            workspaces.record_process(user, session, result["id"])
        forwarded = {name: response.headers[name] for name in RESPONSE_HEADERS
                     if name in response.headers and name != "Content-Length"}
        return web.Response(body=payload, status=response.status, headers=forwarded)


async def change_chat_packages(workspaces, user, session, operations):
    try:
        return await workspaces.change_packages(user, session, operations)
    except (ClientError, RuntimeError, TimeoutError, sqlite3.Error) as error:
        detail = str(error) if isinstance(error, DockerError) else type(error).__name__
        LOG.warning("Chat package change failed (%s)", detail)
        raise web.HTTPServiceUnavailable(text="Chat package installation could not finish.") from error


async def package_request(request, workspaces, path):
    user, session = owner(request), terminal_session(request)
    try:
        body = await request.json()
    except ValueError as error:
        raise web.HTTPBadRequest(text="Expected a JSON object with package names.") from error
    names = body.get("packages") if isinstance(body, dict) else None
    if not isinstance(names, list) or not 1 <= len(names) <= APT_MAX_PACKAGES or any(
        not isinstance(name, str) or len(name) > 128 or not APT_PACKAGE.fullmatch(name) for name in names
    ):
        raise web.HTTPBadRequest(text=f"Supply 1 to {APT_MAX_PACKAGES} Debian package names.")
    names = list(dict.fromkeys(names))
    action = "install" if path == APT_INSTALL_PATH else "remove"
    result = await change_chat_packages(workspaces, user, session, [(action, names)])
    return web.json_response({"installed" if action == "install" else "removed": names, **result})


def package_summary(operations, result):
    lines = []
    if result["changed"]:
        for action, label in (("install", "Installed for this chat"), ("remove", "Removed from this chat")):
            names = [name for kind, group in operations if kind == action for name in group]
            if names:
                lines.append(f"{label}: {', '.join(names)}.")
        lines.append("The chat's terminal restarts with these packages. Files in /home/user and /home/chat are kept.")
        if result.get("untracked_reset"):
            lines.append("Packages that an earlier release installed in this chat were not kept. Install them again "
                         "if needed.")
    else:
        lines.append("The chat's packages are unchanged.")
    lines.append(f"Packages installed for this chat: {', '.join(result['packages']) or 'none'}.")
    return "\n".join(lines) + "\n"


def command_result(command, cwd, exit_code, text):
    """Answer like Open Terminal's run_command for an APT command handled by the provisioner."""
    return web.json_response({
        "id": "packages-" + secrets.token_hex(3), "command": command, "cwd": cwd, "status": "done",
        "exit_code": exit_code, "log_path": "",
        "output": [{"type": "stdout" if exit_code == 0 else "stderr", "data": text}],
        "next_offset": 1, "truncated": False,
    })


async def package_command(workspaces, install_enabled, user, session, payload, operations):
    command = payload["command"]
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else (
        workspaces.directories.get(account_workspace_key(user, session), CHAT_HOME))
    if not install_enabled:
        return command_result(command, cwd, 100, "APT packages cannot be changed here: the terminal server has no "
                                                 "package source configured.\n")
    if not operations:
        return command_result(command, cwd, 0, "Package lists are refreshed whenever packages are installed for "
                                               "this chat. Nothing to change.\n")
    try:
        result = await change_chat_packages(workspaces, user, session, operations)
    except web.HTTPException as error:
        return command_result(command, cwd, 100, (error.text or error.reason).rstrip("\n") + "\n")
    return command_result(command, cwd, 0, package_summary(operations, result))


async def relay(source, destination):
    async for message in source:
        if message.type == WSMsgType.TEXT:
            await destination.send_str(message.data)
        elif message.type == WSMsgType.BINARY:
            await destination.send_bytes(message.data)
        elif message.type == WSMsgType.ERROR:
            break


async def proxy_socket(request, downstream, workspaces, secret):
    payload = await downstream.receive_json(timeout=10)
    if (not isinstance(payload, dict) or payload.get("type") != "auth"
            or not same_secret(payload.get("token"), secret)):
        await downstream.close(code=4001)
        return
    user = owner(request)
    session = terminal_session(request)
    # Command streams follow their command. Interactive shells and events belong to the account's runner.
    role = workspaces.command_role(user, session, request.path) if request.path.startswith("/execute") else USER_ROLE
    async with workspaces.lease(user, session, role) as workspace:
        headers = workspace.headers(user, request.headers)
        async with workspaces.client.ws_connect(
            workspace.url(request.path, request.query), headers=headers, max_msg_size=4 * 1024 * 1024,
        ) as upstream:
            await upstream.send_json({"type": "auth", "token": workspace.credential,
                                      "chat_id": session})
            tasks = [asyncio.create_task(relay(downstream, upstream)), asyncio.create_task(relay(upstream, downstream))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            await downstream.close(code=upstream.close_code or 1000)


def application(workspaces, secret, metadata, control_network=None):
    config = metadata["config"]
    config["features"]["agents"] = False
    install_enabled = workspaces.packages is not None and workspaces.packages.source is not None
    schema = metadata["schema"]
    schema["paths"] = agent_schema(schema["paths"], install_enabled)

    async def dispatch(request):
        if control_network is not None and (
            request.remote is None or ipaddress.IPv4Address(request.remote) not in control_network
        ):
            raise web.HTTPForbidden()
        path = request.path
        if request.method == "GET" and path == "/health":
            return web.json_response({"status": "ok"})
        if request.method == "GET" and path in ("/api/config", "/openapi.json"):
            authenticate_http(request, secret)
            return web.json_response(config if path == "/api/config" else schema)
        # Open WebUI calls model tools at the published agent paths. Its user interface uses the plain paths.
        tool = path.startswith(AGENT_PREFIX + "/")
        if tool:
            if path[len(AGENT_PREFIX):] in (APT_INSTALL_PATH, APT_REMOVE_PATH):
                if request.method != "POST" or not install_enabled:
                    raise web.HTTPNotFound()
                authenticate_http(request, secret)
                return await package_request(request, workspaces, path[len(AGENT_PREFIX):])
            path = agent_path(path)
            if path is None or (path == AGENT_READS_USER_SHELL and request.method != "GET"):
                raise web.HTTPNotFound()
        elif not permitted_path(path):
            raise web.HTTPNotFound()
        downstream = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
        if downstream.can_prepare(request).ok:
            if tool:
                raise web.HTTPNotFound()
            await downstream.prepare(request)
            try:
                await proxy_socket(request, downstream, workspaces, secret)
            except (ValueError, TypeError, web.HTTPBadRequest, web.HTTPForbidden, web.HTTPNotFound):
                await downstream.close(code=4001)
            except (ClientError, DockerError, RuntimeError, TimeoutError, sqlite3.Error, web.HTTPServiceUnavailable):
                await downstream.close(code=1013)
            return downstream
        authenticate_http(request, secret)
        user = owner(request)
        session = terminal_session(request)
        body = None
        if tool:
            role = USER_ROLE if path == AGENT_READS_USER_SHELL else AGENT_ROLE
            if path == "/execute" and request.method == "POST":
                body = await request.read()
                try:
                    payload = json.loads(body)
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and (operations := parse_apt_command(payload.get("command"))) is not None:
                    return await package_command(workspaces, install_enabled, user, session, payload, operations)
        elif path.startswith("/execute") and not (path == "/execute" and request.method == "POST"):
            role = workspaces.command_role(user, session, path)
        else:
            role = USER_ROLE
        try:
            async with workspaces.lease(user, session, role) as workspace:
                if role == USER_ROLE and request.method == "POST" and path in ("/files/cwd", "/execute"):
                    return await proxy_recorded(request, workspace, user, session, workspaces, path)
                return await proxy_http(request, workspace, user, workspaces.client, path, body=body)
        except (ClientError, DockerError, RuntimeError, TimeoutError, sqlite3.Error) as error:
            LOG.warning("Tool workspace request failed (%s)", type(error).__name__)
            raise web.HTTPServiceUnavailable(text="Tool workspace is unavailable.") from error

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", dispatch)

    async def lifecycle(_):
        sweeper = asyncio.create_task(workspaces.sweep())
        try:
            yield
        finally:
            sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper
            await workspaces.close()

    app.cleanup_ctx.append(lifecycle)
    return app


def workspace_runtime_mounts(container, source_format):
    if source_format not in ("list", "sources"):
        raise ValueError("Use a .list or .sources file for the runtime APT source.")
    targets = (f"/etc/apt/sources.list.d/loaf-terminal.{source_format}", RUNTIME_RUNNER)
    actual = {mount["Destination"]: mount for mount in container["Mounts"]}
    mounts = []
    revision = hashlib.sha256()
    for target in targets:
        mount = actual.get(target, {})
        source = mount.get("Source", "")
        if mount.get("Type") != "bind" or mount.get("RW") is not False or not Path(source).is_absolute():
            raise ValueError(f"Expected a read-only runtime bind mount at {target}.")
        revision.update(os.fsencode(source) + b"\0" + os.fsencode(target) + b"\0")
        content = hashlib.sha256()
        with Path(target).open("rb") as payload:
            for block in iter(lambda: payload.read(65536), b""):
                content.update(block)
        revision.update(content.digest())
        mounts.append({"Type": "bind", "Source": source, "Target": target, "ReadOnly": True})
    return tuple(mounts), revision.hexdigest()


def read_metadata(*, runtime):
    baked = Path("/opt/loaf-terminal/metadata.json")
    if not runtime or baked.is_file():
        return json.loads(baked.read_text())
    # A runtime overlay can use an already-loaded image that supplies Open Terminal.
    with TemporaryDirectory() as folder:
        metadata = Path(folder) / "metadata.json"
        subprocess.run([
            "/usr/local/bin/python", "-c", """\
import asyncio
import json
import sys

from open_terminal.main import app, get_config

with open(sys.argv[1], "w", encoding="utf-8") as output:
    json.dump({"config": asyncio.run(get_config()), "schema": app.openapi()}, output)
""",
            str(metadata),
        ], env={**os.environ, "HOME": folder, "OPEN_TERMINAL_API_KEY": "metadata-only"},
            stdout=subprocess.DEVNULL, check=True, timeout=30)
        return json.loads(metadata.read_text())


def read_terminal_key(path):
    with path.open("rb") as source:
        key = source.read(515)
    if not re.fullmatch(rb"[!-~]{32,512}(?:\r?\n)?", key):
        raise ValueError("The terminal server secret must be 32–512 printable ASCII characters without spaces.")
    return key.rstrip(b"\r\n").decode("ascii")


async def serve(*, allow_legacy_bridge=False):
    secret = read_terminal_key(KEY_FILE)
    source_format = os.environ.get("LOAF_TERMINAL_RUNTIME_APT_SOURCE_FORMAT")
    metadata = read_metadata(runtime=bool(source_format))

    async def reject_redirect(*_):
        raise ClientError("Workspace redirects are disabled")

    trace = TraceConfig()
    trace.on_request_redirect.append(reject_redirect)
    async with (
        ClientSession(connector=UnixConnector(path="/var/run/docker.sock"), timeout=ClientTimeout(total=15)) as engine,
        ClientSession(auto_decompress=False, timeout=ClientTimeout(total=320, connect=5),
                      cookie_jar=DummyCookieJar(), trace_configs=[trace]) as runners,
    ):
        docker = Docker(engine)
        version = (await docker.call("GET", "/version"))["Version"]
        match = re.match(r"(\d+)\.", version)
        if not match:
            raise ValueError(f"Cannot determine Docker Engine version from {version!r}.")
        major = int(match[1])
        if major < 26:
            raise ValueError("The terminal server requires Docker Engine 26 or newer.")
        if major >= 28:
            bridge_options = {GATEWAY: "isolated"}
        elif allow_legacy_bridge:
            bridge_options = {}
        else:
            bridge_options = {INHIBIT_IPV4: "true"}
        control = await docker.call("GET", f"/containers/{os.environ['HOSTNAME']}/json")
        project = control["Config"]["Labels"]["com.docker.compose.project"]
        endpoint = control["NetworkSettings"]["Networks"][project + "_terminal-control"]
        address = ipaddress.IPv4Address(endpoint["IPAddress"])
        network = ipaddress.IPv4Network((address, endpoint["IPPrefixLen"]), strict=False)
        policy = Policy.load(project, os.environ, bridge_options=bridge_options)
        mounts, revision = workspace_runtime_mounts(control, source_format) if source_format else ((), None)
        repository = os.environ.get("LOAF_TERMINAL_APT_REPOSITORY_DIR") or None
        packages = ChatPackages(policy, docker, mounts[0] if mounts else None, repository)
        workspaces = Workspaces(policy, docker, runners, control["Id"], control["Image"], Chats(CHAT_DB),
                                runtime_mounts=mounts, runtime_revision=revision, packages=packages)
        await workspaces.initialize()
        runner = web.AppRunner(application(workspaces, secret, metadata, network), access_log=None, shutdown_timeout=5)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await runner.setup()
        try:
            await web.TCPSite(runner, str(address), PORT).start()
            await stop.wait()
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(serve())
    except (KeyError, OSError, ValueError, RuntimeError, ClientError, TimeoutError, subprocess.SubprocessError) as error:
        LOG.error("Terminal server startup failed: %s", error)
        raise SystemExit(1)
