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
            (project / "docker-project").symlink_to(DATA, target_is_directory=True)
            (project / "config/config.ini").write_text(
                "[terminal_server]\nenabled=true\nsecret_file=private/terminal-server.key\ncpus=3\n"
            )
            settings = config.load(DATA / "config", project / "config")
            compose = cli.Compose(project, settings=settings)
            self.assertEqual(compose.variables()["LOAF_TERMINAL_SECRET_FILE"], str(project / "private/terminal-server.key"))
            self.assertEqual(compose.variables()["LOAF_TERMINAL_CPUS"], "3")
            for mode in ("online", "offline", "build", "stop"):
                with self.subTest(mode=mode), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli.main([mode, "--project-dir", folder, "--dry-run", "-v"]), 0)
                    self.assertIn("--profile terminal", output.getvalue())
                    if mode in ("online", "offline"):
                        self.assertIn("--wait-timeout 300 terminal-server", output.getvalue())
        stop = cli.plan("offline", terminal_server=False)[1]
        self.assertIn("terminal-server", stop.arguments)
        self.assertEqual(cli.plan("build", terminal_server=True)[-1].arguments[-1], "terminal-server")
        self.assertEqual(cli.plan("offline", terminal_server=True, import_definitions=True)[-1].arguments[-1], "ollama-offline")

    def test_missing_key_and_disabling_auth_fail_before_docker(self):
        with TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "config").mkdir()
            (project / "docker-project").symlink_to(DATA, target_is_directory=True)
            settings = project / "config/config.ini"
            settings.write_text("[terminal_server]\nenabled=true\n")
            with patch.object(cli.Compose, "execute") as execute, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(cli.main(["offline", "--project-dir", folder]), 1)
                self.assertIn("Terminal server key file is missing", errors.getvalue())
                execute.assert_not_called()
            invalid = "a" * 32 + " b"
            (project / config.TERMINAL_SECRET).write_text(invalid)
            with patch.object(cli.Compose, "execute") as execute, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(cli.main(["offline", "--project-dir", folder]), 1)
                self.assertIn("Invalid terminal server key", errors.getvalue())
                self.assertNotIn(invalid, errors.getvalue())
                execute.assert_not_called()
            settings.write_text("[webui]\nauth=false\n[terminal_server]\nenabled=true\n")
            with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
                cli.main(["online", "--project-dir", folder])
            self.assertIn("auth = true", errors.getvalue())

    def test_terminal_key_uses_the_current_path_and_validates_its_maximum_size(self):
        with TemporaryDirectory() as folder:
            project = Path(folder)
            settings = project / "config"
            settings.mkdir()
            (settings / "tool-server.key").write_text("a" * 32 + "\n")
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
            (project / "docker-project").symlink_to(DATA, target_is_directory=True)
            (project / "config").mkdir()
            (project / config.TERMINAL_SECRET).write_text("a" * 32)
            (project / "config/config.ini").write_text(
                "[terminal_server]\nenabled=true\napt_source_file=config/missing.list\n",
            )
            with patch.object(cli.Compose, "execute") as execute, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(cli.main(["offline", "--project-dir", folder]), 1)
            self.assertIn("APT source file is missing", errors.getvalue())
            execute.assert_not_called()

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

    async def test_discovery_and_streams_use_verified_identity_and_runner_credential(self):
        module = self.module
        try:
            with sockets.socket() as listener:
                listener.bind(("127.0.0.1", 0))
        except PermissionError as exc:
            self.skipTest(f"Loopback sockets unavailable: {exc}")

        async def backend(request):
            self.assertEqual(request.headers.get("Authorization"), "Bearer runner-only")
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
                "id": "started-by-account", "cwd": "/home/user/project",
            })

        upstream = web.Application()
        upstream.router.add_route("*", "/{path:.*}", backend)
        async with TestServer(upstream) as runner, ClientSession() as proxy_client:
            class Workspaces(module.Workspaces):
                packages = None

                def __init__(self):
                    self.client = proxy_client
                    self.directories = {}
                    self.user_processes = {}
                    self.users = []

                @asynccontextmanager
                async def lease(self, user, session="", role=module.USER_ROLE):
                    self.users.append((user, session, role))
                    workspace = module.Workspace("container", "network", runner.host, "runner-only")
                    with patch.object(module, "PORT", runner.port):
                        yield workspace

                async def sweep(self):
                    await asyncio.Future()

                async def close(self):
                    pass

            manager = Workspaces()
            metadata = {"config": {"features": {"terminal": True}}, "schema": {"paths": {
                "/execute": {"post": {"operationId": "run_command", "description": "Execute."}}, "/agents": {},
                "/api/terminals/user/output": {}, "/api/terminals/user/input": {},
            }}}
            headers = {"Authorization": "Bearer " + self.secret}
            identity = {"X-User-Id": "account-a", "X-Session-Id": "chat-a"}
            authorized = {**headers, **identity}
            async with TestClient(TestServer(module.application(manager, self.secret, metadata))) as client:
                async with client.get("/api/config", headers=headers) as result:
                    self.assertTrue((await result.json())["features"]["terminal"])
                async with client.get("/openapi.json", headers=headers) as result:
                    paths = (await result.json())["paths"]
                self.assertEqual(set(paths), {"/agent/execute", "/agent/api/terminals/user/output"})
                self.assertIn("/home/user is read-only", paths["/agent/execute"]["post"]["description"])
                self.assertNotIn("apt-get", paths["/agent/execute"]["post"]["description"])
                for path, info, status in (("/execute", {}, 401), ("/execute", headers, 403),
                                           ("/api/v1/terminals/templates", headers, 404),
                                           ("/agent/api/terminals/user/input", authorized, 404),
                                           ("/agent/api/terminals", authorized, 404),
                                           ("/agent/agents", authorized, 404),
                                           ("/agent/packages/install", authorized, 404)):
                    async with client.get(path, headers=info) as result:
                        self.assertEqual(result.status, status, path)
                async with client.post("/agent/api/terminals/user/output", headers=authorized) as result:
                    self.assertEqual(result.status, 404)
                self.assertEqual(manager.users, [])

                # The account's own requests use the user runner.
                async with client.post("/files/upload", headers=authorized,
                                       params={"file": "hello + world"}, data="hello") as result:
                    reply = await result.json()
                self.assertEqual((reply["user"], reply["session"], reply["path"], reply["body"], reply["query"]),
                                 ("account-a", "chat-a", "/files/upload", "hello", {"file": "hello + world"}))
                async with client.post("/files/cwd", headers=authorized, json={"path": "/home/user/project"}):
                    pass
                key = module.account_workspace_key("account-a", "chat-a")
                self.assertEqual(manager.directories, {key: "/home/user/project"})
                async with client.post("/execute", headers=authorized, json={"command": "git clone x"}):
                    pass
                self.assertEqual(manager.users[-3:], [("account-a", "chat-a", module.USER_ROLE)] * 3)

                # Model tools reach the agent runner without the prefix. Command views follow their command.
                async with client.post("/agent/execute", headers=authorized, json={"command": "ls"}) as result:
                    reply = await result.json()
                self.assertEqual((reply["path"], json.loads(reply["body"])), ("/execute", {"command": "ls"}))
                async with client.get("/agent/api/terminals/user/output", headers=authorized) as result:
                    self.assertEqual((await result.json())["path"], "/api/terminals/user/output")
                for path in ("/execute", "/execute/started-by-account/status", "/execute/model-command/status"):
                    async with client.get(path, headers=authorized) as result:
                        self.assertEqual(result.status, 200)
                self.assertEqual([role for _, _, role in manager.users[-5:]], [
                    module.AGENT_ROLE, module.USER_ROLE, module.AGENT_ROLE, module.USER_ROLE, module.AGENT_ROLE,
                ])

                # APT commands never reach a runner, and the server reports when it has no package source.
                calls = len(manager.users)
                async with client.post("/agent/execute", headers=authorized,
                                       json={"command": "sudo apt-get update && apt-get install -y curl"}) as result:
                    reply = await result.json()
                self.assertEqual((reply["status"], reply["exit_code"]), ("done", 100))
                self.assertIn("no package source", reply["output"][0]["data"])
                self.assertEqual(len(manager.users), calls)

                async with client.ws_connect("/api/terminals/session", headers=identity) as socket:
                    await socket.send_json({"type": "auth", "token": "bad"})
                    self.assertEqual((await socket.receive()).type, WSMsgType.CLOSE)
                self.assertEqual(len(manager.users), calls)
                async with client.ws_connect("/api/terminals/session", headers=identity) as socket:
                    await socket.send_json({"type": "auth", "token": self.secret})
                    self.assertEqual(await socket.receive_json(), {
                        "type": "auth", "token": "runner-only", "chat_id": "chat-a", "identity": "account-a",
                    })
                async with client.ws_connect("/execute", headers=identity) as socket:
                    await socket.send_json({"type": "auth", "token": self.secret})
                    await socket.receive_json()
                self.assertEqual(manager.users[calls:], [("account-a", "chat-a", module.USER_ROLE),
                                                         ("account-a", "chat-a", module.AGENT_ROLE)])
                with self.assertRaises(Exception):
                    async with client.ws_connect("/agent/execute", headers=identity):
                        pass

    async def test_concurrent_requests_reuse_runners_and_idle_cleanup_keeps_active_jobs(self):
        module = self.module
        policy = module.Policy("test-project", 1, 256, 2, 5, 1, "local", {})
        docker = AsyncMock()

        class HealthyClient:
            @asynccontextmanager
            async def get(self, *_args, **_kwargs):
                class Response:
                    status = 200
                await asyncio.sleep(0)
                yield Response()

        manager = module.Workspaces(policy, docker, HealthyClient(), "provisioner", "image", None)
        first = module.Workspace("a", "a-net", "127.0.0.1", "key-a")
        second = module.Workspace("b", "b-net", "127.0.0.1", "key-b")
        third = module.Workspace("c", "c-net", "127.0.0.1", "key-c")
        manager.create = AsyncMock(side_effect=[first, second, third])
        manager.remove = AsyncMock()

        async def open_workspace(user):
            async with manager.lease(user) as workspace:
                await asyncio.sleep(0)
                return workspace

        a, again, b = await asyncio.gather(open_workspace("A"), open_workspace("A"), open_workspace("B"))
        self.assertIs(a, again)
        self.assertIsNot(a, b)
        self.assertEqual(manager.create.await_count, 2)
        first.used = second.used = monotonic() - 6
        docker.call.side_effect = [
            {"Processes": [["1"], ["2"], ["3"]]},
            {"Processes": [["1"], ["2"]]},
            {"Processes": [["1"], ["2"], ["3"]]},
            {"Processes": [["1"], ["2"]]},
        ]
        self.assertIs(await open_workspace("C"), third)
        manager.remove.assert_awaited_once_with("b", "b-net")
        self.assertEqual(list(manager.active.values()), [first, third])
        with self.assertRaises(web.HTTPServiceUnavailable):
            await open_workspace("D")
        manager.remove.assert_awaited_once_with("b", "b-net")
        await manager.reap_idle()
        manager.remove.assert_awaited_with("a", "a-net")
        self.assertEqual(list(manager.active.values()), [third])

    def test_workload_uses_fixed_unprivileged_docker_policy(self):
        module = self.module
        policy = module.Policy("test-project", 2, 512, 2, 5, 1, "local", {})
        manager = module.Workspaces(policy, None, None, "provisioner", "pinned-image-id", None)
        spec = manager.container_spec("workspace", policy.runner_labels("key", "user"), "runner-only", "user-volume")
        self.assertEqual(spec["User"], "1000:1000")
        self.assertEqual(spec["Image"], "pinned-image-id")
        self.assertEqual(spec["WorkingDir"], "/home/user")
        self.assertIn("HOME=/home/user", spec["Env"])
        self.assertIn("OPEN_TERMINAL_START_DIR=/home/user", spec["Env"])
        self.assertIn("OPEN_TERMINAL_FILE_BROWSER_ROOT=/home", spec["Env"])
        host = spec["HostConfig"]
        self.assertEqual(host["NetworkMode"], "workspace")
        user_mount = {"Type": "volume", "Source": "user-volume", "Target": "/home/user", "ReadOnly": False}
        chat_mount = {"Type": "volume", "Source": "chat-volume", "Target": "/home/chat"}
        self.assertEqual(host["Mounts"], [user_mount])
        self.assertNotIn("/home/chat", host["Tmpfs"])
        self.assertTrue(host["ReadonlyRootfs"])
        self.assertEqual(host["CapDrop"], ["ALL"])
        self.assertNotIn("PortBindings", host)
        saved = manager.container_spec("workspace", policy.runner_labels("key", "user"), "runner-only",
                                       "user-volume", chat_volume="chat-volume")
        self.assertEqual(saved["WorkingDir"], "/home/user")
        self.assertIn("OPEN_TERMINAL_START_DIR=/home/user", saved["Env"])
        self.assertIn("OPEN_TERMINAL_FILE_BROWSER_ROOT=/home", saved["Env"])
        self.assertEqual(saved["HostConfig"]["Mounts"], [user_mount, chat_mount])

        # Model tool calls get the same chat folder and a read-only view of the account's folder.
        agent = manager.container_spec("workspace-agent", policy.runner_labels("key", "agent"), "runner-only",
                                       "user-volume", role="agent", chat_volume="chat-volume")
        self.assertEqual(agent["WorkingDir"], "/home/chat")
        self.assertIn("HOME=/home/chat", agent["Env"])
        self.assertIn("OPEN_TERMINAL_START_DIR=/home/chat", agent["Env"])
        self.assertEqual(agent["HostConfig"]["Mounts"], [{**user_mount, "ReadOnly": True}, chat_mount])
        self.assertEqual({key: agent["HostConfig"][key] for key in ("ReadonlyRootfs", "CapDrop", "SecurityOpt")},
                         {key: host[key] for key in ("ReadonlyRootfs", "CapDrop", "SecurityOpt")})
        draft = manager.container_spec("workspace-agent", policy.runner_labels("key", "agent"), "runner-only",
                                       "user-volume", role="agent")
        self.assertEqual(draft["HostConfig"]["Mounts"], [{**user_mount, "ReadOnly": True}])
        self.assertIn("/home/chat", draft["HostConfig"]["Tmpfs"])
        self.assertEqual(policy.runner_name("key", "agent"), policy.asset_name("key") + "-agent")
        key = "a" * 64
        agent_network = policy.runner_name(key, "agent")
        self.assertEqual(policy.runner_identity(policy.runner_labels(key, "agent"), agent_network), (key, "agent"))
        self.assertIsNone(policy.runner_identity(policy.runner_labels(key, "agent"), policy.asset_name(key)))
        self.assertIsNone(policy.runner_identity(policy.runner_labels(key, "user"), agent_network))
        self.assertEqual(policy.runner_identity(policy.labels(key), policy.asset_name(key)), (key, None))

    async def test_deleted_chats_remove_only_verified_chat_volumes(self):
        module = self.module
        policy = module.Policy("test-project", 1, 256, 4, 5, 1, "local", {})
        owner_key = hashlib.sha256(b"account").hexdigest()
        deleted_key = module.workspace_key(owner_key, "deleted")
        deleted_volume = policy.chat_volume(deleted_key)
        kept_volume = policy.chat_volume(module.workspace_key(owner_key, "kept"))
        docker = AsyncMock()
        listing = {"Volumes": [
            {"Name": deleted_volume, "Labels": policy.chat_labels("deleted", owner_key)},
            {"Name": kept_volume, "Labels": policy.chat_labels("kept", owner_key)},
            {"Name": policy.asset_name(owner_key), "Labels": policy.labels(owner_key)},
            {"Name": "unrelated", "Labels": policy.chat_labels("deleted", owner_key)},
        ]}
        docker.call.return_value = listing
        with TemporaryDirectory() as folder:
            database = Path(folder) / "webui.db"
            manager = module.Workspaces(policy, docker, None, "provisioner", "image", module.Chats(database))
            manager.remove = AsyncMock()
            manager.active[(deleted_key, "agent")] = module.Workspace("container", "network", "127.0.0.1", "key",
                                                                         active=1)
            with self.assertRaises(sqlite3.OperationalError):
                await manager.reap_deleted_chats()
            manager.remove.assert_not_awaited()
            self.assertEqual(docker.call.await_count, 1)
            self.assertFalse(database.exists())

            with closing(sqlite3.connect(database)) as db, db:
                db.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT, archived INTEGER)")
                db.execute("INSERT INTO chat VALUES ('kept', 'account', 1)")
            await manager.reap_deleted_chats()
            manager.remove.assert_awaited_once_with("container", "network")
            docker.call.assert_awaited_with("DELETE", f"/volumes/{deleted_volume}", missing=True)
            self.assertEqual(docker.call.await_count, 3)
            self.assertNotIn(deleted_key, manager.active)
            with self.assertRaises(web.HTTPNotFound):
                async with manager.lease("someone else", "kept"):
                    self.fail("A different account reached the saved chat's workspace")

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


    def test_plain_apt_commands_become_chat_package_operations(self):
        parse = self.module.parse_apt_command
        for command, expected in (
            ("apt-get install -y curl", [("install", ["curl"])]),
            ("sudo apt update && sudo apt install -y --no-install-recommends python3-numpy g++",
             [("install", ["python3-numpy", "g++"])]),
            ("DEBIAN_FRONTEND=noninteractive apt-get -yq install jq=1.7-1 libc6:amd64;",
             [("install", ["jq=1.7-1", "libc6:amd64"])]),
            ("apt-get purge curl; apt-get remove --auto-remove jq", [("remove", ["curl"]), ("remove", ["jq"])]),
            ("apt-get install 'curl'", [("install", ["curl"])]),
            ("apt-get update", []),
            ("sudo -E apt-get autoremove -y", []),
        ):
            with self.subTest(command=command):
                self.assertEqual(parse(command), expected)
        for command in (
            "", "ls", "apt-get install", "apt-get update curl", "apt-get upgrade", "apt-get install curl | tee log",
            "apt-get install curl && ls", "apt-get install curl || true", "apt-get install $PACKAGE",
            "apt-get install $(cat list)", "apt-get install ./local.deb", "apt-get install curl-",
            "apt-get -o Debug::pkgProblemResolver=1 install curl", "apt-get install curl > log",
            "&& apt-get install curl", "apt-get install curl &&", "apt-get install curl\napt-get install jq",
            "apt-get install 'a;b'", "sudo -u root apt-get install curl",
            "apt-get install " + " ".join(f"package{number}" for number in range(33)), None,
        ):
            with self.subTest(command=command):
                self.assertIsNone(parse(command))

    def test_chat_package_list_changes_only_its_own_packages(self):
        module = self.module
        plan = module.plan_packages
        self.assertEqual(plan([], [("install", ["jq", "curl"])]), ["curl", "jq"])
        self.assertEqual(plan(["curl", "jq"], [("install", ["jq=1.7-1"]), ("remove", ["curl:amd64"])]), ["jq=1.7-1"])
        self.assertEqual(plan(["curl"], [("remove", ["curl"]), ("install", ["curl"])]), ["curl"])
        with self.assertRaises(web.HTTPUnprocessableEntity) as refused:
            plan(["curl"], [("remove", ["cmake"])])
        self.assertIn("cmake was not installed for this chat", refused.exception.text)

    async def test_package_changes_rebuild_the_chat_image_from_the_shared_image(self):
        module = self.module
        policy = module.Policy("test-project", 1, 256, 2, 5, 1, "local", {})
        docker = AsyncMock()
        docker.call.return_value = {"Labels": policy.chat_labels("chat", hashlib.sha256(b"account").hexdigest()),
                                    "Driver": "local", "Options": {}}

        class Chats:
            async def owners(self, chats):
                return {chat: "account" for chat in chats}

        class Packages:
            def __init__(self):
                self.current, self.events = [], []

            async def requested(self, key):
                return self.current

            @asynccontextmanager
            async def install(self, key, image, packages):
                self.events.append(("install", image, packages))
                yield "installer"

            async def publish(self, key, container, image, packages, *, saved):
                self.events.append(("publish", container, image, packages, saved))
                self.current = packages

            async def remove(self, key):
                self.events.append(("remove",))
                self.current = []

        packages = Packages()
        manager = module.Workspaces(policy, docker, None, "provisioner", "shared-image", Chats(), packages=packages)
        manager.remove = AsyncMock()
        key = module.account_workspace_key("account", "chat")
        for role in module.ROLES:
            manager.active[(key, role)] = module.Workspace(role, role + "-net", "127.0.0.1", "credential")
        manager.directories[key] = "/home/chat"

        result = await manager.change_packages("account", "chat", [("install", ["jq", "curl"])])
        self.assertEqual(result, {"packages": ["curl", "jq"], "changed": True, "untracked_reset": False})
        self.assertEqual(packages.events, [("install", "shared-image", ["curl", "jq"]),
                                           ("publish", "installer", "shared-image", ["curl", "jq"], True)])
        self.assertEqual(manager.remove.await_args_list, [unittest.mock.call("user", "user-net"),
                                                          unittest.mock.call("agent", "agent-net")])
        self.assertEqual(manager.active, {})
        self.assertEqual(manager.installing, set())

        packages.events.clear()
        self.assertEqual(await manager.change_packages("account", "chat", [("install", ["jq"])]),
                         {"packages": ["curl", "jq"], "changed": False})
        with self.assertRaises(web.HTTPUnprocessableEntity):
            await manager.change_packages("account", "chat", [("remove", ["cmake"])])
        self.assertEqual(packages.events, [])
        await manager.change_packages("account", "chat", [("remove", ["jq"])])
        self.assertEqual(packages.events[0], ("install", "shared-image", ["curl"]))
        packages.events.clear()
        await manager.change_packages("account", "chat", [("remove", ["curl"])])
        self.assertEqual(packages.events, [("remove",)])

        packages.current = None
        result = await manager.change_packages("account", "chat", [("install", ["jq"])])
        self.assertTrue(result["untracked_reset"])
        for session in ("", "channel:room"):
            with self.assertRaises(web.HTTPBadRequest):
                await manager.change_packages("account", session, [("install", ["jq"])])
        with self.assertRaises(web.HTTPNotFound):
            await manager.change_packages("someone else", "chat", [("install", ["jq"])])

    async def test_apt_commands_from_model_tools_change_chat_packages(self):
        module = self.module

        class Workspaces(module.Workspaces):
            packages = type("Packages", (), {"source": {"Target": "/etc/apt/sources.list.d/loaf.list"}})()

            def __init__(self):
                self.directories = {}
                self.calls = []
                self.failure = None

            async def change_packages(self, user, session, operations):
                self.calls.append((user, session, operations))
                if self.failure:
                    raise self.failure
                return {"packages": ["curl"], "changed": True, "untracked_reset": False}

            @asynccontextmanager
            async def lease(self, user, session="", role=module.USER_ROLE):
                raise AssertionError("A package command reached a runner")
                yield

            async def sweep(self):
                await asyncio.Future()

            async def close(self):
                pass

        manager = Workspaces()
        metadata = {"config": {"features": {}}, "schema": {"paths": {"/execute": {"post": {"description": "Run."}}}}}
        headers = {"Authorization": "Bearer " + self.secret, "X-User-Id": "account", "X-Session-Id": "chat"}
        async with TestClient(TestServer(module.application(manager, self.secret, metadata))) as client:
            async with client.get("/openapi.json", headers=headers) as result:
                paths = (await result.json())["paths"]
            self.assertEqual({paths[path]["post"].get("operationId") for path in paths},
                             {None, "install_chat_packages", "remove_chat_packages"})
            self.assertIn("apt-get install|remove|purge", paths["/agent/execute"]["post"]["description"])

            command = "sudo apt-get update && sudo apt-get install -y curl"
            async with client.post("/agent/execute", headers=headers, json={"command": command}) as result:
                reply = await result.json()
            self.assertEqual((reply["command"], reply["status"], reply["exit_code"], reply["cwd"]),
                             (command, "done", 0, "/home/chat"))
            self.assertIn("Installed for this chat: curl.", reply["output"][0]["data"])
            self.assertEqual(manager.calls, [("account", "chat", [("install", ["curl"])])])

            manager.failure = web.HTTPUnprocessableEntity(text="E: Unable to locate package curl")
            async with client.post("/agent/execute", headers=headers,
                                   json={"command": "apt-get purge curl", "cwd": "/home/chat/project"}) as result:
                reply = await result.json()
            self.assertEqual((reply["exit_code"], reply["cwd"], reply["output"]),
                             (100, "/home/chat/project", [{"type": "stderr",
                                                           "data": "E: Unable to locate package curl\n"}]))
            self.assertEqual(manager.calls[-1][2], [("remove", ["curl"])])

            manager.failure = None
            async with client.post("/agent/packages/remove", headers=headers, json={"packages": ["curl"]}) as result:
                self.assertEqual(await result.json(), {"removed": ["curl"], "packages": ["curl"], "changed": True,
                                                       "untracked_reset": False})
            for body in ({"packages": ["curl-"]}, {"packages": []}, {"packages": "curl"}):
                async with client.post("/agent/packages/install", headers=headers, json=body) as result:
                    self.assertEqual(result.status, 400)
            async with client.post("/packages/install", headers=headers, json={"packages": ["curl"]}) as result:
                self.assertEqual(result.status, 404)

    def test_apt_guard_explains_chat_packages_in_the_shell(self):
        if os.geteuid() == 0:
            self.skipTest("The guard passes root through to APT.")
        with TemporaryDirectory() as folder:
            guard = Path(folder) / "apt-get"
            shutil.copy(DATA / "docker/terminal-server/apt-guard.sh", guard)
            guard.chmod(0o755)
            for arguments in (["install", "curl"], ["-y", "remove", "curl"], ["update"]):
                with self.subTest(arguments=arguments):
                    result = subprocess.run([str(guard), *arguments], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 100)
                    self.assertIn("apt-get: packages cannot be changed from this shell", result.stderr)
                    self.assertIn("apt-get install PACKAGE", result.stderr)
            if Path("/usr/bin/apt-get").is_file():
                self.assertEqual(subprocess.run([str(guard), "--version"], capture_output=True).returncode, 0)


