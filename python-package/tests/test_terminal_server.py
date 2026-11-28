"""Check terminal-server options, per-account lifecycle and the terminal proxy."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing, redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import lzma
import os
from pathlib import Path
import secrets
import shutil
import socket as sockets
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from loaf import cli, config, transfer
from tests import DATA
from tests.docker_support import command, compose_command

try:
    from aiohttp import ClientSession, WSMsgType, web
    from aiohttp.test_utils import TestClient, TestServer
except ImportError:
    ClientSession = None


def load_component(name):
    path = DATA / "docker/terminal-server" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"loaf_test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDocker:
    """Answer Docker API calls from a table; a list answers successive calls in turn."""

    def __init__(self):
        self.responses = {}
        self.calls = []

    async def call(self, method, path, *, body=None, **_):
        self.calls.append((method, path, body))
        answer = self.responses.get((method, path))
        return answer.pop(0) if isinstance(answer, list) else answer


KEY_FORMAT_CASES = (
    ("minimum length", b"!" + b"k" * 30 + b"~", True),
    ("LF", b"k" * 32 + b"\n", True),
    ("maximum length with CRLF", b"k" * 512 + b"\r\n", True),
    ("too short", b"k" * 31, False),
    ("too long", b"k" * 513, False),
    ("space", b"k" * 32 + b" ", False),
    ("control byte", b"k" * 32 + b"\0", False),
    ("non-ASCII byte", b"k" * 32 + b"\xff", False),
    ("bare CR", b"k" * 32 + b"\r", False),
    ("multiple records", b"k" * 512 + b"\r\n\n", False),
)


class TerminalServerSettingsTests(unittest.TestCase):
    def test_selected_profiles_and_secret_path(self):
        with TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "config").mkdir()
            (project / "config/config.ini").write_text(
                "[terminal_server]\nenabled=true\nsecret_file=private/terminal-server.key\ncpus=3\n"
            )
            # A dry run refuses a missing key like a real run, and names the command that creates it.
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(cli.main(["run", "offline", "--project-dir", folder, "--dry-run"]), 1)
            self.assertIn("setup terminal-server-key", errors.getvalue())
            (project / "private").mkdir()
            (project / "private/terminal-server.key").write_text("k" * 64)
            settings = config.load(DATA / "config", project / "config")
            compose = cli.Compose(project, settings=settings)
            self.assertEqual(compose.variables()["LOAF_TERMINAL_SECRET_FILE"], str(project / "private/terminal-server.key"))
            self.assertEqual(compose.variables()["LOAF_TERMINAL_CPUS"], "3")
            self.assertEqual(compose.variables()["LOAF_TERMINAL_WEBUI_ACCESS"], "admins")
            for mode in ("online", "offline", "build", "stop"):
                with self.subTest(mode=mode), redirect_stdout(io.StringIO()) as output:
                    command = ["run", mode] if mode in cli.SERVING else [mode]
                    self.assertEqual(cli.main([*command, "--project-dir", folder, "--dry-run", "-v"]), 0)
                    self.assertIn("--profile terminal", output.getvalue())
                    # Open WebUI receives the bearer key for its preset connection.
                    self.assertIn("compose.terminal-webui.yml", output.getvalue())
                    if mode in ("online", "offline"):
                        self.assertIn("--wait-timeout 300 terminal-server", output.getvalue())
        disabled = cli.Compose(DATA, settings=config.Settings()).command(cli.Step(("offline",), ("up",)))
        self.assertNotIn(str(DATA / "compose.terminal-webui.yml"), disabled)
        stop = cli.plan("offline", terminal_server=False)[1]
        self.assertIn("terminal-server", stop.arguments)
        self.assertEqual(cli.plan("build", terminal_server=True)[-1].arguments[-1], "terminal-server")
        self.assertEqual(cli.plan("offline", terminal_server=True, import_definitions=True)[-1].arguments[-1], "ollama-offline")

    def test_webui_access_selects_admins_or_every_account(self):
        with TemporaryDirectory() as folder:
            settings = Path(folder)
            for value in ("admins", "users"):
                (settings / "config.ini").write_text(f"[terminal_server]\nwebui_access = {value}\n")
                self.assertEqual(config.load(DATA / "config", settings).compose_variables()["LOAF_TERMINAL_WEBUI_ACCESS"],
                                 value)
            (settings / "config.ini").write_text("[terminal_server]\nwebui_access = everyone\n")
            with self.assertRaisesRegex(config.ConfigError, "webui_access: expected admins or users"):
                config.load(DATA / "config", settings)

    def test_webui_start_script_presets_one_terminal_connection_from_the_key(self):
        spec = importlib.util.spec_from_file_location("loaf_test_start_webui", DATA / "docker/open-webui/start-webui.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with TemporaryDirectory() as folder:
            key = Path(folder) / "terminal-server.key"
            self.assertEqual(module.environment({"PATH": "/bin"}, key), {"PATH": "/bin"})
            secret = 'k"\\' + "x" * 30
            key.write_bytes(secret.encode() + b"\r\n")
            environment = module.environment({"PATH": "/bin"}, key)
            connection, = json.loads(environment["TERMINAL_SERVER_CONNECTIONS"])
            self.assertEqual(environment["PATH"], "/bin")
            self.assertEqual(connection, {
                "id": "loaf-terminal", "name": "Terminal", "enabled": True, "url": "http://terminal-server:8000",
                "path": "/openapi.json", "auth_type": "bearer", "key": secret, "server_type": "terminal",
                "config": {"access_grants": []},
            })
            every, = json.loads(module.environment({"LOAF_TERMINAL_WEBUI_ACCESS": "users"}, key)
                                ["TERMINAL_SERVER_CONNECTIONS"])
            self.assertEqual(every["config"]["access_grants"],
                             [{"principal_type": "user", "principal_id": "*", "permission": "read"}])
            # Connections that the caller configured stay untouched.
            own = {"TERMINAL_SERVER_CONNECTIONS": "[]"}
            self.assertEqual(module.environment(own, key), own)

    def test_missing_key_and_disabling_auth_fail_before_docker(self):
        with TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "config").mkdir()
            settings = project / "config/config.ini"
            settings.write_text("[terminal_server]\nenabled=true\n")
            with patch.object(cli.Compose, "execute") as execute, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(cli.main(["run", "offline", "--project-dir", folder]), 1)
                self.assertIn("Terminal server key file is missing", errors.getvalue())
                execute.assert_not_called()
            invalid = "a" * 32 + " b"
            (project / config.TERMINAL_SECRET).write_text(invalid)
            with patch.object(cli.Compose, "execute") as execute, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(cli.main(["run", "offline", "--project-dir", folder]), 1)
                self.assertIn("Invalid terminal server key", errors.getvalue())
                self.assertNotIn(invalid, errors.getvalue())
                execute.assert_not_called()
            settings.write_text("[webui]\nauth=false\n[terminal_server]\nenabled=true\n")
            with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
                cli.main(["run", "online", "--project-dir", folder])
            self.assertIn("auth = true", errors.getvalue())

    def test_terminal_key_uses_the_current_path_and_validates_its_maximum_size(self):
        with TemporaryDirectory() as folder:
            project = Path(folder)
            settings = project / "config"
            settings.mkdir()
            current = project / config.TERMINAL_SECRET
            self.assertEqual(cli.Compose(project).terminal_secret_file(), current)
            with self.assertRaises(FileNotFoundError):
                config.read_terminal_secret(current)
            current.write_bytes(b"a" * 512 + b"\r\n")
            config.read_terminal_secret(current)
            current.write_bytes(b"a" * 512 + b"\r\n\n")
            with self.assertRaises(config.ConfigError):
                config.read_terminal_secret(current)

    def test_missing_runtime_apt_source_fails_before_changing_services(self):
        with TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "config").mkdir()
            (project / config.TERMINAL_SECRET).write_text("a" * 32)
            (project / "config/config.ini").write_text(
                "[terminal_server]\nenabled=true\napt_source_file=config/missing.list\n",
            )
            with patch.object(cli.Compose, "execute") as execute, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(cli.main(["run", "offline", "--project-dir", folder]), 1)
            self.assertIn("APT source file is missing", errors.getvalue())
            execute.assert_not_called()

    def test_runtime_scripts_stay_at_one_project_path_across_releases(self):
        with TemporaryDirectory() as folder:
            project = Path(folder)
            source = project / "mirror.list"
            source.write_text("deb http://mirror.invalid/debian trixie main\n")
            compose = cli.Compose(project, files=[DATA / "compose.yml"], settings=config.Settings(
                terminal_server_enabled=True, terminal_server_apt_source_file=source))
            step = cli.Step(("terminal",), ("up", "-d", "terminal-server"))
            runtime = project / cli.TERMINAL_RUNTIME_DIR
            self.assertEqual(compose.variables(step)["LOAF_TERMINAL_RUNTIME_DIR"], str(runtime))
            compose.stage_terminal_runtime()
            for name in cli.TERMINAL_RUNTIME_FILES:
                self.assertEqual((runtime / name).read_bytes(), (DATA / "docker/terminal-server" / name).read_bytes())
            # Another release replaces changed files at the same path, so chat containers keep their mounts.
            (runtime / "runner.py").write_text("# an earlier release\n")
            inode = (runtime / "provisioner.py").stat().st_ino
            compose.stage_terminal_runtime()
            self.assertEqual((runtime / "runner.py").read_bytes(), (DATA / "docker/terminal-server/runner.py").read_bytes())
            self.assertEqual((runtime / "provisioner.py").stat().st_ino, inode)
            self.assertEqual(sorted(path.name for path in runtime.iterdir()), sorted(cli.TERMINAL_RUNTIME_FILES))

    def test_terminal_key_format_preserves_the_file_bytes(self):
        with TemporaryDirectory() as folder:
            key = Path(folder) / "terminal.key"
            for label, contents, valid in KEY_FORMAT_CASES:
                with self.subTest(label=label):
                    key.write_bytes(contents)
                    if valid:
                        self.assertEqual(config.read_terminal_secret(key), contents)
                    else:
                        with self.assertRaises(config.ConfigError):
                            config.read_terminal_secret(key)


@unittest.skipUnless(ClientSession, "Install aiohttp to run the Docker-free provisioner tests.")
class ProvisionerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = load_component("provisioner")
        self.secret = secrets.token_urlsafe(32)

    def test_provisioner_accepts_the_same_key_format_as_the_launcher(self):
        with TemporaryDirectory() as folder:
            key = Path(folder) / "terminal.key"
            for label, contents, valid in KEY_FORMAT_CASES:
                with self.subTest(label=label):
                    key.write_bytes(contents)
                    if valid:
                        self.assertEqual(self.module.read_terminal_key(key), contents.rstrip(b"\r\n").decode("ascii"))
                    else:
                        with self.assertRaises(ValueError):
                            self.module.read_terminal_key(key)

    async def test_discovery_and_streams_use_verified_identity_and_container_credential(self):
        module = self.module
        try:
            with sockets.socket() as listener:
                listener.bind(("127.0.0.1", 0))
        except PermissionError as exc:
            self.skipTest(f"Loopback sockets unavailable: {exc}")

        async def backend(request):
            self.assertEqual(request.headers.get("Authorization"), "Bearer container-only")
            socket = web.WebSocketResponse()
            if socket.can_prepare(request).ok:
                await socket.prepare(request)
                hello = await socket.receive_json()
                await socket.send_json({"identity": request.headers["X-User-Id"], **hello})
                await socket.close()
                return socket
            return web.json_response({
                "user": request.headers["X-User-Id"], "session": request.headers.get("X-Session-Id"),
                "path": request.path, "body": await request.text(), "query": dict(request.query),
                "cwd": "/home/user/project",
            })

        upstream = web.Application()
        upstream.router.add_route("*", "/{path:.*}", backend)
        async with TestServer(upstream) as runner, ClientSession() as proxy_client:
            class Workspaces(module.Workspaces):
                def __init__(self):
                    self.client = proxy_client
                    self.directories = {}
                    self.leases = []
                    self.package_source = True

                @asynccontextmanager
                async def lease(self, user, session=""):
                    self.leases.append((user, session))
                    workspace = module.Workspace("container", "network", runner.host, "container-only")
                    with patch.object(module, "PORT", runner.port):
                        yield workspace

                async def sweep(self):
                    await asyncio.Future()

                async def close(self):
                    pass

            manager = Workspaces()
            metadata = {"config": {"features": {"terminal": True}}, "schema": {"paths": {
                "/execute": {"post": {"operationId": "run_command", "description": "Execute."}}, "/agents": {},
                "/api/terminals/user/output": {}, "/api/v1/terminals/templates": {},
            }}}
            headers = {"Authorization": "Bearer " + self.secret}
            identity = {"X-User-Id": "account-a", "X-Session-Id": "chat-a"}
            authorized = {**headers, **identity}
            async with TestClient(TestServer(module.application(manager, self.secret, metadata))) as client:
                async with client.get("/api/config", headers=headers) as result:
                    self.assertEqual((await result.json())["features"], {"terminal": True, "agents": False})
                async with client.get("/openapi.json", headers=headers) as result:
                    paths = (await result.json())["paths"]
                # Model tools keep Open Terminal's own paths; the chat's single container serves them.
                self.assertEqual(set(paths), {"/execute", "/api/terminals/user/output"})
                description = paths["/execute"]["post"]["description"]
                self.assertTrue(description.startswith("Execute.\n\nTerminal: call run_command"), description)
                self.assertIn("apt update, then apt install PACKAGE", description)
                for path, info, status in (("/execute", {}, 401), ("/execute", headers, 403),
                                           ("/api/v1/terminals/templates", headers, 404),
                                           ("/agents", authorized, 404), ("/agent/execute", authorized, 404)):
                    async with client.get(path, headers=info) as result:
                        self.assertEqual(result.status, status, path)
                self.assertEqual(manager.leases, [])

                async with client.post("/files/upload", headers=authorized,
                                       params={"file": "hello + world"}, data="hello") as result:
                    reply = await result.json()
                self.assertEqual((reply["user"], reply["session"], reply["path"], reply["body"], reply["query"]),
                                 ("account-a", "chat-a", "/files/upload", "hello", {"file": "hello + world"}))
                async with client.post("/files/cwd", headers=authorized, json={"path": "/home/user/project"}):
                    pass
                self.assertEqual(manager.directories,
                                 {module.account_workspace_key("account-a", "chat-a"): "/home/user/project"})
                # Package commands reach the container unchanged.
                command = {"command": "sudo apt-get update && apt-get install -y curl"}
                async with client.post("/execute", headers=authorized, json=command) as result:
                    reply = await result.json()
                self.assertEqual((reply["path"], json.loads(reply["body"])), ("/execute", command))
                self.assertEqual(manager.leases, [("account-a", "chat-a")] * 3)

                async with client.ws_connect("/api/terminals/session", headers=identity) as socket:
                    await socket.send_json({"type": "auth", "token": "bad"})
                    self.assertEqual((await socket.receive()).type, WSMsgType.CLOSE)
                self.assertEqual(len(manager.leases), 3)
                async with client.ws_connect("/api/terminals/session", headers=identity) as socket:
                    await socket.send_json({"type": "auth", "token": self.secret})
                    self.assertEqual(await socket.receive_json(), {
                        "type": "auth", "token": "container-only", "chat_id": "chat-a", "identity": "account-a",
                    })
                self.assertEqual(manager.leases[-1], ("account-a", "chat-a"))

    async def test_concurrent_requests_reuse_containers_and_idle_cleanup_keeps_active_jobs(self):
        module = self.module
        policy = module.Policy("test-project", 1, 256, 2, 5, 1, "local", {})
        docker = AsyncMock()

        closed = []

        class HealthyClient:
            @asynccontextmanager
            async def get(self, url, **kwargs):
                class Response:
                    status = 200

                    async def json(self):
                        # Open Terminal lists the shells of the account and chat named in the headers.
                        return [{"id": f"shell-{kwargs['headers']['X-User-Id']}", "pid": 7}]
                await asyncio.sleep(0)
                yield Response()

            @asynccontextmanager
            async def delete(self, url, **kwargs):
                closed.append((url.path, kwargs["headers"]["X-User-Id"], kwargs["headers"]["Authorization"]))
                yield None

        manager = module.Workspaces(policy, docker, HealthyClient(), "provisioner", "image", None)
        first = module.Workspace("a", "a-net", "127.0.0.1", "key-a")
        second = module.Workspace("b", "b-net", "127.0.0.1", "key-b")
        third = module.Workspace("c", "c-net", "127.0.0.1", "key-c")
        manager.open = AsyncMock(side_effect=[first, second, third])
        manager.park = AsyncMock()

        async def open_workspace(user):
            async with manager.lease(user) as workspace:
                await asyncio.sleep(0)
                return workspace

        a, again, b = await asyncio.gather(open_workspace("A"), open_workspace("A"), open_workspace("B"))
        self.assertIs(a, again)
        self.assertIsNot(a, b)
        self.assertEqual(manager.open.await_count, 2)
        first.used = second.used = monotonic() - 6
        docker.call.side_effect = [
            {"Processes": [["1"], ["2"], ["3"]]},
            {"Processes": [["1"], ["2"]]},
            {"Processes": [["1"], ["2"], ["3"]]},
            {"Processes": [["1"], ["2"]]},
        ]
        # A full slot list stops one idle container; a busy one keeps running.
        self.assertIs(await open_workspace("C"), third)
        manager.park.assert_awaited_once_with(module.account_key("B"), "b")
        # Idle workspaces close their disconnected shells before the processes are counted.
        self.assertEqual(closed, [("/api/terminals/shell-A", "A", "Bearer key-a"),
                                  ("/api/terminals/shell-B", "B", "Bearer key-b")])
        self.assertEqual(list(manager.active.values()), [first, third])
        with self.assertRaises(web.HTTPServiceUnavailable):
            await open_workspace("D")
        await manager.reap_idle()
        manager.park.assert_awaited_with(module.account_key("A"), "a")
        self.assertEqual(list(manager.active.values()), [third])
        # A container recovered after a restart has no known account and keeps the process count alone.
        closed.clear()
        recovered = module.Workspace("r", "r-net", "127.0.0.1", "key-r")
        recovered.used = monotonic() - 6
        docker.call.side_effect = [{"Processes": [["1"], ["2"]]}]
        self.assertTrue(await manager.idle(recovered))
        self.assertEqual(closed, [])

    def test_container_runs_as_root_with_only_the_account_folder(self):
        module = self.module
        policy = module.Policy("test-project", 2, 512, 2, 5, 1, "local", {})
        manager = module.Workspaces(policy, None, None, "provisioner", "pinned-image-id", None)
        owner_key = module.account_key("account")
        key = module.workspace_key(owner_key, "chat-1")
        labels = policy.container_labels(key, owner_key, "chat-1", manager.revision)
        spec = manager.container_spec("network", labels, "container-only", owner_key, "chat-1")
        self.assertEqual((spec["Image"], spec["User"], spec["WorkingDir"]), ("pinned-image-id", "0:0", "/home/user"))
        for variable in ("HOME=/home/user", "OPEN_TERMINAL_START_DIR=/home/user", "OPEN_TERMINAL_FILE_BROWSER_ROOT=/home",
                         "OPEN_TERMINAL_API_KEY=container-only", "DEBIAN_FRONTEND=noninteractive"):
            self.assertIn(variable, spec["Env"])
        host = spec["HostConfig"]
        self.assertEqual(host["Mounts"], [{"Type": "volume", "Source": policy.users_volume, "Target": "/home/user",
                                           "VolumeOptions": {"Subpath": owner_key}}])
        self.assertNotIn("ReadonlyRootfs", host)
        self.assertEqual((host["CapDrop"], host["CapAdd"], host["SecurityOpt"]),
                         (["ALL"], list(module.ROOT_CAPABILITIES), ["no-new-privileges:true"]))
        self.assertEqual((host["NetworkMode"], host["Dns"]), ("network", ["127.0.0.1"]))
        self.assertEqual(set(host["Tmpfs"]), {"/tmp"})
        self.assertNotIn("PortBindings", host)

        # Outbound access uses Docker's resolver; a local repository is mounted read-only.
        connected_policy = module.Policy("test-project", 2, 512, 2, 5, 1, "local", {}, "connected")
        connected = module.Workspaces(connected_policy, None, None, "provisioner", "image", None, repository="/srv/repo")
        mounts = connected.container_spec("network", labels, "key", owner_key, "chat-1")["HostConfig"]
        self.assertNotIn("Dns", mounts)
        self.assertEqual(mounts["Mounts"][-1], {"Type": "bind", "Source": "/srv/repo",
                                                "Target": "/run/loaf-apt/repository", "ReadOnly": True})
        self.assertNotEqual(connected.revision, manager.revision)

        # Ownership needs the deployment, the container name and a matching chat key.
        names = ["/" + policy.asset_name(key)]
        self.assertEqual(policy.container_identity(labels, names), (key, "chat-1"))
        self.assertEqual(policy.container_identity(policy.container_labels(owner_key, owner_key, "", "r"),
                                                   ["/" + policy.asset_name(owner_key)]), (owner_key, ""))
        self.assertIsNone(policy.container_identity(labels, ["/" + policy.asset_name(owner_key)]))
        self.assertIsNone(policy.container_identity({**labels, module.CHAT_ID: "chat-2"}, names))
        self.assertIsNone(policy.container_identity({**labels, module.PROJECT: "other"}, names))

    def test_model_guidance_names_the_tool_and_whether_apt_can_reach_a_source(self):
        module = self.module
        for network, repository, package_source in (("isolated", None, False), ("isolated", "/srv/repo", True),
                                                    ("connected", None, True)):
            with self.subTest(network=network, repository=repository):
                policy = module.Policy("p", 1, 256, 2, 5, 1, "local", {}, network)
                manager = module.Workspaces(policy, None, None, "provisioner", "image", None, repository=repository)
                self.assertEqual(manager.package_source, package_source)
        available, unavailable = module.tool_note(True), module.tool_note(False)
        for note in (available, unavailable):
            self.assertIn("call run_command to run shell commands yourself", note)
            self.assertIn("/home/chat is deleted with the chat; keep lasting files in /home/user.", note)
            self.assertLess(len(note), 400)
        self.assertIn("apt update, then apt install PACKAGE", available)
        self.assertIn("No package source is reachable; use the installed tools.", unavailable)

    def test_policy_reads_the_network_access(self):
        module = self.module
        env = {"LOAF_TERMINAL_CPUS": "1", "LOAF_TERMINAL_MEMORY_MIB": "256", "LOAF_TERMINAL_MAX_WORKSPACES": "2",
               "LOAF_TERMINAL_IDLE_SECONDS": "5", "LOAF_TERMINAL_COMMAND_SECONDS": "1",
               "LOAF_TERMINAL_VOLUME_DRIVER": "local"}
        self.assertEqual(module.Policy.load("p", env, bridge_options={}).network, "isolated")
        self.assertEqual(module.Policy.load("p", {**env, "LOAF_TERMINAL_NETWORK": "connected"},
                                            bridge_options={}).network, "connected")
        with self.assertRaisesRegex(ValueError, "isolated or connected"):
            module.Policy.load("p", {**env, "LOAF_TERMINAL_NETWORK": "open"}, bridge_options={})

    def test_a_runtime_apt_source_hides_the_images_own_sources(self):
        with TemporaryDirectory() as folder:
            etc = Path(folder)
            (etc / "sources.list.d").mkdir()
            for name in ("sources.list", "sources.list.d/debian.sources", "sources.list.d/loaf-terminal.list",
                         "sources.list.d/loaf-terminal.sources", "sources.list.d/notes.txt"):
                (etc / name).write_text("")
            runtime = str(etc / "sources.list.d/loaf-terminal.sources")
            # A build-time source in the other format is inherited as well.
            self.assertEqual(self.module.inherited_apt_sources(runtime, etc), [
                str(etc / "sources.list"), str(etc / "sources.list.d/loaf-terminal.list"),
                str(etc / "sources.list.d/debian.sources"),
            ])

    def test_runtime_revision_follows_the_mounts_and_not_the_file_contents(self):
        def control(directory):
            targets = {"/etc/apt/sources.list.d/loaf-terminal.list": "mirror.list",
                       self.module.RUNTIME_RUNNER: "runner.py", self.module.NO_APT_SOURCE: "no-apt-source.list"}
            return {"Mounts": [{"Destination": target, "Source": f"{directory}/{name}", "Type": "bind", "RW": False}
                               for target, name in targets.items()]}

        # The files are not read: a new loaf release changes them at the same stable paths.
        mounts, revision = self.module.workspace_runtime_mounts(control("/project/.loaf-terminal-runtime"), "list")
        self.assertEqual(self.module.workspace_runtime_mounts(control("/project/.loaf-terminal-runtime"), "list"),
                         (mounts, revision))
        self.assertNotEqual(self.module.workspace_runtime_mounts(control("/elsewhere"), "list")[1], revision)
        self.assertEqual(mounts[1], {"Type": "bind", "Source": "/project/.loaf-terminal-runtime/runner.py",
                                     "Target": self.module.RUNTIME_RUNNER, "ReadOnly": True})

    async def test_open_resumes_a_stopped_container_or_recreates_it_after_a_change(self):
        module = self.module
        policy = module.Policy("test-project", 1, 256, 2, 5, 1, "local", {})
        owner_key = module.account_key("account")
        key = module.workspace_key(owner_key, "chat-1")
        name = policy.asset_name(key)
        network = {"Labels": policy.labels(key), "Driver": "bridge", "Internal": True, "EnableIPv6": False,
                   "Options": dict(policy.bridge_options), "Containers": {}}
        docker = FakeDocker()
        manager = module.Workspaces(policy, docker, None, "provisioner", "image", None)
        stopped = {"Id": "stopped", "Name": "/" + name, "NetworkSettings": {"Networks": {}}, "Config": {
            "Labels": policy.container_labels(key, owner_key, "chat-1", manager.revision),
            "Env": ["OPEN_TERMINAL_API_KEY=kept-credential"]}}
        started = {**stopped, "NetworkSettings": {"Networks": {name: {"IPAddress": "10.1.0.2"}}}}
        docker.responses = {
            ("GET", f"/containers/{name}/json"): stopped, ("GET", f"/networks/{name}"): [None, network],
            ("GET", "/containers/stopped/json"): started,
        }
        workspace = await manager.open(key, owner_key, "chat-1")
        self.assertEqual((workspace.container, workspace.address, workspace.credential),
                         ("stopped", "10.1.0.2", "kept-credential"))
        self.assertIn(("POST", f"/networks/{name}/connect", {"Container": "stopped"}), docker.calls)
        self.assertIn(("POST", "/containers/stopped/start", None), docker.calls)
        self.assertFalse([call for call in docker.calls if call[1] == "/containers/create"])

        # A container from another configuration is replaced, after creating the account's folder.
        stopped["Config"]["Labels"][module.REVISION] = "earlier"
        docker.calls.clear()
        docker.responses.update({
            ("GET", f"/networks/{name}"): [None, None, network], ("POST", "/containers/create"): [
                {"Id": "folder-helper"}, {"Id": "new"}],
            ("POST", "/containers/folder-helper/wait"): {"StatusCode": 0},
            ("GET", "/containers/new/json"): {**started, "Id": "new"},
        })
        workspace = await manager.open(key, owner_key, "chat-1")
        self.assertEqual(workspace.container, "new")
        self.assertIn(("DELETE", "/containers/stopped", None), docker.calls)
        helper = next(body for method, path, body in docker.calls
                      if path == "/containers/create" and module.HELPER in body["Labels"])
        self.assertEqual(helper["Entrypoint"], ["install", "-d", "-m", "0700", f"/users/{owner_key}"])
        self.assertEqual(helper["HostConfig"]["Mounts"],
                         [{"Type": "volume", "Source": policy.users_volume, "Target": "/users"}])
        self.assertEqual(helper["HostConfig"]["NetworkMode"], "none")
        self.assertIn(("DELETE", "/containers/folder-helper", None), docker.calls)
        self.assertEqual(manager.user_folders, {owner_key})

    async def test_initialize_recovers_running_stops_saved_and_removes_temporary_containers(self):
        module = self.module
        policy = module.Policy("test-project", 1, 256, 1, 5, 1, "local", {})
        manager = module.Workspaces(policy, FakeDocker(), None, "provisioner", "image", None)
        owner_key = module.account_key("account")
        containers = {}
        for session, running, revision in (("chat-running", True, manager.revision),
                                           ("chat-extra", True, manager.revision),
                                           ("temporary:chat", False, manager.revision),
                                           ("chat-changed", False, "earlier"), ("", False, manager.revision)):
            key = module.workspace_key(owner_key, session)
            labels = policy.container_labels(key, owner_key, session, revision)
            containers[key] = {"Id": session or "draft", "State": {"Running": running}, "Config": {"Labels": labels},
                               "Names": ["/" + policy.asset_name(key)], "Labels": labels}
        foreign = {**containers[module.workspace_key(owner_key, "chat-running")], "Id": "foreign",
                   "Names": ["/someone-else"]}

        async def call(method, path, *, params=None, **_):
            if path == "/containers/json":
                return [] if module.HELPER in params["filters"] else [*containers.values(), foreign]
            if path == "/networks":
                return []
            return next(record for record in containers.values() if path == f"/containers/{record['Id']}/json")

        manager.docker.call = call
        manager.ensure_users_volume = AsyncMock()
        manager.recover = AsyncMock(return_value="recovered")
        manager.park = AsyncMock()
        manager.remove = AsyncMock()
        await manager.initialize()
        self.assertEqual(manager.active, {module.workspace_key(owner_key, "chat-running"): "recovered"})
        self.assertEqual(sorted(call.args[1] for call in manager.park.await_args_list), ["chat-extra", "draft"])
        self.assertEqual(sorted(call.args[0] for call in manager.remove.await_args_list),
                         ["chat-changed", "temporary:chat"])

    async def test_deleted_chats_remove_only_their_containers(self):
        module = self.module
        policy = module.Policy("test-project", 1, 256, 4, 5, 1, "local", {})
        owner_key = module.account_key("account")
        keys = {session: module.workspace_key(owner_key, session) for session in ("deleted", "kept", "temporary:x", "")}
        with TemporaryDirectory() as folder:
            database = Path(folder) / "webui.db"
            manager = module.Workspaces(policy, None, None, "provisioner", "image", module.Chats(database))
            manager.owned_containers = AsyncMock(return_value=[
                (key, session, f"container-{session or 'draft'}") for session, key in keys.items()])
            manager.discard = AsyncMock()
            manager.active[keys["deleted"]] = module.Workspace("container-deleted", "network", "127.0.0.1", "key")
            with self.assertRaises(sqlite3.OperationalError):
                await manager.reap_deleted_chats()
            manager.discard.assert_not_awaited()
            self.assertFalse(database.exists())

            with closing(sqlite3.connect(database)) as db, db:
                db.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT, archived INTEGER)")
                db.execute("INSERT INTO chat VALUES ('kept', 'account', 1)")
            await manager.reap_deleted_chats()
            manager.discard.assert_awaited_once_with(keys["deleted"], "container-deleted")
            self.assertNotIn(keys["deleted"], manager.active)
            with self.assertRaises(web.HTTPNotFound):
                async with manager.lease("someone else", "kept"):
                    self.fail("A different account reached the saved chat's container")

    async def test_shutdown_stops_saved_chats_and_removes_temporary_ones(self):
        module = self.module
        policy = module.Policy("test-project", 1, 256, 4, 5, 1, "local", {})
        manager = module.Workspaces(policy, None, None, "provisioner", "image", None)
        manager.active = {"saved": module.Workspace("a", "a-net", "127.0.0.1", "k"),
                          "temporary": module.Workspace("b", "b-net", "127.0.0.1", "k")}
        manager.temporary = {"temporary": 0}
        manager.park, manager.remove = AsyncMock(), AsyncMock()
        await manager.close()
        manager.park.assert_awaited_once_with("saved", "a")
        manager.remove.assert_awaited_once_with("b", "temporary")
        self.assertEqual(manager.temporary, {})

    async def test_connected_network_disables_traffic_between_chat_containers(self):
        module = self.module
        policy = module.Policy("test-project", 1, 256, 4, 5, 1, "local", {}, "connected")
        docker = FakeDocker()
        docker.responses = {("GET", f"/networks/{policy.connected_network}"): None}
        manager = module.Workspaces(policy, docker, None, "provisioner", "image", None)
        await manager.ensure_connected_network()
        _, _, body = docker.calls[-1]
        self.assertEqual((body["Internal"], body["Options"], body["Labels"]),
                         (False, {module.INTER_CONTAINER: "false"}, policy.connected_labels))
        docker.responses[("GET", f"/networks/{policy.connected_network}")] = {
            "Labels": policy.connected_labels, "Driver": "bridge", "Internal": False, "Options": {}}
        with self.assertRaisesRegex(RuntimeError, "isolation mismatch"):
            await manager.ensure_connected_network()

    async def test_workspace_bridge_requires_the_selected_isolation(self):
        module = self.module
        policy = module.Policy("test-project", 2, 512, 2, 5, 1, "local", {})
        labels = policy.labels("key")
        docker = AsyncMock()
        network = {"Labels": labels, "Driver": "bridge", "Internal": True, "EnableIPv6": False, "Options": {}}
        docker.call.return_value = network
        isolated = module.Workspaces(policy, docker, None, "provisioner", "image", None)
        with self.assertRaisesRegex(RuntimeError, "required isolation"):
            await isolated.connect("workspace", labels)
        docker.call.assert_awaited_once_with("GET", "/networks/workspace")
        options = {module.INHIBIT_IPV4: "true"}
        addressless_policy = module.Policy("test-project", 2, 512, 2, 5, 1, "local", {}, bridge_options=options)
        addressless = module.Workspaces(addressless_policy, docker, None, "provisioner", "image", None)
        with self.assertRaisesRegex(RuntimeError, "required isolation"):
            await addressless.connect("workspace", labels)
        network["Options"] = options
        self.assertIs(await addressless.connect("workspace", labels), network)
        network["Options"] = {**options, module.BRIDGE_NAME: "existing-bridge"}
        with self.assertRaisesRegex(RuntimeError, "required isolation"):
            await addressless.connect("workspace", labels)
        network.update(Internal=False, Options=options)
        with self.assertRaisesRegex(RuntimeError, "required isolation"):
            await addressless.connect("workspace", labels)
        network.update(Internal=True, Options={module.GATEWAY: "isolated"})
        self.assertIs(await isolated.connect("workspace", labels), network)


@unittest.skipUnless(os.environ.get("RUN_TERMINAL_SERVER_TESTS") == "1", "Set RUN_TERMINAL_SERVER_TESTS=1 for terminal-server Docker tests.")
class TerminalServerDockerTests(unittest.TestCase):
    image = os.environ.get("LOAF_TEST_TERMINAL_IMAGE", "loaf/open-terminal:latest")

    @staticmethod
    def remove_terminal_assets(project):
        """Remove the chat containers, networks and user volume that the provisioner keeps for later use."""
        label = f"label={transfer.WORKSPACE_PROJECT_LABEL}={project}"
        for kind, listing in (("container", ("ps", "-aq")), ("network", ("network", "ls", "-q")),
                              ("volume", ("volume", "ls", "-q"))):
            names = command(["docker", *listing, "--filter", label]).split()
            if names:
                command(["docker", kind, "rm", *(("--force",) if kind == "container" else ()), *names])

    def test_chat_containers_run_apt_as_root_and_keep_chat_files(self):
        self.bridge_options()
        if not shutil.which("dpkg-deb"):
            self.skipTest("dpkg-deb is required to create the local APT fixture.")
        project_name = f"loaf-test-apt-{uuid4().hex}"
        control = f"{project_name}_terminal-control"
        image = transfer.image_names(project_name, include_terminal=True)[-1]
        webui = f"{project_name}-open-webui"
        command(["docker", "image", "tag", self.image, image])
        self.addCleanup(command, ["docker", "image", "rm", image])
        self.addCleanup(self.remove_terminal_assets, project_name)
        with TemporaryDirectory() as temporary:
            project = Path(temporary)
            key = project / "terminal-server.key"
            key.write_text(secrets.token_urlsafe(32), encoding="utf-8")
            key.chmod(0o600)
            repository = project / "mirror"
            repository.mkdir()
            source = project / "offline.sources"
            source.write_text("Types: deb\nURIs: file:/run/loaf-apt/repository\nSuites: ./\nTrusted: yes\n")
            self.create_apt_repository(project, repository)
            compose = cli.Compose(project, files=[DATA / "compose.yml"], namespace=project_name,
                                  settings=config.Settings(terminal_server_enabled=True,
                                                           terminal_server_secret_file=key,
                                                           terminal_server_apt_source_file=source,
                                                           terminal_server_apt_repository_dir=repository))

            def invoke(*arguments):
                return compose_command(compose, cli.Step(("terminal",), arguments))

            def seed_database(statement):
                command(["docker", "run", "--rm", "-i", "--pull", "never", "--network", "none",
                         "--mount", f"type=volume,source={webui},target=/db", "--entrypoint", "/usr/bin/python3",
                         image, "-"], input="import sqlite3\nwith sqlite3.connect('/db/webui.db') as db:\n"
                         f"    db.executescript({statement!r})\n")

            def check(script):
                command(["docker", "run", "--rm", "-i", "--pull", "never", "--network", control,
                         "--mount", f"type=bind,source={key},target=/test.key,readonly",
                         "--entrypoint", "/usr/bin/python3", image, "-"], input="""\
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

