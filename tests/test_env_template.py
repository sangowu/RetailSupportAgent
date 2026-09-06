"""`.env.example` 与 `Settings` 的一致性。

模板会变乱不是因为谁不小心，而是因为**没有东西拦着它跑偏**：
新增一个配置字段时，模板不同步也不会有任何报错，
下一个照着模板配环境的人就少配一项、静默退回默认端点。
这里把它钉死。
"""

from __future__ import annotations

import re
from pathlib import Path

from retail_support_agent.config import Settings

TEMPLATE = Path(__file__).resolve().parents[1] / ".env.example"

# 已知在模板里、但 Settings 不读的变量。留着是有意的标记，不是遗漏。
# Langfuse 三项目前**没有任何代码读取**（tau2 的 USE_LANGFUSE=False，
# 且包未安装），模板里写明了这一点。
DELIBERATELY_UNREAD = {"LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"}


def template_vars() -> set[str]:
    text = TEMPLATE.read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE))


def settings_vars() -> set[str]:
    return {str(f.validation_alias or name) for name, f in Settings.model_fields.items()}


def test_every_settings_field_appears_in_the_template() -> None:
    """新增字段却不更新模板，别人照着模板配就会少一项。"""
    missing = settings_vars() - template_vars()
    assert not missing, f".env.example 缺少：{sorted(missing)}"


def test_template_has_no_variables_the_code_ignores() -> None:
    """模板里出现代码根本不读的变量，会让人以为配了就生效。

    Langfuse 三项是例外，且模板里已明确标注「未接入、配了也不生效」。
    """
    stray = template_vars() - settings_vars() - DELIBERATELY_UNREAD
    assert not stray, f".env.example 里有代码不读的变量：{sorted(stray)}"


def test_template_carries_no_secret_values() -> None:
    """模板必须是空壳。填了真值就会随仓库泄露出去。

    只放行 base_url 与模型 ID 这类非密值——它们本来就是公开信息，
    预填能省掉一次查文档。
    """
    allowed_prefixes = ("https://", "")
    allowed_exact = {"glm-5.3-flash", "qwen3.7-flash-2026-07-15"}
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        if not re.match(r"^[A-Z][A-Z0-9_]*=", line):
            continue
        name, _, value = line.partition("=")
        if value in allowed_exact or value.startswith(allowed_prefixes):
            continue
        raise AssertionError(f"{name} 在模板里带了疑似真实值")


def test_secret_fields_are_never_prefilled() -> None:
    """凡是名字里带 KEY 的，模板里必须留空。"""
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.endswith(("API_KEY", "SECRET_KEY", "PUBLIC_KEY")):
            assert value == "", f"{name} 不该在模板里预填任何值"
