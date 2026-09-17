"""Runtime configuration.

Secrets are held in `SecretStr`, so printing, serializing, or including the settings
object in a traceback yields `**********` rather than the key. `get_secret_value()`
is called once, when constructing an HTTP client.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# The fixed M0.2/M0.4 candidate pool, matching docs/MODEL_EXPERIMENTS.md.
# This is a historical record. Self-preference and same-family checks are measured
# against it, so changing it would invalidate archived experiment conclusions.
REFERENCE_MODEL = "qwen3.7-max-2026-06-08"
CANDIDATE_MODELS: tuple[str, ...] = (
    REFERENCE_MODEL,
    "qwen3.8-max",
    "deepseek-v4-pro-0813",
    "glm-5.2",
    "qwen3.8-flash",
)

# The production model chosen in M0.4, served by DeepSeek's own endpoint
# (see RSA_AGENT_BASE_URL).
#
# It was chosen on cost, not capability:
#   holdout 40 tasks scored 0.600 (24/40) against qwen3.8-max at 0.800 (32/40);
#   paired McNemar p=0.077, not significant but directionally clear.
#   In exchange, 0.079 CNY per task versus 0.419, roughly 5x cheaper.
# That is about 0.34 CNY saved per task at the cost of roughly one extra wrong order
# in five. A business trade-off, not a technical conclusion.
#
# Distinct from `deepseek-v4-pro-0813` in the candidate pool: that is the same model
# on DashScope at 3-5x the price. The names must stay separate or costs get computed
# from the wrong price table.
SELECTED_MODEL = "deepseek-v4-pro"


def model_family(model_id: str) -> str:
    """The model's family identifier, used to detect same-origin models.

    Self-preference is not limited to a model grading itself: models in one family
    often share training data and output style, and a judge may systematically favour
    its own family's phrasing. Exact matching misses this
    (`deepseek-v4-flash` != `deepseek-v4-pro-0813`), so families are compared as well
    and recorded as a warning. It does not block a run, but it must be on record.

    Rule: take the first segment; include the second if it looks like v<digits>.
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
    """Read from the environment and .env. .env is gitignored and must not be committed."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application state database (M1). Default None: without it the service falls back
    # to M0 in-memory sessions and still starts. The URL contains a password, so it is
    # held in SecretStr and never leaks through printing or a traceback.
    database_url: SecretStr | None = Field(default=None, validation_alias="RSA_DATABASE_URL")

    # Authentication (M3). Unset means authentication is off: the service keeps the M0
    # fixed-identity, loopback-only behaviour, because the evaluation loop and the local
    # demo do not go through an identity provider and a hard dependency would stop
    # evaluation from running at all. Setting the issuer and the audience turns it on
    # for every route.
    #
    # For a Cognito user pool the issuer is
    # https://cognito-idp.<region>.amazonaws.com/<pool id> and the key set is the
    # standard path under it, which is why the URL is derived rather than configured
    # twice. None of these are secrets: they identify a pool, they do not open it.
    auth_issuer: str | None = Field(default=None, validation_alias="RSA_AUTH_ISSUER")
    auth_audience: str | None = Field(default=None, validation_alias="RSA_AUTH_AUDIENCE")
    auth_jwks_url: str | None = Field(default=None, validation_alias="RSA_AUTH_JWKS_URL")
    auth_token_use: str = Field(default="access", validation_alias="RSA_AUTH_TOKEN_USE")

    # Which image is answering. Reported by /health so that "which version is running" is a
    # question with an answer rather than an assumption, which is what makes a rollback
    # checkable (M6.8). Unset outside a container, where the question does not arise.
    image: str | None = Field(default=None, validation_alias="RSA_IMAGE")

    # How long a supervisor's decision on an escalated action stays usable (M4). The
    # architecture document fixes the default at 30 minutes and requires it to be
    # configurable: a deployment where cases sit in a queue for an hour needs a longer
    # window, and one handling refunds may want a much shorter one.
    approval_ttl_seconds: float = Field(default=1800.0, validation_alias="RSA_APPROVAL_TTL_SECONDS")

    # The column slice: fields hidden from the model in every tool result. Comma
    # separated, empty to disable. The default hides the customer's email, which no tool
    # consumes as input. `payment_methods` is available but deliberately not default:
    # a return needs the payment_method_id, so hiding it would break the workflow rather
    # than protect it, and a control that breaks the task gets turned off in practice.
    scope_redact_fields: str = Field(default="email", validation_alias="RSA_SCOPE_REDACT_FIELDS")

    # The row slice: order lifecycle stages a session may write to. Comma separated,
    # empty for every stage, which is the default because the upstream tools already
    # restrict themselves (a cancellation needs a pending order, a return a delivered
    # one) and naming a single stage here would deny one of them outright.
    scope_write_statuses: str = Field(default="", validation_alias="RSA_SCOPE_WRITE_STATUSES")

    dashscope_api_key: SecretStr = Field(validation_alias="DASHSCOPE_API_KEY")
    base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        validation_alias="RSA_OPENAI_BASE_URL",
    )

    # The NL-assertion judge runs on a separate provider, so its quota is isolated
    # from the agent under test: exhausting the agent's account cannot take the judge
    # down with it. Set all three or none.
    judge_base_url: str | None = Field(default=None, validation_alias="RSA_JUDGE_BASE_URL")
    judge_api_key: SecretStr | None = Field(default=None, validation_alias="RSA_JUDGE_API_KEY")
    judge_model: str | None = Field(default=None, validation_alias="RSA_JUDGE_MODEL")

    # The agent under test can also use its own endpoint: the same model can cost 3x
    # more on one platform than another (measured for deepseek-v4-pro: 0.133 CNY per
    # task on DeepSeek off-peak, 0.391 on DashScope).
    # Never do this by changing RSA_OPENAI_BASE_URL: that would move the simulated
    # user too, and the simulated user is part of the frozen evaluation stack. Change
    # it and old and new scores stop being comparable.
    agent_base_url: str | None = Field(default=None, validation_alias="RSA_AGENT_BASE_URL")
    agent_api_key: SecretStr | None = Field(default=None, validation_alias="RSA_AGENT_API_KEY")

    # The simulated user can move to another platform as well. Prices differ several
    # fold, and the simulated user is 40% of the three-role cost, so it is worth
    # pricing separately. As with the judge: set all three or none. A half-set
    # configuration silently falls back to DashScope, and the resulting scores would
    # be read as belonging to the new platform.
    user_base_url: str | None = Field(default=None, validation_alias="RSA_USER_BASE_URL")
    user_api_key: SecretStr | None = Field(default=None, validation_alias="RSA_USER_API_KEY")
    user_model: str | None = Field(default=None, validation_alias="RSA_USER_MODEL")

    # Langfuse cloud observability. Set all three or none: a partial configuration is
    # not used at all, rather than attempting to connect and failing mid-run.
    # `host` is a self-hosted instance or cloud.langfuse.com, depending on deployment.
    langfuse_public_key: SecretStr | None = Field(
        default=None, validation_alias="LANGFUSE_PUBLIC_KEY"
    )
    langfuse_secret_key: SecretStr | None = Field(
        default=None, validation_alias="LANGFUSE_SECRET_KEY"
    )
    langfuse_host: str | None = Field(default=None, validation_alias="LANGFUSE_HOST")

    def has_langfuse(self) -> bool:
        """Langfuse counts as configured only when all three values are present."""
        return bool(self.langfuse_public_key and self.langfuse_secret_key and self.langfuse_host)

    def has_auth(self) -> bool:
        """Authentication counts as configured only with both an issuer and an audience.

        Both, because a token is only meaningful as "minted by this issuer for this
        application": an issuer with no audience accepts a token issued for a different
        client of the same pool.
        """
        return bool(self.auth_issuer and self.auth_audience)

    def jwks_url(self) -> str | None:
        """Where the signing keys come from, derived from the issuer unless overridden."""
        if self.auth_jwks_url:
            return self.auth_jwks_url
        if not self.auth_issuer:
            return None
        return f"{self.auth_issuer.rstrip('/')}/.well-known/jwks.json"

    def key_presence(self) -> str:
        """Reports record only whether a key is set, never any part of its value."""
        return f"present(len={len(self.dashscope_api_key.get_secret_value())})"

    def has_independent_judge(self) -> bool:
        """A separate judge endpoint counts as configured only with all three values."""
        return bool(self.judge_base_url and self.judge_api_key and self.judge_model)

    def has_independent_agent(self) -> bool:
        """A separate agent endpoint needs both values; a half-set falls back to the default."""
        return bool(self.agent_base_url and self.agent_api_key)

    def has_independent_user(self) -> bool:
        """A separate simulated-user endpoint counts as configured only with all three values."""
        return bool(self.user_base_url and self.user_api_key and self.user_model)

    def agent_endpoint(self) -> tuple[str, str]:
        """Where the agent connects. Returns (base_url, plaintext api key).

        Evaluation and the application share this resolution so the two cannot drift
        apart.
        """
        if self.has_independent_agent():
            assert self.agent_base_url and self.agent_api_key  # guaranteed above
            return self.agent_base_url, self.agent_api_key.get_secret_value()
        return self.base_url, self.dashscope_api_key.get_secret_value()

    def agent_is_dashscope(self) -> bool:
        """DashScope-only thinking parameters (enable_thinking and friends) may only
        be sent to DashScope."""
        return "dashscope" in self.agent_endpoint()[0]
