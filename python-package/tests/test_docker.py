"""Check Compose contracts, images, and disposable Docker workflows."""

import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from loaf import cli, config, transfer
from tests import DATA, TESTS_DIR
from tests.docker_support import DEFAULT_IMAGES, DOCKER_TESTS, command, compose_command, compose_config


DOCKER_DIR = DATA / "docker"


class CommandRunnerTests(unittest.TestCase):
    def test_utf8_output_is_decoded_independently_of_windows_locale(self):
        output = command([
            sys.executable, "-c",
            "import sys; sys.stdout.buffer.write('Gr\\u00fc\\u00dfe \\U0001f680'.encode('utf-8'))",
        ])
        self.assertEqual(output, "Gr\u00fc\u00dfe \U0001f680")

    def test_failure_includes_the_command_exit_status_and_both_streams(self):
        args = [
            sys.executable, "-c",
            "import sys; print('partial'); sys.stderr.buffer.write('caf\\u00e9'.encode('utf-8')); sys.exit(3)",
        ]
        with self.assertRaises(AssertionError) as failure:
            command(args)
        self.assertIn(repr(args), str(failure.exception))
        self.assertIn("exited with status 3", str(failure.exception))
        self.assertIn("stdout\npartial", str(failure.exception))
        self.assertIn("stderr\ncafé", str(failure.exception))

    def test_timeout_includes_partial_output(self):
        args = ["docker", "compose", "up"]
        timeout = subprocess.TimeoutExpired(args, 10, output=b"starting\n", stderr=b"waiting\xc4\n")
        with patch("tests.docker_support.subprocess.run", side_effect=timeout):
            with self.assertRaisesRegex(AssertionError, "(?s)timed out after 10s.*starting.*waiting\ufffd"):
                command(args, timeout=10)

    def test_raw_diagnostic_bytes_can_be_replaced(self):
        output = command([
            sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'log: \\xc4')",
        ], decode_errors="replace")
        self.assertEqual(output, "log: \ufffd")

    def test_structured_output_rejects_invalid_utf8(self):
        with self.assertRaises(UnicodeDecodeError):
            command([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'log: \\xc4')"])


@unittest.skipUnless(DOCKER_TESTS, "Set RUN_DOCKER_TESTS=1 to enable Docker tests.")
class ComposeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rendered = compose_config()
        cls.services = cls.rendered["services"]

    def test_profiles_use_local_singletons(self):
        self.assertEqual(set(self.services), {service for group in cli.SERVICES.values() for service in group})
        for profile, names in cli.SERVICES.items():
            for name in names:
                with self.subTest(service=name):
                    service = self.services[name]
                    self.assertEqual(service["profiles"], [profile])
                    self.assertEqual(service["scale"], 1)
                    self.assertEqual(service["pull_policy"], "never")

    def test_serving_services_and_the_terminal_server_restart_with_docker(self):
        rendered = compose_config(profiles=(*cli.PROFILES, "terminal"))["services"]
        for name, service in rendered.items():
            with self.subTest(service=name):
                # A prepared downloader must not start by itself on an offline host.
                self.assertEqual(service["restart"], "no" if name == "ollama-online" else "unless-stopped")

    def test_only_core_images_have_local_build_contexts(self):
        builders = {name for name, service in self.services.items() if "build" in service}
        self.assertEqual(builders, {"ollama-online", "ollama-offline", "open-webui"})
        for name in ("ollama-online", "ollama-offline"):
            with self.subTest(service=name):
                build = self.services[name]["build"]
                self.assertEqual(Path(build["context"]).resolve(), (DOCKER_DIR / "ollama").resolve())
                self.assertEqual(build["target"], name)

    def test_build_inputs_stay_inside_their_contexts(self):
        # Compose before 5.7.0 grants Buildx read access only to build contexts.
        # A Dockerfile or file secret elsewhere fails with Buildx 0.37.2 and newer.
        rendered = compose_config(profiles=(*cli.PROFILES, "terminal"))
        builds = {name: service["build"] for name, service in rendered["services"].items() if "build" in service}
        self.assertEqual(set(builds), {"ollama-online", "ollama-offline", "open-webui", "terminal-server"})
        for name, build in builds.items():
            with self.subTest(service=name):
                context = Path(build["context"]).resolve()
                self.assertTrue(context.is_relative_to(DOCKER_DIR.resolve()))
                dockerfile = (context / build.get("dockerfile", "Dockerfile")).resolve()
                self.assertTrue(dockerfile.is_file())
                self.assertTrue(dockerfile.is_relative_to(context), dockerfile)
                for secret in build.get("secrets", ()):
                    source = Path(rendered["secrets"][secret["source"]]["file"]).resolve()
                    self.assertTrue(source.is_relative_to(context), source)

    def test_ollama_profiles_share_model_storage(self):
        self.assertEqual(self.rendered["volumes"]["ollama"]["name"], "loaf-ollama")
        for name in ("ollama-online", "ollama-offline", "ollama-connected"):
            with self.subTest(service=name):
                service = self.services[name]
                self.assertTrue(any(
                    mount["source"] == "ollama" and mount["target"] == "/ollama"
                    for mount in service["volumes"]
                ))
                self.assertFalse(service.get("ports"))

    def test_serving_backends_share_image_and_request_nvidia_devices(self):
        offline = self.services["ollama-offline"]
        connected = self.services["ollama-connected"]
        self.assertEqual(connected["image"], offline["image"])
        for service in (offline, connected):
            with self.subTest(profile=service["profiles"][0]):
                devices = service["deploy"]["resources"]["reservations"]["devices"]
                self.assertIn({"driver": "nvidia", "count": -1, "capabilities": ["gpu"]}, devices)
                self.assertEqual(service["environment"]["OLLAMA_CONTEXT_LENGTH"], "16384")
                self.assertIn("ollama-imports-complete", service["healthcheck"]["test"][1])

    def test_cpu_override_removes_device_requests_from_both_backends(self):
        rendered = compose_config(cpu=True)
        self.assertEqual(rendered["volumes"], self.rendered["volumes"])
        self.assertEqual(rendered["networks"], self.rendered["networks"])
        for name in ("ollama-offline", "ollama-connected"):
            with self.subTest(service=name):
                service = rendered["services"][name]
                self.assertFalse(service.get("gpus"))
                resources = service.get("deploy", {}).get("resources", {})
                self.assertFalse(resources.get("reservations", {}).get("devices"))
                self.assertEqual(service["environment"]["CUDA_VISIBLE_DEVICES"], "-1")
                self.assertEqual(service["environment"]["NVIDIA_VISIBLE_DEVICES"], "void")
                self.assertEqual(service["image"], self.services[name]["image"])
                self.assertEqual(service["healthcheck"], self.services[name]["healthcheck"])

    def test_offline_serving_routes_webui_to_the_healthy_internal_backend(self):
        self.assertEqual(set(self.services["ollama-offline"]["networks"]), {"inference"})
        self.assertTrue(self.rendered["networks"]["inference"]["internal"])
        webui = self.services["open-webui"]
        self.assertEqual(set(webui["networks"]), {"inference", "web", "terminal-control"})
        self.assertEqual(webui["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual(webui["environment"]["OLLAMA_BASE_URL"], "http://ollama-offline:11434")
        self.assertEqual(webui["environment"]["WEBUI_AUTH"], "true")
        self.assertEqual(webui["depends_on"]["ollama-offline"]["condition"], "service_healthy")

    def test_connected_serving_retains_backend_alias_and_allows_downloads(self):
        connected = self.services["ollama-connected"]
        self.assertIn("ollama-offline", connected["networks"]["connected"]["aliases"])
        self.assertFalse(self.rendered["networks"]["connected"].get("internal", False))
        self.assertEqual(connected["environment"]["OLLAMA_NO_CLOUD"], "false")
        webui = self.services["open-webui-online"]
        self.assertEqual(webui["image"], self.services["open-webui"]["image"])
        self.assertEqual(webui["environment"]["OFFLINE_MODE"], "false")
        self.assertEqual(webui["environment"]["OLLAMA_BASE_URL"], "http://ollama-offline:11434")
        self.assertEqual(webui["environment"]["WEBUI_AUTH"], "true")
        self.assertEqual(webui["depends_on"]["ollama-connected"]["condition"], "service_healthy")

    def test_optional_terminal_uses_the_internal_control_network_and_private_key(self):
        namespace = config.namespace("terminal-contract")
        rendered = compose_config(namespace, profiles=(*cli.PROFILES, "terminal"))
        services = rendered["services"]
        terminal = services["terminal-server"]
        self.assertEqual(terminal["image"], "loaf/open-terminal:terminal-contract")
        self.assertEqual(terminal["pull_policy"], "never")
        self.assertEqual(set(terminal["networks"]), {"terminal-control"})
        self.assertTrue(rendered["networks"]["terminal-control"]["internal"])
        self.assertFalse(terminal.get("ports"))
        self.assertTrue(terminal["read_only"])
        webui_data, = [mount for mount in terminal["volumes"] if mount["target"] == "/var/lib/loaf-webui"]
        self.assertEqual(webui_data["source"], "open-webui")
        self.assertEqual(rendered["volumes"]["open-webui"]["name"], f"{namespace}-open-webui")
        self.assertTrue(webui_data["read_only"])
        self.assertEqual({secret["source"] for secret in terminal["secrets"]}, {"terminal-server-key"})
        for name, service in services.items():
            if name != "terminal-server":
                self.assertFalse(service.get("secrets"), name)
        for webui in ("open-webui", "open-webui-online"):
            self.assertIn("terminal-control", services[webui]["networks"])

    def test_enabled_terminal_gives_both_webui_services_the_key_for_their_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            key = Path(folder) / "terminal-server.key"
            key.write_text("k" * 32)
            settings = config.Settings(terminal_server_enabled=True, terminal_server_secret_file=key,
                                       terminal_server_webui_access="users")
            services = compose_config(profiles=(*cli.PROFILES, "terminal"), settings=settings)["services"]
        for name in ("open-webui", "open-webui-online"):
            with self.subTest(service=name):
                self.assertEqual([secret["source"] for secret in services[name]["secrets"]], ["terminal-server-key"])
                self.assertEqual(services[name]["environment"]["LOAF_TERMINAL_WEBUI_ACCESS"], "users")
        self.assertFalse(services["ollama-offline"].get("secrets"))

    def test_versioned_compose_uses_separate_images_and_volumes(self):
        namespace = config.namespace("v1-2")
        rendered = compose_config(namespace)
        self.assertEqual(rendered["name"], namespace)
        self.assertEqual(
            {service["image"] for service in rendered["services"].values()},
            set(transfer.image_names(namespace)),
        )
        self.assertEqual(
            {volume["name"] for volume in rendered["volumes"].values()},
            {f"{namespace}-ollama", f"{namespace}-open-webui"},
        )
        self.assertEqual(
            {key: network["name"] for key, network in rendered["networks"].items()},
            {key: f"{namespace}_{key}" for key in rendered["networks"]},
        )

    def test_ini_settings_reach_compose_and_environment_overrides_still_win(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "config.ini").write_text(
                "[images]\nollama_image = registry.example/ollama:pinned\n"
                "webui_image = registry.example/webui:pinned\n"
                "[ollama]\ncontext_length = 8192\nstartup_timeout = 75\ncuda_visible_devices = 0\n"
                "[webui]\nbind_address = ::1\nport = 3101\nauth = false\n"
                "[online]\nollama_cloud = false\nwebui_downloads = false\n",
                encoding="utf-8",
            )
            (project / ".env").write_text(
                "OLLAMA_CONTEXT_LENGTH=12288\nOLLAMA_IMAGE=registry.example/ollama:dotenv\n",
                encoding="utf-8",
            )
            env = {
                key: value for key, value in os.environ.items()
                if key not in {
                    "COMPOSE_ENV_FILES", "COMPOSE_DISABLE_ENV_FILE", "CUDA_VISIBLE_DEVICES",
                    "OLLAMA_CONTEXT_LENGTH", "OLLAMA_STARTUP_TIMEOUT", "OPEN_WEBUI_IMAGE",
                }
            }
            env["OLLAMA_IMAGE"] = "registry.example/ollama:shell"
            env["OLLAMA_SOURCE"] = "registry.example/ollama:source"
            compose = cli.Compose(project, settings=config.load(project))
            with patch.dict(os.environ, env, clear=True):
                env.update(compose.variables())
            step = cli.Step(cli.PROFILES, ("config", "--format", "json"))
            resolved = json.loads(compose_command(compose, step, env=env))
            # The project's .env applies only with docker_env_enable; exported variables win either way.
            enabled = cli.Compose(project, settings=dataclasses.replace(compose.settings, docker_env_enable=True))
            dotenv = json.loads(compose_command(enabled, step, env=env))["services"]
        self.assertEqual(dotenv["ollama-offline"]["environment"]["OLLAMA_CONTEXT_LENGTH"], "12288")
        self.assertEqual(dotenv["ollama-offline"]["build"]["args"]["OLLAMA_IMAGE"], env["OLLAMA_IMAGE"])
        services = resolved["services"]
        self.assertEqual(Path(services["ollama-online"]["build"]["context"]), DOCKER_DIR / "ollama")
        for name in ("ollama-online", "ollama-offline"):
            args = services[name]["build"]["args"]
            self.assertEqual(args["OLLAMA_IMAGE"], env["OLLAMA_IMAGE"])
            self.assertEqual(args["OLLAMA_SOURCE"], env["OLLAMA_SOURCE"])
        self.assertEqual(services["open-webui"]["build"]["args"]["OPEN_WEBUI_IMAGE"], "registry.example/webui:pinned")
        offline = services["ollama-offline"]["environment"]
        self.assertEqual(offline["OLLAMA_CONTEXT_LENGTH"], "8192")
        self.assertEqual(offline["OLLAMA_STARTUP_TIMEOUT"], "75")
        self.assertEqual(offline["CUDA_VISIBLE_DEVICES"], "0")
        for name in ("open-webui", "open-webui-online"):
            port, = services[name]["ports"]
            self.assertEqual((port["host_ip"], port["published"], port["target"]), ("::1", "3101", 8080))
            self.assertEqual(services[name]["environment"]["WEBUI_AUTH"], "false")
        self.assertEqual(services["ollama-connected"]["environment"]["OLLAMA_NO_CLOUD"], "true")
        self.assertEqual(services["open-webui-online"]["environment"]["OFFLINE_MODE"], "true")
        self.assertEqual(services["open-webui-online"]["environment"]["HF_HUB_OFFLINE"], "1")
        self.assertTrue(resolved["networks"]["inference"]["internal"])

    def test_retired_compose_variables_do_not_change_the_deployment(self):
        # loaf sets LOAF_NAMESPACE itself; LOAF_VERSION is the name it had before.
        with patch.dict(os.environ, LOAF_NAMESPACE="other-deployment", LOAF_VERSION="other-deployment"):
            resolved = compose_config()
        self.assertEqual(resolved["name"], "loaf")
        self.assertEqual(resolved["volumes"]["ollama"]["name"], "loaf-ollama")
        self.assertEqual(resolved["services"]["open-webui"]["image"], "loaf/open-webui:latest")
        self.assertEqual(resolved["services"]["open-webui"]["ports"][0]["published"], "3000")
        self.assertEqual(Path(resolved["services"]["ollama-online"]["build"]["context"]), DOCKER_DIR / "ollama")



@unittest.skipUnless(DOCKER_TESTS, "Set RUN_DOCKER_TESTS=1 to enable Docker tests.")
class BuiltImageTests(unittest.TestCase):
    def assert_image_config(self, image, environment, *, entrypoint=None):
        [details] = json.loads(command(["docker", "image", "inspect", image]))
        image_config = details["Config"]
        values = dict(item.split("=", 1) for item in image_config["Env"])
        self.assertEqual({key: values.get(key) for key in environment}, environment)
        if entrypoint is not None:
            self.assertEqual(image_config["Entrypoint"], entrypoint)

    def test_downloader_image_uses_shared_model_storage(self):
        self.assert_image_config(DEFAULT_IMAGES[0], {
            "OLLAMA_MODELS": "/ollama/models", "OLLAMA_NOPRUNE": "true",
        })

    def test_gpu_image_uses_offline_wrapper_and_default_context(self):
        self.assert_image_config(DEFAULT_IMAGES[1], {
            "OLLAMA_MODELS": "/ollama/models", "OLLAMA_NOPRUNE": "true",
            "OLLAMA_NO_CLOUD": "1", "OLLAMA_CONTEXT_LENGTH": "16384",
        }, entrypoint=["/usr/local/bin/start-ollama-offline"])

    def test_webui_image_defaults_to_local_offline_inference(self):
        self.assert_image_config(DEFAULT_IMAGES[2], {
            "OLLAMA_BASE_URL": "http://ollama-offline:11434", "OFFLINE_MODE": "true",
            "HF_HUB_OFFLINE": "1", "ENABLE_OPENAI_API": "false",
        }, entrypoint=["python3", "/usr/local/lib/loaf/start-webui.py"])


@unittest.skipUnless(DOCKER_TESTS, "Set RUN_DOCKER_TESTS=1 to enable Docker tests.")
class ShellHelperTests(unittest.TestCase):
    def check_script(self, case):
        command([
            "docker", "run", "--rm", "--pull", "never", "--network", "none", "-i",
            "--env", "DISPOSABLE_SCRIPT_TEST=1",
            "--mount", f"type=bind,source={DOCKER_DIR / 'ollama/export-modelfiles.sh'},target=/export.sh,readonly",
            "--mount", f"type=bind,source={DOCKER_DIR / 'ollama/start-ollama-offline.sh'},target=/start.sh,readonly",
            "--entrypoint", "bash", DEFAULT_IMAGES[0], "-s", "--", case,
        ], input=(TESTS_DIR / "script_checks.sh").read_text(encoding="utf-8"), timeout=90)

    def test_exports_namespaced_models_and_both_multimodal_blobs(self):
        self.check_script("export-all")

    def test_failed_export_preserves_previous_definition(self):
        self.check_script("export-atomic")

    def test_export_rejects_directory_destinations(self):
        self.check_script("export-directory")

    def test_export_validates_all_names_before_writing(self):
        self.check_script("export-invalid-input")

    def test_export_fails_when_ollama_api_is_unavailable(self):
        self.check_script("export-unavailable-api")

    def test_export_accepts_an_empty_catalog(self):
        self.check_script("export-empty")

    def test_startup_recursively_imports_models_and_reaps_server(self):
        self.check_script("startup-import")

    def test_startup_with_no_exports_propagates_sigterm(self):
        self.check_script("startup-signal")

    def test_failed_import_clears_readiness_and_stops_server(self):
        self.check_script("startup-import-failure")

    def test_startup_detects_early_server_death(self):
        self.check_script("startup-server-death")

    def test_startup_times_out_when_ollama_is_unready(self):
        self.check_script("startup-timeout")

    def test_startup_deadline_bounds_a_hung_api_request(self):
        self.check_script("startup-hung-api")

    def test_invalid_startup_timeout_prevents_server_creation(self):
        self.check_script("startup-invalid-timeout")

    def test_server_failure_after_readiness_propagates_exit_status(self):
        self.check_script("startup-server-error")


@unittest.skipUnless(DOCKER_TESTS, "Set RUN_DOCKER_TESTS=1 to enable Docker tests.")
class DockerTests(unittest.TestCase):
    def test_archive_round_trip_preserves_model_names_and_webui_data(self):
        prefix = f"loaf-archive-test-{uuid4().hex}"
        fixture = (TESTS_DIR / "archive_checks.sh").read_text(encoding="utf-8")

        def create_volumes(role):
            volumes = {f"{prefix}-{role}-{target}": target for target in transfer.VOLUMES.values()}
            for name in volumes:
                command(["docker", "volume", "create", name])
                self.addCleanup(command, ["docker", "volume", "rm", name])
            return volumes

        def check_fixture(phase, volumes, *, readonly=False):
            mounts = []
            for name, target in volumes.items():
                mount = f"type=volume,source={name},target=/transfer/{target},volume-nocopy"
                mounts.extend(("--mount", mount + ",readonly" if readonly else mount))
            command([
                "docker", "run", "--rm", "--pull", "never", "--network", "none", "-i",
                "--env", "DISPOSABLE_ARCHIVE_TEST=1",
                *mounts, "--entrypoint", "bash", DEFAULT_IMAGES[0], "-s", "--", phase,
            ], input=fixture)

        source = create_volumes("source")
        target = create_volumes("target")
        check_fixture("seed", source)
        with tempfile.TemporaryDirectory(prefix="loaf archive test ") as folder:
            project = Path(folder)
            archive = project / "volumes.tar"
            # volume_command names the volumes directly; the deployment's own names stay untouched.
            transfer.docker_archive(project, transfer.volume_command(volumes=source), archive, "Save test volumes")
            transfer.docker_archive(
                project, transfer.volume_command(restore=True, volumes=target), archive, "Restore test volumes",
                send=True,
            )
        check_fixture("verify", target, readonly=True)

    def test_models_merge_and_mirror_into_another_deployment_version(self):
        prefix = f"loaf-model-transfer-{uuid4().hex}"

        def create_store(role):
            version = f"{prefix}-{role}"
            namespace = config.namespace(version)
            volume = next(iter(transfer.volume_names(namespace)))
            image = transfer.image_names(namespace)[0]
            command(["docker", "volume", "create", volume])
            self.addCleanup(command, ["docker", "volume", "rm", volume])
            command(["docker", "image", "tag", DEFAULT_IMAGES[0], image])
            self.addCleanup(command, ["docker", "image", "rm", image])
            return version, volume

        def store_shell(volume, script):
            return command([
                "docker", "run", "--rm", "--pull", "never", "--network", "none",
                "--mount", f"type=volume,source={volume},target=/ollama,volume-nocopy",
                "--entrypoint", "/bin/sh", DEFAULT_IMAGES[0], "-c", script,
            ])

        source, source_volume = create_store("source")
        target, target_volume = create_store("target")
        store_shell(source_volume, "mkdir -p /ollama/models /ollama/config; printf source > /ollama/config/source.model")
        store_shell(target_volume, "mkdir -p /ollama/models /ollama/config; printf local > /ollama/config/local.model")
        with tempfile.TemporaryDirectory(prefix="loaf-model-transfer-") as folder:
            project = Path(folder)
            # setup's settings make the directory a project with the packaged deployment files.
            (project / "config").mkdir()
            (project / "config/config.ini").touch()
            # Every backup folder carries a launcher; the fixture stands in for the standalone executable.
            (project / "dist").mkdir()
            (project / "dist/loaf").write_bytes(transfer.EXECUTABLE_HEADER + b"\n__LOAF_PAYLOAD__\n\x1f\x8bfixture")
            launcher = [sys.executable, "-m", "loaf"]
            command([*launcher, "--namespace", source, "backup", "models", "--backup-dir", "archives"], cwd=project)
            bundle, = (project / "archives").iterdir()
            # Models carry no namespaced resources, so they restore into another deployment version.
            restore = [*launcher, "--namespace", target, "restore", "models", "--archive", str(bundle)]
            command(restore, cwd=project)
            self.assertEqual(store_shell(target_volume, "ls /ollama/config").split(), ["local.model", "source.model"])
            command([*restore, "--delete"], cwd=project)
            self.assertEqual(store_shell(target_volume, "ls /ollama/config").split(), ["source.model"])

    def start_stop_fixture(self):
        """A deployment with settings whose downloader runs from the packaged Compose file in its own namespace."""
        folder = tempfile.TemporaryDirectory(prefix="loaf-stop-")
        self.addCleanup(folder.cleanup)
        project = Path(folder.name)
        (project / "config").mkdir()
        (project / "config/config.ini").touch()
        version = f"loaf-stop-test-{uuid4().hex}"
        namespace = config.namespace(version)
        volume = f"{namespace}-ollama"
        image = transfer.image_names(namespace)[0]
        command(["docker", "image", "tag", DEFAULT_IMAGES[0], image])
        self.addCleanup(command, ["docker", "image", "rm", image])
        compose = cli.Compose(project, namespace=namespace)

        def invoke(*args):
            return compose_command(compose, cli.Step(("download",), args))

        self.addCleanup(invoke, "down", "--volumes", "--timeout", "5")
        invoke("up", "-d", "--no-build", "--pull", "never", "ollama-online")
        stop = [sys.executable, "-m", "loaf", "--namespace", version, "stop"]
        return invoke, stop, project, volume

    def test_docker_rm_removes_exactly_the_deployments_resources(self):
        version = f"resource-test-{uuid4().hex}"
        namespace = config.namespace(version)
        key = "a" * 64
        project = {"com.docker.compose.project": namespace}
        terminal = {"loaf.terminal.project": namespace, "loaf.terminal.workspace": key}
        image = transfer.image_names(namespace)[0]
        network, workspace, volume = f"{namespace}_inference", f"{namespace}-terminal-{key}", f"{namespace}-ollama"

        def labels(values):
            return [part for name, value in values.items() for part in ("--label", f"{name}={value}")]

        for kind, names in (("container", (f"{namespace}-ollama-offline-1", workspace)), ("network", (network, workspace)),
                            ("volume", (volume, transfer.terminal_volume(namespace))), ("image", (image,))):
            self.addCleanup(subprocess.run, ["docker", kind, "rm", *(("--force",) if kind == "container" else ()),
                                             *names], capture_output=True)
        command(["docker", "network", "create", "--internal",
                 *labels({**project, "com.docker.compose.network": "inference"}), network])
        command(["docker", "network", "create", "--internal", *labels(terminal), workspace])
        command(["docker", "volume", "create", *labels({**project, "com.docker.compose.volume": "ollama"}), volume])
        command(["docker", "volume", "create", *labels({"loaf.terminal.project": namespace, "loaf.terminal.users": "true"}),
                 transfer.terminal_volume(namespace)])
        command(["docker", "image", "tag", DEFAULT_IMAGES[0], image])
        command(["docker", "run", "--detach", "--init", "--network", network, "--mount", f"source={volume},target=/data",
                 *labels({**project, "com.docker.compose.service": "ollama-offline"}),
                 "--name", f"{namespace}-ollama-offline-1", "--entrypoint", "/bin/sleep", DEFAULT_IMAGES[0], "180"])
        command(["docker", "create", "--network", workspace, *labels(terminal), "--name", workspace,
                 "--entrypoint", "/bin/true", DEFAULT_IMAGES[0]])

        with tempfile.TemporaryDirectory(prefix="loaf-resources-") as folder:
            loaf = [sys.executable, "-m", "loaf", "--project-dir", folder, "--namespace", version, "docker"]
            listing = command([*loaf, "ps"])
            for name in (f"{namespace}-ollama-offline-1", workspace, image, volume, network):
                self.assertIn(name, listing)
            refused = subprocess.run([*loaf, "rm", "volumes", "--force"], capture_output=True, text=True)
            self.assertEqual(refused.returncode, 1)
            self.assertIn("still has 2 containers", refused.stderr)
            self.assertIn("Removed 7 Docker resources", command([*loaf, "rm", "all", "--force"]))
            self.assertIn("Containers: none", command([*loaf, "ps"]))
        for kind, name in (("container", workspace), ("network", network), ("volume", volume), ("image", image)):
            self.assertEqual(subprocess.run(["docker", kind, "inspect", name], capture_output=True).returncode, 1, name)
        command(["docker", "image", "inspect", DEFAULT_IMAGES[0]])

    def test_stop_keeps_container_when_removal_is_disabled(self):
        invoke, stop, project, _ = self.start_stop_fixture()
        container = invoke("ps", "--quiet", "ollama-online").strip()
        self.assertTrue(container)
        command([*stop, "--no-remove-containers"], cwd=project)
        self.assertEqual(invoke("ps", "--all", "--quiet", "ollama-online").strip(), container)
        self.assertEqual(
            command(["docker", "inspect", "--format", "{{.State.Running}}", container]).strip(),
            "false",
        )

    def test_stop_removes_container_and_preserves_its_named_volume(self):
        invoke, stop, project, volume = self.start_stop_fixture()
        peer, _, _, _ = self.start_stop_fixture()
        container = invoke("ps", "--quiet", "ollama-online").strip()
        peer_container = peer("ps", "--quiet", "ollama-online").strip()
        self.assertTrue(container)
        self.assertTrue(peer_container)
        invoke("exec", "-T", "ollama-online", "sh", "-c", "printf persisted >/ollama/marker")
        command(stop, cwd=project)
        self.assertEqual(invoke("ps", "--all", "--quiet", "ollama-online").strip(), "")
        self.assertEqual(peer("ps", "--quiet", "ollama-online").strip(), peer_container)
        self.assertEqual(command(["docker", "volume", "inspect", "--format", "{{.Name}}", volume]).strip(), volume)
        invoke("up", "-d", "--no-build", "--pull", "never", "ollama-online")
        replacement = invoke("ps", "--quiet", "ollama-online").strip()
        self.assertTrue(replacement)
        self.assertNotEqual(replacement, container)
        self.assertEqual(invoke("exec", "-T", "ollama-online", "cat", "/ollama/marker"), "persisted")
