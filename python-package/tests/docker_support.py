"""Run Docker test commands with captured output and bounded waits."""

import json
import os
import subprocess

from loaf import cli, config, transfer
from tests import DATA, ROOT


DOCKER_TESTS = os.environ.get("RUN_DOCKER_TESTS") == "1"
CPU_TESTS = DOCKER_TESTS and os.environ.get("RUN_CPU_TESTS") == "1"
GPU_TESTS = DOCKER_TESTS and not CPU_TESTS and os.environ.get("RUN_GPU_TESTS") == "1"
# The downloader, serving and WebUI images of the default deployment.
DEFAULT_IMAGES = transfer.image_names(config.DEPLOYMENT)


def command(args, *, input=None, timeout=180, decode_errors="strict", env=None, cwd=None):
    # Work with bytes so a host locale cannot break UTF-8 or hide Docker diagnostics.
    try:
        result = subprocess.run(
            args, input=input.encode("utf-8") if input is not None else b"",
            capture_output=True, timeout=timeout, env=env, cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        problem = f"Command timed out after {timeout}s"
        stdout, stderr = exc.stdout, exc.stderr
    else:
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors=decode_errors)
        problem = f"Command exited with status {result.returncode}"
        stdout, stderr = result.stdout, result.stderr

    details = [problem, repr(args)]
    for name, output in (("stdout", stdout), ("stderr", stderr)):
        if output:
            details.extend((name, output.decode("utf-8", errors="replace").rstrip()))
    raise AssertionError("\n".join(details))


def compose_command(compose, step, *, env=None, **options):
    # Like Compose.execute, stage the scripts that the terminal runtime override binds.
    if compose.uses_terminal_runtime(step):
        compose.stage_terminal_runtime()
    if env is None:
        env = dict(os.environ, **compose.variables(step))
    return command(compose.command(step), env=env, cwd=compose.project_dir, **options)


def compose_config(namespace=config.DEPLOYMENT, *, profiles=cli.PROFILES, cpu=False, settings=None):
    files = (DATA / "compose.yml",)
    if cpu:
        files += (DATA / "compose.cpu.yml",)
    compose = cli.Compose(ROOT, files=files, namespace=namespace, settings=settings)
    step = cli.Step(profiles, ("config", "--format", "json"))
    # Render the packaged configuration without the caller's profiles or .env.
    env = dict(os.environ, **compose.variables(step))
    env.pop("COMPOSE_ENV_FILES", None)
    env.update(COMPOSE_PROFILES="", COMPOSE_DISABLE_ENV_FILE="1", OLLAMA_CONTEXT_LENGTH="", OLLAMA_STARTUP_TIMEOUT="")
    return json.loads(compose_command(compose, step, env=env))


def poll_fixture_health(services):
    """Poll real health checks sooner without shortening their failure windows."""
    for service in services.values():
        if "healthcheck" in service:
            health = service["healthcheck"]
            health.update(interval="1s", start_interval="1s", retries=health["retries"] * 5)
