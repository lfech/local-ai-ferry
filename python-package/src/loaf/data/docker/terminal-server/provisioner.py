"""Route one Open Terminal connection to a Docker container per chat and per account draft."""

import asyncio
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
import signal
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, TraceConfig, UnixConnector, WSMsgType, web
from multidict import MultiDict
from yarl import URL


LOG = logging.getLogger(__name__)
# Ownership is persisted in Docker labels.
PROJECT = "loaf.terminal.project"
OWNER = "loaf.terminal.workspace"
CHAT_ID = "loaf.terminal.chat-id"
CHAT_OWNER = "loaf.terminal.chat-owner"
# The configuration a chat container was created with: runtime mounts, local repository and network access.
REVISION = "loaf.terminal.revision"
USERS = "loaf.terminal.users"
CONNECTED = "loaf.terminal.connected"
HELPER = "loaf.terminal.helper"
NETWORK_MODES = ("isolated", "connected")
APT_REPOSITORY = "/run/loaf-apt/repository"
# Commands run as root so that apt, dpkg and sudo work. These are the capabilities they need.
ROOT_CAPABILITIES = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "SETGID", "SETUID", "SETFCAP", "AUDIT_WRITE")
GATEWAY = "com.docker.network.bridge.gateway_mode_ipv4"
INHIBIT_IPV4 = "com.docker.network.bridge.inhibit_ipv4"
BRIDGE_NAME = "com.docker.network.bridge.name"
# Chat containers on the shared connected network cannot reach each other.
INTER_CONTAINER = "com.docker.network.bridge.enable_icc"
KEY_FILE = Path("/run/secrets/terminal-server-key")
CHAT_DB = Path("/var/lib/loaf-webui/webui.db")
RUNTIME_RUNNER = "/run/loaf-terminal/runner.py"
# An empty source that hides each inherited APT source when the runtime source replaces them.
NO_APT_SOURCE = "/run/loaf-terminal/no-apt-source.list"
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
WORKSPACE_KEY = re.compile(r"[0-9a-f]{64}")
# Open WebUI's terminal proxy gives a request 300 seconds; the default wait leaves time for the rest.
COMMAND_WAIT_MAX = 240
PROCESS_STATUS = re.compile(r"/execute/[^/]+/status")


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
            # Starting a running or stopping a stopped container changes nothing.
            if response.status == 304:
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


def account_key(user):
    return hashlib.sha256(user.encode()).hexdigest()


def account_workspace_key(user, session):
    return workspace_key(account_key(user), session)


def valid_session(value):
    return isinstance(value, str) and re.fullmatch(r"[!-~]{1,128}", value) is not None


