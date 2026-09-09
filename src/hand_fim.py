"""HAND-FIM mode: image management, argument collection, validation, and container execution."""

import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

console = Console()

DOCKER_IMAGE = "cuahsi/handfim:latest"

COMMANDS = {
    "1": "reachfim",
    "2": "reachfim_interval",
}

COMMAND_MENU = "\n".join(
    [
        "  [bold cyan][1][/bold cyan] reachfim          — FIM for specific reach IDs and flow rates",
        "  [bold cyan][2][/bold cyan] reachfim_interval — FIM across stage increments from rating curves",
    ]
)


def image_exists(image: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
    )
    return result.returncode == 0


def pull_image(image: str) -> None:
    """Pull a Docker image, streaming progress to the terminal."""
    console.print(f"\n[cyan]Pulling [bold]{image}[/bold] …[/cyan]")
    result = subprocess.run(["docker", "pull", image])
    if result.returncode != 0:
        console.print(f"[bold red]✗ Failed to pull image {image}.[/bold red]")
        raise typer.Exit(1)
    console.print(f"[bold green]✓ Image {image} is ready.[/bold green]")


def ensure_image(image: str) -> None:
    """Check that the required image is present; offer to pull it if not."""
    if image_exists(image):
        console.print(
            f"[bold green]✓ Image [italic]{image}[/italic] found locally.[/bold green]"
        )
        return

    console.print(
        f"\n[yellow]Image [bold]{image}[/bold] was not found on this machine.[/yellow]"
    )
    if not Confirm.ask("Would you like to download it now?"):
        console.print("[red]Image download declined. Exiting.[/red]")
        raise typer.Exit(0)

    pull_image(image)


def prepare_volumes(base_dir: Path) -> tuple[Path, Path]:
    """
    Create input/output mount directories and return their paths.
    Input and output paths are relative to the directory in which
    the CLI is executed. Paths are only created if they don't
    already exist.
    """
    input_dir = base_dir / "input"
    output_dir = base_dir / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"  [dim]input  → {input_dir}[/dim]")
    console.print(f"  [dim]output → {output_dir}[/dim]")
    return input_dir, output_dir


def select_command() -> str:
    """Prompt the user to select a HAND-FIM sub-command."""
    console.print(
        Panel(
            COMMAND_MENU, title="[bold]Select a HAND-FIM command[/bold]", expand=False
        )
    )
    choice = Prompt.ask("Enter choice", choices=list(COMMANDS.keys()), default="1")
    return COMMANDS[choice]


def collect_args() -> dict:
    """
    Interactively collect HAND-FIM run parameters from the user.
    Typer handles type coercion and re-prompts on invalid input automatically.
    """
    command = select_command()

    console.print(
        Panel(
            f"[bold]HAND-FIM · {command} parameters[/bold]\n"
            "[dim]Press Enter to accept the default value shown in brackets.[/dim]",
            expand=False,
        )
    )

    huc_id: str = typer.prompt("  HUC-8 watershed identifier", default="03020202")

    if command == "reachfim":
        reach_ids: str = typer.prompt(
            "  NWM reach ID(s) — comma-separated", default="11239409"
        )
        flow_rates: str = typer.prompt(
            "  Flow rate(s) in m³/s — comma-separated, one per reach ID",
            default="200.23",
        )
        max_procs: int = typer.prompt("  Max concurrent processes", default=1)
        output_labels: str = typer.prompt(
            "  Output labels — comma-separated to use when saving FIM outputs (leave blank to auto-generate)",
            default="",
        )
        subdir: str = typer.prompt(
            "  Output subdirectory (leave blank for none)", default=""
        )
        return {
            "command": command,
            "huc_id": huc_id,
            "reach_ids": reach_ids,
            "flow_rates": flow_rates,
            "max_procs": max_procs,
            "output_labels": output_labels or None,
            "subdir": subdir or None,
        }

    else:  # reachfim_interval
        reach_id: str = typer.prompt("  NWM reach ID", default="11239409")
        stage_increment: float = typer.prompt(
            "  Stage increment in meters", default=0.5
        )
        max_procs: int = typer.prompt("  Max concurrent processes", default=1)
        subdir: str = typer.prompt(
            "  Output subdirectory (leave blank for none)", default=""
        )
        return {
            "command": command,
            "huc_id": huc_id,
            "reach_id": reach_id,
            "stage_increment": stage_increment,
            "max_procs": max_procs,
            "subdir": subdir or None,
        }


def validate_args(args: dict) -> None:
    """
    Validate business logic that Typer cannot enforce at prompt time.
    Type validation (int, float) is handled automatically by typer.prompt().
    """
    errors = []

    if not args.get("huc_id", "").strip():
        errors.append("HUC-8 identifier must not be empty.")

    if args["command"] == "reachfim":
        reach_ids = args.get("reach_ids", "")
        flow_rates = args.get("flow_rates", "")
        if not reach_ids.strip():
            errors.append("At least one reach ID is required.")
        if not flow_rates.strip():
            errors.append("At least one flow rate is required.")
        if reach_ids.strip() and flow_rates.strip():
            r_count = len(reach_ids.split(","))
            f_count = len(flow_rates.split(","))
            if r_count != f_count:
                errors.append(
                    f"Number of reach IDs ({r_count}) must match number of flow rates ({f_count})."
                )
    else:  # reachfim_interval
        if not args.get("reach_id", "").strip():
            errors.append("A reach ID is required.")

    if errors:
        console.print("\n[bold red]✗ Validation failed:[/bold red]")
        for err in errors:
            console.print(f"  [red]• {err}[/red]")
        raise typer.Exit(1)

    console.print("[bold green]✓ Input validation passed.[/bold green]")


def run() -> None:
    """Entry point for the HAND-FIM mode."""
    ensure_image(DOCKER_IMAGE)

    args = collect_args()

    base_dir = Path.cwd()

    console.print("\n[bold]Preparing mount directories …[/bold]")
    input_dir, output_dir = prepare_volumes(base_dir)

    console.print("\n[bold]Validating inputs …[/bold]")
    validate_args(args)

    # Build the docker run command, mapping collected args to container positional arguments.
    # entry.py uses typer.Argument (positional), so args are passed in order.
    container_cmd = [args["command"], args["huc_id"]]

    if args["command"] == "reachfim":
        container_cmd += [args["reach_ids"], args["flow_rates"], str(args["max_procs"])]
        if args.get("output_labels"):
            container_cmd.append(args["output_labels"])
        if args.get("subdir"):
            container_cmd.append(args["subdir"])
    else:  # reachfim_interval
        container_cmd += [
            args["reach_id"],
            str(args["stage_increment"]),
            str(args["max_procs"]),
        ]
        if args.get("subdir"):
            container_cmd.append(args["subdir"])

    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{input_dir}:/home/data/inputs",
        "-v",
        f"{output_dir}:/home/output",
        DOCKER_IMAGE,
    ] + container_cmd

    console.print("\n[bold]Running container …[/bold]")
    console.print(f"[dim]{' '.join(docker_cmd)}[/dim]\n")

    result = subprocess.run(docker_cmd)
    if result.returncode != 0:
        console.print("[bold red]✗ Container exited with an error.[/bold red]")
        raise typer.Exit(result.returncode)

    console.print(f"\n[bold green]✓ Done! Output written to: {output_dir}[/bold green]")