@unittest.skipUnless(os.environ.get("RUN_TERMINAL_SERVER_TESTS") == "1", "Set RUN_TERMINAL_SERVER_TESTS=1 for terminal-server Docker tests.")
class TerminalServerDockerTests(unittest.TestCase):
    image = os.environ.get("LOAF_TEST_TERMINAL_IMAGE", "local/open-terminal:latest")

    def test_chat_packages_use_the_local_mirror_and_survive_restart(self):
        self.bridge_options()
        if not shutil.which("dpkg-deb"):
            self.skipTest("dpkg-deb is required to create the local APT fixture.")
        project_name = f"loaf-terminal-apt-{uuid4().hex}"
        control = f"{project_name}_terminal-control"
        image = f"local/open-terminal:{project_name}"
        webui = f"{project_name}-open-webui"
        command(["docker", "image", "tag", self.image, image])
        self.addCleanup(command, ["docker", "image", "rm", image])
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
        with urlopen(req, timeout=90) as response:
            assert response.status == status, (path, response.status)
            return json.load(response)
    except HTTPError as error:
        assert error.code == status, (path, error.code, error.read().decode())

def run(command, *, chat='first', agent=True, exit_code=0):
    # Model tools use the agent prefix. Plain paths are the account's own requests.
    result = request(('/agent' if agent else '') + '/execute?wait=15', {'command': command}, chat=chat)
    assert result['exit_code'] == exit_code, result
    return result
