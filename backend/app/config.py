"""Application settings (environment variables). Every variable of spec section 20 plus runtime knobs."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # infrastructure
    database_url: str = "postgresql://alerting:alerting@localhost:5432/alerting"
    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_from: str = "alerts@gs-alerting.local"
    mockgw_url: str = "http://localhost:8090"
    public_base_url: str = "http://localhost:8080"

    # secrets (dev defaults; override in .env / Railway)
    jwt_secret: str = "dev-jwt-secret-change-me"
    app_secret: str = "dev-app-secret-change-me"
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 7 * 24 * 3600
    bcrypt_rounds: int = 12

    # LLM (LiteLLM; keys optional)
    llm_model_fast: str = ""
    llm_model_smart: str = ""
    llm_fallbacks: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    embeddings_model: str = "BAAI/bge-small-en-v1.5"
    # ONNX threads for local embeddings. Kept small so the async AI worker never starves the hot path
    # when it shares a host with ingest/worker processes.
    embeddings_threads: int = 2

    # runtime
    app_role: str = "api"
    port: int = 8080
    metrics_port: int = 9101
    stats_flush_seconds: float = 1.0
    environment: str = "local"
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    skip_migrations: bool = False
    demo_fast_escalation: bool = False

    # test isolation / multi-tenant prefixes
    mq_prefix: str = ""
    redis_prefix: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
