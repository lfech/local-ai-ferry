"""loaf docker ps and rm against a fake Docker daemon: ownership, report, confirmation and removal order."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from loaf import cli, resources, transfer


KEY = "a" * 64
OTHER_KEY = "b" * 64


class FakeDocker:
    """Answer the listing, inspect and removal commands that loaf docker uses."""

    def __init__(self, namespace="loaf"):
        project = {"com.docker.compose.project": namespace}
        terminal = {"loaf.terminal.project": namespace}
        self.containers = {
            "c1": self.container(f"{namespace}-open-webui-1", {**project, "com.docker.compose.service": "open-webui"},
                                 "running", health="healthy"),
            "c2": self.container(f"{namespace}-ollama-offline-1",
                                 {**project, "com.docker.compose.service": "ollama-offline"}, "exited", code=137),
            # Chat containers inherit the Compose labels of the provisioner's image.
            "c3": self.container(f"{namespace}-terminal-{KEY}",
                                 {**project, "com.docker.compose.service": "terminal-server", **terminal,
                                  "loaf.terminal.workspace": KEY, "loaf.terminal.chat-id": "chat"}, "running"),
            "c4": self.container(f"{namespace}-terminal-{OTHER_KEY}", {**terminal, "loaf.terminal.workspace": OTHER_KEY},
                                 "exited"),
            # Same labels, but a name this deployment's provisioner never gives.
            "c5": self.container("intruder", {**terminal, "loaf.terminal.workspace": KEY}, "running"),
            "c6": self.container("loaf-v2-open-webui-1", {"com.docker.compose.project": "loaf-v2",
                                                          "com.docker.compose.service": "open-webui"}, "running"),
        }
        self.networks = {
            "n1": {"Name": f"{namespace}_inference", "Internal": True,
                   "Labels": {**project, "com.docker.compose.network": "inference"}},
            "n2": {"Name": f"{namespace}-terminal-{KEY}", "Internal": True,
                   "Labels": {**terminal, "loaf.terminal.workspace": KEY}},
            "n3": {"Name": f"{namespace}-terminal-connected", "Internal": False,
                   "Labels": {**terminal, "loaf.terminal.connected": "true"}},
            "n4": {"Name": "loaf-v2_inference", "Internal": True,
                   "Labels": {"com.docker.compose.project": "loaf-v2", "com.docker.compose.network": "inference"}},
        }
        self.volumes = [f"{namespace}-ollama", f"{namespace}-terminal-users", "ollama", "loaf-v2-ollama"]
        tag = "latest" if namespace == "loaf" else namespace.removeprefix("loaf-")
        self.images = [f"loaf/open-webui:{tag}", "loaf/open-webui:v2", "local/open-webui:latest", "<none>:<none>"]
        self.calls = []

    @staticmethod
    def container(name, labels, status, *, health=None, code=0):
        state = {"Status": status, "Running": status == "running", "ExitCode": code}
        if health:
            state["Health"] = {"Status": health}
        return {"Name": "/" + name, "Config": {"Labels": labels}, "State": state}

    def labeled(self, objects, label):
        key, value = label.removeprefix("label=").split("=", 1)
        return [identifier for identifier, record in objects.items()
                if (record["Config"]["Labels"] if "Config" in record else record["Labels"]).get(key) == value]

    def find(self, objects, reference):
        return next(identifier for identifier, record in objects.items()
                    if reference in (identifier, record["Name"].removeprefix("/")))

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        arguments = command[1:]
        output = ""
        if arguments[:2] in (["container", "ls"], ["network", "ls"]):
            objects = self.containers if arguments[0] == "container" else self.networks
            output = "\n".join(self.labeled(objects, arguments[-1]))
        elif arguments[1:2] == ["inspect"]:
            objects = self.containers if arguments[0] == "container" else self.networks
            output = json.dumps([objects[identifier] for identifier in arguments[2:] if identifier in objects])
        elif arguments[:2] == ["image", "ls"]:
            output = "\n".join(json.dumps({
                "Repository": image.split(":")[0], "Tag": image.split(":")[1], "Size": "1.5GB",
                "CreatedSince": "2 days ago", "ID": "0123456789abcdef",
            }) for image in self.images)
        elif arguments[:2] == ["volume", "ls"]:
            output = "\n".join(json.dumps({"Name": name, "Driver": "local"}) for name in self.volumes)
        elif arguments[:2] == ["container", "stop"]:
            for name in arguments[2:]:
                self.containers[self.find(self.containers, name)]["State"].update(Status="exited", Running=False)
            # The provisioner removes its temporary and stopped workspaces' bridges when it stops.
            self.networks.pop("n2", None)
        elif arguments[:2] == ["container", "rm"]:
            for name in arguments[3:]:
                del self.containers[self.find(self.containers, name)]
        elif arguments[:2] == ["network", "rm"]:
            for name in arguments[2:]:
                del self.networks[self.find(self.networks, name)]
        elif arguments[:2] == ["volume", "rm"]:
            self.volumes = [name for name in self.volumes if name not in arguments[2:]]
        elif arguments[:2] == ["image", "rm"]:
            self.images = [name for name in self.images if name not in arguments[2:]]
        else:
            raise AssertionError(f"Unexpected Docker command {command}")
        return subprocess.CompletedProcess(command, 0, stdout=output)

    def removals(self):
        return [call[1:3] for call in self.calls if call[2] in ("stop", "rm")]


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.docker = FakeDocker()

    def invoke(self, *arguments, answer=None, tty=True):
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch("loaf.resources.subprocess.run", side_effect=self.docker),
            patch("sys.stdin.isatty", return_value=tty),
            patch("builtins.input", return_value=answer) as prompt,
            redirect_stdout(output), redirect_stderr(errors),
        ):
            code = cli.main(["--project-dir", self.folder.name, "docker", *arguments])
        self.prompt = prompt
        return code, output.getvalue(), errors.getvalue()

    def test_ps_reports_only_the_deployments_resources_in_aligned_tables(self):
        code, output, _ = self.invoke("ps")
        self.assertEqual(code, 0)
        self.assertEqual(output, f"""\
