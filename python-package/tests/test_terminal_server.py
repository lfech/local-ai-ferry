"""Check terminal-server options, per-account lifecycle and the terminal proxy."""

import asyncio
from contextlib import asynccontextmanager, closing, redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
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

    def test_legacy_settings_and_default_key_survive_rename(self):
        with TemporaryDirectory() as folder:
            project = Path(folder)
            settings = project / "config"
            settings.mkdir()
            (settings / "config.ini").write_text(
                "[terminal_server]\ncpus=3\n"
                "[tool_server]\nenabled=true\ncpus=5\n"
                "[tests]\nrun_tool_server_tests=true\nrun_terminal_server_tests=false\nallow_legacy_tool_bridge=true\n"
            )
            legacy = project / config.LEGACY_TERMINAL_SECRET
            legacy.write_text("a" * 32 + "\n")
            loaded = config.load(DATA / "config", settings)
            self.assertTrue(loaded.terminal_server_enabled)
            self.assertEqual(loaded.terminal_server_cpus, 3)
            self.assertFalse(loaded.run_terminal_server_tests)
            self.assertTrue(loaded.allow_legacy_terminal_bridge)
            compose = cli.Compose(project, settings=loaded)
            self.assertEqual(compose.terminal_secret_file(), legacy)
            current = project / config.TERMINAL_SECRET
            current.write_bytes(b"a" * 512 + b"\r\n")
            self.assertEqual(compose.terminal_secret_file(), current)
            config.read_terminal_secret(current)
            current.write_bytes(b"a" * 512 + b"\r\n\n")
            with self.assertRaises(config.ConfigError):
                config.read_terminal_secret(current)


@unittest.skipUnless(ClientSession, "Install aiohttp to run the Docker-free provisioner tests.")
class ProvisionerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = load_component("provisioner")
        self.secret = secrets.token_urlsafe(32)

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
                "user": request.headers["X-User-Id"], "session": request.headers["X-Session-Id"],
                "file": await request.text(), "query": request.query["file"],
            })

        upstream = web.Application()
        upstream.router.add_route("*", "/{path:.*}", backend)
        async with TestServer(upstream) as runner, ClientSession() as proxy_client:
            class Workspaces:
                client = proxy_client
                users = []

                @asynccontextmanager
                async def lease(self, user, session=""):
                    self.users.append((user, session))
                    workspace = module.Workspace("container", "network", runner.host, "runner-only")
                    with patch.object(module, "PORT", runner.port):
                        yield workspace

                async def sweep(self):
                    await asyncio.Future()

                async def close(self):
                    pass

            manager = Workspaces()
            metadata = {"config": {"features": {"terminal": True}}, "schema": {
                "paths": {"/execute": {}, "/agents": {}},
            }}
            headers = {"Authorization": "Bearer " + self.secret}
            identity = {"X-User-Id": "account-a", "X-Session-Id": "chat-a"}
            async with TestClient(TestServer(module.application(manager, self.secret, metadata))) as client:
                async with client.get("/api/config", headers=headers) as result:
                    self.assertTrue((await result.json())["features"]["terminal"])
                async with client.get("/openapi.json", headers=headers) as result:
                    self.assertEqual(set((await result.json())["paths"]), {"/execute"})
                for path, info, status in (("/execute", {}, 401), ("/execute", headers, 403),
                                           ("/api/v1/terminals/templates", headers, 404)):
                    async with client.get(path, headers=info) as result:
                        self.assertEqual(result.status, status)
                self.assertEqual(manager.users, [])
                async with client.post("/files/upload", headers={**headers, **identity},
                                       params={"file": "hello + world"}, data="hello") as result:
                    self.assertEqual(await result.json(), {
                        "user": "account-a", "session": "chat-a", "file": "hello", "query": "hello + world",
                    })
                async with client.ws_connect("/api/terminals/session", headers=identity) as socket:
                    await socket.send_json({"type": "auth", "token": "bad"})
                    self.assertEqual((await socket.receive()).type, WSMsgType.CLOSE)
                self.assertEqual(manager.users, [("account-a", "chat-a")])
                async with client.ws_connect("/api/terminals/session", headers=identity) as socket:
                    await socket.send_json({"type": "auth", "token": self.secret})
                    self.assertEqual(await socket.receive_json(), {
                        "type": "auth", "token": "runner-only", "chat_id": "chat-a", "identity": "account-a",
                    })
                self.assertEqual(manager.users, [("account-a", "chat-a"), ("account-a", "chat-a")])

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
        spec = manager.container_spec("workspace", policy.labels("key"), "runner-only", "user-volume")
        self.assertEqual(spec["User"], "1000:1000")
        self.assertEqual(spec["Image"], "pinned-image-id")
        self.assertEqual(spec["WorkingDir"], "/home/user")
        self.assertIn("OPEN_TERMINAL_START_DIR=/home/user", spec["Env"])
        self.assertIn("OPEN_TERMINAL_FILE_BROWSER_ROOT=/home", spec["Env"])
        host = spec["HostConfig"]
        self.assertEqual(host["NetworkMode"], "workspace")
        user_mount = {"Type": "volume", "Source": "user-volume", "Target": "/home/user"}
        self.assertEqual(host["Mounts"], [user_mount])
        self.assertTrue(host["ReadonlyRootfs"])
        self.assertEqual(host["CapDrop"], ["ALL"])
        self.assertNotIn("PortBindings", host)
        saved = manager.container_spec("workspace", policy.labels("key"), "runner-only", "user-volume",
                                       chat_volume="chat-volume")
        self.assertEqual(saved["WorkingDir"], "/home/user")
        self.assertIn("OPEN_TERMINAL_START_DIR=/home/user", saved["Env"])
        self.assertIn("OPEN_TERMINAL_FILE_BROWSER_ROOT=/home", saved["Env"])
        self.assertEqual(saved["HostConfig"]["Mounts"], [
            user_mount, {"Type": "volume", "Source": "chat-volume", "Target": "/home/chat"},
        ])
        temporary = manager.container_spec("workspace", policy.labels("key"), "runner-only", "user-volume",
                                            temporary=True)
        self.assertEqual(temporary["HostConfig"]["Mounts"], [user_mount])
        self.assertIn("/home/chat", temporary["HostConfig"]["Tmpfs"])

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
            manager.active[deleted_key] = module.Workspace("container", "network", "127.0.0.1", "key", active=1)
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


