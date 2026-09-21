"""Validated environment configuration."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    data_mode: Literal["demo", "live"] = "demo"
    database_url: str = "sqlite:///data/travel.db"
    chroma_path: str = "data/chroma"
    max_iterations: int = Field(6, ge=1, le=6)
    token_limit: int = Field(8000, ge=64, le=8000)
    planning_timeout: float = Field(30, gt=0, le=30)
    tool_timeout: float = Field(5, gt=0)
    tool_backoff: float = Field(0.1, ge=0)
    transfer_buffer_min: int = Field(15, ge=0)
    max_expansions: int = Field(15000, ge=1)
    provider_url: str = ""
    provider_api_key: SecretStr = SecretStr("")
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_extra_body: dict = Field(default_factory=dict)
    amap_api_key: SecretStr = SecretStr("")
    api_access_token: SecretStr = SecretStr("")
    enable_ui: bool = True
