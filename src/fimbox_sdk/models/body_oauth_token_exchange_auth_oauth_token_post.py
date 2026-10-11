from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field
from typing_extensions import Self

from ..types import UNSET, Unset

T = TypeVar("T", bound="BodyOauthTokenExchangeAuthOauthTokenPost")


@_attrs_define
class BodyOauthTokenExchangeAuthOauthTokenPost:
    """
    Attributes:
        code (str):
        code_verifier (str):
        grant_type (str | Unset):  Default: 'authorization_code'.
        redirect_uri (None | str | Unset):
    """

    code: str
    code_verifier: str
    grant_type: str | Unset = "authorization_code"
    redirect_uri: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        code = self.code

        code_verifier = self.code_verifier

        grant_type = self.grant_type

        redirect_uri: None | str | Unset
        if isinstance(self.redirect_uri, Unset):
            redirect_uri = UNSET
        else:
            redirect_uri = self.redirect_uri

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "code": code,
                "code_verifier": code_verifier,
            }
        )
        if grant_type is not UNSET:
            field_dict["grant_type"] = grant_type
        if redirect_uri is not UNSET:
            field_dict["redirect_uri"] = redirect_uri

        return field_dict

    @classmethod
    def from_dict(cls, src_dict: Mapping[str, Any]) -> Self:
        d = dict(src_dict)
        code = d.pop("code")

        code_verifier = d.pop("code_verifier")

        grant_type = d.pop("grant_type", UNSET)

        def _parse_redirect_uri(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        redirect_uri = _parse_redirect_uri(d.pop("redirect_uri", UNSET))

        body_oauth_token_exchange_auth_oauth_token_post = cls(
            code=code,
            code_verifier=code_verifier,
            grant_type=grant_type,
            redirect_uri=redirect_uri,
        )

        body_oauth_token_exchange_auth_oauth_token_post.additional_properties = d
        return body_oauth_token_exchange_auth_oauth_token_post

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
