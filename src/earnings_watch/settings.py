from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: Path = Field(
        default_factory=lambda: (
            Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
            / "earnings-watch"
        )
    )
    backends: list[str] = Field(default_factory=lambda: ["none"])
    model: str | None = None
    max_calls_per_run: int = Field(default=20, ge=0, le=200)
    allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1", "[::1]"])
    backend_tokens_env: list[str] = Field(default_factory=list)
    # Optional OpenAI Responses-compatible endpoint for the Codex backend; the
    # key is read from the named environment variable, never from this file.
    codex_base_url: str = ""
    codex_key_env: str = ""
    public_url: str = ""
    oidc_issuer: str = ""
    oidc_client_id: str = "earnings-connections"
    oidc_required_group: str = ""
    directory_url: str = ""
    directory_key_env: str = "EARNINGS_DIRECTORY_API_KEY"
    directory_required: bool = False
    mail_url: str = ""
    mail_token_env: str = "EARNINGS_MAIL_TOKEN"
    sec_contact: str = ""
    downloads_root: Path | None = None
    cache_max_mb: int = Field(default=512, ge=1)
    provider_allowlist: list[str] = Field(
        default_factory=lambda: ["generic_ir", "sec_edgar", "hkexnews"]
    )

    @field_validator("provider_allowlist")
    @classmethod
    def known_providers(cls, values):
        from .providers import NAMES

        if set(values) - NAMES:
            raise ValueError("unknown provider in allowlist")
        return values

    @field_validator("codex_base_url")
    @classmethod
    def endpoint_url(cls, value):
        if value and not re.fullmatch(r"https?://[^/\s\"\\]+[^\s\"\\]*", value):
            raise ValueError("codex_base_url must be an http(s) URL")
        return value.rstrip("/")

    @field_validator("codex_key_env")
    @classmethod
    def endpoint_key(cls, value):
        if value and not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", value):
            raise ValueError("codex_key_env must be an environment variable name")
        return value

    @model_validator(mode="after")
    def endpoint_pair(self):
        if bool(self.codex_base_url) != bool(self.codex_key_env):
            raise ValueError("set codex_base_url and codex_key_env together")
        return self

    @property
    def codex_endpoint(self) -> tuple[str, str] | None:
        return (self.codex_base_url, self.codex_key_env) if self.codex_base_url else None

    @property
    def db(self) -> Path:
        return self.data_dir / "earnings.db"


def load(path: Path | None = None) -> Settings:
    path = path or Path(os.environ.get("EARNINGS_WATCH_CONFIG", "earnings-watch.toml"))
    raw = tomllib.loads(path.read_text()) if path.is_file() else {}
    values = dict(raw.get("paths", {}))
    values.update(raw.get("serve", {}))
    values.update(raw.get("documents", {}))
    values.update(raw.get("llm", {}))
    values.update(raw.get("connections", {}))
    values.update(raw.get("notifications", {}))
    values["sec_contact"] = raw.get("providers", {}).get("sec_edgar", {}).get("contact", "")
    for key in Settings.model_fields:
        env = os.environ.get("EARNINGS_WATCH_" + key.upper())
        if env is not None:
            if key in {"backends", "allowed_hosts", "backend_tokens_env", "provider_allowlist"}:
                values[key] = env.split(",") if env else []
            else:
                values[key] = env
    return Settings.model_validate(values)
