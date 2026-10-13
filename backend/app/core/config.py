"""全局配置：pydantic-settings 读取环境变量 / .env。"""
from __future__ import annotations

import base64
import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 应用数据库（Docker 内为 PostgreSQL；本地开发/测试可指向 SQLite）
    app_db_url: str = "sqlite+aiosqlite:///./local/data_agent.db"

    # 安全
    jwt_secret: str = "dev-insecure-secret-change-me-please-32"
    jwt_expire_hours: int = 24 * 7
    secret_encryption_key: str = ""  # base64(32B)；空则用 jwt_secret 派生（仅限开发）

    # 默认管理员
    default_admin_username: str = "admin"
    default_admin_password: str = "admin12345"

    # Qdrant / Tavily
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    tavily_api_key: str = ""

    # 演示库
    demo_db_enabled: bool = True
    demo_db_path: str = "./local/ecom_demo.db"

    # 查询性能与安全
    query_cache_ttl_sec: int = 300
    sql_max_rows: int = 10000
    query_timeout_sec: int = 30
    scan_batch_size: int = 20

    # 应用行为
    bootstrap_create_all: bool = True
    cors_origins: str = "*"

    # AI 建模工程师（PRD 3.3D-6）：内置编码代理引擎（吸收 OpenAI Codex 核心架构），
    # 代理所用 LLM 经 modeling_agent 场景绑定配置，无需额外引擎配置

    @property
    def encryption_key_bytes(self) -> bytes:
        """32 字节 AES 主密钥。优先 SECRET_ENCRYPTION_KEY（base64）；否则由 jwt_secret 派生（开发用）。"""
        raw = self.secret_encryption_key.strip()
        if raw:
            try:
                key = base64.b64decode(raw)
                if len(key) == 32:
                    return key
            except Exception:
                pass
        # 开发降级：sha256(jwt_secret)
        import hashlib

        return hashlib.sha256(self.jwt_secret.encode()).digest()

    @property
    def is_sqlite_app_db(self) -> bool:
        return self.app_db_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# 便于环境变量注入（tests / bootstrap 脚本）
def reload_settings() -> Settings:
    global settings
    get_settings.cache_clear()
    settings = get_settings()
    return settings
