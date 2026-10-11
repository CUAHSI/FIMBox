from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from typing_extensions import Self

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.hand_fim_output import HandFimOutput
    from ..models.output_file import OutputFile


T = TypeVar("T", bound="StoredHandFimResult")


@_attrs_define
class StoredHandFimResult:
    """Successful outputs and the destination persisted by the worker.

    Attributes:
        outputs (list[HandFimOutput]):
        output_bucket (None | str | Unset):
        output_prefix (str | Unset):  Default: ''.
        files (list[OutputFile] | Unset):
        local_output_dir (None | str | Unset):
    """

    outputs: list[HandFimOutput]
    output_bucket: None | str | Unset = UNSET
    output_prefix: str | Unset = ""
    files: list[OutputFile] | Unset = UNSET
    local_output_dir: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        outputs = []
        for outputs_item_data in self.outputs:
            outputs_item = outputs_item_data.to_dict()
            outputs.append(outputs_item)

        output_bucket: None | str | Unset
        if isinstance(self.output_bucket, Unset):
            output_bucket = UNSET
        else:
            output_bucket = self.output_bucket

        output_prefix = self.output_prefix

        files: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.files, Unset):
            files = []
            for files_item_data in self.files:
                files_item = files_item_data.to_dict()
                files.append(files_item)

        local_output_dir: None | str | Unset
        if isinstance(self.local_output_dir, Unset):
            local_output_dir = UNSET
        else:
            local_output_dir = self.local_output_dir

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "outputs": outputs,
            }
        )
        if output_bucket is not UNSET:
            field_dict["output_bucket"] = output_bucket
        if output_prefix is not UNSET:
            field_dict["output_prefix"] = output_prefix
        if files is not UNSET:
            field_dict["files"] = files
        if local_output_dir is not UNSET:
            field_dict["local_output_dir"] = local_output_dir

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        from ..models.hand_fim_output import HandFimOutput
        from ..models.output_file import OutputFile

        d = dict(src_dict)
        outputs = []
        _outputs = d.pop("outputs")
        for outputs_item_data in _outputs:
            outputs_item = HandFimOutput.from_dict(outputs_item_data)

            outputs.append(outputs_item)

        def _parse_output_bucket(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        output_bucket = _parse_output_bucket(d.pop("output_bucket", UNSET))

        output_prefix = d.pop("output_prefix", UNSET)

        _files = d.pop("files", UNSET)
        files: list[OutputFile] | Unset = UNSET
        if _files is not UNSET:
            files = []
            for files_item_data in _files:
                files_item = OutputFile.from_dict(files_item_data)

                files.append(files_item)

        def _parse_local_output_dir(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        local_output_dir = _parse_local_output_dir(d.pop("local_output_dir", UNSET))

        stored_hand_fim_result = cls(
            outputs=outputs,
            output_bucket=output_bucket,
            output_prefix=output_prefix,
            files=files,
            local_output_dir=local_output_dir,
        )

        return stored_hand_fim_result