def saved_chat(session):
    return bool(session) and not session.startswith(NON_SAVED_PREFIXES)


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
    network: str = "isolated"
    bridge_options: dict[str, str] = field(default_factory=lambda: {GATEWAY: "isolated"})
    # Seconds that run_command and get_process_status wait for a command when the call names no wait.
    command_wait: int = 0

    @classmethod
    def load(cls, project, env, *, bridge_options):
        numbers = [
            int(env["LOAF_TERMINAL_" + name]) for name in
            ("CPUS", "MEMORY_MIB", "MAX_WORKSPACES", "IDLE_SECONDS", "COMMAND_SECONDS")
        ]
        if min(numbers) < 1:
            raise ValueError("Terminal server limits must be positive.")
        command_wait = int(env.get("LOAF_TERMINAL_COMMAND_WAIT_SECONDS") or 0)
        if not 0 <= command_wait <= COMMAND_WAIT_MAX:
            raise ValueError(f"The command wait must be 0 to {COMMAND_WAIT_MAX} seconds.")
        options = json.loads(env.get("LOAF_TERMINAL_VOLUME_OPTIONS") or "{}")
        if not isinstance(options, dict) or any(not isinstance(value, str) for value in options.values()):
            raise ValueError("Terminal server volume options must be an object of strings.")
        network = env.get("LOAF_TERMINAL_NETWORK") or "isolated"
        if network not in NETWORK_MODES:
            raise ValueError("The terminal server network must be isolated or connected.")
        return cls(project, *numbers, env["LOAF_TERMINAL_VOLUME_DRIVER"], options, network, bridge_options,
                   command_wait)

    def asset_name(self, key):
        """Containers, networks and the user volume start with the deployment's Compose project."""
        return f"{self.project}-terminal-{key}"

    def labels(self, key):
        return {PROJECT: self.project, OWNER: key}

    @property
    def users_volume(self):
        return self.asset_name("users")

    @property
    def users_labels(self):
        return {PROJECT: self.project, USERS: "true"}

    @property
    def connected_network(self):
        return self.asset_name("connected")

    @property
    def connected_labels(self):
        return {PROJECT: self.project, CONNECTED: "true"}

    def container_labels(self, key, owner_key, session, revision):
        labels = {**self.labels(key), REVISION: revision}
        if session:
            labels.update({CHAT_ID: session, CHAT_OWNER: owner_key})
        return labels

    def container_identity(self, labels, names):
        """Return the workspace key and chat of a container this deployment owns, or None."""
        key = labels.get(OWNER, "")
        if labels.get(PROJECT) != self.project or not WORKSPACE_KEY.fullmatch(key):
            return None
        if "/" + self.asset_name(key) not in names:
            return None
        session, owner_key = labels.get(CHAT_ID, ""), labels.get(CHAT_OWNER, "")
        if session and not (valid_session(session) and WORKSPACE_KEY.fullmatch(owner_key)
                            and workspace_key(owner_key, session) == key):
            return None
        return key, session

    def network_owned(self, labels, name):
        key = labels.get(OWNER, "")
        return bool(WORKSPACE_KEY.fullmatch(key)) and labels == self.labels(key) and name == self.asset_name(key)


@dataclass
class Workspace:
    container: str
    network: str
    address: str
    credential: str
    active: int = 0
    used: float = 0
    # The working directory as last applied to this container.
    directory: str | None = None
    # The Open WebUI account and chat of the last lease; unknown for a container recovered after a restart.
    user: str | None = None
    session: str = ""

    def url(self, path, query=None):
        return URL.build(scheme="http", host=self.address, port=PORT, path=path, query=query)

    def headers(self, user, headers):
        forwarded = {name: headers[name] for name in REQUEST_HEADERS if name in headers}
        return {**forwarded, "X-User-Id": user, "Authorization": "Bearer " + self.credential}


