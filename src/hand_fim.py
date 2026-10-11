"""HAND-FIM mode: image management, argument collection, validation, and container execution."""

import subprocess
import sys
from pathlib import Path
from typing import Literal, TypeVar

import typer
from pydantic import BaseModel, Field, ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from compose_runner import run_compose

# The Pydantic schema models live in a sibling `schema/` directory rather
# than as an installed package, so it must be added to sys.path explicitly.
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"
if str(SCHEMA_DIR) not in sys.path:
    sys.path.insert(0, str(SCHEMA_DIR))

from schemas.hand_fim_schema import Reach, ScenarioList  # noqa: E402

console = Console()

ArgsModelType = TypeVar("ArgsModelType", bound=BaseModel)


class BaseArgs(BaseModel):
    max_procs: int = Field(default=1, description="Max concurrent processes")
    input_dir: Path = Field(
        default=Path("data/input/"), description="Input directory on the host"
    )
    output_dir: Path = Field(
        default=Path("data/output/"), description="Output directory on the host"
    )

    model_config = {"extra": "forbid"}


class ReachFimArgs(BaseArgs):
    command: Literal["reachfim"] = "reachfim"
    reaches: list[Reach] = Field(
        min_length=1, description="Reach/streamflow inputs for this run"
    )
    output_labels: str | None = None

    model_config = {"extra": "forbid"}

class ScenarioArgs(BaseArgs):
    command: Literal["scenario"] = "scenario"
    json_path: Path = Field(description="Path to scenario JSON file")

    model_config = {"extra": "forbid"}


HandFimArgs = ReachFimArgs | ScenarioArgs


INPUT_MODES = {
    "1": "manual",
    "2": "json",
}

INPUT_MODE_MENU = "\n".join(
    [
        "  [bold cyan][1][/bold cyan] Enter parameters manually",
        "  [bold cyan][2][/bold cyan] Load scenarios from a JSON file",
    ]
)


def select_input_mode() -> str:
    """Prompt the user to choose how HAND-FIM inputs will be provided."""
    console.print(
        Panel(INPUT_MODE_MENU, title="[bold]Select an input method[/bold]", expand=False)
    )
    choice = Prompt.ask("Enter choice", choices=list(INPUT_MODES.keys()), default="1")
    return INPUT_MODES[choice]


def __parse_args(model: type[ArgsModelType], values: dict) -> ArgsModelType:
    try:
        return model.model_validate(values)
    except ValidationError as error:
        console.print("\n[bold red]✗ Input validation failed:[/bold red]")
        for detail in error.errors():
            field = ".".join(str(part) for part in detail["loc"]) or "arguments"
            console.print(f"  [red]{field}: {detail['msg']}[/red]")
        raise typer.Exit(1) from error


def load_scenarios(json_path: Path) -> ScenarioList:
    """Load and validate a scenario JSON file against the HAND-FIM schema."""
    if not json_path.exists():
        console.print(f"[bold red]✗ Scenario file not found: {json_path}[/bold red]")
        raise typer.Exit(1)

    try:
        scenarios = ScenarioList.model_validate_json(json_path.read_text())
    except Exception as e:
        console.print(f"[bold red]✗ Invalid scenario file {json_path}:[/bold red]\n{e}")
        raise typer.Exit(1)

    console.print(
        f"[bold green]✓ Loaded {len(scenarios.root)} scenario(s) from {json_path}.[/bold green]"
    )
    return scenarios