def request(path, body=None, *, chat='first', user='alice', status=200):
    req = Request('http://terminal-server:8000' + path,
                  data=json.dumps(body).encode() if body is not None else None,
                  headers={'Authorization': 'Bearer ' + Path('/test.key').read_text(),
                           'X-User-Id': user, 'X-Session-Id': chat, 'Content-Type': 'application/json'})
    try:
        with urlopen(req, timeout=120) as response:
            assert response.status == status, (path, response.status)
            return json.load(response)
    except HTTPError as error:
        assert error.code == status, (path, error.code, error.read().decode())

def run(command, *, chat='first', user='alice', exit_code=0):
    result = request('/execute?wait=90', {'command': command}, chat=chat, user=user)
    assert result['exit_code'] == exit_code, result
    return result
""" + script)

            workspace = f"label={transfer.WORKSPACE_PROJECT_LABEL}={project_name}"
            start = ("up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "60", "terminal-server")
            try:
                invoke("create", "--no-build", "terminal-server")
                seed_database("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT);"
                              "INSERT INTO chat VALUES ('first', 'alice'), ('second', 'alice');")
                invoke(*start)
                check("""\
paths = request('/openapi.json')['paths']
assert '/execute' in paths and not any(path.startswith('/agent/') for path in paths), sorted(paths)
run('test "$(id -u)" = 0 && test "$PWD" = /home/chat && test "$HOME" = /home/user')
run('test "$(ls -A /home | sort | xargs)" = "chat user"')
run('echo personal > /home/user/shared.txt && echo chat > kept.txt')
# Only the configured source is active, and apt installs without -y or a terminal.
run('apt-get update >/tmp/update.log 2>&1; ! grep -q deb.debian.org /tmp/update.log')
run('apt install loaf-chat-demo </dev/null')
run('test "$(loaf-chat-demo)" = configured')
run('sudo apt-get install -y --reinstall loaf-chat-demo && sudo -n true')
run('test -f /home/user/shared.txt && test ! -e /home/chat/kept.txt && ! command -v loaf-chat-demo', chat='second')
run('test "$PWD" = /home/user && test ! -e /home/user/shared.txt', chat='', user='bob')
run('printf temporary > /home/chat/note.txt', chat='temporary:first')
""")
                invoke("stop", "--timeout", "45", "terminal-server")
                # Saved chats and drafts keep their stopped containers; temporary chats end with the server.
                self.assertEqual(command(["docker", "ps", "-q", "--filter", workspace]).strip(), "")
                self.assertEqual(len(command(["docker", "ps", "-aq", "--filter", workspace]).split()), 3)
                invoke(*start)
                check("run('test \"$(loaf-chat-demo)\" = configured && test -f kept.txt')\n"
                      "run('! command -v loaf-chat-demo', chat='second')\n"
                      "run('test ! -e /home/chat/note.txt', chat='temporary:first')\n")
                seed_database("DELETE FROM chat WHERE id = 'first';")
                deadline = monotonic() + 20
                while command(["docker", "ps", "-aq", "--filter", workspace,
                               "--filter", "label=loaf.terminal.chat-id=first"]).strip():
                    self.assertLess(monotonic(), deadline, "Deleted chat's container was not removed")
                    sleep(1)
                check("run('test -f /home/user/shared.txt', chat='second')\n")
            except AssertionError:
                print(invoke("logs", "--no-color", "terminal-server"))
                raise
            finally:
                invoke("down", "--volumes", "--timeout", "45")

    @staticmethod
    def create_apt_repository(project, repository):
        index = []
        for name in ("loaf-chat-dependency", "loaf-chat-demo"):
            root = project / name
            control = root / "DEBIAN"
            control.mkdir(parents=True)
            metadata = f"Package: {name}\nVersion: 1.0\nArchitecture: all\nMaintainer: Loaf tests\nDescription: Fixture\n"
            if name == "loaf-chat-demo":
                metadata += "Depends: loaf-chat-dependency\n"
                executable = root / "usr/bin" / name
                executable.parent.mkdir(parents=True)
                executable.write_text("#!/bin/sh\ncat /usr/share/loaf-chat-fixture/marker\n")
                executable.chmod(0o755)
            else:
                postinst = control / "postinst"
                postinst.write_text("#!/bin/sh\nset -e\nmkdir -p /usr/share/loaf-chat-fixture\n"
                                    "printf 'configured\\n' > /usr/share/loaf-chat-fixture/marker\n")
                postinst.chmod(0o755)
            (control / "control").write_text(metadata)
            deb = repository / f"{name}_1.0_all.deb"
            command(["dpkg-deb", "--build", "--root-owner-group", str(root), str(deb)])
            payload = deb.read_bytes()
            index.append(metadata + f"Filename: ./{deb.name}\nSize: {len(payload)}\n"
                         f"SHA256: {hashlib.sha256(payload).hexdigest()}\n\n")
        (repository / "Packages.xz").write_bytes(lzma.compress("".join(index).encode()))

    def test_runtime_apt_source_is_read_only_and_updates_without_replacing_the_container(self):
        self.bridge_options()
        project_name = f"loaf-test-runtime-{uuid4().hex}"
        control = f"{project_name}_terminal-control"
        image = transfer.image_names(project_name, include_terminal=True)[-1]
        command(["docker", "image", "tag", self.image, image])
        self.addCleanup(command, ["docker", "image", "rm", image])
        self.addCleanup(self.remove_terminal_assets, project_name)
        with TemporaryDirectory() as temporary:
            project = Path(temporary)
            key = project / "terminal-server.key"
            key.write_text(secrets.token_urlsafe(32), encoding="utf-8")
            key.chmod(0o600)
            source = project / "offline mirror.sources"
            source.write_text("Types: deb\nURIs: http://first.invalid/debian\nSuites: trixie\nComponents: main\n")
            compose = cli.Compose(project, files=[DATA / "compose.yml"], namespace=project_name,
                                  settings=config.Settings(terminal_server_enabled=True,
                                                           terminal_server_secret_file=key,
                                                           terminal_server_apt_source_file=source))
            apt_target = "/etc/apt/sources.list.d/loaf-terminal.sources"

            def invoke(*arguments):
                return compose_command(compose, cli.Step(("terminal",), arguments))

            def check_workspace():
                command([
                    "docker", "run", "--rm", "--pull", "never", "--network", control, "-i",
                    "--mount", f"type=bind,source={key},target=/test.key,readonly",
                    "--entrypoint", "/usr/bin/python3", image, "-",
                ], input="""\
