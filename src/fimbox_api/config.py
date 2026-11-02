from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "dev"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    celery_task_default_queue: str = "default"
    result_ttl_seconds: int = Field(default=86400, ge=1)
    visibility_timeout_seconds: int = Field(default=86400, ge=1)
    hand_fim_entry: Path = Path("/home/entry.py")
    hand_fim_input_dir: Path = Path("/home/data/inputs")
    hand_fim_output_dir: Path = Path("/home/output")
    hand_fim_max_procs: int = Field(default=1, ge=1)
    require_hydroshare_output: bool = False
    s3_endpoint_url: str = "https://s3.hydroshare.org"
    s3_region: str = "us-east-1"
    s3_addressing_style: Literal["path", "virtual", "auto"] = "path"
    hydroshare_s3_credentials_url: str = "https://www.hydroshare.org/hsapi/user/service/accounts/s3/"
    hydroshare_s3_credentials_expiry_days: int = Field(default=1, ge=1)
    stream_input_bucket: str = ""
    stream_input_prefix: str = ""
    stream_input_unsigned: bool = False
    stream_tile_size: int = Field(default=2048, ge=512, le=8192, multiple_of=512)
    stream_min_credential_seconds: int = Field(default=3600, ge=60)
    stream_queue: str = ""
    stream_display_max_tiles: int = Field(default=16, ge=4, le=64)
    examples_dir: Path = Path(__file__).resolve().parents[2] / "test-data"
    redis_url: str = "redis://localhost:6379/2"
    session_ttl_seconds: int = Field(default=86400, ge=1)
    hydroshare_oauth_authorize_url: str = "https://www.hydroshare.org/o/authorize/"
    hydroshare_oauth_token_url: str = "https://www.hydroshare.org/o/token/"
    hydroshare_userinfo_url: str = "https://www.hydroshare.org/hsapi/userInfo/"
    hydroshare_oauth_redirect_uri: str = "http://localhost:8000/auth/callback"
    hydroshare_client_id: str = Field(
        default="",
        validation_alias=AliasChoices("HYDROSHARE_CLIENT_ID", "API_CLIENT_ID"),
    )
    hydroshare_client_secret: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("HYDROSHARE_CLIENT_SECRET", "API_CLIENT_SECRET"),
    )
    hydroshare_scope: str = "read write"
    oauth_state_cookie_secure: bool = True
    swagger_oauth_redirect_uri: str = "http://localhost:8000/docs/oauth2-redirect"

    @property
    def streaming_enabled(self) -> bool:
        return bool(self.stream_input_bucket)

    @property
    def authentication_enabled(self) -> bool:
        return bool(self.hydroshare_client_id and self.hydroshare_client_secret.get_secret_value())

    @model_validator(mode="after")
    def validate_authentication_configuration(self) -> "Settings":
        if bool(self.hydroshare_client_id) != bool(self.hydroshare_client_secret.get_secret_value()):
            raise ValueError("Configure both HydroShare client ID and secret, or neither for local mode")
        if self.require_hydroshare_output and not self.authentication_enabled:
            raise ValueError("REQUIRE_HYDROSHARE_OUTPUT requires HydroShare client ID and secret")
        return self

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True, hide_input_in_errors=True,
    )


settings = Settings()