def collect_args_manual() -> ReachFimArgs:
    """
    Interactively collect HAND-FIM run parameters from the user.
    Typer handles type coercion and re-prompts on invalid input automatically.
    """
    console.print(
        Panel(
            f"[bold]HAND-FIM · Parameters[/bold]\n"
            "[dim]Press Enter to accept the default value shown in brackets.[/dim]",
            expand=False,
        )
    )

    values = {
        "max_procs": typer.prompt("  Max concurrent processes", default=1),
        "input_dir": Path(
            typer.prompt("  Input directory", default="data/input/")
        ),
        "output_dir": Path(
            typer.prompt("  Output directory", default="data/output/")
        ),
    }
    huc_id = typer.prompt("  HUC-8 watershed identifier", default="03020202")
    reach_ids = typer.prompt(
        "  NWM reach ID(s) — comma-separated", default="11239409"
    )
    flow_rates = typer.prompt(
        "  Flow rate(s) in m³/s — comma-separated, one per reach ID",
        default="200.23",
    )
    output_labels = typer.prompt(
        "  Output labels — comma-separated to use when saving FIM outputs "
        "(leave blank to auto-generate)",
        default="",
    )

    reach_ids = [reach_id.strip() for reach_id in reach_ids.split(",")]
    flow_rates = [flow_rate.strip() for flow_rate in flow_rates.split(",")]
    if len(reach_ids) != len(flow_rates):
        console.print(
            "[bold red]✗ Number of reach IDs must match number of flow rates.[/bold red]"
        )
        raise typer.Exit(1)

    values["reaches"] = [
        {"HUC": huc_id.strip(), "ReachID": reach_id, "Streamflow": flow_rate}
        for reach_id, flow_rate in zip(reach_ids, flow_rates)
    ]
    values["output_labels"] = output_labels or None
    return __parse_args(ReachFimArgs, values)


def collect_args_from_json() -> ScenarioArgs:
    """
    Interactively collect a scenario JSON file (see schema/schema.md /
    schema/schema.json), validate it, and gather the remaining run
    parameters shared across all scenarios in the file.
    """
    console.print(
        Panel(
            "[bold]HAND-FIM · Scenario file[/bold]\n"
            "[dim]Provide a JSON file matching schema/schema.json. Each scenario "
            "produces one combined output GeoTIFF.[/dim]",
            expand=False,
        )
    )

    json_path = Path(typer.prompt("  Path to scenario JSON file"))
    load_scenarios(json_path)
    values = {
        "json_path": json_path,
        "max_procs": typer.prompt("  Max concurrent processes", default=1),
        "input_dir": Path(
            typer.prompt("  Input directory", default="data/input/")
        ),
        "output_dir": Path(
            typer.prompt("  Output directory", default="data/output/")
        ),
    }
    return __parse_args(ScenarioArgs, values)


def collect_args() -> HandFimArgs:
    """Dispatch to the manual or JSON-file argument collection flow."""
    mode = select_input_mode()
    if mode == "json":
        return collect_args_from_json()
    return collect_args_manual()


def run() -> None:
    """Entry point for the HAND-FIM mode."""
    args = collect_args()

    base_dir = Path.cwd()
    input_dir = (base_dir / args.input_dir).resolve()
    output_dir = (base_dir / args.output_dir).resolve()

    # Compose creates the bind-mount directories and maps them into the container.
    # entry.py uses typer.Argument (positional), so args are passed in order.
    volumes = []
    if args.command == "scenario":
        scenario_path = args.json_path.resolve()
        container_cmd = ["scenario", "scenario.json", str(args.max_procs)]
        volumes.append(
            f"{scenario_path}:/home/data/inputs/scenario.json:ro",
        )
    else:
        container_cmd = [args.command, args.reaches[0].HUC]
        reach_ids = ",".join(reach.ReachID for reach in args.reaches)
        flow_rates = ",".join(format(reach.Streamflow, "g") for reach in args.reaches)
        container_cmd += [reach_ids, flow_rates, str(args.max_procs)]
        if args.output_labels:
            container_cmd.append(args.output_labels)

    result = run_compose(
        "fimbox",
        container_cmd,
        environment={
            "FIMBOX_INPUT_DIR": str(input_dir),
            "FIMBOX_OUTPUT_DIR": str(output_dir),
        },
        volumes=volumes,
    )
    if result.returncode != 0:
        console.print("[bold red]✗ Container exited with an error.[/bold red]")
        raise typer.Exit(result.returncode)

    console.print(f"\n[bold green]✓ Done! Output written to: {output_dir}[/bold green]")
