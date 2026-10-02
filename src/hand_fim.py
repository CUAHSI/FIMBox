"""HAND-FIM mode: image management, argument collection, validation, and container execution."""

import subprocess
import sys
from pathlib import Path
from typing import Literal, TypeVar

import typer
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from compose_runner import run_compose
from prompt_runner import prompt_model_fields

# The Pydantic schema models live in a sibling `schema/` directory rather
# than as an installed package, so it must be added to sys.path explicitly.
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"
if str(SCHEMA_DIR) not in sys.path:
    sys.path.insert(0, str(SCHEMA_DIR))

from schemas.hand_fim_schema import ScenarioList  # noqa: E402

console = Console()

DOCKER_IMAGE = "cuahsi/handfim:latest"
ArgsModelType = TypeVar("ArgsModelType", bound=BaseModel)


class ReachFimArgs(BaseModel):
    command: Literal["reachfim"] = "reachfim"
    huc_id: str = Field(default="03020202", description="HUC-8 watershed identifier")
    reach_ids: str = Field(
        default="11239409", description="NWM reach ID(s) — comma-separated"
    )
    flow_rates: str = Field(
        default="200.23",
        description="Flow rate(s) in m³/s — comma-separated, one per reach ID",
    )
    max_procs: int = Field(default=1, description="Max concurrent processes")
    output_labels: str | None = Field(
        default=None,
        description=(
            "Output labels — comma-separated to use when saving FIM outputs "
            "(leave blank to auto-generate)"
        ),
    )
    input_dir: Path = Field(
        default=Path("input/"), description="Input directory on the host"
    )
    output_dir: Path = Field(
        default=Path("output/"), description="Output directory on the host"
    )

    model_config = {"extra": "forbid"}

    @field_validator("huc_id", "reach_ids", "flow_rates")
    @classmethod
    def require_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must not be blank.")
        return value

    @model_validator(mode="after")
    def validate_reach_flow_counts(self) -> "ReachFimArgs":
        reach_count = len(self.reach_ids.split(","))
        flow_count = len(self.flow_rates.split(","))
        if reach_count != flow_count:
            raise ValueError(
                f"Number of reach IDs ({reach_count}) must match "
                f"number of flow rates ({flow_count})."
            )
        return self


class ScenarioArgs(BaseModel):
    command: Literal["scenario"] = "scenario"
    json_path: Path = Field(description="Path to scenario JSON file")
    max_procs: int = Field(default=1, description="Max concurrent processes")
    input_dir: Path = Field(
        default=Path("data/input/"), description="Input directory on the host"
    )
    output_dir: Path = Field(
        default=Path("data/output/"), description="Output directory on the host"
    )

    model_config = {"extra": "forbid"}


HandFimArgs = ReachFimArgs | ScenarioArgs


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

    return __parse_args(
        ReachFimArgs,
        prompt_model_fields(ReachFimArgs, exclude={"command"}, prefix="  "),
    )


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

    values = prompt_model_fields(
        ScenarioArgs, include={"json_path"}, prefix="  "
    )
    json_path = values["json_path"]
    load_scenarios(json_path)
    values.update(
        prompt_model_fields(
            ScenarioArgs,
            include={"max_procs", "input_dir", "output_dir"},
            prefix="  ",
        )
    )
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

    ensure_image(DOCKER_IMAGE)

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
        container_cmd = [args.command, args.huc_id]
        container_cmd += [args.reach_ids, args.flow_rates, str(args.max_procs)]
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
