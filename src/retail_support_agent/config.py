"""运行配置。

密钥一律用 `SecretStr` 承载：即使配置对象被打印、序列化或进异常栈，
输出的也是 `**********` 而不是真实 Key。取真实值只能显式调用
`get_secret_value()`，全项目仅在构造 HTTP 客户端时调用一次。
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# M0.2 的固定候选池，与 docs/MODEL_EXPERIMENTS.md 一致。
# 顺序即为参照模型在前，不代表任何优劣预设；最终胜者必须由实验决定。
REFERENCE_MODEL = "qwen3.7-max-2026-06-08"
CANDIDATE_MODELS: tuple[str, ...] = (
    REFERENCE_MODEL,
    "qwen3.8-max",
    "deepseek-v4-pro-0813",
    "glm-5.2",
    "qwen3.8-flash",
)


class Settings(BaseSettings):
    """从环境与 .env 读取；.env 已被 gitignore，禁止提交。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    dashscope_api_key: SecretStr = Field(validation_alias="DASHSCOPE_API_KEY")
    base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        validation_alias="RSA_OPENAI_BASE_URL",
    )

    def key_presence(self) -> str:
        """报告里只记录"配了没配"，连末几位都不暴露。"""
        return f"present(len={len(self.dashscope_api_key.get_secret_value())})"
