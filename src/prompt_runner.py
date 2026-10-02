"""Prompt for Pydantic model fields using their declared metadata."""

from pathlib import Path
from types import UnionType
from typing import Union, get_args, get_origin

import typer
from pydantic import BaseModel


def __prompt_type(annotation: object) -> type:
    if get_origin(annotation) in (Union, UnionType):
        annotation = next(
            arg for arg in get_args(annotation) if arg is not type(None)
        )
    if annotation in (str, int, float, bool):
        return annotation
    return str


def prompt_model_fields(
    model: type[BaseModel],
    *,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
    prefix: str = "",
) -> dict[str, object]:
    """Prompt for selected model fields and return their raw values.

    Arguments:
        model: Pydantic model whose fields define prompts, types, and defaults.
        include: Optional allowlist of field names to prompt for.
        exclude: Field names to omit from prompting.
        prefix: Text prepended to each prompt label.
    """
    values: dict[str, object] = {}
    excluded_fields = exclude or set()

    for name, field in model.model_fields.items():
        if name in excluded_fields or (include is not None and name not in include):
            continue

        prompt = f"{prefix}{field.description or name.replace('_', ' ')}"
        prompt_options = {"type": __prompt_type(field.annotation)}
        if field.is_required():
            value = typer.prompt(prompt, **prompt_options)
        else:
            default = "" if field.default is None else field.default
            value = typer.prompt(prompt, default=default, **prompt_options)
            if field.default is None and value == "":
                value = None
        if field.annotation is Path and value is not None:
            value = Path(value)
        values[name] = value

    return values
