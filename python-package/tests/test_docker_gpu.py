"""Exercise model deployment and transfer with isolated Compose projects."""

from copy import deepcopy
import json
from pathlib import Path
from secrets import token_urlsafe
import tempfile
from textwrap import dedent
import unittest
import urllib.request
from uuid import uuid4

from loaf import cli, transfer
from tests.docker_support import CPU_TESTS, GPU_TESTS, command, compose_command, compose_config
from tests.webui_chat import WebUIChat


CACHED_MODEL = "hf.co/mistralai/Ministral-3-3B-Reasoning-2512-GGUF:Q4_K_M"
DISPLAY_NAME = "Ministral-3B-Reasoning:latest"
SELECTION = (cli.Model(CACHED_MODEL), cli.Model(CACHED_MODEL, DISPLAY_NAME))
OLLAMA_REQUEST = dedent("""\
    import sys
    from urllib.request import Request, urlopen

    data = sys.stdin.buffer.read() or None
    url = "http://ollama-offline:11434/api/" + sys.argv[1]
    request = Request(url, data=data, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=240) as response:
        sys.stdout.buffer.write(response.read())
""")


@unittest.skipUnless(CPU_TESTS or GPU_TESTS, "Enable the CPU or GPU Docker model workflow.")
class ModelWorkflowTests(unittest.TestCase):
    cpu = CPU_TESTS

    @classmethod
    def setUpClass(cls):
        cls.template = compose_config(cpu=cls.cpu)
        for service in cls.template["services"].values():
            if "container_name" in service or any(
                mount["type"] != "volume" or mount["source"] not in cls.template["volumes"]
                for mount in service.get("volumes", ())
            ):
                raise AssertionError("Model fixtures require project-local containers and named volumes.")
            service["restart"] = "no"
        for name in ("open-webui", "open-webui-online"):
            port, = cls.template["services"][name]["ports"]
            if port["host_ip"] != "127.0.0.1":
                raise AssertionError("Model fixtures require loopback WebUI ports.")
            port["published"] = "0"

        # Download once. Each workflow gets its own copy of the stopped model store.
        seed = cls.create_project(cls.addClassCleanup)
        cls.seed_volume = cls.model_volume(seed)
        cls.workflow(seed, "download", SELECTION)
        cls.workflow(seed, "download", SELECTION, export_definitions=True)
        compose_command(seed, cli.STOP_DOWNLOADER)

    @classmethod
    def create_project(cls, add_cleanup):
        config = deepcopy(cls.template)
        project = f"loaf-model-test-{uuid4().hex}"
        config["name"] = project
        for kind in ("networks", "volumes"):
            for key, resource in config[kind].items():
                resource["name"] = f"{project}-{key}"
        folder = tempfile.TemporaryDirectory(prefix="loaf-model-")
        add_cleanup(folder.cleanup)
        root = Path(folder.name)
        compose_file = root / "compose.json"
        compose_file.write_text(json.dumps(config), encoding="utf-8")
        compose = cli.Compose(root, files=(compose_file,), project_name=project)
        volume = cls.model_volume(compose)
        command([
            "docker", "volume", "create",
            "--label", f"com.docker.compose.project={project}",
            "--label", "com.docker.compose.volume=ollama", volume,
        ])
        # Explicit fallback covers setup failures before Compose creates containers.
        add_cleanup(cls.remove_volume_if_present, volume)
        add_cleanup(compose_command, compose, cli.Step(cli.PROFILES, ("down", "--volumes")), timeout=120)
        return compose

    @staticmethod
    def model_volume(compose):
        return f"{compose.project_name}-ollama"

    @staticmethod
    def remove_volume_if_present(name):
        names = command(["docker", "volume", "ls", "--quiet", "--filter", f"name={name}"]).split()
        if name in names:
            command(["docker", "volume", "rm", name])

    def new_deployment(self):
        compose = self.create_project(self.addCleanup)
        volume = self.model_volume(compose)
        command([
            "docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
            "--mount", f"type=volume,source={self.seed_volume},target=/seed,volume-nocopy,readonly",
            "--mount", f"type=volume,source={volume},target=/target,volume-nocopy",
            "--entrypoint", "/bin/cp", transfer.IMAGES[2], "-a", "--reflink=auto", "/seed/.", "/target",
        ])
        return compose

    @classmethod
    def workflow(cls, compose, mode, models=(), **options):
        for step in cli.plan(mode, models, cpu=cls.cpu, **options):
            # The pull can take much longer than the Compose readiness wait.
            compose_command(compose, step, timeout=1800)

    @staticmethod
    def invoke(compose, profile, *args, **options):
        return compose_command(compose, cli.Step((profile,), args), **options)

    @staticmethod
    def volume_script(volume, script, *args, readonly=False):
        mount = f"type=volume,source={volume},target=/target,volume-nocopy"
        return command([
            "docker", "run", "--rm", "--pull", "never", "--network", "none",
            "--mount", mount + (",readonly" if readonly else ""),
            "--entrypoint", "python", transfer.IMAGES[2], "-c", dedent(script), *args,
        ], timeout=90)

    def webui_request(self, compose, profile, api, data=None):
        service = cli.SERVICES[profile][1]
        return json.loads(self.invoke(
            compose, profile, "exec", "-T", service, "python", "-c", OLLAMA_REQUEST, api,
            input=json.dumps(data) if data is not None else None, timeout=300,
        ))

    def model_names(self, compose, profile):
        return {model["name"] for model in self.webui_request(compose, profile, "tags")["models"]}

    def assert_webui_healthy(self, compose, profile):
        address = self.invoke(compose, profile, "port", cli.SERVICES[profile][1], "8080").strip()
        self.assertRegex(address, r"^127\.0\.0\.1:[0-9]+$")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{address}/health", timeout=15) as response:
            self.assertTrue(json.load(response)["status"])
        return f"http://{address}"

    def container_id(self, compose, profile, service):
        container = self.invoke(compose, profile, "ps", "--all", "--quiet", service).strip()
        self.assertTrue(container, f"{profile}/{service} has no container")
        return container

    def assert_stopped(self, container):
        self.assertEqual(command([
            "docker", "inspect", container, "--format", "{{.State.Running}}",
        ]).strip(), "false")

    def test_download_exports_source_and_display_alias_modelfiles(self):
        output = self.volume_script(self.seed_volume, """\
            import json
            from pathlib import Path

            directory = Path("/target/config")
            print(json.dumps([str(path.relative_to(directory)) for path in directory.rglob("*.model")
                              if path.stat().st_size]))
        """, readonly=True)
        self.assertCountEqual(json.loads(output), [f"{CACHED_MODEL}.model", f"{DISPLAY_NAME}.model"])

    def test_offline_inference_uses_the_selected_device_at_default_context(self):
        compose = self.new_deployment()
        self.workflow(compose, "offline")
        webui = WebUIChat(self.assert_webui_healthy(compose, "offline"))
        webui.require_sign_in()
        webui.sign_up({"email": "inference@example.invalid", "password": token_urlsafe(24)})
        webui.require_model(DISPLAY_NAME)
        self.assertIn(DISPLAY_NAME, self.model_names(compose, "offline"))
        # Keep the long prompt for the GPU context regression check.
        prompt = "Reply with a short greeting."
        if not self.cpu:
            prompt += " Ignore " + ("padding " * 7600)
        payload = {
            "model": CACHED_MODEL,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "hello :)"},
            ],
            "stream": False,
            "options": {"temperature": 0.7, "num_predict": 128 if self.cpu else 2048},
            "keep_alive": "1m",
        }
        response = self.webui_request(compose, "offline", "chat", payload)
        if self.cpu:
            self.assertGreater(response["eval_count"], 0)
        else:
            self.assertGreater(response["prompt_eval_count"], 7008)
            self.assertEqual(response["done_reason"], "stop")
            self.assertTrue(response["message"]["content"].strip())
        placement = self.invoke(compose, "offline", "exec", "-T", "ollama-offline", "ollama", "ps")
        self.assertIn("100% CPU" if self.cpu else "100% GPU", placement)
        self.assertIn("16384", placement)

    def test_import_only_rebuilds_a_missing_manifest_without_starting_webui(self):
        compose = self.new_deployment()
        repository, _, tag = CACHED_MODEL.partition(":")
        self.volume_script(self.model_volume(compose), """\
            from pathlib import Path
            import sys

            models = Path("/target/models")
            tags = [models / catalog / sys.argv[1] for catalog in ("manifests", "manifests-v2")]
            existing = [tag for tag in tags if tag.exists() or tag.is_symlink()]
            assert existing, tags
            for tag in existing:
                tag.unlink()
        """, f"{repository}/{tag}")
        self.workflow(compose, "offline", import_definitions=True)
        self.assertEqual(self.invoke(compose, "offline", "ps", "--all", "--quiet", "open-webui").strip(), "")
        backend = self.container_id(compose, "offline", "ollama-offline")
        self.workflow(compose, "offline")
        self.assertEqual(self.container_id(compose, "offline", "ollama-offline"), backend)
        self.assertEqual(self.model_names(compose, "offline"), {CACHED_MODEL, DISPLAY_NAME})

    def test_explicit_import_refresh_replays_new_definition_before_webui_reopens(self):
        compose = self.new_deployment()
        self.workflow(compose, "offline")
        backend = self.container_id(compose, "offline", "ollama-offline")
        webui = self.container_id(compose, "offline", "open-webui")
        alias = "import-refresh:latest"
        self.volume_script(self.model_volume(compose), """\
            from pathlib import Path
            import shutil
            import sys

            config = Path("/target/config")
            shutil.copyfile(config / (sys.argv[1] + ".model"), config / (sys.argv[2] + ".model"))
        """, CACHED_MODEL, alias)
        self.workflow(compose, "offline")
        self.assertEqual(self.container_id(compose, "offline", "ollama-offline"), backend)
        self.assertEqual(self.container_id(compose, "offline", "open-webui"), webui)
        self.assertNotIn(alias, self.model_names(compose, "offline"))

        self.workflow(compose, "offline", import_definitions=True)
        self.assert_stopped(webui)
        self.workflow(compose, "offline")
        self.assertEqual(self.model_names(compose, "offline"), {CACHED_MODEL, DISPLAY_NAME, alias})

    def test_invalid_modelfile_blocks_serving_until_the_definition_is_fixed(self):
        compose = self.new_deployment()
        volume = self.model_volume(compose)
        self.volume_script(
            volume, "from pathlib import Path; Path('/target/config/broken.model').write_text('NOT_A_DIRECTIVE broken\\n')",
        )
        with self.assertRaisesRegex(AssertionError, "Command exited with status"):
            self.workflow(compose, "offline", wait_timeout=30)
        log = self.invoke(compose, "offline", "logs", "--no-color", "ollama-offline")
        self.assertIn("broken.model", log)
        self.assertEqual(self.invoke(compose, "offline", "ps", "--quiet", "open-webui").strip(), "")

        self.volume_script(volume, "from pathlib import Path; Path('/target/config/broken.model').unlink()")
        self.workflow(compose, "offline")
        self.assert_webui_healthy(compose, "offline")
        self.assertEqual(self.model_names(compose, "offline"), {CACHED_MODEL, DISPLAY_NAME})

    def test_model_archive_mirrors_the_store_without_restarting_connected_services(self):
        compose = self.new_deployment()
        self.workflow(compose, "offline")
        offline_backend = self.container_id(compose, "offline", "ollama-offline")
        offline_webui = self.container_id(compose, "offline", "open-webui")

        self.workflow(compose, "online")
        self.assert_stopped(offline_backend)
        self.assert_stopped(offline_webui)
        self.assert_webui_healthy(compose, "online")
        backend = self.container_id(compose, "online", "ollama-connected")
        webui = self.container_id(compose, "online", "open-webui-online")
        project = compose.project_name
        helper = transfer.image_names(project)[0]
        command(["docker", "image", "tag", transfer.IMAGES[0], helper])
        self.addCleanup(command, ["docker", "image", "rm", helper])
        archive = compose.project_dir / "models.tar.gz"

        def stop_downloader():
            compose_command(compose, cli.STOP_DOWNLOADER)

        transfer.export_models(compose.project_dir, archive, project, 1, stop_downloader)
        added = "only-on-destination:latest"
        self.invoke(compose, "online", "exec", "-T", "ollama-connected", "ollama", "cp", CACHED_MODEL, added)
        self.assertIn(added, self.model_names(compose, "online"))
        transfer.import_models(compose.project_dir, archive, project, stop_downloader, delete=True)
        self.assertEqual(self.model_names(compose, "online"), {CACHED_MODEL, DISPLAY_NAME})
        self.assertEqual(self.container_id(compose, "online", "ollama-connected"), backend)
        self.assertEqual(self.container_id(compose, "online", "open-webui-online"), webui)
