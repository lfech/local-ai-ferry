"""Check Compose contracts, images, and disposable Docker workflows."""

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
from tests.docker_support import DOCKER_TESTS, command, compose_command, compose_config


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
        self.assertEqual(self.rendered["volumes"]["ollama"]["name"], "ollama")
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
        namespace = "terminal-contract-local-inference"
        rendered = compose_config(namespace, profiles=(*cli.PROFILES, "terminal"))
        services = rendered["services"]
        terminal = services["terminal-server"]
        self.assertEqual(terminal["image"], f"local/open-terminal:{namespace}")
        self.assertEqual(terminal["pull_policy"], "never")
        self.assertEqual(set(terminal["networks"]), {"terminal-control"})
        self.assertTrue(rendered["networks"]["terminal-control"]["internal"])
        self.assertNotIn("tool-server", (terminal["networks"]["terminal-control"] or {}).get("aliases", ()))
        self.assertFalse(terminal.get("ports"))
        self.assertTrue(terminal["read_only"])
        webui_data, = [mount for mount in terminal["volumes"] if mount["target"] == "/var/lib/loaf-webui"]
        self.assertEqual(webui_data["source"], "open-webui")
        self.assertTrue(webui_data["read_only"])
        self.assertEqual({secret["source"] for secret in terminal["secrets"]}, {"terminal-server-key"})
        for name, service in services.items():
            if name != "terminal-server":
                self.assertFalse(service.get("secrets"), name)
        for webui in ("open-webui", "open-webui-online"):
            self.assertIn("terminal-control", services[webui]["networks"])

    def test_versioned_compose_uses_separate_images_and_volumes(self):
        namespace = "v1-2-local-inference"
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
            (project / "docker-project").symlink_to(DATA, target_is_directory=True)
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
        services = resolved["services"]
        self.assertEqual(Path(services["ollama-online"]["build"]["context"]), DOCKER_DIR / "ollama")
        for name in ("ollama-online", "ollama-offline"):
            args = services[name]["build"]["args"]
            self.assertEqual(args["OLLAMA_IMAGE"], env["OLLAMA_IMAGE"])
            self.assertEqual(args["OLLAMA_SOURCE"], env["OLLAMA_SOURCE"])
        self.assertEqual(services["open-webui"]["build"]["args"]["OPEN_WEBUI_IMAGE"], "registry.example/webui:pinned")
        offline = services["ollama-offline"]["environment"]
        self.assertEqual(offline["OLLAMA_CONTEXT_LENGTH"], "12288")
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
        with patch.dict(os.environ, OWO_NAMESPACE="other-deployment", OWO_DOCKER_DIR="/old/path",
                        OWO_WEBUI_PORT="5999"):
            resolved = compose_config()
        self.assertEqual(resolved["volumes"]["ollama"]["name"], "ollama")
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
        self.assert_image_config(transfer.IMAGES[0], {
            "OLLAMA_MODELS": "/ollama/models", "OLLAMA_NOPRUNE": "true",
        })

    def test_gpu_image_uses_offline_wrapper_and_default_context(self):
        self.assert_image_config(transfer.IMAGES[1], {
            "OLLAMA_MODELS": "/ollama/models", "OLLAMA_NOPRUNE": "true",
            "OLLAMA_NO_CLOUD": "1", "OLLAMA_CONTEXT_LENGTH": "16384",
        }, entrypoint=["/usr/local/bin/start-ollama-offline"])

    def test_webui_image_defaults_to_local_offline_inference(self):
        self.assert_image_config(transfer.IMAGES[2], {
            "OLLAMA_BASE_URL": "http://ollama-offline:11434", "OFFLINE_MODE": "true",
            "HF_HUB_OFFLINE": "1", "ENABLE_OPENAI_API": "false",
        })


