#!/usr/bin/env python3.13
"""fimbox - CLI tool for running FIM (Flood Inundation Mapping) Docker workflows."""

import subprocess
from enum import Enum

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

import hand_fim

app = typer.Typer(
    name="fimbox",
    help="Run FIM (Flood Inundation Mapping) Docker workflows.",
    add_completion=False,
)
console = Console()


class Mode(str, Enum):
    derived_hand = "Derived HAND Input"
    hand_fim = "HAND-FIM"
    hand_ml = "HAND-ML"
    fim_evaluation = "FIM Evaluation"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check_docker() -> None:
    """Verify Docker is installed and the daemon is running. Exits on failure."""
    if subprocess.run(["which", "docker"], capture_output=True).returncode != 0:
        console.print("[bold red]✗ Docker is not installed.[/bold red] "
                      "Please install Docker and try again.")
        raise typer.Exit(1)

    result = subprocess.run(["docker", "info"], capture_output=True)
    if result.returncode != 0:
        console.print("[bold red]✗ Docker daemon is not running.[/bold red] "
                      "Please start Docker and try again.")
        raise typer.Exit(1)

    console.print("[bold green]✓ Docker is installed and running.[/bold green]")


# ---------------------------------------------------------------------------
# Run logic per mode
# ---------------------------------------------------------------------------

def run_coming_soon(mode_name: str) -> None:
    console.print(Panel(
        f"[bold yellow]🚧 {mode_name}[/bold yellow]\n\n"
        "This mode is not yet implemented.\n[dim]Check back in a future release.[/dim]",
        expand=False,
    ))


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------

MODE_LABELS = {
    "1": Mode.hand_fim,
    "2": Mode.derived_hand,
    "3": Mode.hand_ml,
    "4": Mode.fim_evaluation,
}

MODE_MENU = "\n".join([
    "  [bold cyan][1][/bold cyan] HAND-FIM",
    "  [bold dim][2][/bold dim] Derived HAND Input  [dim](coming soon)[/dim]",
    "  [bold dim][3][/bold dim] HAND-ML             [dim](coming soon)[/dim]",
    "  [bold dim][4][/bold dim] FIM Evaluation      [dim](coming soon)[/dim]",
])


def select_mode() -> Mode:
    console.print(Panel(MODE_MENU, title="[bold]Select a mode[/bold]", expand=False))
    choice = Prompt.ask("Enter choice", choices=list(MODE_LABELS.keys()), default="1")
    return MODE_LABELS[choice]


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------

@app.command()
def main(
    mode: str = typer.Option(
        None,
        "--mode", "-m",
        help="Mode to run: handfim | derived-hand | hand-ml | fim-evaluation. "
             "If omitted, an interactive menu is shown.",
    ),
) -> None:
    """
    Launch a FIM Docker workflow interactively or via --mode.
    """
    console.print(Panel(
        "[bold blue]fimbox[/bold blue] — Flood Inundation Mapping workflow runner",
        expand=False,
    ))

    # Step 1: verify Docker
    console.print("\n[bold]Checking Docker …[/bold]")
    check_docker()

    # Step 2: choose mode
    if mode:
        mode_map = {
            "handfim": Mode.hand_fim,
            "derived-hand": Mode.derived_hand,
            "hand-ml": Mode.hand_ml,
            "fim-evaluation": Mode.fim_evaluation,
        }
        selected = mode_map.get(mode.lower())
        if selected is None:
            console.print(f"[red]Unknown mode: {mode}[/red]")
            raise typer.Exit(1)
    else:
        selected = select_mode()

    console.print(f"\n[bold]Selected mode:[/bold] {selected.value}\n")

    # Step 3-6: dispatch
    if selected == Mode.hand_fim:
        hand_fim.run()
    else:
        run_coming_soon(selected.value)


if __name__ == "__main__":
    app()
