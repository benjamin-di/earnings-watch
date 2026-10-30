from __future__ import annotations

import os
import tomllib
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    values["sec_contact"] = raw.get("providers", {}).get("sec_edgar", {}).get("contact", "")
    for key in Settings.model_fields:
        env = os.environ.get("EARNINGS_WATCH_" + key.upper())
        if env is not None:
            if key in {"backends", "allowed_hosts", "backend_tokens_env", "provider_allowlist"}:
                values[key] = env.split(",") if env else []
            else:
                values[key] = env
    return Settings.model_validate(values)