@unittest.skipUnless(DOCKER_TESTS, "Set RUN_DOCKER_TESTS=1 to enable Docker tests.")
class ShellHelperTests(unittest.TestCase):
    def check_script(self, case):
        command([
            "docker", "run", "--rm", "--pull", "never", "--network", "none", "-i",
            "--env", "DISPOSABLE_SCRIPT_TEST=1",
            "--mount", f"type=bind,source={DOCKER_DIR / 'ollama/export-modelfiles.sh'},target=/export.sh,readonly",
            "--mount", f"type=bind,source={DOCKER_DIR / 'ollama/start-ollama-offline.sh'},target=/start.sh,readonly",
            "--entrypoint", "bash", transfer.IMAGES[0], "-s", "--", case,
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
                *mounts, "--entrypoint", "bash", transfer.IMAGES[0], "-s", "--", phase,
            ], input=fixture)

        source = create_volumes("source")
        target = create_volumes("target")
        check_fixture("seed", source)
        with tempfile.TemporaryDirectory(prefix="loaf archive test ") as folder:
            project = Path(folder)
            archive = project / "volumes.tar"
            with patch.object(transfer, "VOLUMES", source):
                transfer.docker_archive(project, transfer.volume_command(), archive, "Save test volumes")
            with patch.object(transfer, "VOLUMES", target):
                transfer.docker_archive(
                    project, transfer.volume_command(restore=True), archive, "Restore test volumes", send=True,
                )
        check_fixture("verify", target, readonly=True)

    def test_models_only_commands_merge_and_mirror_the_destination(self):
        prefix = f"loaf-model-transfer-{uuid4().hex}"

        def create_store(role):
            version = f"{prefix}-{role}"
            namespace = config.namespace(version)
            volume = next(iter(transfer.volume_names(namespace)))
            image = transfer.image_names(namespace)[0]
            command(["docker", "volume", "create", volume])
            self.addCleanup(command, ["docker", "volume", "rm", volume])
            command(["docker", "image", "tag", transfer.IMAGES[0], image])
            self.addCleanup(command, ["docker", "image", "rm", image])
            return version, volume

        def store_shell(volume, script):
            return command([
                "docker", "run", "--rm", "--pull", "never", "--network", "none",
                "--mount", f"type=volume,source={volume},target=/ollama,volume-nocopy",
                "--entrypoint", "/bin/sh", transfer.IMAGES[0], "-c", script,
            ])

        source, source_volume = create_store("source")
        target, target_volume = create_store("target")
        store_shell(source_volume, "mkdir -p /ollama/models /ollama/config; printf source > /ollama/config/source.model")
        store_shell(target_volume, "mkdir -p /ollama/models /ollama/config; printf local > /ollama/config/local.model")
        with tempfile.TemporaryDirectory(prefix="loaf-model-transfer-") as folder:
            project = Path(folder)
            compose = {"services": {"ollama-online": {"image": transfer.IMAGES[0]}}}
            (project / "compose.yml").write_text(json.dumps(compose), encoding="utf-8")
            launcher = [sys.executable, "-m", "loaf"]
            options = ["volumes", "--models-only", "--archive", str(project / "models.tar.gz")]
            command([*launcher, "--version", source, "backup", *options], cwd=project)
            restore = [*launcher, "--version", target, "restore", *options]
            command(restore, cwd=project)
            self.assertEqual(store_shell(target_volume, "ls /ollama/config").split(), ["local.model", "source.model"])
            command([*restore, "--delete"], cwd=project)
            self.assertEqual(store_shell(target_volume, "ls /ollama/config").split(), ["source.model"])

    def start_stop_fixture(self):
        folder = tempfile.TemporaryDirectory(prefix="loaf-stop-")
        self.addCleanup(folder.cleanup)
        project = Path(folder.name)
        version = f"loaf-stop-test-{uuid4().hex}"
        namespace = config.namespace(version)
        volume = f"{namespace}-data"
        (project / "compose.yml").write_text(json.dumps({
            "services": {
                "worker": {
                    "image": transfer.IMAGES[0],
                    "pull_policy": "never",
                    "profiles": ["offline"],
                    "init": True,
                    "entrypoint": ["/bin/sleep", "180"],
                    "network_mode": "none",
                    "volumes": ["data:/data"],
                },
            },
            "volumes": {"data": {"name": volume}},
        }), encoding="utf-8")
        compose = cli.Compose(project, namespace=namespace)

        def invoke(*args):
            return compose_command(compose, cli.Step(("offline",), args))

        self.addCleanup(invoke, "down", "--volumes", "--timeout", "5")
        invoke("up", "-d", "--no-build", "--pull", "never")
        stop = [sys.executable, "-m", "loaf", "--version", version, "stop"]
        return invoke, stop, project, volume

    def test_stop_keeps_container_when_removal_is_disabled(self):
        invoke, stop, project, _ = self.start_stop_fixture()
        container = invoke("ps", "--quiet", "worker").strip()
        self.assertTrue(container)
        command([*stop, "-n"], cwd=project)
        self.assertEqual(invoke("ps", "--all", "--quiet", "worker").strip(), container)
        self.assertEqual(
            command(["docker", "inspect", "--format", "{{.State.Running}}", container]).strip(),
            "false",
        )

    def test_stop_removes_container_and_preserves_its_named_volume(self):
        invoke, stop, project, volume = self.start_stop_fixture()
        peer, _, _, _ = self.start_stop_fixture()
        container = invoke("ps", "--quiet", "worker").strip()
        peer_container = peer("ps", "--quiet", "worker").strip()
        self.assertTrue(container)
        self.assertTrue(peer_container)
        invoke("exec", "-T", "worker", "sh", "-c", "printf persisted >/data/marker")
        command(stop, cwd=project)
        self.assertEqual(invoke("ps", "--all", "--quiet", "worker").strip(), "")
        self.assertEqual(peer("ps", "--quiet", "worker").strip(), peer_container)
        self.assertEqual(command(["docker", "volume", "inspect", "--format", "{{.Name}}", volume]).strip(), volume)
        invoke("up", "-d", "--no-build", "--pull", "never")
        replacement = invoke("ps", "--quiet", "worker").strip()
        self.assertTrue(replacement)
        self.assertNotEqual(replacement, container)
        self.assertEqual(invoke("exec", "-T", "worker", "cat", "/data/marker"), "persisted")
