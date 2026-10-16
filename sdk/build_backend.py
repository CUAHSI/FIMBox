"""Generate the SDK before setuptools discovers and packages it."""

from setuptools import build_meta
from setuptools.build_meta import (
    get_requires_for_build_editable,
    get_requires_for_build_sdist,
    get_requires_for_build_wheel,
    prepare_metadata_for_build_editable,
    prepare_metadata_for_build_wheel,
)

from generate import main as generate_sdk


def build_wheel(
    wheel_directory: str,
    config_settings: dict[str, str | list[str]] | None = None,
    metadata_directory: str | None = None,
) -> str:
    generate_sdk()
    return build_meta.build_wheel(
        wheel_directory, config_settings, metadata_directory
    )


def build_editable(
    wheel_directory: str,
    config_settings: dict[str, str | list[str]] | None = None,
    metadata_directory: str | None = None,
) -> str:
    generate_sdk()
    return build_meta.build_editable(
        wheel_directory, config_settings, metadata_directory
    )


def build_sdist(
    sdist_directory: str,
    config_settings: dict[str, str | list[str]] | None = None,
) -> str:
    generate_sdk()
    return build_meta.build_sdist(sdist_directory, config_settings)
