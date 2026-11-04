from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    database_url: str
    demo_password: str
    tool_service_key: str
    dify_base_url: str = 'http://127.0.0.1:8088/v1'
    dify_operator_key: str = ''
    dify_knowledge_key: str = ''
    dify_public_dataset: str = ''
    dify_internal_dataset: str = ''
    ops_worker_enabled: bool = True
    platform_worker_enabled: bool = True
    chat_recovery_seconds: int = Field(default=300, ge=240, le=3600)
    platform_encryption_key: str = ''
    memory_enabled: bool = True
    memory_worker_enabled: bool = True
    memory_embedding_enabled: bool = True
    memory_embedding_url: str = ''
    memory_embedding_api_key: SecretStr = SecretStr('')
    memory_embedding_model: str = ''
    memory_embedding_revision: str = 'v1'
    memory_embedding_dimensions: int = Field(default=0, ge=0, le=16000)
    memory_embedding_query_timeout: float = Field(default=1.5, gt=0, le=30)
    memory_embedding_index_timeout: float = Field(default=30, gt=0, le=120)
    memory_context_chars: int = 8000
    # Conservative local token accounting; no tokenizer/model downloads at runtime.
    memory_context_tokens: int = Field(default=32000, ge=2048, le=100000)
    memory_model_context_tokens: int = Field(default=65536, ge=8192)
    memory_reserved_tokens: int = Field(default=16384, ge=4096)
    memory_recent_turns: int = Field(default=3, ge=1, le=20)
    memory_light_turns: int = Field(default=0, ge=0, le=60)  # legacy config, no proactive light history
    memory_segment_turns: int = Field(default=8, ge=2, le=20)
    memory_index_active_limit: int = Field(default=3, ge=0, le=10)
    memory_index_idle_turns: int = Field(default=24, ge=1)
    memory_index_idle_days: int = Field(default=7, ge=1)
    memory_index_context_tokens: int = Field(default=2400, ge=256)
    memory_task_state_tokens: int = Field(default=1200, ge=256)
    memory_long_context_tokens: int = Field(default=2400, ge=256)
    memory_reactive_enabled: bool = True
    memory_reactive_summary_tokens: int = Field(default=1600, ge=256, le=8000)
    memory_reactive_input_tokens: int = Field(default=48000, ge=1024, le=100000)
    memory_reactive_timeout: float = Field(default=20, gt=0, le=60)
    memory_reactive_retry_ratio: float = Field(default=0.6, gt=0, lt=1)


settings = Settings()
