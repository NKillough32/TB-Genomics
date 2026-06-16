import os
from dataclasses import dataclass

TRUTHY_VALUES = {"1", "true", "yes", "on"}
PRODUCTION_VALUES = {"prod", "production"}
PLACEHOLDER_AUTH_TOKEN_MARKERS = (
    "change-me",
    "changeme",
    "replace-me",
    "replace-with",
    "example-token",
)
LOCAL_CORS_ORIGINS = (
    "http://localhost:8081",
    "http://127.0.0.1:8081",
)


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUTHY_VALUES


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str
    auto_migrate: bool
    deployment_mode: str
    cors_origins: tuple[str, ...]
    auth_required: bool
    auth_tokens: str
    ingest_api_key: str
    enable_synthetic_seeding: bool
    allow_non_operational_actions: bool
    allow_mock_outbreaker: bool

    @property
    def is_production(self) -> bool:
        return self.deployment_mode.strip().lower() in PRODUCTION_VALUES

    @property
    def auth_enabled(self) -> bool:
        return self.auth_required or bool(self.auth_tokens.strip())

    @property
    def auth_tokens_configured(self) -> bool:
        return bool(self.auth_tokens.strip())


def load_settings() -> Settings:
    configured_cors = _csv(os.getenv("TB_CORS_ORIGINS"))
    return Settings(
        database_url=os.getenv("DATABASE_URL", "postgresql://tb:tb@localhost/tb_surveillance").strip(),
        auto_migrate=not truthy(os.getenv("TB_DISABLE_AUTO_MIGRATE"))
        and str(os.getenv("TB_AUTO_MIGRATE", "1")).strip().lower() not in {"0", "false", "no", "off"},
        deployment_mode=os.getenv("TB_DEPLOYMENT_MODE", os.getenv("TB_ENV", "local")).strip().lower()
        or "local",
        cors_origins=configured_cors or LOCAL_CORS_ORIGINS,
        auth_required=truthy(os.getenv("TB_AUTH_REQUIRED")),
        auth_tokens=os.getenv("TB_AUTH_TOKENS", ""),
        ingest_api_key=os.getenv("TB_INGEST_API_KEY", ""),
        enable_synthetic_seeding=truthy(os.getenv("TB_ENABLE_SYNTHETIC_SEEDING")),
        allow_non_operational_actions=truthy(os.getenv("TB_ALLOW_NON_OPERATIONAL_ACTIONS")),
        allow_mock_outbreaker=truthy(os.getenv("TB_ALLOW_MOCK_OUTBREAKER")),
    )


def runtime_safety_findings(settings: Settings | None = None) -> list[dict[str, str]]:
    settings = settings or load_settings()
    findings: list[dict[str, str]] = []

    if not settings.auth_enabled:
        findings.append(
            {
                "level": "error" if settings.is_production else "warning",
                "code": "auth_disabled",
                "message": "API authentication is disabled.",
            }
        )
    elif settings.auth_required and not settings.auth_tokens_configured:
        findings.append(
            {
                "level": "error" if settings.is_production else "warning",
                "code": "auth_required_without_tokens",
                "message": "TB_AUTH_REQUIRED is set but TB_AUTH_TOKENS is empty.",
            }
        )

    if settings.auth_tokens_configured and any(
        marker in settings.auth_tokens.lower() for marker in PLACEHOLDER_AUTH_TOKEN_MARKERS
    ):
        findings.append(
            {
                "level": "error" if settings.is_production else "warning",
                "code": "placeholder_auth_token",
                "message": "TB_AUTH_TOKENS contains a placeholder token.",
            }
        )

    if settings.enable_synthetic_seeding:
        findings.append(
            {
                "level": "error" if settings.is_production else "warning",
                "code": "synthetic_seeding_enabled",
                "message": "Synthetic demo seeding is enabled.",
            }
        )

    if settings.allow_non_operational_actions:
        findings.append(
            {
                "level": "error" if settings.is_production else "warning",
                "code": "non_operational_actions_allowed",
                "message": "Non-operational demo datasets can be used for sensitive actions.",
            }
        )

    if settings.allow_mock_outbreaker:
        findings.append(
            {
                "level": "warning",
                "code": "mock_outbreaker_allowed",
                "message": "Mock outbreaker fallback is allowed; outputs must be treated as demonstration-only.",
            }
        )

    if "*" in settings.cors_origins:
        findings.append(
            {
                "level": "error" if settings.is_production else "warning",
                "code": "cors_wildcard",
                "message": "CORS allows every origin.",
            }
        )

    if settings.is_production:
        local_origins = sorted(set(settings.cors_origins).intersection(LOCAL_CORS_ORIGINS))
        if local_origins:
            findings.append(
                {
                    "level": "error",
                    "code": "production_local_cors",
                    "message": f"Production CORS includes local development origins: {', '.join(local_origins)}.",
                }
            )

    return findings


def enforce_startup_safety(settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    blocking = [item for item in runtime_safety_findings(settings) if item["level"] == "error"]
    if blocking:
        summary = "; ".join(f"{item['code']}: {item['message']}" for item in blocking)
        raise RuntimeError(f"Unsafe production configuration: {summary}")
