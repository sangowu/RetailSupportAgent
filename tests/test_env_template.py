"""Consistency between `.env.example` and `Settings`.

The template drifts not through carelessness but because nothing stops it: adding a
settings field without updating the template raises no error, and the next person
configuring from the template silently misses a value and falls back to a default
endpoint. This pins it.
"""

from __future__ import annotations

import re
from pathlib import Path

from retail_support_agent.config import Settings

TEMPLATE = Path(__file__).resolve().parents[1] / ".env.example"

# Every variable in the template is now read by code, so this exception set is empty.
# It stays as a mechanism: a future "in the template but read by nobody" variable must
# be listed here with a reason rather than slipping past the test below.
#
# Emptied 2026-09-07: the three Langfuse variables were previously unread and are now
# used by Settings.has_langfuse() and evaluation.enable_langfuse().
DELIBERATELY_UNREAD: set[str] = set()


def template_vars() -> set[str]:
    text = TEMPLATE.read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE))


def settings_vars() -> set[str]:
    return {str(f.validation_alias or name) for name, f in Settings.model_fields.items()}


def test_every_settings_field_appears_in_the_template() -> None:
    """A new field without a template entry means the next person misses it."""
    missing = settings_vars() - template_vars()
    assert not missing, f".env.example is missing: {sorted(missing)}"


def test_template_has_no_variables_the_code_ignores() -> None:
    """A variable the code never reads suggests setting it has an effect.

    No exceptions since 2026-09-07: the three Langfuse variables are wired up.
    """
    stray = template_vars() - settings_vars() - DELIBERATELY_UNREAD
    assert not stray, f".env.example has variables the code does not read: {sorted(stray)}"


def test_template_carries_no_secret_values() -> None:
    """The template must be empty of values. A real value would leak with the repository.

    Only non-secret values such as base URLs and model ids are allowed; they are public
    anyway and prefilling them saves a documentation lookup.
    """
    # The empty string used to be in this tuple, and str.startswith("") is always
    # True, so the check passed everything. Empty values are handled explicitly now.
    allowed_prefixes = ("https://",)
    allowed_exact = {
        "glm-5.3-flash",
        "qwen3.7-flash-2026-07-15",
        # The local development database from compose.yaml, which listens on the
        # loopback interface only. Not a credential for anything real, and keeping it
        # here makes it obvious which database a developer is pointed at.
        "postgresql+psycopg://rsa:rsa_local_dev@127.0.0.1:55432/rsa",
    }
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        if not re.match(r"^[A-Z][A-Z0-9_]*=", line):
            continue
        name, _, value = line.partition("=")
        if value == "" or value in allowed_exact or value.startswith(allowed_prefixes):
            continue
        raise AssertionError(f"{name} carries what looks like a real value in the template")


def test_secret_fields_are_never_prefilled() -> None:
    """Anything with KEY in its name must be blank in the template."""
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.endswith(("API_KEY", "SECRET_KEY", "PUBLIC_KEY")):
            assert value == "", f"{name} must not be prefilled in the template"