@unittest.skipUnless(os.environ.get("RUN_TERMINAL_SERVER_TESTS") == "1", "Set RUN_TERMINAL_SERVER_TESTS=1 for terminal-server Docker tests.")
class TerminalServerDockerTests(unittest.TestCase):
    def test_terminal_backup_restores_image_home_and_key(self):
        if not shutil.which("git"):
            self.skipTest("Git is required for full backups.")
        namespace = f"loaf-terminal-transfer-{uuid4().hex}"
        tags = transfer.image_names(namespace, include_terminal=True)
        workspace = transfer.workspace_prefix(namespace) + "a" * 64
        volumes = (*transfer.volume_names(namespace), workspace)
        for resource, names in (("image", tags), ("volume", volumes)):
            self.addCleanup(subprocess.run, ["docker", resource, "rm", *names],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for tag in tags:
            command(["docker", "image", "tag", "local/open-terminal:latest", tag])

        def workspace_shell(script):
            return command([
                "docker", "run", "--rm", "--pull", "never", "--network", "none",
                "--mount", f"type=volume,source={workspace},target=/workspace,volume-nocopy",
                "--entrypoint", "/bin/sh", tags[-1], "-c", script,
            ])

        with TemporaryDirectory() as folder:
            project = Path(folder)
            command(["git", "init", "--quiet"], cwd=project)
            secret = secrets.token_hex(32).encode() + b"\n"
            key = project / "source.key"
            key.write_bytes(secret)
            archive = project / "backup" / "fixture.tar.gz"
            transfer.create_workspace_volumes(project, namespace, (workspace,), "local", {})
            workspace_shell("printf 'persistent work' > /workspace/code.c && chown 1000:1000 /workspace/code.c")
            transfer.backup(
                project, archive, lambda: None, namespace=namespace, compression_level=0,
                exclude_project_file=key, include_terminal=True,
            )
            command(["docker", "volume", "rm", *volumes])
            command(["docker", "image", "rm", tags[-1]])
            target = project / "restored" / "terminal.key"
            transfer.restore(project, archive, namespace=namespace, terminal_key=target)
            self.assertEqual(target.read_bytes(), secret)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(workspace_shell("cat /workspace/code.c; stat -c ' %u:%g' /workspace/code.c").strip(),
                             "persistent work 1000:1000")
            details, = json.loads(command(["docker", "volume", "inspect", workspace]))
            self.assertEqual(details["Labels"], transfer.workspace_labels(namespace, workspace))

    def test_offline_workspaces_survive_a_cold_restart(self):
        self.check_profile("offline", "open-webui", bridge_options=self.bridge_options())

    def test_connected_workspaces_reopen_after_removing_the_uplink(self):
        self.check_profile("online", "open-webui-online", bridge_options=self.bridge_options())

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

    def test_legacy_terminal_blocks_migration_until_launcher_stops_it(self):
        self.bridge_options()  # Applies the same Engine prerequisite to the provisioner.
        temporary = TemporaryDirectory(prefix="loaf-terminal-migration-")
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        version = f"loaf-terminal-migration-{uuid4().hex}"
        project = config.namespace(version)
        profiles = ("terminal",)
        _, original = self.render_with_key(folder, project, profiles)
        terminal = original["services"]["terminal-server"]
        # Use a normal checkout layout for the real stop command.
        (folder / "docker").symlink_to(DATA / "docker", target_is_directory=True)
        (folder / "compose.yml").write_text(json.dumps({
            "services": {"terminal-server": terminal},
            "networks": {"terminal-control": original["networks"]["terminal-control"]},
            "volumes": {"open-webui": {"name": f"{project}-open-webui"}},
            "secrets": original["secrets"],
        }), encoding="utf-8")
        compose = cli.Compose(folder, namespace=project)
        self.addCleanup(compose_command, compose, cli.Step(profiles, ("down", "--volumes")), timeout=90)

        def legacy_container(owner):
            container = command([
                "docker", "run", "-d", "--init", "--pull", "never", "--network", "none",
                "--label", f"com.docker.compose.project={owner}",
                "--label", "com.docker.compose.service=tool-server",
                "--entrypoint", "/bin/sleep", terminal["image"], "180",
            ]).strip()
            self.addCleanup(command, ["docker", "rm", "-f", container])
            return container

        legacy = legacy_container(project)
        unrelated = legacy_container(f"{project}-unrelated")
        with self.assertRaisesRegex(AssertionError, "previous terminal server is still running"):
            compose_command(compose, cli.Step(profiles, ("run", "--rm", "--no-deps", "-T", "terminal-server")))
        command([sys.executable, "-m", "loaf", "--version", version, "stop", "-n"], cwd=folder)
        for container, running in ((legacy, "false"), (unrelated, "true")):
            with self.subTest(container=container):
                self.assertEqual(
                    command(["docker", "inspect", "-f", "{{.State.Running}}", container]).strip(),
                    running,
                )
        compose_command(compose, cli.Step(
            profiles, ("up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "45", "terminal-server"),
        ))

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
            if not bridge_options:
                services["terminal-server"]["working_dir"] = "/opt/owo-terminal"
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
