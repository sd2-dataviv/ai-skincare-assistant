from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_SECRET = "change-me-in-env-not-for-production-use"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "AI Skincare Assistant"
    environment: str = "development"  # development | test | production

    database_url: str = "postgresql+asyncpg://skincare:skincare@localhost:5433/skincare"

    # Auth
    jwt_secret: str = Field(default=DEFAULT_SECRET, min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = 12 * 60
    cors_origins: str = "http://localhost:5173"

    # LLM ("brain")
    llm_provider: str = "groq"  # groq | openai | ollama | scripted
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout_s: float = 30.0
    llm_max_tool_steps: int = 6
    llm_max_completion_tokens: int = 2048  # reasoning models spend part of this on hidden reasoning
    llm_reasoning_effort: str | None = "low"  # sent to reasoning models (gpt-oss); low keeps turns fast

    # Adapters
    analysis_engine: str = "mock"  # mock | http
    analysis_engine_url: str | None = None
    d365_mode: str = "mock"  # mock | http
    d365_base_url: str | None = None
    otp_mode: str = "mock"  # mock | sms
    messaging_mode: str = "mock"  # mock | http
    storage_dir: Path = Path("./var/photos")
    public_base_url: str = "http://localhost:5173"

    # Photo quality gates
    photo_face_check: bool = True  # disable only for demos without a real face (e.g. synthetic camera)
    photo_min_side: int = 480
    photo_min_sharpness: float = 40.0
    photo_max_bytes: int = 10 * 1024 * 1024

    # Clinical policy
    escalation_min_confidence: float = 0.6
    client_sees_draft_plan: bool = True
    summary_link_ttl_days: int = 7
    assessment_reuse_days: int = 180
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5
    otp_max_sends_per_10min: int = 3
    integration_max_retries: int = 3

    # OT / procedure room booking
    clinic_timezone: str = "Asia/Kolkata"
    ot_booking_enabled: bool = True  # after approval, the AI books the plan's procedures into a suitable room
    ot_offer_count: int = 3  # slots offered at a time
    ot_slot_step_minutes: int = 30  # start times fall on this grid
    ot_min_lead_minutes: int = 120  # earliest start from now (prep, consent forms)
    ot_horizon_days: int = 21  # how far ahead the AI searches

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def resolved_llm(self) -> tuple[str, str | None, str | None, str | None]:
        """Return (provider, base_url, api_key, model), falling back to scripted when no key is set."""
        p = self.llm_provider.lower()
        defaults = {
            "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
            "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
            "ollama": ("http://localhost:11434/v1", "qwen2.5:7b-instruct"),
        }
        if p == "scripted":
            return ("scripted", None, None, None)
        base, model = defaults.get(p, (self.llm_base_url, self.llm_model))
        base = self.llm_base_url or base
        model = self.llm_model or model
        key = self.llm_api_key or ("ollama" if p == "ollama" else None)
        if not key or not base:
            return ("scripted", None, None, None)
        return (p, base, key, model)


@lru_cache
def get_settings() -> Settings:
    return Settings()
