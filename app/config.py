from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Literal

from fastapi import Request
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class OllamaNodeConfig:
    """Parsed config for a single Ollama node."""

    def __init__(self, node_id: str, url: str, model: str, enabled: bool) -> None:
        self.id = node_id  # e.g. "node1"
        self.url = url.rstrip("/")
        self.model = model
        self.enabled = enabled

    def __repr__(self) -> str:
        return f"OllamaNodeConfig(id={self.id!r}, url={self.url!r}, model={self.model!r}, enabled={self.enabled})"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="allow")

    app_env: Literal["development", "staging", "production", "test"] = Field(default="development", alias="APP_ENV")
    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")

    database_url: str = Field(default="sqlite+aiosqlite:///./nexus.db", alias="DATABASE_URL")
    database_url_sync: str = Field(default="sqlite:///./nexus.db", alias="DATABASE_URL_SYNC")
    # Phase 2B — database foundation per AGENTS §15/§18/§22
    database_read_only: bool = Field(default=True, alias="DATABASE_READ_ONLY")
    db_max_rows: int = Field(default=200, alias="DB_MAX_ROWS")
    db_query_timeout_seconds: int = Field(default=10, alias="DB_QUERY_TIMEOUT_SECONDS")
    db_max_cell_length: int = Field(default=4000, alias="DB_MAX_CELL_LENGTH")
    # Backward-compatible aliases for §18 naming (MAX_ROWS / QUERY_TIMEOUT_SECONDS)
    max_rows: int | None = Field(default=None, alias="MAX_ROWS")
    query_timeout_seconds: int | None = Field(default=None, alias="QUERY_TIMEOUT_SECONDS")
    ai_max_tool_steps: int = Field(default=3, alias="AI_MAX_TOOL_STEPS")

    secret_key: str = Field(alias="SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(default=1440, alias="ACCESS_TOKEN_EXPIRE_MINUTES")

    # Explicit first two nodes — disabled/unset by default; deployment must supply via env/.env
    ollama_node_1_url: str = Field(default="", alias="OLLAMA_NODE_1_URL")
    ollama_node_1_model: str = Field(default="", alias="OLLAMA_NODE_1_MODEL")
    ollama_node_1_enabled: bool = Field(default=False, alias="OLLAMA_NODE_1_ENABLED")

    ollama_node_2_url: str = Field(default="", alias="OLLAMA_NODE_2_URL")
    ollama_node_2_model: str = Field(default="", alias="OLLAMA_NODE_2_MODEL")
    ollama_node_2_enabled: bool = Field(default=False, alias="OLLAMA_NODE_2_ENABLED")

    ai_default_node: str = Field(default="node1", alias="AI_DEFAULT_NODE")
    ai_timeout_seconds: int = Field(default=120, alias="AI_TIMEOUT_SECONDS")
    ai_max_output_tokens: int = Field(default=1024, alias="AI_MAX_OUTPUT_TOKENS")
    ai_max_context_tokens: int = Field(default=8192, alias="AI_MAX_CONTEXT_TOKENS")
    ai_max_concurrent_requests_per_node: int = Field(default=1, alias="AI_MAX_CONCURRENT_REQUESTS_PER_NODE")
    ai_fallback_enabled: bool = Field(default=True, alias="AI_FALLBACK_ENABLED")
    # Phase 2A: conservative context per AGENTS §9, plus file thresholds per §8
    ai_num_ctx: int = Field(default=4096, alias="AI_NUM_CTX")
    # File pipeline — token thresholds per §8 (starting values, benchmark later)
    file_small_token_threshold: int = Field(default=4000, alias="FILE_SMALL_TOKEN_THRESHOLD")
    file_medium_token_threshold: int = Field(default=12000, alias="FILE_MEDIUM_TOKEN_THRESHOLD")
    file_max_size_bytes: int = Field(default=10 * 1024 * 1024, alias="FILE_MAX_SIZE_BYTES")  # 10MB default for small office files
    file_max_text_preview_chars: int = Field(default=12000, alias="FILE_MAX_TEXT_PREVIEW_CHARS")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_sensitive_content: bool = Field(default=False, alias="LOG_SENSITIVE_CONTENT")

    cors_allow_origins: list[str] | str = Field(default_factory=list, alias="CORS_ALLOW_ORIGINS")  # type: ignore[assignment]

    # --- helpers -----------------------------------------------------------

    @field_validator("secret_key")
    @classmethod
    def _validate_secret_key(cls, v: str, info) -> str:  # type: ignore[no-untyped-def]
        placeholder = "change-me-to-a-random-secret-key"
        test_value = "test-secret-key-for-unit-tests-32chars!!"
        # Pydantic v2: info.data contains already-validated fields (app_env is before secret_key)
        app_env = None
        try:
            app_env = info.data.get("app_env")  # type: ignore[attr-defined]
        except Exception:
            app_env = None
        if not v or not v.strip():
            raise ValueError("SECRET_KEY is required")
        if v == placeholder:
            raise ValueError("SECRET_KEY placeholder value not allowed")
        if len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        if v == test_value and app_env != "test":
            raise ValueError("Test SECRET_KEY not allowed outside test environment")
        return v

    @field_validator("ai_default_node")
    @classmethod
    def _validate_default_node(cls, v: str) -> str:
        return v.lower()

    @field_validator("db_max_rows", "db_query_timeout_seconds", "db_max_cell_length", "ai_max_tool_steps")
    @classmethod
    def _validate_positive_int(cls, v: int) -> int:  # type: ignore[no-untyped-def]
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v):  # type: ignore[no-untyped-def]
        if v is None:
            return []
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("["):
                import json

                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed if str(x).strip()]
                except Exception:
                    # Not valid JSON, fall back to comma-separated parsing below
                    pass
            return [s.strip() for s in v.split(",") if s.strip()]
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return v

    # --- Phase 2B helpers ----------------------------------------------------
    @property
    def effective_db_max_rows(self) -> int:
        """Return effective max rows respecting MAX_ROWS alias per AGENTS §18."""
        if self.max_rows is not None and self.max_rows > 0:
            return self.max_rows
        return self.db_max_rows

    @property
    def effective_db_query_timeout(self) -> int:
        """Return effective query timeout respecting QUERY_TIMEOUT_SECONDS alias."""
        if self.query_timeout_seconds is not None and self.query_timeout_seconds > 0:
            return self.query_timeout_seconds
        return self.db_query_timeout_seconds

    def ollama_nodes(self) -> list[OllamaNodeConfig]:
        """Collect all OLLAMA_NODE_* configs from environment.

        Supports arbitrary N (e.g. NODE_3) without code changes, satisfying
        the future-scaling contract in AGENTS.md §20.
        Explicit Settings fields cover node1/2 for docs; additional nodes are
        discovered by scanning os.environ.
        """
        # Start with the two explicit fields
        nodes: dict[str, OllamaNodeConfig] = {
            "node1": OllamaNodeConfig("node1", self.ollama_node_1_url, self.ollama_node_1_model, self.ollama_node_1_enabled),
            "node2": OllamaNodeConfig("node2", self.ollama_node_2_url, self.ollama_node_2_model, self.ollama_node_2_enabled),
        }
        # Scan for generic OLLAMA_NODE_<N>_URL pattern from both os.environ and settings (.env via DotEnv)
        pattern = re.compile(r"^OLLAMA_NODE_(\w+)_URL$", re.IGNORECASE)
        # Collect suffixes from os.environ
        suffixes: dict[str, str] = {}  # lower suffix -> original suffix case
        for key in os.environ:
            m = pattern.match(key)
            if not m:
                continue
            raw = m.group(1)
            suffixes[raw.lower()] = raw
        # Also collect from settings extra/dotenv (when extra="allow")
        # Pydantic lowercases extra keys, so we need case-insensitive handling
        extra_dict: dict[str, str] = {}  # lower key -> value str
        # Pydantic v2 stores extra in __pydantic_extra__ and/or model_extra
        for attr in ("__pydantic_extra__", "model_extra"):
            if hasattr(self, attr):
                val = getattr(self, attr)
                if isinstance(val, dict):
                    for k, v in val.items():
                        if isinstance(k, str) and pattern.match(k):
                            raw = pattern.match(k).group(1)  # type: ignore[union-attr]
                            suffixes.setdefault(raw.lower(), raw)
                            # keep value for later lookup (store lowercased key for case-insensitive lookup)
                            extra_dict[k.lower()] = str(v) if v is not None else ""
        # Also check __dict__ for any direct OLLAMA_NODE keys (covers some Pydantic versions)
        for k in list(self.__dict__.keys()):
            if isinstance(k, str) and pattern.match(k):
                raw = pattern.match(k).group(1)  # type: ignore[union-attr]
                suffixes.setdefault(raw.lower(), raw)

        def _get_setting_value(suffix_raw: str, field: str) -> str | None:
            # field is URL, MODEL, ENABLED — preserve env override behavior (env vars win over .env)
            key_upper = f"OLLAMA_NODE_{suffix_raw}_{field}"
            # 1) Check os.environ first (env vars override .env)
            for sfx in (suffix_raw, suffix_raw.lower(), suffix_raw.upper()):
                v = os.environ.get(f"OLLAMA_NODE_{sfx}_{field}")
                if v is not None:
                    return v
                v = os.environ.get(f"OLLAMA_NODE_{sfx.lower()}_{field}")
                if v is not None:
                    return v
            # 2) Check settings extra dict (from .env via DotEnvSettingsSource)
            low_key = key_upper.lower()
            if low_key in extra_dict:
                return extra_dict[low_key]
            # 3) Check via getattr on self (covers extra="allow" attribute access)
            for k in (key_upper, key_upper.lower(), key_upper.upper()):
                if hasattr(self, k):
                    v = getattr(self, k)
                    if v is not None:
                        return str(v)
                for ek, ev in extra_dict.items():
                    if ek.lower() == k.lower():
                        return str(ev)
            return None

        for lower_suffix, raw_suffix in list(suffixes.items()):
            # Normalise id: if suffix is numeric use node<N>
            suffix = lower_suffix  # already lower
            if suffix.isdigit():
                node_id = f"node{suffix}"
            else:
                node_id = suffix if suffix.startswith("node") else f"node{suffix}"
            if node_id in nodes:
                continue  # already covered
            url = _get_setting_value(raw_suffix, "URL") or ""
            model = _get_setting_value(raw_suffix, "MODEL") or ""
            enabled_raw = _get_setting_value(raw_suffix, "ENABLED")
            if enabled_raw is None:
                enabled = False
            else:
                v = str(enabled_raw).strip().lower()
                if v in ("true", "1", "yes", "on"):
                    enabled = True
                elif v in ("false", "0", "no", "off"):
                    enabled = False
                else:
                    raise ValueError(f"Invalid OLLAMA_NODE_{raw_suffix}_ENABLED value {enabled_raw!r}: must be one of true/false, 1/0, yes/no, on/off")
            if url and model:
                if not url.lower().startswith(("http://", "https://")):
                    raise ValueError(f"Invalid OLLAMA_NODE_{raw_suffix}_URL {url!r}: must use http:// or https://")
                nodes[node_id] = OllamaNodeConfig(node_id, url, model, enabled)
        # Return sorted for deterministic routing
        return [nodes[k] for k in sorted(nodes.keys())]

    def get_node(self, node_id: str) -> OllamaNodeConfig | None:
        node_id = node_id.lower()
        for n in self.ollama_nodes():
            if n.id == node_id:
                return n
        return None


@lru_cache
def get_settings() -> Settings:
    """Cached Settings instance — reuses single instance to avoid reloading .env."""
    return Settings()  # type: ignore[call-arg]


def clear_settings_cache() -> None:
    """Invalidate the cached Settings instance for tests or config changes."""
    get_settings.cache_clear()  # type: ignore[attr-defined]


def get_settings_dep(request: Request) -> Settings:
    """FastAPI dependency — returns the Settings instance from app.state."""
    return request.app.state.settings  # type: ignore[attr-defined]
