"""The local start script, against the environment the UI actually reads.

None of this runs here: `start-local.ps1` needs PowerShell, two ports and a virtualenv,
and CI has no reason to start a demo. What a test can still hold is the join between the
two halves, which is where the defect was. The script takes `-ApiPort` and `-UiPort`; the
UI reads its port and the API address from the environment. Nothing connected them, so a
non-default `-UiPort` made the UI bind its own default 7860 while the script waited on the
port it had been given, timed out after the full budget, and rolled the API back with it.
The process never failed, so both log files were empty.

The checks below fail if either half is renamed or if the assignment moves after the
launch, and they read the names out of `ui.py` rather than repeating them, so the pair
cannot drift apart silently.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
START = REPO / "scripts" / "start-local.ps1"
UI = REPO / "src" / "retail_support_agent" / "ui.py"


@pytest.fixture(scope="module")
def start_script() -> str:
    return START.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ui_source() -> str:
    return UI.read_text(encoding="utf-8")


def env_names_read(source: str) -> set[str]:
    """The `RSA_*` environment names a module reads through `os.getenv`."""
    return set(re.findall(r'os\.getenv\(\s*"(RSA_[A-Z_]+)"', source))


def test_ui_reads_its_port_and_api_address_from_the_environment(ui_source: str) -> None:
    assert {"RSA_UI_PORT", "RSA_API_URL"} <= env_names_read(ui_source)


@pytest.mark.parametrize(
    ("name", "value"),
    [("RSA_UI_PORT", '"$UiPort"'), ("RSA_API_URL", "$apiUrl")],
)
def test_start_script_passes_its_parameters_to_the_ui(
    start_script: str, name: str, value: str
) -> None:
    """The parameter reaches the UI, and it is set before the UI is launched.

    Order is part of the contract: an assignment after `Start-Service` would be inherited
    by nothing, and the failure would look exactly like the original defect.
    """
    assignment = start_script.find(f"$env:{name} = {value}")
    assert assignment != -1, f"start-local.ps1 does not give the UI {name}"

    launch = start_script.find('Start-Service -Name "ui"')
    assert launch != -1, "the ui service is no longer started by that name"
    assert assignment < launch, f"{name} is set after the UI is launched"


def test_the_api_is_told_its_port_on_the_command_line(start_script: str) -> None:
    """Uvicorn takes the port as an argument, so `-ApiPort` was never broken for the API.

    Kept so that a move to the environment has to be made deliberately on both sides.
    """
    assert '"--port", "$ApiPort"' in start_script
