# api/config.py
"""Strict settings validation. The process refuses to start with weak or missing secrets."""
import json
import re
import sys
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDERS = {"changeme", "default", "secret", "password"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- LLM ---
    GROQ_API_KEY: str = Field(..., min_length=8)
    LLM_MODEL: str = "llama-3.3-70b-versatile"
    LLM_TIMEOUT_SECONDS: float = 20.0

    # --- Services / environment ---
    ERP_API_BASE_URL: str = "http://127.0.0.1:8001"
    ENVIRONMENT: str = "development"  # development | staging | production
    DB_PATH: str = "erp_crm_enterprise.db"
    DEFAULT_TENANT_ID: str = "tenant-default"

    # --- Secrets (no defaults on purpose) ---
    AGENT_SECURITY_SECRET: str = Field(..., min_length=32)
    WEBHOOK_SIGNING_SECRET: str = Field(..., min_length=32)
    ERP_INTERNAL_API_KEY: str = Field(..., min_length=24)

    # --- Role keys (all land in DEFAULT_TENANT_ID) ---
    ADMIN_API_KEY: str = Field(..., min_length=24)
    OPERATOR_API_KEY: str = Field(..., min_length=24)
    APPROVER_API_KEY: str = Field(..., min_length=24)
    AUDITOR_API_KEY: str = Field(..., min_length=24)
    # Optional extra users, JSON list: [{"key": "...", "user_id": "...", "tenant_id": "...", "role": "OPERATOR"}]
    EXTRA_USERS_JSON: Optional[str] = None

    # --- Webhooks ---
    APPROVAL_WEBHOOK_URL: Optional[str] = None
    WEBHOOK_ALLOW_PRIVATE_TARGETS: bool = False  # honoured only when ENVIRONMENT != production
    WEBHOOK_MAX_ATTEMPTS: int = 6

    # --- Agent bounds ---
    MAX_PLAN_STEPS: int = 8
    MAX_RETRIES: int = 3
    APPROVAL_TTL_SECONDS: int = 600
    STALE_EXECUTION_SECONDS: int = 60
    RECONCILE_ATTEMPTS: int = 3
    RECONCILE_BACKOFF_SECONDS: float = 1.0
    ERP_HTTP_TIMEOUT_SECONDS: float = 8.0

    @field_validator("AGENT_SECURITY_SECRET", "WEBHOOK_SIGNING_SECRET")
    @classmethod
    def _entropy(cls, v: str, info) -> str:
        if v.lower() in _PLACEHOLDERS or len(set(v)) < 8:
            raise ValueError(f"{info.field_name} is a placeholder or lacks character diversity.")
        return v

    @field_validator("ENVIRONMENT")
    @classmethod
    def _env(cls, v: str) -> str:
        if v not in {"development", "staging", "production"}:
            raise ValueError("ENVIRONMENT must be development | staging | production")
        return v

    def extra_users(self) -> List[dict]:
        if not self.EXTRA_USERS_JSON:
            return []
        return json.loads(self.EXTRA_USERS_JSON)


def _load() -> Settings:
    try:
        return Settings()
    except Exception as exc:  # fail fast, never echo secret values
        msg = re.sub(r"input_value=\S+", "input_value=[REDACTED]", str(exc))
        sys.stderr.write(f"\n[FATAL CONFIGURATION ERROR] Startup aborted.\n{msg}\n\n")
        sys.exit(1)


settings = _load()
