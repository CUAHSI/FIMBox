"""Generate locally, preserving OpenAPI's optional-security semantics."""

import json
from pathlib import Path

from openapi_python_client import Project
from openapi_python_client.config import Config, ConfigFile, MetaType
from openapi_python_client.parser import GeneratorData
from openapi_python_client.parser.errors import GeneratorError


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "sdk" / "openapi.json"
    config = Config.from_sources(
        config_file=ConfigFile.load_from_path(root / "sdk" / "generator.yaml"),
        meta_type=MetaType.NONE,
        document_source=source,
        file_encoding="utf-8",
        overwrite=True,
        output_path=root / "src" / "fimbox_sdk",
    )
    document = json.loads(source.read_text())
    data = GeneratorData.from_dict(document, config=config)
    if isinstance(data, GeneratorError):
        raise SystemExit(str(data))
    for collection in data.endpoint_collections_by_tag.values():
        for endpoint in collection.endpoints:
            operation = document["paths"][endpoint.path][endpoint.method]
            security = operation.get("security", document.get("security", []))
            # Generator 0.28 treats a nonempty alternatives list as mandatory auth.
            endpoint.requires_security = bool(security) and all(security)
    errors = Project(openapi=data, config=config).build()
    if errors:
        raise SystemExit("\n".join(str(error) for error in errors))


if __name__ == "__main__":
    main()
