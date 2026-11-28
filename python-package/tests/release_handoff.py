"""Run the standalone release through a small, disconnected CPU handoff."""

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4

from webui_chat import WebUIChat


FIXTURES = Path(__file__).resolve().parent
MODEL = "hf.co/bartowski/SmolLM2-135M-Instruct-GGUF:Q2_K"
WEBUI_IMAGE = "ghcr.io/open-webui/open-webui:v0.13.4-slim"
OLLAMA_IMAGE = "loaf-ollama-cpu"
CUDA_IMAGE = "loaf-ollama-cuda13"
COLD_IMAGE = "loaf/cold-test:28.5.2"


def run(*args, cwd=None, env=None, input=None, capture=False, timeout=180):
    return subprocess.run(
        args, cwd=cwd, env=env, input=input, text=True, check=True, timeout=timeout,
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def copy_to_media(bundle, media, deb=None):
    run("docker", "volume", "create", media, capture=True)
    run("docker", "run", "-d", "--name", media, "--pull", "never", "--network", "none",
        "--mount", f"type=volume,source={media},target=/handoff",
        "--entrypoint", "sleep", COLD_IMAGE, "infinity", capture=True)
    try:
        run("docker", "cp", f"{bundle}/.", f"{media}:/handoff")
        if deb is not None:
            run("docker", "cp", str(deb), f"{media}:/handoff/loaf.deb")
    finally:
        subprocess.run(["docker", "rm", "-f", media], check=False, capture_output=True, timeout=30)


def cold_handoff(bundle, name, namespace, port, fixture, deb=None):
    media = name + "-media"
    try:
        copy_to_media(bundle, media, deb)
        run("docker", "run", "-d", "--name", name, "--privileged", "--network", "none", "--pull", "never",
            "--mount", f"type=volume,source={media},target=/handoff,readonly",
            "--mount", "type=volume,destination=/var/lib/docker",
            COLD_IMAGE, capture=True)

        def target(*args, **options):
            return run("docker", "exec", "-i", name, *args, **options)

        deadline = monotonic() + 45
        while subprocess.run(
            ["docker", "exec", name, "docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10,
        ).returncode:
            if monotonic() >= deadline:
                raise RuntimeError("The destination Docker daemon failed to start.")
            sleep(0.25)

        [container] = json.loads(run("docker", "inspect", name, capture=True))
        [mount] = [entry for entry in container["Mounts"] if entry["Destination"] == "/handoff"]
        if mount["RW"] or container["HostConfig"]["NetworkMode"] != "none":
            raise AssertionError("The destination requires read-only transfer media and a disconnected network.")
        for kind, flag in (("image", "-aq"), ("volume", "-q"), ("container", "-aq")):
            if target("docker", kind, "ls", flag, capture=True).strip():
                raise AssertionError(f"The destination already has Docker {kind} data.")
        target("sh", "-ec", "test ! -e /work/cache && test ! -e /work/config")
        target("docker", "version", "--format", "{{.Server.Version}}")
        target("docker", "compose", "version", "--short")
        target("docker", "info", "--format", "Storage driver: {{.Driver}}")
        if deb is not None:
            # The fixture's Docker comes from static binaries, so its Debian packages are absent.
            target("dpkg", "--force-depends", "-i", "/handoff/loaf.deb")
            if "Installation: Debian package" not in target("loaf", "version", capture=True):
                raise AssertionError("The installed loaf does not report the Debian package.")
            target("loaf", "backup", "--verify", "--archive", "/handoff")
            print("Debian package check passed. The system launcher verified the transferred backup offline.")

        launcher = ("env", "XDG_DATA_HOME=/work/cache", "/handoff/loaf")
        target(*launcher, "setup", timeout=300)
        target(*launcher, "restore", timeout=600)
        target(*launcher, "run", "offline", "--cpu")
        target("python3", "-c", (FIXTURES / "webui_chat.py").read_text(),
               f"http://127.0.0.1:{port}", MODEL, input=json.dumps(fixture))
        backend = target(
            "docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={namespace}",
            "--filter", "label=com.docker.compose.service=ollama-offline", capture=True,
        ).strip()
        ollama = ("docker", "exec", "-e", "OLLAMA_HOST=127.0.0.1:11434", backend, "ollama")
        target(*ollama, "stop", MODEL)
        target("docker", "exec", backend, "sh", "-ec", """
            set -- /ollama/models/blobs/sha256-*
            for blob do
                if [ "$(stat -c %s "$blob")" -gt 1048576 ]; then
                    [ -z "${weights:-}" ]
                    weights=$blob
                fi
            done
            [ -n "${weights:-}" ]
            dd if=/dev/zero of="$weights" bs=4 count=1 conv=notrunc status=none
        """)
        target(*launcher, "status", "--cpu")
        if MODEL not in target(*ollama, "list", capture=True):
            raise AssertionError("The corrupted model disappeared from the catalog.")
        result = subprocess.run(
            ["docker", "exec", name, *ollama, "run", MODEL, "Hello."],
            capture_output=True, text=True, timeout=90,
        )
        if result.returncode != 1 or "unsupported file type [0 0 0 0]" not in result.stderr:
            raise AssertionError(f"Expected the broken weights to fail inference: {result.stderr}")
        print("Corrupt-weight check passed. Preflight found the model and inference rejected its weights.")
    except (subprocess.SubprocessError, AssertionError, RuntimeError):
        subprocess.run(["docker", "logs", "--tail", "40", name], check=False)
        raise
    finally:
        subprocess.run(["docker", "rm", "-fv", name], check=False, capture_output=True, timeout=30)
        subprocess.run(["docker", "volume", "rm", media], check=False, capture_output=True, timeout=30)


def rehearse(executable, root, compression, retain, *, chat_only, ollama_image, ollama_source, deb=None):
    source = root / "source"
    source.mkdir()
    loaf = source / "loaf"
    shutil.copyfile(executable, loaf)
    loaf.chmod(0o700)
    version = "loaf-handoff-" + uuid4().hex[:12]
    namespace = "loaf-" + version
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    env = {name: value for name, value in os.environ.items()
           if not name.startswith("COMPOSE_") and name not in
           ("OLLAMA_IMAGE", "OLLAMA_SOURCE", "OPEN_WEBUI_IMAGE", "OLLAMA_CONTEXT_LENGTH", "OLLAMA_STARTUP_TIMEOUT")}
    if ollama_source:
        env["OLLAMA_SOURCE"] = ollama_source
    env["XDG_DATA_HOME"] = str(root / "source-cache")

    def launch(*args, **options):
        return run(str(loaf), *args, cwd=source, env=env, **options)

    launch("setup")
    settings = (
        f"[launcher]\nnamespace = {version}\nwait_timeout = 120\n"
        f"[images]\nollama_image = {ollama_image}\nwebui_image = {WEBUI_IMAGE}\n"
        f"[webui]\nport = {port}\n[ollama]\ncontext_length = 1024\n"
    )
    settings += f"[transfer]\ncompression_level = {compression}\n"
    (source / "config/config.ini").write_text(settings, encoding="utf-8")
    (source / "config/models.ini").write_text(f"[models.default]\nmodel = {MODEL}\n", encoding="utf-8")

    try:
        launch("build", timeout=900)
        launch("download", timeout=600)
        cpu = ollama_image == OLLAMA_IMAGE
        launch("run", "offline", *(("--cpu",) if cpu else ()))
        webui = WebUIChat(f"http://127.0.0.1:{port}")
        webui.require_sign_in()
        credentials = {"email": "loaf-handoff@example.invalid", "password": secrets.token_urlsafe(24)}
        account = webui.sign_up(credentials)
        webui.require_model(MODEL)
        messages = [{"role": "user", "content": "What is the capital of France?"}]
        messages.append(webui.reply(MODEL, messages))
        chat_id = webui.save(MODEL, messages)
        if webui.saved_messages(chat_id, MODEL) != messages:
            raise AssertionError("Open WebUI changed the messages when saving the source chat.")
        backend = run(
            "docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={namespace}",
            "--filter", "label=com.docker.compose.service=ollama-offline", capture=True,
        ).strip()
        placement = run("docker", "exec", "-e", "OLLAMA_HOST=127.0.0.1:11434", backend, "ollama", "ps", capture=True)
        device = "CPU" if cpu else "GPU"
        if cpu:
            [container] = json.loads(
                run("docker", "inspect", backend, capture=True)
            )
            host_config = container["HostConfig"]
            if (
                host_config.get("DeviceRequests")
                or host_config.get("Devices")
                or host_config.get("Privileged")
                or host_config.get("Runtime") == "nvidia"
            ):
                raise AssertionError(
                    "CPU container has unexpected device access."
                )
            if "100% CPU" not in placement:
                print(
                    f"WARNING: Unexpected Ollama CPU memory "
                    f"accounting:\n{placement}",
                    flush=True,
                )
                run("docker", "logs", backend)
        elif "100% GPU" not in placement:
            raise AssertionError(
                f"Expected full GPU inference placement:\n{placement}"
            )
        print(f"Authenticated {device} inference and chat storage passed.", flush=True)
        if chat_only:
            return
        launch("backup", "--no-terminal-server", timeout=600)
        [bundle] = (source / "backup").iterdir()
        [archive] = bundle.glob("*.tar.gz")
        print(f"Backup archive: {archive.stat().st_size:,} bytes", flush=True)
        if retain:
            retain.mkdir(mode=0o700)
            shutil.move(bundle, retain)
            bundle = retain / bundle.name
            fd = os.open(retain / "test-login.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(credentials, output, indent=2)
                output.write("\n")
            print(f"Retained backup: {bundle}", flush=True)
        cold_handoff(bundle, version + "-cold", namespace, port, {
            "credentials": credentials, "account": account, "chat_id": chat_id, "messages": messages,
        }, deb)
    finally:
        # The deployment's settings select its namespace: remove its containers, networks, volumes and images.
        subprocess.run([str(loaf), "docker", "rm", "all", "--force"], cwd=source, env=env, check=False,
                       capture_output=True, timeout=180)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path, help="Standalone loaf executable to test")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--keep", type=Path, help="Retain the backup and fixture login in a new directory")
    mode.add_argument("--chat-only", action="store_true", help="Run the short authenticated chat without a handoff")
    parser.add_argument("--gpu", action="store_true", help="Use the CUDA 13 fixture for --chat-only")
    parser.add_argument("--compression", type=int, choices=range(10), default=1,
                        help="Backup gzip level for the handoff fixture (default: %(default)s)")
    parser.add_argument("--deb", type=Path, help="Install this loaf Debian package in the disconnected destination")
    parser.add_argument("--ollama-source",
                        help="Use a matching Ollama v0.40.18 image as the source of compact runtime files")
    args = parser.parse_args()
    if args.gpu and not args.chat_only:
        parser.error("--gpu requires --chat-only. The cold handoff uses CPU inference.")
    if args.deb and args.chat_only:
        parser.error("--deb requires the cold handoff. Omit --chat-only.")
    deb = args.deb.resolve(strict=True) if args.deb else None
    executable = args.executable.resolve(strict=True)
    retain = args.keep.resolve() if args.keep else None
    if retain and (retain.exists() or not retain.parent.is_dir()):
        parser.error("--keep requires a new directory with an existing parent")
    started = monotonic()
    ollama_image = CUDA_IMAGE if args.gpu else OLLAMA_IMAGE
    if not args.chat_only:
        run("docker", "build", "--file", "release-handoff.Dockerfile", "--tag", COLD_IMAGE, ".",
            cwd=FIXTURES, timeout=900)
    with TemporaryDirectory(prefix=".loaf-handoff-", dir=retain.parent if retain else None) as folder:
        rehearse(executable, Path(folder).resolve(), args.compression, retain,
                 chat_only=args.chat_only, ollama_image=ollama_image, ollama_source=args.ollama_source, deb=deb)
    check = "Authenticated chat smoke" if args.chat_only else "Cold release handoff"
    print(f"{check} passed in {monotonic() - started:.1f}s.")


if __name__ == "__main__":
    main()
