"""Route one Open Terminal connection to deployment-scoped Docker workspaces."""

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
from time import monotonic
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, TraceConfig, UnixConnector, WSMsgType, web
from yarl import URL


LOG = logging.getLogger(__name__)
# Existing workspace volumes are found by these persisted Docker labels.
# Preserve Docker ownership labels for existing workspace data.
PROJECT = "owo.tool-server.project"
OWNER = "owo.tool-server.workspace"
CHAT_PROJECT = "owo.terminal.chat-project"
CHAT_ID = "owo.terminal.chat-id"
CHAT_OWNER = "owo.terminal.chat-owner"
GATEWAY = "com.docker.network.bridge.gateway_mode_ipv4"
INHIBIT_IPV4 = "com.docker.network.bridge.inhibit_ipv4"
BRIDGE_NAME = "com.docker.network.bridge.name"
KEY_FILE = Path("/run/secrets/terminal-server-key")
CHAT_DB = Path("/var/lib/owo-webui/webui.db")
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

    async def call(self, method, path, *, body=None, params=None, missing=False):
        async with self.session.request(method, "http://docker" + path, json=body, params=params) as response:
            if response.status == 404 and missing:
                return None
            if response.status >= 300:
                # Docker's error body can contain private configuration.
                raise DockerError(response.status, path.split("/")[1])
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
            int(env["OWO_TERMINAL_" + name]) for name in
            ("CPUS", "MEMORY_MIB", "MAX_WORKSPACES", "IDLE_SECONDS", "COMMAND_SECONDS")
        ]
        if min(numbers) < 1:
            raise ValueError("Terminal server limits must be positive.")
        options = json.loads(env.get("OWO_TERMINAL_VOLUME_OPTIONS") or "{}")
        if not isinstance(options, dict) or any(not isinstance(value, str) for value in options.values()):
            raise ValueError("Terminal server volume options must be an object of strings.")
        return cls(project, *numbers, env["OWO_TERMINAL_VOLUME_DRIVER"], options, bridge_options)

    def asset_name(self, key):
        project = hashlib.sha256(self.project.encode()).hexdigest()[:16]
        return f"owo-tools-{project}-{key}"

    def labels(self, key):
        return {PROJECT: self.project, OWNER: key}

    def chat_volume(self, key):
        return f"{self.asset_name(key)}-chat"

    def chat_labels(self, chat, owner_key):
        return {CHAT_PROJECT: self.project, CHAT_ID: chat, CHAT_OWNER: owner_key}


@dataclass
class Workspace:
    container: str
    network: str
    address: str
    credential: str
    active: int = 0
    used: float = 0

    def url(self, path, query=None):
        return URL.build(scheme="http", host=self.address, port=PORT, path=path, query=query)

    def headers(self, user, headers):
        forwarded = {name: headers[name] for name in REQUEST_HEADERS if name in headers}
        return {**forwarded, "X-User-Id": user, "Authorization": "Bearer " + self.credential}