from pathlib import Path
from urllib.request import Request, urlopen

key = Path("/test.key").read_text()
request = Request("http://terminal-server:8000/files/cwd", headers={
    "Authorization": "Bearer " + key, "X-User-Id": "runtime-mirror-test",
})
with urlopen(request, timeout=45) as response:
    assert response.status == 200
""")
                runner, = command(["docker", "ps", "-q", "--filter",
                                   f"label={transfer.WORKSPACE_PROJECT_LABEL}={project_name}"]).split()
                details, = json.loads(command(["docker", "inspect", runner]))
                mounts = {mount["Destination"]: mount for mount in details["Mounts"]}
                self.assertEqual(mounts[apt_target]["Source"], str(source))
                self.assertFalse(mounts[apt_target]["RW"])
                self.assertFalse(details["HostConfig"]["ReadonlyRootfs"])
                self.assertEqual(details["Config"]["User"], "0:0")
                self.assertEqual(command(["docker", "exec", runner, "/bin/cat", apt_target]), source.read_text())
                # The runtime source replaces the image's own sources, as it does in a build.
                active = command(["docker", "exec", runner, "/bin/sh", "-c",
                                  "cat /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null | grep '^URIs:'"])
                self.assertEqual(active.splitlines(),
                                 [line for line in source.read_text().splitlines() if line.startswith("URIs:")])
                return runner

            try:
                start = ("up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "60", "terminal-server")
                invoke(*start)
                first = check_workspace()
                source.write_text(source.read_text().replace("first.invalid", "live-edit.invalid"))
                self.assertEqual(command(["docker", "exec", first, "/bin/cat", apt_target]), source.read_text())
                replacement = source.with_name("replace.sources")
                replacement.write_text(source.read_text().replace("live-edit.invalid", "second.invalid"))
                replacement.replace(source)
                # The chat keeps its container, files and packages; its next start binds the replaced file.
                invoke(*start)
                self.assertEqual(check_workspace(), first)
            finally:
                invoke("down", "--volumes", "--timeout", "45")

    def test_terminal_backup_restores_image_user_volume_and_key(self):
        namespace = f"loaf-test-transfer-{uuid4().hex}"
        tags = transfer.image_names(namespace, include_terminal=True)
        users = transfer.terminal_volume(namespace)
        folder_name = "a" * 64
        webui = next(name for name, role in transfer.volume_names(namespace).items() if role == "webui")
        volumes = (webui, users)
        for resource, names in (("image", tags), ("volume", volumes)):
            self.addCleanup(subprocess.run, ["docker", resource, "rm", *names],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for tag in tags:
            command(["docker", "image", "tag", self.image, tag])

        def volume_shell(script):
            return command([
                "docker", "run", "--rm", "--pull", "never", "--network", "none",
                "--mount", f"type=volume,source={users},target=/users,volume-nocopy",
                "--entrypoint", "/bin/sh", tags[-1], "-c", script,
            ])

        def labels():
            details, = json.loads(command(["docker", "volume", "inspect", users]))
            return details["Labels"]

        with TemporaryDirectory() as folder:
            project = Path(folder)
            secret = secrets.token_hex(32).encode() + b"\n"
            key = project / "source.key"
            key.write_bytes(secret)
            archive = project / "backup" / "fixture.tar.gz"
            transfer.create_labeled_volume(project, users, transfer.terminal_volume_labels(namespace), "local", {})
            volume_shell(f"install -d -m 0700 /users/{folder_name} && printf 'persistent work' > /users/{folder_name}/code.c"
                         f" && chown 1000:1000 /users/{folder_name}/code.c")
            transfer.backup(project, archive, lambda: None, types=("images", "webui", "terminal"), namespace=namespace,
                            compression_level=0, terminal_key=key)
            command(["docker", "volume", "rm", *volumes])
            command(["docker", "image", "rm", tags[-1]])
            target = project / config.TERMINAL_SECRET
            restored = transfer.restore(project, transfer.open_archive(archive), namespace=namespace)
            self.assertEqual(restored.types, ("images", "webui", "terminal"))
            self.assertEqual(target.read_bytes(), secret)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(volume_shell(f"cat /users/{folder_name}/code.c; stat -c ' %u:%g' /users/{folder_name}/code.c;"
                                          f" stat -c ' %a' /users/{folder_name}").split(),
                             ["persistent", "work", "1000:1000", "700"])
            self.assertEqual(labels(), transfer.terminal_volume_labels(namespace))

            # The terminal type alone brings the WebUI data that owns its accounts.
            terminal_archive = archive.with_name("terminal-only.tar.gz")
            transfer.backup(project, terminal_archive, lambda: None, types=("webui", "terminal"), namespace=namespace,
                            terminal_key=target)
            command(["docker", "volume", "rm", *volumes])
            target.unlink()
            restored = transfer.restore(project, transfer.open_archive(terminal_archive), ("terminal",),
                                        namespace=namespace)
            self.assertEqual(restored.types, ("webui", "terminal"))
            self.assertEqual(target.read_bytes(), secret)
            self.assertEqual(volume_shell(f"cat /users/{folder_name}/code.c"), "persistent work")

    def test_offline_and_connected_workspaces_survive_a_cold_restart(self):
        # Each profile uses its own Compose project, volumes and labels, so both run at once.
        bridge_options = self.bridge_options()
        self.require_current_scripts()
        with ThreadPoolExecutor(max_workers=2) as executor:
            checks = {
                profile: executor.submit(self.check_profile, profile, webui, bridge_options=bridge_options)
                for profile, webui in (("offline", "open-webui"), ("online", "open-webui-online"))
            }
            for profile, check in checks.items():
                with self.subTest(profile=profile):
                    check.result()

    def require_current_scripts(self):
        """This test runs the scripts baked into the image, so the image must match the checkout."""
        for name in ("provisioner.py", "runner.py"):
            baked = subprocess.run([
                "docker", "run", "--rm", "--pull", "never", "--network", "none", "--entrypoint", "cat",
                self.image, f"/opt/loaf-terminal/{name}",
            ], capture_output=True)
            if baked.returncode or baked.stdout != (DATA / "docker/terminal-server" / name).read_bytes():
                self.fail(f"{self.image} lacks this checkout's {name}. Rebuild it with loaf build, "
                          "or select a current image with LOAF_TEST_TERMINAL_IMAGE.")

    def bridge_options(self):
        version = command(["docker", "version", "--format", "{{.Server.Version}}"]).strip()
        major = int(version.split(".")[0])
        if major < 26:
            self.skipTest(f"The terminal server requires Docker Engine 26 or newer (found {version}).")
        if major >= 28:
            return {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"}
        if os.environ.get("ALLOW_LEGACY_TERMINAL_BRIDGE") == "1":
            return {}
        return {"com.docker.network.bridge.inhibit_ipv4": "true"}

    @staticmethod
    def render_with_key(folder, project, profiles):
        key = folder / "terminal-server.key"
        key.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        key.chmod(0o600)
        packaged = cli.Compose(folder, project_name=project, files=[DATA / "compose.yml"])
        env = dict(os.environ, **packaged.variables())
        env.update(LOAF_TERMINAL_SECRET_FILE=str(key), LOAF_TERMINAL_COMMAND_SECONDS="45")
        step = cli.Step(profiles, ("config", "--format", "json"))
        return key, json.loads(compose_command(packaged, step, env=env))

    def check_profile(self, profile, webui, *, bridge_options):
        project = f"loaf-test-webui-{uuid4().hex}"
        control = f"{project}_terminal-control"
        # The terminal image uses these persisted labels for workspace ownership.
        label = f"label={transfer.WORKSPACE_PROJECT_LABEL}={project}"
        self.addCleanup(self.remove_terminal_assets, project)
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            profiles = (profile, "terminal")
            key, original = self.render_with_key(folder, project, profiles)

            # Use the packaged services with disposable data and a closed network.
            # This test exercises the WebUI connection without starting inference.
            services = {name: original["services"][name] for name in (webui, "terminal-server")}
            services["terminal-server"]["image"] = self.image
            if not bridge_options:
                services["terminal-server"]["working_dir"] = "/opt/loaf-terminal"
                services["terminal-server"]["entrypoint"] = [
                    "/usr/bin/python3", "-c",
                    "import asyncio; from provisioner import serve; asyncio.run(serve(allow_legacy_bridge=True))",
                ]
            services[webui].pop("depends_on")
            services[webui].pop("ports")
            services[webui]["environment"].update(
                ENABLE_TOOL_SERVERS="true", ENABLE_INITIAL_ADMIN_SIGNUP="true",
                ENABLE_OLLAMA_API="false", ENABLE_VERSION_UPDATE_CHECK="false",
                RAG_EMBEDDING_ENGINE="ollama", WEBUI_SECRET_KEY=secrets.token_urlsafe(32),
            )
            private_network = {"terminal-control": {}}
            services[webui]["networks"] = dict(private_network)
            networks = {"terminal-control": original["networks"]["terminal-control"]}
            if profile == "online":
                # Represent the online attachment with an internal bridge so
                # even the seed phase requires neither Internet nor host changes.
                services[webui]["networks"]["connected"] = {}
                networks["connected"] = {"internal": True, "name": f"{project}_connected"}
            fixture = {
                "services": services,
                "networks": networks,
                "volumes": {"open-webui": {"name": f"{project}-open-webui"}},
                "secrets": original["secrets"],
            }
            path = folder / "compose.json"
            compose = cli.Compose(folder, project_name=project, files=[path])

            def invoke(*arguments):
                return compose_command(compose, cli.Step(profiles, arguments))

            def check(phase, *args):
                mount = ["--mount", f"type=bind,source={key},target=/test.key,readonly"] if phase == "seed" else []
                output = command([
                    "docker", "run", "--rm", "--pull", "never", "--network", control, *mount,
                    "--entrypoint", "/usr/bin/python3", "-i", services["terminal-server"]["image"], "-", webui, phase, *args,
                ], input=Path(__file__).with_name("terminal_server_checks.py").read_text(encoding="utf-8"), timeout=270)
                retained = json.loads(output) if phase == "seed" else None
                expected = [retained] if retained else []
                deadline = monotonic() + 15
                while True:
                    # Draft containers have no chat label; deleted chats lose their container.
                    ids = command([
                        "docker", "ps", "-a", "--filter", label, "--format", '{{.Label "loaf.terminal.chat-id"}}',
                    ]).split()
                    if ids == expected or monotonic() >= deadline:
                        self.assertEqual(ids, expected)
                        break
                    sleep(0.25)
                self.assertTrue(transfer.terminal_volume_exists(folder, project))
                runners = command(["docker", "ps", "-q", "--filter", label]).split()
                self.assertEqual(len(runners), 3 if retained else 2)
                workspace_networks = command(["docker", "network", "ls", "-q", "--filter", label]).split()
                self.assertEqual(len(workspace_networks), len(runners))
                for network in json.loads(command(["docker", "network", "inspect", *workspace_networks])):
                    self.assertTrue(network["Internal"])
                    self.assertFalse(network["EnableIPv6"])
                    options = network["Options"] or {}
                    for option, value in bridge_options.items():
                        self.assertEqual(options.get(option), value)
                    if bridge_options:
                        bridge = f"br-{network['Id'][:12]}"
                        interface, = json.loads(command(["ip", "-j", "address", "show", "dev", bridge]))
                        ipv4 = [address for address in interface["addr_info"] if address["family"] == "inet"]
                        self.assertEqual(ipv4, [], bridge)
                if bridge_options:
                    provisioner = invoke("ps", "-q", "terminal-server").strip()
                    self.check_workspace_routes(runners, provisioner, control)
                return retained

            def start():
                invoke("up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "150", *services)
                return invoke("ps", "-q", webui).strip()

            def webui_networks(container):
                return set(json.loads(command([
                    "docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", container,
                ])))

            path.write_text(json.dumps(fixture), encoding="utf-8")
            try:
                webui_container = start()
                retained = check("seed")
                if profile == "online":
                    uplink = networks["connected"]["name"]
                    self.assertEqual(webui_networks(webui_container), {control, uplink})
                    command(["docker", "network", "disconnect", uplink, webui_container])
                    services[webui]["networks"] = private_network
                self.assertEqual(webui_networks(webui_container), {control})
                invoke("down", "--timeout", "45")
                # The drafts and the retained chat keep their stopped containers for the next start.
                self.assertEqual(command(["docker", "ps", "-q", "--filter", label]).strip(), "")
                self.assertEqual(len(command(["docker", "ps", "-aq", "--filter", label]).split()), 3)
                networks.pop("connected", None)
                path.write_text(json.dumps(fixture), encoding="utf-8")
                self.assertEqual(webui_networks(start()), {control})
                check("reopen", retained)
            finally:
                invoke("down", "--volumes", "--timeout", "45")

    def check_workspace_routes(self, runners, provisioner, control):
        inspected = json.loads(command(["docker", "inspect", *runners, provisioner]))
        broker = inspected.pop()["NetworkSettings"]["Networks"][control]["IPAddress"]
        addresses = []
        for runner in inspected:
            network, = runner["NetworkSettings"]["Networks"].values()
            addresses.append(network["IPAddress"])
        for index, runner in enumerate(runners):
            peers = addresses[:index] + addresses[index + 1:]
            command(["docker", "exec", "-i", runner, "/usr/bin/python3", "-", broker, *peers], input="""