Docker resources of deployment loaf

Containers (4)
  SERVICE         STATE             NAME
  ollama-offline  exited (137)      loaf-ollama-offline-1
  open-webui      running, healthy  loaf-open-webui-1
  terminal chat   running           loaf-terminal-{KEY}
  terminal draft  exited (0)        loaf-terminal-{OTHER_KEY}

Images (1)
  IMAGE                   SIZE   CREATED     ID
  loaf/open-webui:latest  1.5GB  2 days ago  0123456789ab

Volumes (2)
  CONTENT                DRIVER  NAME
  Ollama models          local   loaf-ollama
  terminal user folders  local   loaf-terminal-users

Networks (3)
  PURPOSE             INTERNAL  NAME
  inference           yes       loaf_inference
  terminal workspace  yes       loaf-terminal-{KEY}
  terminal connected  no        loaf-terminal-connected
""")
        self.assertEqual(self.docker.removals(), [])

    def test_version_selects_another_deployment(self):
        code, output, _ = self.invoke("ps", "--namespace", "v2")
        self.assertEqual(code, 0)
        self.assertIn("Docker resources of deployment loaf-v2", output)
        for name in ("loaf-v2-open-webui-1", "loaf/open-webui:v2", "loaf-v2-ollama", "loaf-v2_inference"):
            self.assertIn(name, output)
        self.assertNotIn("loaf-open-webui-1", output)

    def test_rm_all_stops_services_then_removes_in_dependency_order(self):
        code, output, errors = self.invoke("rm", "all", answer="y")
        self.assertEqual(code, 0, errors)
        self.prompt.assert_called_once_with("\nRemove 10 resources? [y/N] ")
        self.assertEqual(self.docker.removals(), [
            ["container", "stop"], ["container", "rm"], ["network", "rm"], ["volume", "rm"], ["image", "rm"],
        ])
        stop, remove = [call for call in self.docker.calls if call[2] in ("stop", "rm")][:2]
        # Only the running Compose service stops gracefully; the provisioner's workspaces go with --force.
        self.assertEqual(stop, ["docker", "container", "stop", "loaf-open-webui-1"])
        self.assertEqual(remove[:4], ["docker", "container", "rm", "--force"])
        self.assertEqual(sorted(remove[4:]), sorted([
            "loaf-open-webui-1", "loaf-ollama-offline-1", f"loaf-terminal-{KEY}", f"loaf-terminal-{OTHER_KEY}",
        ]))
        # The stopped provisioner removed a bridge after the listing; only existing networks are removed.
        self.assertIn(["docker", "network", "rm", "loaf_inference", "loaf-terminal-connected"], self.docker.calls)
        self.assertEqual(sorted(self.docker.containers), ["c5", "c6"])
        self.assertEqual(self.docker.volumes, ["ollama", "loaf-v2-ollama"])
        self.assertEqual(self.docker.images, ["loaf/open-webui:v2", "local/open-webui:latest", "<none>:<none>"])
        self.assertIn("Removed 9 Docker resources of deployment loaf.", output)
        self.assertIn("Stopping 1 service container…", errors)

    def test_rm_without_confirmation_removes_nothing(self):
        for answer in ("", "n", "no thanks"):
            with self.subTest(answer=answer):
                self.assertEqual(self.invoke("rm", "volumes", "containers", answer=answer)[0], 1)
                self.assertEqual(self.docker.removals(), [])
        code, _, errors = self.invoke("rm", "containers", tty=False)
        self.assertEqual(code, 1)
        self.assertIn("Confirm the removal in a terminal, or add --yes.", errors)
        self.assertEqual(self.docker.removals(), [])

    def test_rm_refuses_networks_volumes_and_images_while_containers_remain(self):
        for kind in ("volumes", "networks", "images"):
            with self.subTest(kind=kind):
                code, output, errors = self.invoke("rm", kind, "--yes")
                self.assertEqual(code, 1)
                self.assertIn("Deployment loaf still has 4 containers", errors)
                self.assertIn(f"docker rm containers {kind}", errors)
                self.assertEqual(output, "")
                self.assertEqual(self.docker.removals(), [])
                self.prompt.assert_not_called()

    def test_rm_with_yes_removes_only_the_selected_type(self):
        self.docker.containers = {name: record for name, record in self.docker.containers.items()
                                  if name in ("c5", "c6")}
        code, output, errors = self.invoke("rm", "volumes", "--yes", "-v")
        self.assertEqual(code, 0, errors)
        self.prompt.assert_not_called()
        self.assertEqual(self.docker.removals(), [["volume", "rm"]])
        self.assertIn("+ docker volume rm loaf-ollama loaf-terminal-users", output)
        self.assertIn("Removed 2 Docker resources of deployment loaf.", output)
        self.assertEqual(self.invoke("rm", "volumes", "--yes")[1].splitlines()[-1], "Nothing to remove.")

    def test_dry_run_needs_no_docker(self):
        code, output, _ = self.invoke("rm", "images", "volumes", "--dry-run", "--namespace", "v2")
        self.assertEqual(code, 0)
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(output, """\
Plan for deployment loaf-v2
  Require that deployment loaf-v2 has no containers
  Remove the volumes loaf-v2-ollama, loaf-v2-open-webui, loaf-v2-terminal-users
  Remove the images loaf/ollama-online:v2, loaf/ollama-offline:v2, loaf/open-webui:v2, loaf/open-terminal:v2