class Workspaces:
    def __init__(self, policy, docker, client, provisioner, image_id, chats):
        self.policy = policy
        self.docker = docker
        self.client = client
        self.provisioner = provisioner
        self.image_id = image_id
        self.chats = chats
        self.active = {}
        self.lock = asyncio.Lock()

    async def initialize(self):
        filters = json.dumps({"label": [f"{PROJECT}={self.policy.project}"]})
        records = await self.docker.call("GET", "/containers/json", params={
            "all": "true", "filters": filters,
        })
        for record in records:
            key = record["Labels"].get(OWNER, "")
            if not re.fullmatch(r"[0-9a-f]{64}", key) or "/" + self.policy.asset_name(key) not in record["Names"]:
                continue
            container = await self.docker.call("GET", f"/containers/{record['Id']}/json")
            if (container["State"]["Running"] and container["Image"] == self.image_id
                    and len(self.active) < self.policy.maximum):
                self.active[key] = await self.recover(key, container)
            else:
                await self.remove(record["Id"], self.policy.asset_name(key))
        networks = await self.docker.call("GET", "/networks", params={"filters": filters})
        for network in networks:
            key = (network["Labels"] or {}).get(OWNER, "")
            if (re.fullmatch(r"[0-9a-f]{64}", key) and key not in self.active
                    and network["Name"] == self.policy.asset_name(key)):
                await self.remove(None, network["Name"])

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

    async def recover(self, key, container):
        network = self.policy.asset_name(key)
        labels = self.policy.labels(key)
        endpoints = container["NetworkSettings"]["Networks"]
        if set(endpoints) != {network}:
            raise RuntimeError("Workspace is attached to another network")
        await self.connect(network, labels)
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

    async def create(self, key, owner_key, session, *, saved):
        name = self.policy.asset_name(key)
        labels = self.policy.labels(key)
        home_volume = self.policy.asset_name(owner_key)
        await self.ensure_volume(home_volume, self.policy.labels(owner_key))
        chat_volume = None
        if saved:
            chat_volume = self.policy.chat_volume(key)
            await self.ensure_volume(chat_volume, self.policy.chat_labels(session, owner_key))
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
                body=self.container_spec(name, labels, credential, home_volume,
                                         chat_volume=chat_volume, temporary=bool(session) and not saved),
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

    def container_spec(self, name, labels, credential, home_volume, *, chat_volume=None, temporary=False):
        mounts = [{"Type": "volume", "Source": home_volume, "Target": WORKSPACE_HOME}]
        tmpfs = {"/tmp": "rw,nosuid,nodev,mode=1777,size=256m"}
        if chat_volume:
            mounts.append({"Type": "volume", "Source": chat_volume, "Target": CHAT_HOME})
        if temporary:
            tmpfs[CHAT_HOME] = "rw,nosuid,nodev,mode=0700,uid=1000,gid=1000,size=256m"
        return {
            # Open Terminal uses the process working directory as its single-user home.
            "Image": self.image_id, "Labels": labels, "User": "1000:1000", "WorkingDir": WORKSPACE_HOME,
            "Entrypoint": ["/usr/local/bin/python", "/opt/owo-terminal/runner.py"],
            "Cmd": [],
            "Env": [
                f"HOME={WORKSPACE_HOME}", f"OPEN_TERMINAL_START_DIR={WORKSPACE_HOME}",
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
        if details and details["Labels"] != self.policy.labels(network.rsplit("-", 1)[-1]):
            raise RuntimeError("Workspace network ownership mismatch")
        if details and self.provisioner in details.get("Containers", {}):
            await self.docker.call("POST", f"/networks/{network}/disconnect",
                                   body={"Container": self.provisioner})
        if details:
            await self.docker.call("DELETE", f"/networks/{network}", missing=True)

    @asynccontextmanager
    async def lease(self, user, session=""):
        owner_key = hashlib.sha256(user.encode()).hexdigest()
        key = workspace_key(owner_key, session)
        saved = bool(session) and not session.startswith(NON_SAVED_PREFIXES)
        async with self.lock:
            if saved and (await self.chats.owners((session,))).get(session) != user:
                raise web.HTTPNotFound(text="This saved chat is unavailable.")
            workspace = self.active.get(key)
            if workspace is None:
                if len(self.active) >= self.policy.maximum:
                    await self.reclaim_idle(one=True)
                if len(self.active) >= self.policy.maximum:
                    raise web.HTTPServiceUnavailable(text="All terminal workspace slots are in use. Try again later.")
                workspace = self.active[key] = await self.create(key, owner_key, session, saved=saved)
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
            yield workspace
        finally:
            workspace.active -= 1
            workspace.used = monotonic()
            if not ready and not workspace.active:
                async with self.lock:
                    if not workspace.active and self.active.get(key) is workspace:
                        await self.remove(workspace.container, workspace.network)
                        del self.active[key]

    async def reap_idle(self):
        async with self.lock:
            await self.reclaim_idle()

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
                if chat in owners:
                    continue
                try:
                    workspace = self.active.get(key)
                    if workspace:
                        await self.remove(workspace.container, workspace.network)
                        del self.active[key]
                    await self.docker.call("DELETE", f"/volumes/{volume}", missing=True)
                except (RuntimeError, ClientError, TimeoutError):
                    LOG.warning("Could not remove a deleted chat's terminal workspace")

    async def reclaim_idle(self, *, one=False):
        # The caller holds the lock so a runner cannot be leased during removal.
        for key, workspace in list(self.active.items()):
            if workspace.active or monotonic() - workspace.used < self.policy.idle_seconds:
                continue
            processes = await self.docker.call("GET", f"/containers/{workspace.container}/top",
                                               params={"ps_args": "-eo pid"}, missing=True)
            # The init process and Open Terminal itself are the only idle processes.
            if processes and len(processes["Processes"] or []) > 2:
                continue
            await self.remove(workspace.container, workspace.network)
            del self.active[key]
            if one:
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


async def proxy_http(request, workspace, user, client):
    # Open WebUI reads the picker options as per-user, path-hashed history files.
    account = quote(user, safe="!~*'()")
    history = f"{WORKSPACE_HOME}/.open-webui/users/{account}/folders"
    history_request = request.method == "GET" and len(request.query) == 1
    if history_request and request.path == "/files/read":
        path = request.query.get("path", "")
        directory, _, name = path.rpartition("/")
        if directory == history and name in WORKING_DIRECTORY_RECORDS:
            return web.json_response({"path": path, "content": WORKING_DIRECTORY_RECORDS[name], "total_lines": 1})

    upstream = workspace.url(request.path, request.query)
    async with client.request(
        request.method, upstream, headers=workspace.headers(user, request.headers),
        data=request.content if request.can_read_body else None, allow_redirects=False,
    ) as response:
        if history_request and request.path == "/files/list" and request.query.get("directory") == history:
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
    schema = metadata["schema"]
    schema["paths"] = {path: info for path, info in schema["paths"].items() if permitted_path(path)}

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
                return await proxy_http(request, workspace, user, workspaces.client)
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


async def serve(*, allow_legacy_bridge=False):
    with KEY_FILE.open("rb") as source:
        key = source.read(515)
    if not re.fullmatch(rb"[!-~]{32,512}(?:\r?\n)?", key):
        raise ValueError("The terminal server secret must be 32–512 printable ASCII characters without spaces.")
    secret = key.rstrip(b"\r\n").decode("ascii")
    metadata = json.loads(Path("/opt/owo-terminal/metadata.json").read_text())

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
        old_service = {"label": [f"com.docker.compose.project={project}", "com.docker.compose.service=tool-server"]}
        if await docker.call("GET", "/containers/json", params={"filters": json.dumps(old_service)}):
            raise RuntimeError("The previous terminal server is still running. Stop it before starting terminal-server.")
        endpoint = control["NetworkSettings"]["Networks"][project + "_terminal-control"]
        address = ipaddress.IPv4Address(endpoint["IPAddress"])
        network = ipaddress.IPv4Network((address, endpoint["IPPrefixLen"]), strict=False)
        policy = Policy.load(project, os.environ, bridge_options=bridge_options)
        workspaces = Workspaces(policy, docker, runners, control["Id"], control["Image"], Chats(CHAT_DB))
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
    except (KeyError, OSError, ValueError, RuntimeError, ClientError, TimeoutError) as error:
        LOG.error("Terminal server startup failed: %s", error)
        raise SystemExit(1)