class Workspaces:
    """One container per chat and one per account draft, kept while the chat exists.

    A container's own filesystem holds /home/chat and the packages installed with apt. /home/user is the
    account's folder in the shared user volume. Idle containers stop and give up their network.
    """

    def __init__(self, policy, docker, client, provisioner, image_id, chats, *, runtime_mounts=(), runtime_revision=None,
                 repository=None):
        self.policy = policy
        self.docker = docker
        self.client = client
        self.provisioner = provisioner
        self.image_id = image_id
        self.chats = chats
        self.runtime_mounts = runtime_mounts
        self.repository = repository
        # Isolated containers reach only the mounted local repository.
        self.package_source = policy.network == "connected" or repository is not None
        self.command_wait = policy.command_wait
        self.revision = hashlib.sha256(json.dumps([runtime_revision, repository, policy.network]).encode()).hexdigest()
        # Running containers by workspace key. Each occupies one of the policy's slots.
        self.active = {}
        # Temporary chats by key with their last use. Their containers end when idle and at shutdown.
        self.temporary = {}
        # The working directory selected for each workspace.
        self.directories = {}
        # Accounts whose folder exists in the user volume.
        self.user_folders = set()
        self.lock = asyncio.Lock()

    def has_slot(self, key):
        return key in self.active or len(self.active) < self.policy.maximum

    async def owned_containers(self):
        filters = json.dumps({"label": [f"{PROJECT}={self.policy.project}"]})
        records = await self.docker.call("GET", "/containers/json", params={"all": "true", "filters": filters})
        owned = []
        for record in records:
            identity = self.policy.container_identity(record["Labels"] or {}, record["Names"])
            if identity is not None:
                owned.append((*identity, record["Id"]))
        return owned

    async def initialize(self):
        await self.ensure_users_volume()
        if self.policy.network == "connected":
            await self.ensure_connected_network()
        helpers = json.dumps({"label": [f"{HELPER}={self.policy.project}"]})
        for record in await self.docker.call("GET", "/containers/json", params={"all": "true", "filters": helpers}):
            await self.docker.call("DELETE", f"/containers/{record['Id']}", params={"force": "true"}, missing=True)
        for key, session, container_id in await self.owned_containers():
            container = await self.docker.call("GET", f"/containers/{container_id}/json")
            temporary = bool(session) and not saved_chat(session)
            if temporary or container["Config"]["Labels"].get(REVISION) != self.revision:
                # Temporary chats end with the provisioner. Changed mounts or network access need a new container.
                await self.remove(container_id, key)
            elif container["State"]["Running"] and self.has_slot(key):
                self.active[key] = await self.recover(key, container)
            else:
                await self.park(key, container_id)
        filters = json.dumps({"label": [f"{PROJECT}={self.policy.project}"]})
        for network in await self.docker.call("GET", "/networks", params={"filters": filters}):
            labels = network["Labels"] or {}
            if self.policy.network_owned(labels, network["Name"]) and labels[OWNER] not in self.active:
                await self.remove_network(labels[OWNER])

    async def ensure_users_volume(self):
        volume = await self.docker.call("POST", "/volumes/create", body={
            "Name": self.policy.users_volume, "Labels": self.policy.users_labels,
            "Driver": self.policy.volume_driver, "DriverOpts": self.policy.volume_options,
        })
        if (volume["Labels"] != self.policy.users_labels or volume["Driver"] != self.policy.volume_driver
                or (volume["Options"] or {}) != self.policy.volume_options):
            raise RuntimeError("Terminal user volume ownership or storage policy mismatch")

    async def ensure_connected_network(self):
        name, labels = self.policy.connected_network, self.policy.connected_labels
        network = await self.docker.call("GET", f"/networks/{name}", missing=True)
        if network is None:
            await self.docker.call("POST", "/networks/create", body={
                "Name": name, "Driver": "bridge", "Internal": False, "EnableIPv6": False, "Labels": labels,
                "Options": {INTER_CONTAINER: "false"},
            })
        elif (network["Labels"] != labels or network["Driver"] != "bridge" or network["Internal"]
              or (network["Options"] or {}).get(INTER_CONTAINER) != "false"):
            raise RuntimeError("Terminal connected network ownership or isolation mismatch")

    async def ensure_user_folder(self, owner_key):
        if owner_key in self.user_folders:
            return
        # Docker refuses a missing volume subpath, so a short-lived container creates the account's folder.
        created = await self.docker.call("POST", "/containers/create", params={
            "name": self.policy.asset_name(owner_key) + "-folder",
        }, body={
            "Image": self.image_id, "User": "0:0", "Labels": {HELPER: self.policy.project},
            "Entrypoint": ["install", "-d", "-m", "0700", f"/users/{owner_key}"], "Cmd": [],
            "Healthcheck": {"Test": ["NONE"]},
            "HostConfig": {
                "NetworkMode": "none", "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"], "PidsLimit": 16,
                "Mounts": [{"Type": "volume", "Source": self.policy.users_volume, "Target": "/users"}],
                "LogConfig": {"Type": "local", "Config": {"max-size": "64k", "max-file": "2"}},
            },
        })
        path = f"/containers/{created['Id']}"
        try:
            await self.docker.call("POST", f"{path}/start")
            result = await self.docker.call("POST", f"{path}/wait", timeout=30)
            if result["StatusCode"]:
                raise RuntimeError("Could not create the account's terminal folder")
        finally:
            await self.docker.call("DELETE", path, params={"force": "true"}, missing=True)
        self.user_folders.add(owner_key)

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

    async def ensure_network(self, key):
        """Create the container's internal API bridge, shared only with the provisioner."""
        name, labels = self.policy.asset_name(key), self.policy.labels(key)
        network = await self.docker.call("GET", f"/networks/{name}", missing=True)
        if network and network["Labels"] != labels:
            raise RuntimeError("Workspace network ownership mismatch")
        if not network:
            await self.docker.call("POST", "/networks/create", body={
                "Name": name, "Driver": "bridge", "Internal": True, "EnableIPv6": False,
                "Options": self.policy.bridge_options, "Labels": labels,
            })
        await self.connect(name, labels)
        return name

    def expected_networks(self, network):
        return {network, self.policy.connected_network} if self.policy.network == "connected" else {network}

    def workspace(self, key, container):
        network = self.policy.asset_name(key)
        endpoints = container["NetworkSettings"]["Networks"]
        if set(endpoints) != self.expected_networks(network):
            raise RuntimeError("Workspace is attached to another network")
        credential = next(value.split("=", 1)[1] for value in container["Config"]["Env"]
                          if value.startswith("OPEN_TERMINAL_API_KEY="))
        return Workspace(container["Id"], network, str(ipaddress.IPv4Address(endpoints[network]["IPAddress"])),
                         credential, used=monotonic())

    async def recover(self, key, container):
        await self.connect(self.policy.asset_name(key), self.policy.labels(key))
        return self.workspace(key, container)

    async def open(self, key, owner_key, session):
        """Start the workspace's container, or create it on first use."""
        existing = await self.docker.call("GET", f"/containers/{self.policy.asset_name(key)}/json", missing=True)
        if existing is not None:
            identity = self.policy.container_identity(existing["Config"]["Labels"] or {}, [existing["Name"]])
            if identity != (key, session):
                raise RuntimeError("Workspace container ownership mismatch")
            if existing["Config"]["Labels"].get(REVISION) == self.revision:
                return await self.resume(key, existing)
            await self.remove(existing["Id"], key)
        if session and not saved_chat(session):
            self.temporary[key] = monotonic()
        return await self.create(key, owner_key, session)

    async def resume(self, key, container):
        network = await self.ensure_network(key)
        if network not in container["NetworkSettings"]["Networks"]:
            await self.docker.call("POST", f"/networks/{network}/connect", body={"Container": container["Id"]})
        await self.docker.call("POST", f"/containers/{container['Id']}/start")
        return self.workspace(key, await self.docker.call("GET", f"/containers/{container['Id']}/json"))

    async def create(self, key, owner_key, session):
        await self.ensure_user_folder(owner_key)
        container = None
        try:
            network = await self.ensure_network(key)
            labels = self.policy.container_labels(key, owner_key, session, self.revision)
            created = await self.docker.call(
                "POST", "/containers/create", params={"name": network},
                body=self.container_spec(network, labels, secrets.token_urlsafe(32), owner_key, session),
            )
            container = created["Id"]
            if self.policy.network == "connected":
                await self.docker.call("POST", f"/networks/{self.policy.connected_network}/connect",
                                       body={"Container": container})
            await self.docker.call("POST", f"/containers/{container}/start")
            return self.workspace(key, await self.docker.call("GET", f"/containers/{container}/json"))
        except (Exception, asyncio.CancelledError):
            await self.remove(container, key)
            raise

    def container_spec(self, network, labels, credential, owner_key, session):
        # Only the account's own folder of the shared user volume is visible.
        mounts = [{"Type": "volume", "Source": self.policy.users_volume, "Target": WORKSPACE_HOME,
                   "VolumeOptions": {"Subpath": owner_key}}]
        runner = "/opt/loaf-terminal/runner.py"
        if self.runtime_mounts:
            mounts.extend(self.runtime_mounts)
            runner = RUNTIME_RUNNER
        if self.repository:
            mounts.append({"Type": "bind", "Source": self.repository, "Target": APT_REPOSITORY, "ReadOnly": True})
        host = {
            "Init": True, "NetworkMode": network,
            "SecurityOpt": ["no-new-privileges:true"], "CapDrop": ["ALL"], "CapAdd": list(ROOT_CAPABILITIES),
            "PidsLimit": 512,
            "NanoCpus": self.policy.cpus * 1_000_000_000,
            "Memory": self.policy.memory_mib * 1024 * 1024,
            "MemorySwap": self.policy.memory_mib * 1024 * 1024,
            # Docker's network flag controls IPv6 allocation. Also suppress kernel link-local addresses.
            "Sysctls": {"net.ipv6.conf.all.disable_ipv6": "1", "net.ipv6.conf.default.disable_ipv6": "1"}
            if Path("/proc/sys/net/ipv6/conf/all/disable_ipv6").is_file() else {},
            "Tmpfs": {"/tmp": "rw,nosuid,nodev,mode=1777,size=256m"}, "Mounts": mounts,
            "LogConfig": {"Type": "local", "Config": {"max-size": "5m", "max-file": "2"}},
        }
        if self.policy.network == "isolated":
            host["Dns"] = ["127.0.0.1"]
        return {
            # Open Terminal uses the process working directory as its single-user home. The account's folder keeps
            # dotfiles across chats; lease() starts a chat in /home/chat.
            "Image": self.image_id, "Labels": labels, "User": "0:0", "WorkingDir": WORKSPACE_HOME,
            "Entrypoint": ["/usr/local/bin/python", runner], "Cmd": [],
            "Env": [
                f"HOME={WORKSPACE_HOME}", f"OPEN_TERMINAL_START_DIR={WORKSPACE_HOME}",
                f"OPEN_TERMINAL_FILE_BROWSER_ROOT={WORKSPACE_ROOT}", "OPEN_TERMINAL_API_KEY=" + credential,
                f"OPEN_TERMINAL_COMMAND_SECONDS={self.policy.command_seconds}",
                "OPEN_TERMINAL_MAX_LOG_SIZE=4194304", "DEBIAN_FRONTEND=noninteractive",
            ],
            "HostConfig": host,
        }

    async def remove_network(self, key, container=None):
        name = self.policy.asset_name(key)
        details = await self.docker.call("GET", f"/networks/{name}", missing=True)
        if details is None:
            return
        if not self.policy.network_owned(details["Labels"] or {}, name):
            raise RuntimeError("Workspace network ownership mismatch")
        if container:
            # A stopped container keeps its endpoint configuration until it is disconnected.
            with suppress(DockerError):
                await self.docker.call("POST", f"/networks/{name}/disconnect",
                                       body={"Container": container, "Force": True})
        if self.provisioner in details.get("Containers", {}):
            await self.docker.call("POST", f"/networks/{name}/disconnect", body={"Container": self.provisioner})
        await self.docker.call("DELETE", f"/networks/{name}", missing=True)

    async def park(self, key, container):
        """Stop an idle container. Its files and packages stay; its network is freed until the next use."""
        await self.docker.call("POST", f"/containers/{container}/stop", params={"t": "5"}, missing=True, timeout=30)
        await self.remove_network(key, container)

    async def remove(self, container, key):
        if container:
            await self.docker.call("DELETE", f"/containers/{container}", params={"force": "true"}, missing=True)
        await self.remove_network(key)

    async def discard(self, key, container):
        await self.remove(container, key)
        self.temporary.pop(key, None)
        self.directories.pop(key, None)

    async def check_chat(self, user, session):
        if saved_chat(session) and (await self.chats.owners((session,))).get(session) != user:
            raise web.HTTPNotFound(text="Chat not found.")

    def record_directory(self, user, session, workspace, directory):
        self.directories[account_workspace_key(user, session)] = workspace.directory = directory

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
    async def lease(self, user, session=""):
        owner_key = account_key(user)
        key = workspace_key(owner_key, session)
        async with self.lock:
            await self.check_chat(user, session)
            workspace = self.active.get(key)
            if workspace is None:
                if not self.has_slot(key):
                    await self.reclaim_idle(one=True)
                if not self.has_slot(key):
                    raise web.HTTPServiceUnavailable(text="All terminal workspace slots are in use. Try again later.")
                workspace = self.active[key] = await self.open(key, owner_key, session)
                if session:
                    # A chat starts in its own folder unless the account selected another one.
                    self.directories.setdefault(key, CHAT_HOME)
            workspace.active += 1
            workspace.user, workspace.session = user, session
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
            if key in self.temporary:
                self.temporary[key] = workspace.used
            if not ready and not workspace.active:
                async with self.lock:
                    if not workspace.active and self.active.get(key) is workspace:
                        del self.active[key]
                        await self.park(key, workspace.container)

    async def idle(self, workspace):
        if workspace.active or monotonic() - workspace.used < self.policy.idle_seconds:
            return False
        await self.close_terminals(workspace)
        processes = await self.docker.call("GET", f"/containers/{workspace.container}/top",
                                           params={"ps_args": "-eo pid"}, missing=True)
        # The init process and Open Terminal itself are the only idle processes.
        return not (processes and len(processes["Processes"] or []) > 2)

    async def close_terminals(self, workspace):
        """Close the account's terminal shells in an unleased workspace.

        Open Terminal keeps a shell running after its browser tab disconnects. A terminal stream holds a
        lease while it is attached, so without one every listed shell is disconnected. Open Terminal lists
        only the shells of the account and chat in the request headers.
        """
        if workspace.user is None:
            return
        headers = {"X-User-Id": workspace.user, "Authorization": "Bearer " + workspace.credential}
        if workspace.session:
            headers["X-Session-Id"] = workspace.session
        options = {"headers": headers, "timeout": ClientTimeout(total=5), "allow_redirects": False}
        try:
            async with self.client.get(workspace.url("/api/terminals"), **options) as response:
                shells = await response.json() if response.status == 200 else []
            for shell in shells if isinstance(shells, list) else ():
                terminal = shell.get("id") if isinstance(shell, dict) else None
                if isinstance(terminal, str) and re.fullmatch(r"[0-9A-Za-z-]{1,64}", terminal):
                    async with self.client.delete(workspace.url(f"/api/terminals/{terminal}"), **options):
                        pass
        except (ClientError, TimeoutError, ValueError):
            LOG.warning("Could not close the disconnected terminals of an idle workspace")

    async def reclaim_idle(self, *, one=False):
        # The caller holds the lock so a container cannot be leased while it stops.
        for key, workspace in list(self.active.items()):
            if not await self.idle(workspace):
                continue
            del self.active[key]
            if key in self.temporary:
                await self.discard(key, workspace.container)
            else:
                await self.park(key, workspace.container)
            if one:
                return

    async def reap_idle(self):
        async with self.lock:
            await self.reclaim_idle()

    async def reap_deleted_chats(self):
        async with self.lock:
            chats = {session: (key, container) for key, session, container in await self.owned_containers()
                     if saved_chat(session)}
            if not chats:
                return
            owners = await self.chats.owners(set(chats))
            for session, (key, container) in chats.items():
                if session in owners:
                    continue
                try:
                    self.active.pop(key, None)
                    await self.discard(key, container)
                except (RuntimeError, ClientError, TimeoutError):
                    LOG.warning("Could not remove a deleted chat's terminal container")

    async def sweep(self):
        while True:
            await asyncio.sleep(min(5, self.policy.idle_seconds))
            try:
                await self.reap_idle()
            except (RuntimeError, ClientError, TimeoutError):
                LOG.warning("Could not stop idle terminal containers")
            try:
                await self.reap_deleted_chats()
            except (DockerError, ClientError, TimeoutError, sqlite3.Error):
                LOG.warning("Could not check for deleted Open WebUI chats")

    async def close(self):
        # Saved chats and drafts keep their stopped containers. Temporary chats end with the provisioner.
        results = await asyncio.gather(*(
            self.discard(key, workspace.container) if key in self.temporary else self.park(key, workspace.container)
            for key, workspace in self.active.items()
        ), return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            LOG.warning("Some terminal containers will need reconciliation after restart")


def same_secret(received, secret):
    return isinstance(received, str) and secrets.compare_digest(received.encode(), secret.encode())


def owner(request):
    user = request.headers.get("X-User-Id", "")
    if len(request.headers.getall("X-User-Id", [])) != 1 or not re.fullmatch(r"[!-~]{1,128}", user):
        raise web.HTTPForbidden(text="A verified Open WebUI account is required.")
    return user


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


def system_prompt(package_source):
    """Instructions for the model, which Open WebUI adds to the system prompt of every chat with the terminal.

    Short and explicit, so that small models act with the tools instead of describing what to do.
    """
    install = ("Install packages with apt update, then apt install PACKAGE." if package_source
               else "No package source is reachable; use the installed programs.")
    return ("You have a Linux terminal: this chat's own Debian container, where you are root. "
            "When the user asks you to run, check, install, build, create, read or change something on the "
            "computer, do it yourself with the terminal tools instead of describing it or asking the user to do it: "
            "run_command runs shell commands, read_file, write_file and list_files work with files, and "
            "display_file shows a file to the user. Answer from the tool results. "
            f"{install} /home/chat is deleted with the chat; keep lasting files in /home/user.")


def command_description(wait):
    """The description of run_command. Open Terminal's own describes the image build host and a background start."""
    if wait:
        result = (f"Without wait, a command still running after {wait} seconds returns status running and its id: "
                  "call get_process_status with the id to wait for the rest.")
    else:
        result = "It returns at once with the command's id: call get_process_status with the id and a wait in seconds."
    return ("Run a shell command in this chat's Debian container, as root, and return its output and exit code. "
            f"{result} Run commands yourself; never ask the user to run them.")


def tool_schema(paths, wait):
    """Publish Open Terminal's tools at its own paths, which the chat's container serves."""
    tools = {path: operations for path, operations in paths.items() if permitted_path(path)}
    run = tools.get("/execute", {}).get("post")
    if isinstance(run, dict):
        run["description"] = command_description(wait)
    for operation in (run, tools.get("/execute/{process_id}/status", {}).get("get")):
        for parameter in operation.get("parameters", ()) if isinstance(operation, dict) else ():
            if isinstance(parameter, dict) and parameter.get("name") == "wait" and wait:
                text = f"Seconds to wait for the command to finish, at most {COMMAND_WAIT_MAX}. Leave it out to wait {wait}."
                parameter["description"] = text
                if isinstance(parameter.get("schema"), dict):
                    parameter["schema"]["description"] = text
    return tools


def default_wait(method, path, query, wait):
    """Add the configured wait to a command request without one, like Open Terminal's execute_timeout setting.

    The terminal server applies it, so that a change reaches existing chat containers without replacing them.
    """
    command = (method == "POST" and path == "/execute") or (method == "GET" and PROCESS_STATUS.fullmatch(path))
    if not wait or not command or "wait" in query:
        return query
    query = MultiDict(query)
    query["wait"] = str(wait)
    return query


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


async def proxy_http(request, workspace, user, client, path, command_wait=0):
    # Open WebUI reads the picker options as per-user, path-hashed history files.
    account = quote(user, safe="!~*'()")
    history = f"{WORKSPACE_HOME}/.open-webui/users/{account}/folders"
    history_request = request.method == "GET" and len(request.query) == 1
    if history_request and path == "/files/read":
        target = request.query.get("path", "")
        directory, _, name = target.rpartition("/")
        if directory == history and name in WORKING_DIRECTORY_RECORDS:
            return web.json_response({"path": target, "content": WORKING_DIRECTORY_RECORDS[name], "total_lines": 1})

    upstream = workspace.url(path, default_wait(request.method, path, request.query, command_wait))
    data = request.content if request.can_read_body else None
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


async def proxy_directory(request, workspace, user, session, workspaces):
    """Forward a working directory change and remember it for the container's next start."""
    headers = {**workspace.headers(user, request.headers), "Accept-Encoding": "identity"}
    async with workspaces.client.request(
        request.method, workspace.url("/files/cwd", request.query), headers=headers,
        data=request.content if request.can_read_body else None, allow_redirects=False,
    ) as response:
        payload = await response.read()
        result = None
        if response.status == 200 and response.content_type == "application/json":
            with suppress(ValueError):
                result = json.loads(payload)
        if isinstance(result, dict) and isinstance(result.get("cwd"), str):
            workspaces.record_directory(user, session, workspace, result["cwd"])
        forwarded = {name: response.headers[name] for name in RESPONSE_HEADERS
                     if name in response.headers and name != "Content-Length"}
        return web.Response(body=payload, status=response.status, headers=forwarded)


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
    async with workspaces.lease(user, session) as workspace:
        headers = workspace.headers(user, request.headers)
        async with workspaces.client.ws_connect(
            workspace.url(request.path, request.query), headers=headers, max_msg_size=4 * 1024 * 1024,
        ) as upstream:
            await upstream.send_json({"type": "auth", "token": workspace.credential, "chat_id": session})
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
    # The terminal server answers /system itself, so Open WebUI gets the prompt without waiting for a container.
    config["features"].update(agents=False, system=True)
    schema = metadata["schema"]
    schema["paths"] = tool_schema(schema["paths"], workspaces.command_wait)
    prompt = {"prompt": system_prompt(workspaces.package_source)}

    async def dispatch(request):
        if control_network is not None and (
            request.remote is None or ipaddress.IPv4Address(request.remote) not in control_network
        ):
            raise web.HTTPForbidden()
        path = request.path
        if request.method == "GET" and path == "/health":
            return web.json_response({"status": "ok"})
        if request.method == "GET" and path in ("/api/config", "/openapi.json", "/system"):
            authenticate_http(request, secret)
            return web.json_response({"/api/config": config, "/openapi.json": schema, "/system": prompt}[path])
        if not permitted_path(path):
            raise web.HTTPNotFound()
        downstream = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
        if downstream.can_prepare(request).ok:
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
        try:
            async with workspaces.lease(user, session) as workspace:
                if request.method == "POST" and path == "/files/cwd":
                    return await proxy_directory(request, workspace, user, session, workspaces)
                return await proxy_http(request, workspace, user, workspaces.client, path, workspaces.command_wait)
        except (ClientError, DockerError, RuntimeError, TimeoutError, sqlite3.Error) as error:
            # A Docker error names the request and status; its response body stays private.
            LOG.warning("Terminal workspace request failed (%s)",
                        error if isinstance(error, (DockerError, RuntimeError)) else type(error).__name__)
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


def inherited_apt_sources(runtime_source, etc=Path("/etc/apt")):
    """The image's own APT sources. The chat containers run this image, too."""
    candidates = [etc / "sources.list", *sorted((etc / "sources.list.d").glob("*.list")),
                  *sorted((etc / "sources.list.d").glob("*.sources"))]
    return [str(path) for path in candidates if path.is_file() and str(path) != runtime_source]


def workspace_runtime_mounts(container, source_format):
    if source_format not in ("list", "sources"):
        raise ValueError("Use a .list or .sources file for the runtime APT source.")
    apt_source = f"/etc/apt/sources.list.d/loaf-terminal.{source_format}"
    actual = {mount["Destination"]: mount for mount in container["Mounts"]}
    sources = {}
    # The launcher keeps these sources at paths that stay the same across loaf releases. A container binds them
    # when it starts, so changed contents apply on its next start; only changed mounts replace it.
    revision = hashlib.sha256()
    for target in (apt_source, RUNTIME_RUNNER, NO_APT_SOURCE):
        mount = actual.get(target, {})
        source = sources[target] = mount.get("Source", "")
        if mount.get("Type") != "bind" or mount.get("RW") is not False or not Path(source).is_absolute():
            raise ValueError(f"Expected a read-only runtime bind mount at {target}.")
        revision.update(os.fsencode(source) + b"\0" + os.fsencode(target) + b"\0")
    # The runtime source replaces the image's own sources, as an image build with it does.
    hidden = inherited_apt_sources(apt_source)
    revision.update(json.dumps(hidden).encode())
    mounts = [{"Type": "bind", "Source": sources[target], "Target": target, "ReadOnly": True}
              for target in (apt_source, RUNTIME_RUNNER)]
    mounts += [{"Type": "bind", "Source": sources[NO_APT_SOURCE], "Target": target, "ReadOnly": True} for target in hidden]
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
        workspaces = Workspaces(policy, docker, runners, control["Id"], control["Image"], Chats(CHAT_DB),
                                runtime_mounts=mounts, runtime_revision=revision, repository=repository)
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