import socket
import sys
from pathlib import Path

routes = Path("/proc/net/route").read_text().splitlines()[1:]
assert all(route.split()[1] != "00000000" for route in routes), routes
ipv6 = Path("/proc/net/if_inet6")
assert not ipv6.is_file() or not ipv6.read_text().strip(), "IPv6 enabled in workspace"
for target in sys.argv[1:]:
    try:
        with socket.create_connection((target, 8000), timeout=0.75):
            raise AssertionError(f"Workspace reached {target}")
    except OSError:
        pass
""")


@unittest.skipUnless(importlib.util.find_spec("open_terminal"), "Install the pinned Open Terminal for API tests.")
class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_tool_process_exits_when_its_limit_expires(self):
        from httpx import ASGITransport, AsyncClient

        with TemporaryDirectory() as folder, patch.dict(os.environ, {
            "HOME": folder,
            "OPEN_TERMINAL_API_KEY": "test-key",
            "OPEN_TERMINAL_LOG_DIR": str(Path(folder) / "logs"),
            "OPEN_TERMINAL_MULTI_USER": "false",
            "OPEN_TERMINAL_ENABLE_BROWSER": "false",
            "OPEN_TERMINAL_ENABLE_DESKTOP": "false",
            "OPEN_TERMINAL_ENABLE_NOTEBOOKS": "false",
        }):
            module = load_component("runner")
            app = module.build_app(1)
            async with app.router.lifespan_context(app), AsyncClient(
                transport=ASGITransport(app=app), base_url="http://runner",
            ) as client:
                self.assertTrue((await client.get("/api/config")).json()["features"]["terminal"])
                self.assertIn("/execute", (await client.get("/openapi.json")).json()["paths"])
                headers = {"Authorization": "Bearer test-key"}
                response = await client.post("/execute?wait=8", headers=headers, json={
                    "command": "sleep 30", "cwd": folder,
                })
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertNotEqual(result["status"], "running")
                self.assertNotEqual(result["exit_code"], 0)
