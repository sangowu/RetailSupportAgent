"""运行配置。

密钥一律用 `SecretStr` 承载：即使配置对象被打印、序列化或进异常栈，
输出的也是 `**********` 而不是真实 Key。取真实值只能显式调用
`get_secret_value()`，全项目仅在构造 HTTP 客户端时调用一次。
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# M0.2/M0.4 的固定候选池，与 docs/MODEL_EXPERIMENTS.md 一致。
# **这是历史记录，不要改**：自评偏袒与同家族检查都以它为基准，
# 改动会让已归档的实验结论对不上。
REFERENCE_MODEL = "qwen3.7-max-2026-06-08"
CANDIDATE_MODELS: tuple[str, ...] = (
    REFERENCE_MODEL,
    "qwen3.8-max",
    "deepseek-v4-pro-0813",
    "glm-5.2",
    "qwen3.8-flash",
)

# M0.4 选定的生产模型，走 DeepSeek 官方端点（见 RSA_AGENT_BASE_URL）。
#
# 选定依据是**成本**，不是"它最强"——这个区别必须一直说清楚：
#   保留集 40 题 0.600（24/40），而 qwen3.8-max 是 0.800（32/40）；
#   McNemar 配对 p=0.077，未达显著，但倾向明确。
#   代价换来的是每题 0.079 元 vs 0.419 元，便宜约 5 倍。
# 即：每题省约 0.34 元，代价是每 5 单可能多错 1 单。这是业务权衡，不是技术结论。
#
# 注意与候选池里的 `deepseek-v4-pro-0813` 区分：那是 DashScope 上的同款，
# 价格是本模型的 3~5 倍，名字必须分开，否则会按错的价目表折算成本。
SELECTED_MODEL = "deepseek-v4-pro"


def model_family(model_id: str) -> str:
    """取模型的"家族"标识，用于识别同源模型。

    自评偏袒不止发生在"同一个模型给自己打分"这种极端情况：同一家族的模型
    往往共享训练数据与输出风格，判官可能系统性地偏爱自家风格的回答。
    精确匹配拦不住这种情况（`deepseek-v4-flash` != `deepseek-v4-pro-0813`），
    所以另外按家族比对，作为**警告**记进报告——不阻断，但必须留痕。

    规则：取第一段；若第二段形如 v<数字> 则一并计入。
        deepseek-v4-flash    -> deepseek-v4
        deepseek-v4-pro-0813 -> deepseek-v4
        qwen3.8-max          -> qwen3.8
        glm-5.2              -> glm
    """
    parts = model_id.split("-")
    head = parts[0]
    if len(parts) > 1 and len(parts[1]) > 1 and parts[1][0] == "v" and parts[1][1:].isdigit():
        return f"{head}-{parts[1]}"
    return head


CANDIDATE_FAMILIES: frozenset[str] = frozenset(model_family(m) for m in CANDIDATE_MODELS)


class Settings(BaseSettings):
    """从环境与 .env 读取；.env 已被 gitignore，禁止提交。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 应用侧状态库（M1）。默认 None：不配就退回 M0 的内存会话，
    # 服务照常起得来——数据库是能力，不是启动的前提条件。
    # 连接串里含口令，所以用 SecretStr 承载：打印或进异常栈时不会泄出去。
    database_url: SecretStr | None = Field(default=None, validation_alias="RSA_DATABASE_URL")

    dashscope_api_key: SecretStr = Field(validation_alias="DASHSCOPE_API_KEY")
    base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        validation_alias="RSA_OPENAI_BASE_URL",
    )

    # NL 断言评判走独立服务商：额度与被测模型完全隔离，
    # 被测账户额度耗尽不会连带判官一起挂掉。三项要么都配，要么都不配。
    judge_base_url: str | None = Field(default=None, validation_alias="RSA_JUDGE_BASE_URL")
    judge_api_key: SecretStr | None = Field(default=None, validation_alias="RSA_JUDGE_API_KEY")
    judge_model: str | None = Field(default=None, validation_alias="RSA_JUDGE_MODEL")

    # 被测 Agent 也可以走独立端点：同一个模型在不同平台价差可达 3 倍
    # （实测 deepseek-v4-pro：DeepSeek 官方谷时 0.133 元/题，DashScope 0.391 元/题）。
    # **绝不能靠改 RSA_OPENAI_BASE_URL 来实现**——那会把模拟用户一起改掉，
    # 而模拟用户是冻结评测栈的一部分，一动，新旧成绩就不可比。
    agent_base_url: str | None = Field(default=None, validation_alias="RSA_AGENT_BASE_URL")
    agent_api_key: SecretStr | None = Field(default=None, validation_alias="RSA_AGENT_API_KEY")

    # 模拟用户也可以搬到别的平台。同一个模型在不同平台价差可达数倍，
    # 而模拟用户占三角色总成本的 40%，值得单独比价。
    # 与判官同样是"三项要么都配、要么都不配"：只配一半会静默退回 DashScope，
    # 那样跑出来的成绩会被误以为是新平台的。
    user_base_url: str | None = Field(default=None, validation_alias="RSA_USER_BASE_URL")
    user_api_key: SecretStr | None = Field(default=None, validation_alias="RSA_USER_API_KEY")
    user_model: str | None = Field(default=None, validation_alias="RSA_USER_MODEL")

    # Langfuse 云端观测。**三项要么都配、要么都不配**：缺一项就整体不启用，
    # 而不是带着半套配置去连——那只会在跑批中途才发现连不上。
    # host 用国内可达的自建实例或 cloud.langfuse.com，按部署决定。
    langfuse_public_key: SecretStr | None = Field(
        default=None, validation_alias="LANGFUSE_PUBLIC_KEY"
    )
    langfuse_secret_key: SecretStr | None = Field(
        default=None, validation_alias="LANGFUSE_SECRET_KEY"
    )
    langfuse_host: str | None = Field(default=None, validation_alias="LANGFUSE_HOST")

    def has_langfuse(self) -> bool:
        """三项齐全才算配好了 Langfuse。"""
        return bool(self.langfuse_public_key and self.langfuse_secret_key and self.langfuse_host)

    def key_presence(self) -> str:
        """报告里只记录"配了没配"，连末几位都不暴露。"""
        return f"present(len={len(self.dashscope_api_key.get_secret_value())})"

    def has_independent_judge(self) -> bool:
        """三项齐全才算配好了独立判官端点。"""
        return bool(self.judge_base_url and self.judge_api_key and self.judge_model)

    def has_independent_agent(self) -> bool:
        """两项齐全才算配好了独立被测端点；只配一半宁可退回默认。"""
        return bool(self.agent_base_url and self.agent_api_key)

    def has_independent_user(self) -> bool:
        """三项齐全才算配好了独立模拟用户端点。"""
        return bool(self.user_base_url and self.user_api_key and self.user_model)

    def agent_endpoint(self) -> tuple[str, str]:
        """被测/生产 Agent 该连哪里。返回 (base_url, api_key 明文)。

        评测与应用共用这一处解析，避免两边各写一份、日后改岔。
        """
        if self.has_independent_agent():
            assert self.agent_base_url and self.agent_api_key  # has_independent_agent 已保证
            return self.agent_base_url, self.agent_api_key.get_secret_value()
        return self.base_url, self.dashscope_api_key.get_secret_value()

    def agent_is_dashscope(self) -> bool:
        """DashScope 专有的思考参数（enable_thinking 等）只能发给 DashScope。"""
        return "dashscope" in self.agent_endpoint()[0]
