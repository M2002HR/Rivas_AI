from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass


@dataclass(slots=True)
class TenantRuntimeSpec:
    container_name: str
    network_name: str
    network_alias: str
    image: str
    envs: dict[str, str]
    command: list[str]


def build_runtime_names(tenant_slug: str) -> tuple[str, str, str]:
    safe_slug = tenant_slug.strip().lower().replace("_", "-")
    container_name = f"rivas-mira-{safe_slug}"
    network_alias = f"tenant-{safe_slug}-mira"
    endpoint_base_url = f"http://{network_alias}:8090"
    return container_name, network_alias, endpoint_base_url


def ensure_tenant_container(spec: TenantRuntimeSpec) -> tuple[bool, str]:
    """
    Ensure tenant container matches desired configuration.
    Returns: (changed, status) where status is running|stopped|created|recreated.
    """
    inspect = _inspect_container(spec.container_name)
    if inspect is None:
        _run_container(spec)
        return True, "created"

    if _needs_recreate(inspect, spec):
        _remove_container(spec.container_name, force=True)
        _run_container(spec)
        return True, "recreated"

    running = _is_running(inspect)
    if running:
        return False, "running"

    _start_container(spec.container_name)
    return True, "running"


def stop_and_remove_tenant_container(container_name: str) -> bool:
    inspect = _inspect_container(container_name)
    if inspect is None:
        return False
    _remove_container(container_name, force=True)
    return True


def check_container_ready(container_name: str, timeout_seconds: int = 45) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        run = subprocess.run(
            [
                "docker",
                "exec",
                container_name,
                "python",
                "-c",
                "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/ready', timeout=3).getcode()==200 else 1)",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if run.returncode == 0:
            return True
        time.sleep(2)
    return False


def _needs_recreate(inspect: dict[str, object], spec: TenantRuntimeSpec) -> bool:
    config = inspect.get("Config") or {}
    current_image = str(config.get("Image") or "")
    if current_image != spec.image:
        return True

    current_env: dict[str, str] = {}
    for item in config.get("Env") or []:
        if "=" not in str(item):
            continue
        key, value = str(item).split("=", 1)
        current_env[key] = value

    for key, desired_value in spec.envs.items():
        if current_env.get(key) != desired_value:
            return True

    networks = ((inspect.get("NetworkSettings") or {}).get("Networks") or {})
    net_info = networks.get(spec.network_name) or {}
    aliases = net_info.get("Aliases") or []
    if spec.network_alias not in aliases:
        return True

    current_command = [str(x) for x in ((config.get("Cmd") or []) if isinstance(config.get("Cmd"), list) else [])]
    if current_command != spec.command:
        return True

    labels = config.get("Labels") or {}
    autoheal_label = str(labels.get("autoheal") or "")
    if autoheal_label.lower() != "true":
        return True

    healthcheck = config.get("Healthcheck") or {}
    health_test = healthcheck.get("Test") or []
    expected_test = [
        "CMD-SHELL",
        "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/ready', timeout=3).getcode()==200 else 1)\"",
    ]
    if list(health_test) != expected_test:
        return True

    return False


def _is_running(inspect: dict[str, object]) -> bool:
    state = inspect.get("State") or {}
    return bool(state.get("Running"))


def _inspect_container(container_name: str) -> dict[str, object] | None:
    run = subprocess.run(
        ["docker", "inspect", container_name],
        check=False,
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        return None
    rows = json.loads(run.stdout)
    if not rows:
        return None
    return dict(rows[0])


def _remove_container(container_name: str, force: bool) -> None:
    cmd = ["docker", "rm"]
    if force:
        cmd.append("-f")
    cmd.append(container_name)
    run = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if run.returncode != 0:
        raise RuntimeError(f"Failed to remove container {container_name}: {run.stderr.strip() or run.stdout.strip()}")


def _start_container(container_name: str) -> None:
    run = subprocess.run(["docker", "start", container_name], check=False, capture_output=True, text=True)
    if run.returncode != 0:
        raise RuntimeError(f"Failed to start container {container_name}: {run.stderr.strip() or run.stdout.strip()}")


def _run_container(spec: TenantRuntimeSpec) -> None:
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        spec.container_name,
        "--network",
        spec.network_name,
        "--network-alias",
        spec.network_alias,
        "--add-host",
        "host.docker.internal:host-gateway",
        "--restart",
        "unless-stopped",
        "--label",
        "autoheal=true",
        "--health-cmd",
        "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/ready', timeout=3).getcode()==200 else 1)\"",
        "--health-interval",
        "20s",
        "--health-timeout",
        "5s",
        "--health-retries",
        "3",
        "--health-start-period",
        "25s",
    ]
    for key, value in spec.envs.items():
        cmd.extend(["-e", f"{key}={value}"])
    cmd.append(spec.image)
    cmd.extend(spec.command)
    run = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if run.returncode != 0:
        raise RuntimeError(f"Failed to run container {spec.container_name}: {run.stderr.strip() or run.stdout.strip()}")
