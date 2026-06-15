from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Qdrant
    qdrant_host: str = Field("localhost", alias="QDRANT_HOST")
    qdrant_port: int = Field(6333, alias="QDRANT_PORT")
    qdrant_in_memory: bool = Field(False, alias="QDRANT_IN_MEMORY")
    qdrant_collection_name: str = Field("agent_memories", alias="QDRANT_COLLECTION_NAME")
    qdrant_learning_collection_name: str = Field(
        "learning_artifacts",
        alias="QDRANT_LEARNING_COLLECTION_NAME",
        description="Separate vector collection for Learning Ledger artifacts (semantic dedup).",
    )

    # Ollama
    ollama_base_url: str = Field("http://localhost:11434", alias="OLLAMA_BASE_URL")
    ollama_embedding_model: str = Field("nomic-embed-text", alias="OLLAMA_EMBEDDING_MODEL")
    local_llm_provider: str = Field("auto", alias="LOCAL_LLM_PROVIDER")
    local_llm_fallback_order: str = Field("ollama,lmstudio", alias="LOCAL_LLM_FALLBACK_ORDER")
    lmstudio_base_url: str = Field("http://localhost:1234/v1", alias="LMSTUDIO_BASE_URL")
    lmstudio_model: str = Field("auto", alias="LMSTUDIO_MODEL")
    # nomic-embed-text (default Ollama model) outputs 768-dim vectors.
    # Override via EMBEDDING_DIMENSIONS env var if using a different model.
    # Tests override this to 1024 in conftest.py to stay self-consistent.
    embedding_dimensions: int = Field(768, alias="EMBEDDING_DIMENSIONS")

    # Server
    server_host: str = Field("0.0.0.0", alias="SERVER_HOST")
    server_port: int = Field(8000, alias="SERVER_PORT")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    api_prefix: str = Field("/api/v1", alias="API_PREFIX")

    # Business logic
    max_search_results: int = Field(20, alias="MAX_SEARCH_RESULTS")
    cleanup_min_importance: float = Field(0.2, alias="CLEANUP_MIN_IMPORTANCE")
    cleanup_max_age_days: int = Field(30, alias="CLEANUP_MAX_AGE_DAYS")
    self_project_id: str = Field("mnemoforge", alias="SELF_PROJECT_ID")
    public_project_alias: str = Field("sloplesscode", alias="PUBLIC_PROJECT_ALIAS")
    project_capabilities: str = Field("", alias="PROJECT_CAPABILITIES")
    runtime_kind: str = Field("auto", alias="MNEMOFORGE_RUNTIME_KIND")
    runtime_owner_guard: bool = Field(True, alias="MNEMOFORGE_RUNTIME_OWNER_GUARD")
    runtime_owner_allow_takeover: bool = Field(False, alias="MNEMOFORGE_RUNTIME_OWNER_ALLOW_TAKEOVER")
    runtime_owner_stale_seconds: float = Field(120.0, alias="MNEMOFORGE_RUNTIME_OWNER_STALE_SECONDS")

    # Module control — comma-separated list of module names to disable
    # Example: DISABLED_MODULES=watcher,layout_fixer,log_filter
    disabled_modules: str = Field("", alias="DISABLED_MODULES")

    # Security — all optional, safe defaults for local use
    # API_KEY: if set, all requests must include X-Api-Key header with this value
    api_key: str = Field("", alias="API_KEY")
    # INGEST_ALLOWED_ROOTS: comma-separated dirs that ingest/project may read
    # Empty = no restriction (local dev). Example: /home/user/projects,/tmp
    ingest_allowed_roots: str = Field("", alias="INGEST_ALLOWED_ROOTS")
    # MAX_REQUEST_SIZE_MB: reject request bodies larger than this (0 = unlimited)
    max_request_size_mb: int = Field(0, alias="MAX_REQUEST_SIZE_MB")
    # LLM_RATE_LIMIT_PER_MIN: max LLM-heavy requests per minute per IP (0 = unlimited)
    llm_rate_limit_per_min: int = Field(0, alias="LLM_RATE_LIMIT_PER_MIN")

    # Redis — optional, used for persisted rate limiting and MCP session context
    # REDIS_URL: e.g. redis://localhost:6379/0 (leave empty to use in-memory fallback)
    redis_url: str = Field("", alias="REDIS_URL")
    # RATE_LIMIT_BACKEND: "redis" | "memory" (auto = redis if REDIS_URL set, else memory)
    rate_limit_backend: str = Field("auto", alias="RATE_LIMIT_BACKEND")

    # Cloud LLM — configurable OpenAI-compatible provider
    # Prefer CLOUD_LLM_* for new setups. Legacy GLM_* stays as fallback.
    cloud_llm_provider: str = Field("", alias="CLOUD_LLM_PROVIDER")
    cloud_llm_api_key: str = Field("", alias="CLOUD_LLM_API_KEY")
    cloud_llm_model: str = Field("", alias="CLOUD_LLM_MODEL")
    cloud_llm_base_url: str = Field(
        "https://generativelanguage.googleapis.com/v1beta/openai",
        alias="CLOUD_LLM_BASE_URL",
    )

    # Legacy GLM / Zhipu AI config (backward compatibility)
    # GLM_API_KEY: get from https://open.bigmodel.cn/
    glm_api_key: str = Field("", alias="GLM_API_KEY")
    glm_model: str = Field("glm-4.5-air", alias="GLM_MODEL")
    glm_base_url: str = Field("https://api.z.ai/api/coding/paas/v4", alias="GLM_BASE_URL")

    # Gemini native config (first-class simple setup)
    gemini_api_key: str = Field("", alias="GEMINI_API_KEY")
    gemini_model: str = Field("", alias="GEMINI_MODEL")
    gemini_base_url: str = Field("https://generativelanguage.googleapis.com/v1beta", alias="GEMINI_BASE_URL")

    # DeepSeek OpenAI-compatible config/profile key
    deepseek_api_key: str = Field("", alias="DEEPSEEK_API_KEY")
    deepseek_model: str = Field("deepseek-chat", alias="DEEPSEEK_MODEL")
    deepseek_base_url: str = Field("https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")

    # Learning Ledger / GLM Mirror
    glm_generate_model: str = Field("qwen3:1.7b", alias="GLM_GENERATE_MODEL")
    glm_response_language: str = Field("Russian", alias="GLM_RESPONSE_LANGUAGE")
    glm_mirror_interval_hours: float = Field(0.1667, alias="GLM_MIRROR_INTERVAL_HOURS")
    glm_skip_evidence_threshold: bool = Field(False, alias="GLM_SKIP_EVIDENCE_THRESHOLD")

    # Cloud routing tiers
    primary_cloud_llm: str = Field("", alias="PRIMARY_CLOUD_LLM")
    fallback_cloud_llms: str = Field("", alias="FALLBACK_CLOUD_LLMS")
    economy_cloud_llms: str = Field("", alias="ECONOMY_CLOUD_LLMS")
    balanced_cloud_llms: str = Field("", alias="BALANCED_CLOUD_LLMS")
    reasoning_cloud_llms: str = Field("", alias="REASONING_CLOUD_LLMS")
    cloud_llm_model_profiles: str = Field("", alias="CLOUD_LLM_MODEL_PROFILES")
    disabled_cloud_llms: str = Field("", alias="DISABLED_CLOUD_LLMS")

    # AI directory watcher / dialogue learning
    watcher_auto_start: bool = Field(False, alias="WATCHER_AUTO_START")
    watcher_agent_id: str = Field("ai-dirs", alias="WATCHER_AGENT_ID")
    watcher_enable_dialogue_analysis: bool = Field(True, alias="WATCHER_ENABLE_DIALOGUE_ANALYSIS")

    model_config = {
        "env_file": ".env",
        "populate_by_name": True,
        "extra": "ignore",
    }

    @property
    def learning_mirror_model(self) -> str:
        return self.glm_generate_model

    @property
    def response_language(self) -> str:
        return self.glm_response_language

    @property
    def learning_mirror_interval_hours(self) -> float:
        return self.glm_mirror_interval_hours

    @property
    def learning_mirror_skip_evidence_threshold(self) -> bool:
        return self.glm_skip_evidence_threshold


settings = Settings()
