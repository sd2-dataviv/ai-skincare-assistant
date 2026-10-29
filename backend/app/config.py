from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_SECRET = "change-me-in-env-not-for-production-use"

# The parameters the clinic's model services were given with (project ids 187 and 167).
DIAGNOSIS_PARAMS = ('{"parameters":{"classes":["acne","scars","pigmentation","dark circles"],"draw_bbox":true,"threshold":0.4,'
                    '"bbox_colors":{"acne":[255,0,0],"scars":[0,255,0],"dark circles":[255,255,0],"pigmentation":[0,0,255]},'
                    '"bbox_thickness":2,"output_image_size":[1024,1024],"model_path":"","project_id":187}}')
POST_TREATMENT_PARAMS = ('{"parameters":{"classes":["acne","scars","pigmentation","dark circles"],"threshold":0.1,"mask_dilate":0,'
                         '"output_image_size":[512,512],"model_path":"","project_id":167}}')


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
    analysis_engine: str = "mock"  # mock | http | ws (the clinic's skin models over WebSocket, mock for the rest)
    analysis_engine_url: str | None = None

    # Skin model services (WebSocket: send the parameters as JSON text, then the image bytes; get an image back, then
    # - when asked with "return_predictions" - a JSON message with what was found). Parameters are sent as written here.
    skin_diagnosis_ws_url: str | None = None  # e.g. ws://192.168.0.84:9601/ws/cv/9819628774/image/image
    skin_diagnosis_params: str = DIAGNOSIS_PARAMS
    skin_post_treatment_ws_url: str | None = None  # e.g. ws://192.168.0.84:9600/ws/cv/3271585349/image/image
    skin_post_treatment_params: str = POST_TREATMENT_PARAMS
    # Model gateway token (ws://<gateway>/ws/models/<project>/...?token=...). Gateway tokens last 15 minutes, so the app
    # keeps one renewed (app/integrations/platform_auth.py), from the first of: a service account on the platform, the
    # platform session of the last staff member who signed in through WorkflowHub, or a fixed token. Tokens are never
    # written to logs, error messages or API responses.
    skin_model_auth_email: str | None = None  # service account: preferred, works without anyone signed in
    skin_model_auth_password: str | None = None
    skin_model_token_from_sso: bool = True  # use the platform session handed over at a staff SSO sign-in
    skin_model_token: str | None = None  # a fixed token: quick checks and tests only (it expires)
    skin_model_unwrap_params: bool = True  # send {"parameters": {...}} as its inner object (see skin_models.call_service)
    skin_model_connect_timeout_s: float = 5.0
    skin_diagnosis_timeout_s: float = 60.0  # per photo
    skin_post_treatment_timeout_s: float = 300.0  # inpainting is slow on a CPU
    skin_model_details_wait_s: float = 1.0  # wait for the findings after the image (updated services send them at once; older ones never)
    skin_model_retry_after_s: float = 60.0  # after a service fails, skip it for this long instead of waiting on it again
    # The talking avatar (app/integrations/avatar.py; docs/09-avatar.md). The AI Avatar service says the assistant's
    # replies in its Direct mode; the browser reaches it only through /api/avatar/ws. It signs in with the same platform
    # session as the model gateway (the service account above). Empty URL = no avatar: the browser's own voice speaks.
    avatar_ws_url: str | None = None  # e.g. wss://avatar.instaviv.in
    avatar_id: int = 14  # the service's avatar catalogue: 14 Meera, 10 Raghav, 18 Vedant Sir
    avatar_language: str = "en"
    avatar_max_speech_chars: int = 1200  # longer replies are cut at a sentence end
    avatar_connect_timeout_s: float = 10.0
    avatar_idle_close_s: float = 600.0  # free the avatar after this long with nothing said; it reconnects on the next line
    # Voice-only mode (the avatar can't be used): the same service's speech engine says the replies in the avatar's
    # voice (Supertonic F1 = Meera). It needs no platform sign-in. Empty = the browser's own voice.
    avatar_voice: str = "F1"
    avatar_tts_timeout_s: float = 20.0
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

    # WorkflowHub platform SSO (docs/08-platform-sso.md). WorkflowHub injects WFH_AUTH_SERVICE_URL into the container.
    wfh_auth_service_url: str = "http://192.168.0.84:5001/api"
    wfh_auth_mode: str = "platform"  # platform | none (SSO off)
    # Role for a platform user with no clinic staff account: "operator" (default) creates a clinic-device account on
    # first sign-in, so any WorkflowHub user can run consultations. Empty = refuse them. Doctors and admins are never
    # created this way: they sign in with their clinic accounts.
    wfh_default_role: str | None = "operator"
    # Sharing the API with other apps and AI agents (node.yaml `api:`; docs/08-platform-sso.md). WorkflowHub calls as
    # the person asking, with a signed X-Wfh-User header; it injects the secret and audience. Unset = refused.
    wfh_app_api_secret: str | None = None
    wfh_app_api_audience: str | None = None

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
