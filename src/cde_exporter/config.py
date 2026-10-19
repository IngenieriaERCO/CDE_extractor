"""Configuración central leída desde variables de entorno y, opcionalmente, .env."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    environment: Literal["development", "test", "production"] = Field(
        default="development", validation_alias="CDE_ENV"
    )
    log_level: str = Field(default="INFO", validation_alias="CDE_LOG_LEVEL")
    input_dir: Path = Field(default=Path("data/input"), validation_alias="CDE_INPUT_DIR")
    output_dir: Path = Field(default=Path("data/output"), validation_alias="CDE_OUTPUT_DIR")
    sharepoint_site_id: str | None = Field(default=None, validation_alias="SHAREPOINT_SITE_ID")
    sharepoint_drive_id: str | None = Field(default=None, validation_alias="SHAREPOINT_DRIVE_ID")
    microsoft_tenant_id: str | None = Field(default=None, validation_alias="MICROSOFT_TENANT_ID")
    microsoft_client_id: str | None = Field(default=None, validation_alias="MICROSOFT_CLIENT_ID")

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("CDE_LOG_LEVEL debe ser CRITICAL, ERROR, WARNING, INFO o DEBUG")
        return normalized

    @field_validator(
        "sharepoint_site_id",
        "sharepoint_drive_id",
        "microsoft_tenant_id",
        "microsoft_client_id",
        mode="before",
    )
    @classmethod
    def empty_optional_values_become_none(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


@lru_cache
def get_settings() -> Settings:
    """Devuelve una instancia de configuración reutilizable durante el proceso."""
    return Settings()