""")

    def test_invalid_arguments_are_rejected_before_docker(self):
        for arguments, message in (
            ((), "docker requires a command: ps or rm."),
            (("start",), "invalid docker command. Use ps or rm."),
            (("ps", "volumes"), "docker ps takes no arguments."),
            (("ps", "--dry-run"), "docker ps changes nothing. Omit --dry-run."),
            (("rm",), "docker rm requires one or more types"),
            (("rm", "container"), "invalid resource type. Use all, containers, images, volumes or networks."),
            (("rm", "images", "images"), "Specify each resource type once."),
            (("ps", "--yes"), "--yes is only supported by docker rm."),
            (("rm", "all", "--attach"), "--attach is available for Compose and transfer commands."),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                self.invoke(*arguments)
            self.assertEqual(self.docker.calls, [])
        with redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit):
            cli.main(["stop", "--yes"])
        self.assertIn("--yes is only supported by docker rm and clean --full.", errors.getvalue())

    def test_all_selects_every_type(self):
        self.assertEqual(resources.plan("loaf", resources.KINDS)[0],
                         "Stop the running services of deployment loaf, then remove all of its containers")
        self.assertEqual(len(resources.plan("loaf", resources.KINDS)), 4)
        self.assertEqual(resources.quantity(1, "image"), "1 image")
        self.assertEqual(resources.quantity(0, "image"), "0 images")


class ComposeVolumeTests(unittest.TestCase):
    def test_missing_volumes_get_the_labels_of_compose_volumes(self):
        calls = []

        def docker(command, **kwargs):
            calls.append(command)
            missing = command[1:3] == ["volume", "inspect"] and command[3] == "loaf-v2-ollama"
            return subprocess.CompletedProcess(command, 1 if missing else 0, stdout=b"")

        with TemporaryDirectory() as folder, patch("loaf.transfer.subprocess.run", side_effect=docker), \
                patch("loaf.transfer.subprocess.check_output", side_effect=lambda command, **_: docker(command).stdout):
            transfer.ensure_compose_volume(folder, "loaf-v2", "ollama")
            transfer.ensure_compose_volume(folder, "loaf-v2", "open-webui")
        self.assertEqual(calls, [
            ["docker", "volume", "inspect", "loaf-v2-ollama"],
            ["docker", "volume", "create", "--label", "com.docker.compose.project=loaf-v2",
             "--label", "com.docker.compose.volume=ollama", "loaf-v2-ollama"],
            ["docker", "volume", "inspect", "loaf-v2-open-webui"],
        ])


if __name__ == "__main__":
    unittest.main()