""" + script)

            apps = f"label=loaf.terminal.apt-project={project_name}"
            saved_chats = f"label=loaf.terminal.chat-project={project_name}"
            workspace = f"label={transfer.WORKSPACE_PROJECT_LABEL}={project_name}"
            start = ("up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "60", "terminal-server")
            try:
                invoke("create", "--no-build", "terminal-server")
                seed_database("CREATE TABLE chat (id TEXT PRIMARY KEY, user_id TEXT);"
                              "INSERT INTO chat VALUES ('first', 'alice'), ('second', 'alice');")
                invoke(*start)
                check("""\
paths = request('/openapi.json')['paths']
assert paths['/agent/packages/install']['post']['operationId'] == 'install_chat_packages'
assert paths['/agent/packages/remove']['post']['operationId'] == 'remove_chat_packages'
assert '/agent/execute' in paths and '/execute' not in paths, sorted(paths)
request('/files/cwd', {'path': '/home/chat'})
run('echo personal > /home/user/shared.txt', agent=False)
run('echo chat > kept.txt && test -f /home/user/shared.txt && ! touch /home/user/model.txt')
run('test "$(id -u)" = 1000 && test "$HOME" = /home/chat && test ! -e /home/user/model.txt')
request('/agent/packages/install', {'packages': ['loaf-chat-demo']}, chat='', status=400)
request('/agent/packages/install', {'packages': ['loaf-chat-demo']}, user='bob', status=404)
request('/agent/packages/install', {'packages': ['--allow-unauthenticated']}, status=400)
result = run('sudo apt-get update && sudo apt-get install -y loaf-chat-demo')
assert 'Installed for this chat: loaf-chat-demo.' in result['output'][0]['data'], result
assert request('/files/cwd')['cwd'] == '/home/chat'
run('test -f kept.txt && test "$(loaf-chat-demo)" = configured')
run('test "$(loaf-chat-demo)" = configured && echo account > /home/user/account.txt', agent=False)
request('/files/cwd', {'path': '/home/user'})
run('test -f shared.txt && test "$(loaf-chat-demo)" = configured && ! touch model.txt')
assert 'cmake was not installed for this chat' in run('apt-get remove cmake', exit_code=100)['output'][0]['data']
request('/agent/packages/install', {'packages': ['loaf-unavailable-package']}, status=422)
run('test "$(loaf-chat-demo)" = configured')
run('test -f /home/user/shared.txt && ! command -v loaf-chat-demo', chat='second')
run('apt-get purge loaf-chat-demo', chat='second', exit_code=100)
result = request('/agent/packages/install', {'packages': ['loaf-chat-demo']}, chat='temporary:first')
assert result['packages'] == ['loaf-chat-demo'], result
run('printf temporary > /home/chat/note.txt', chat='temporary:first')
run('test "$(loaf-chat-demo)" = configured && test -f /home/chat/note.txt', chat='temporary:first', agent=False)
run('! command -v loaf-chat-demo', chat='temporary:second')
""")
                invoke("stop", "--timeout", "45", "terminal-server")
                invoke(*start)
                check("run('test \"$(loaf-chat-demo)\" = configured')\n"
                      "run('! command -v loaf-chat-demo', chat='second')\n"
                      "run('! command -v loaf-chat-demo && test ! -e /home/chat/note.txt', chat='temporary:first')\n"
                      "run('apt-get purge loaf-chat-demo')\n"
                      "run('! command -v loaf-chat-demo && test -f /home/chat/kept.txt')\n"
                      "run('apt-get install loaf-chat-demo')\n")
                seed_database("DELETE FROM chat WHERE id = 'first';")
                deadline = monotonic() + 20
                while command(["docker", "image", "ls", "-q", "--filter", apps]).strip():
                    self.assertLess(monotonic(), deadline, "Deleted chat's image was not removed")
                    sleep(1)
            except AssertionError:
                print(invoke("logs", "--no-color", "terminal-server"))
                raise
            finally:
                invoke("down", "--volumes", "--timeout", "45")
                for label in (workspace, saved_chats):
                    volumes = command(["docker", "volume", "ls", "-q", "--filter", label]).split()
                    if volumes:
                        command(["docker", "volume", "rm", *volumes])
                images = command(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}",
                                  "--filter", apps]).split()
                for tag in images:
                    if tag.startswith("local/loaf-terminal-chat:loaf-tools-"):
                        command(["docker", "image", "rm", tag])

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

    def test_runtime_apt_source_is_read_only_and_updates_on_recreate(self):
        self.bridge_options()
        project_name = f"loaf-terminal-runtime-{uuid4().hex}"
        control = f"{project_name}_terminal-control"
        image = f"local/open-terminal:{project_name}"
        command(["docker", "image", "tag", self.image, image])
        self.addCleanup(command, ["docker", "image", "rm", image])
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
                self.assertTrue(details["HostConfig"]["ReadonlyRootfs"])
                self.assertEqual(details["Config"]["User"], "1000:1000")
                self.assertEqual(command(["docker", "exec", runner, "/bin/cat", apt_target]), source.read_text())
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
                invoke(*start)
                self.assertNotEqual(check_workspace(), first)
            finally:
                invoke("down", "--volumes", "--timeout", "45")
                volumes = command(["docker", "volume", "ls", "-q", "--filter",
                                   f"label={transfer.WORKSPACE_PROJECT_LABEL}={project_name}"]).split()
                if volumes:
                    command(["docker", "volume", "rm", *volumes])

    def test_terminal_backup_restores_image_home_and_key(self):
        namespace = f"loaf-terminal-transfer-{uuid4().hex}"
        tags = transfer.image_names(namespace, include_terminal=True)
        workspace = transfer.workspace_prefix(namespace) + "a" * 64
        chat = {"id": "saved-chat", "owner": "a" * 64}
        chat_volume = transfer.chat_volume(namespace, chat)
        webui = next(name for name, role in transfer.volume_names(namespace).items() if role == "webui")
        volumes = (webui, workspace, chat_volume)
        for resource, names in (("image", tags), ("volume", volumes)):
            self.addCleanup(subprocess.run, ["docker", resource, "rm", *names],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for tag in tags:
            command(["docker", "image", "tag", self.image, tag])

        def volume_shell(volume, script):
            return command([
                "docker", "run", "--rm", "--pull", "never", "--network", "none",
                "--mount", f"type=volume,source={volume},target=/workspace,volume-nocopy",
                "--entrypoint", "/bin/sh", tags[-1], "-c", script,
            ])

        def labels(volume):
            details, = json.loads(command(["docker", "volume", "inspect", volume]))
            return details["Labels"]

        with TemporaryDirectory() as folder:
            project = Path(folder)
            secret = secrets.token_hex(32).encode() + b"\n"
            key = project / "source.key"
            key.write_bytes(secret)
            archive = project / "backup" / "fixture.tar.gz"
            transfer.create_workspace_volumes(project, namespace, (workspace,), "local", {})
            transfer.create_labeled_volume(project, chat_volume, transfer.chat_labels(namespace, chat), "local", {})
            volume_shell(workspace, "printf 'persistent work' > /workspace/code.c && chown 1000:1000 /workspace/code.c")
            volume_shell(chat_volume, "printf 'chat work' > /workspace/notes.md")
            transfer.backup(project, archive, lambda: None, types=("images", "webui", "chats"), namespace=namespace,
                            compression_level=0, terminal_key=key)
            command(["docker", "volume", "rm", *volumes])
            command(["docker", "image", "rm", tags[-1]])
            target = project / config.TERMINAL_SECRET
            restored = transfer.restore(project, transfer.open_archive(archive), namespace=namespace)
            self.assertEqual(restored.types, ("images", "webui", "chats"))
            self.assertEqual(target.read_bytes(), secret)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(volume_shell(workspace, "cat /workspace/code.c; stat -c ' %u:%g' /workspace/code.c").strip(),
                             "persistent work 1000:1000")
            self.assertEqual(volume_shell(chat_volume, "cat /workspace/notes.md"), "chat work")
            self.assertEqual(labels(workspace), transfer.workspace_labels(namespace, workspace))
            self.assertEqual(labels(chat_volume), transfer.chat_labels(namespace, chat))

            # Chats alone bring the WebUI data they belong to.
            chats_archive = archive.with_name("chats-only.tar.gz")
            transfer.backup(project, chats_archive, lambda: None, types=("webui", "chats"), namespace=namespace,
                            terminal_key=target)
            command(["docker", "volume", "rm", *volumes])
            target.unlink()
            restored = transfer.restore(project, transfer.open_archive(chats_archive), ("chats",), namespace=namespace)
            self.assertEqual(restored.types, ("webui", "chats"))
            self.assertEqual(target.read_bytes(), secret)
            self.assertEqual(volume_shell(workspace, "cat /workspace/code.c"), "persistent work")

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
        for name in ("provisioner.py", "runner.py", "apt-guard.sh"):
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
        project = f"loaf-terminal-test-{uuid4().hex}"
        control = f"{project}_terminal-control"
        # The terminal image uses these persisted labels for workspace ownership.
        label = f"label={transfer.WORKSPACE_PROJECT_LABEL}={project}"
        chat_label = f"label=loaf.terminal.chat-project={project}"
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
                    ids = command([
                        "docker", "volume", "ls", "--filter", chat_label,
                        "--format", '{{.Label "loaf.terminal.chat-id"}}',
                    ]).split()
                    if ids == expected or monotonic() >= deadline:
                        self.assertEqual(ids, expected)
                        break
                    sleep(0.25)
                self.assertEqual(len(transfer.list_terminal_workspaces(folder, project)), 2)
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
                self.assertEqual(command(["docker", "ps", "-aq", "--filter", label]).strip(), "")
                networks.pop("connected", None)
                path.write_text(json.dumps(fixture), encoding="utf-8")
                self.assertEqual(webui_networks(start()), {control})
                check("reopen", retained)
            finally:
                invoke("down", "--volumes", "--timeout", "45")
                volumes = [
                    name for scope in (label, chat_label)
                    for name in command(["docker", "volume", "ls", "-q", "--filter", scope]).split()
                ]
                if volumes:
                    command(["docker", "volume", "rm", *volumes])

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
