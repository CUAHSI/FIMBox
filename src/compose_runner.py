"""Shared Docker Compose service runner for FIMBox modes."""

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from rich.console import Console

COMPOSE_FILE = Path(__file__).resolve().parent.parent / "docker" / "docker-compose.yml"
console = Console()


def run_compose(
    service: str,
    command: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    volumes: Sequence[str] = (),
) -> subprocess.CompletedProcess:
    """Run a one-off command in a Compose service."""
    docker_cmd = [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "run",
        "--rm",
    ]
    for volume in volumes:
        docker_cmd.extend(["--volume", volume])
    docker_cmd.extend([service, *command])

    console.print("\n[bold]Running container …[/bold]")
    console.print(f"[dim]{' '.join(docker_cmd)}[/dim]\n")

    compose_environment = os.environ.copy()
    if environment:
        compose_environment.update(environment)
    return subprocess.run(docker_cmd, env=compose_environment)