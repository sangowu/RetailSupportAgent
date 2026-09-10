"""The files that are copied onto the demo instance.

None of this runs here, which is exactly why it is worth a test. A mistake in these files
shows up as an instance that boots and does nothing, ten minutes and one `terraform apply`
after it was made. The checks are the ones that catch that class of mistake without an
instance: the two compose files describing the same stack, the cloud-init template naming
every file it is given, and shell scripts that a Linux shell can actually run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
INSTANCE = REPO / "deploy" / "instance"
TEMPLATE = INSTANCE / "cloud-init.sh.tftpl"
DEMO_TF = REPO / "deploy" / "terraform" / "demo" / "main.tf"

SERVICES = ("db", "migrate", "seed", "api", "ui")
# Caddy runs only on the instance: development reaches the UI on the loopback address and
# has nothing to get a certificate for.
INSTANCE_ONLY = ("caddy",)


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def local_compose() -> dict:
    return load(REPO / "compose.yaml")


@pytest.fixture(scope="module")
def instance_compose() -> dict:
    return load(INSTANCE / "compose.yaml")


class TestTheTwoComposeFiles:
    """Development builds the image; the instance pulls it. Everything else must match.

    They are separate files because the differences are real, and separate files drift.
    """

    def test_they_define_the_same_services(self, local_compose, instance_compose) -> None:
        assert set(local_compose["services"]) == set(SERVICES)
        assert set(instance_compose["services"]) == set(SERVICES) | set(INSTANCE_ONLY)

    @pytest.mark.parametrize("service", SERVICES)
    def test_each_shared_service_runs_the_same_command(
        self, service: str, local_compose, instance_compose
    ) -> None:
        assert local_compose["services"][service].get("command") == instance_compose["services"][
            service
        ].get("command")

    @pytest.mark.parametrize("service", ("migrate", "seed", "api", "ui"))
    def test_each_service_is_given_the_same_settings(
        self, service: str, local_compose, instance_compose
    ) -> None:
        """The names, not the values: the database host is the same, the key is not."""
        assert set(local_compose["services"][service]["environment"]) == set(
            instance_compose["services"][service]["environment"]
        )

    @pytest.mark.parametrize("service", ("db", "api", "ui"))
    def test_each_long_running_service_has_a_health_check(
        self, service: str, instance_compose
    ) -> None:
        """`docker compose up --wait` returns when health checks pass.

        Without one, "the container started" is what the boot waits for, and the instance
        reports itself ready before the application answers.
        """
        assert "healthcheck" in instance_compose["services"][service]

    def test_migrations_run_once_and_are_not_restarted(self, instance_compose) -> None:
        migrate = instance_compose["services"]["migrate"]
        assert migrate["restart"] == "no"
        for dependent in ("api", "ui"):
            condition = instance_compose["services"][dependent]["depends_on"]["migrate"]
            assert condition["condition"] == "service_completed_successfully"

    def test_the_instance_file_names_no_key(self, instance_compose) -> None:
        """Every value must be a substitution, never a literal.

        The key reaches the instance from Parameter Store at boot. A default here would put
        it in the repository, and a placeholder that looks like a key is worse than none.
        """
        for service in ("migrate", "seed", "api", "ui"):
            value = instance_compose["services"][service]["environment"]["DASHSCOPE_API_KEY"]
            assert value.startswith("${") and value.endswith("}")

    def test_the_instance_file_requires_an_image_rather_than_defaulting(
        self, instance_compose
    ) -> None:
        for service in ("migrate", "seed", "api", "ui"):
            assert instance_compose["services"][service]["image"].startswith("${RSA_IMAGE:?")


class TestTheCloudInitTemplate:
    """Terraform renders this. A placeholder it does not fill is a boot that half happens."""

    @staticmethod
    @pytest.fixture(scope="class")
    def placeholders() -> set[str]:
        return set(re.findall(r"\$\{(\w+)\}", TEMPLATE.read_text(encoding="utf-8")))

    @staticmethod
    @pytest.fixture(scope="class")
    def supplied() -> set[str]:
        """What `templatefile` is given in the demo layer, read out of the call itself."""
        text = DEMO_TF.read_text(encoding="utf-8")
        block = text[text.index("templatefile(") : text.index('resource "aws_instance"')]
        return set(re.findall(r"^\s{4}(\w+)\s*=", block, re.MULTILINE))

    def test_every_placeholder_is_supplied(self, placeholders, supplied) -> None:
        assert placeholders - supplied == set()

    def test_nothing_is_supplied_that_the_template_ignores(self, placeholders, supplied) -> None:
        """Terraform errors on an unused variable, so this would break an apply."""
        assert supplied - placeholders == set()

    def test_every_file_it_installs_exists(self) -> None:
        """Read out of the `filebase64` calls rather than listed here.

        A list would go stale in the direction that matters: a file added to the template
        and not to the list is exactly the case this is for.
        """
        text = DEMO_TF.read_text(encoding="utf-8")
        referenced = re.findall(
            r'filebase64\("\$\{path\.module\}/\.\./\.\./instance/([^"]+)"\)', text
        )
        assert referenced, "no instance files are rendered into user data at all"
        for name in referenced:
            assert (INSTANCE / name).is_file(), name

    def test_the_stack_definition_is_not_in_user_data(self) -> None:
        """It ships inside the image instead (M6.8).

        User data runs once per machine, so a compose file written there can only be
        changed by replacing the machine - and a rollback would then leave the old image
        running under the new file, which is how M6.7's compose would have asked M6.6's
        image for a `seed` service it has never heard of.
        """
        text = DEMO_TF.read_text(encoding="utf-8")
        assert "instance/compose.yaml" not in text
        assert "instance/Caddyfile" not in text

    def test_the_version_is_not_in_user_data(self) -> None:
        """The defect M6.8 exists to fix: a version in user data can only be changed by
        destroying the instance, which discards its database and its certificate."""
        template = TEMPLATE.read_text(encoding="utf-8")
        assert "RSA_IMAGE=" not in template
        assert "RSA_IDENTITIES_B64" not in template
        assert "RSA_IMAGE_PARAMETER=" in template
        assert "RSA_IDENTITIES_PARAMETER=" in template

    def test_it_enables_both_units(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        assert "systemctl enable --now rsa-idle.timer" in text
        assert "systemctl enable rsa.service" in text

    def test_the_watcher_is_enabled_before_the_stack(self) -> None:
        """The order is the fix for a real failure, not a preference.

        The first Terraform-built instance failed to start its stack, cloud-init stopped
        at that line under `set -e`, and the timer was never enabled. Nothing could then
        stop the machine, which is the one failure that costs money by the hour.
        """
        text = TEMPLATE.read_text(encoding="utf-8")
        assert text.index("enable --now rsa-idle.timer") < text.index("enable rsa.service")

    def test_a_failing_stack_does_not_abort_the_boot(self) -> None:
        text = TEMPLATE.read_text(encoding="utf-8")
        assert "systemctl start rsa.service || true" in text

    def test_the_watcher_does_not_require_the_stack(self) -> None:
        """An unreachable application is never idle, so the watcher must run without it."""
        unit = (INSTANCE / "rsa-idle.service").read_text(encoding="utf-8")
        assert "After=rsa.service" in unit
        assert "Requires=" not in unit


class TestTheShellScripts:
    @pytest.mark.parametrize(
        "name",
        ("rsa-start.sh", "update-dns.sh", "cloud-init.sh.tftpl", "../../docker/entrypoint.sh"),
    )
    def test_no_carriage_returns(self, name: str) -> None:
        """A CRLF shebang fails as 'no such file or directory', naming the wrong problem.

        .gitattributes pins *.sh to LF; the template is not a .sh file and this is what
        covers it. Measured the hard way in M6.2.
        """
        assert b"\r" not in (INSTANCE / name).read_bytes()

    def test_the_start_script_refuses_to_run_without_a_registry(self) -> None:
        assert "RSA_REGISTRY:?" in (INSTANCE / "rsa-start.sh").read_text(encoding="utf-8")

    def test_the_key_never_reaches_a_command_line(self) -> None:
        """A value in argv is visible to every process on the machine.

        The key is written to a file that only root can read, and the file compose reads is
        created under a umask that makes that true from the moment it exists.
        """
        text = (INSTANCE / "rsa-start.sh").read_text(encoding="utf-8")
        assert "umask 077" in text
        assert "chmod 600 /opt/rsa/.env" in text
        assert "--with-decryption" in text


class TestAVersionBringsItsOwnStack:
    """The image carries how to run it, and the start script goes and gets it.

    Without this, rolling the image back would leave the machine running the newest
    compose file against an older image, and the failure would be a service that version
    does not have rather than anything to do with the rollback.
    """

    def test_the_image_carries_the_stack_definition(self) -> None:
        text = (REPO / "Dockerfile").read_text(encoding="utf-8")
        assert "deploy/instance/compose.yaml /app/deploy/compose.yaml" in text
        assert "deploy/instance/Caddyfile /app/deploy/Caddyfile" in text

    def test_the_start_script_extracts_it_from_the_image(self) -> None:
        text = (INSTANCE / "rsa-start.sh").read_text(encoding="utf-8")
        assert "/app/deploy/compose.yaml" in text
        assert "/app/deploy/Caddyfile" in text

    def test_it_is_moved_into_place_only_once_both_files_are_there(self) -> None:
        """A half-written compose file leaves the machine unable to bring up either the
        old stack or the new one."""
        text = (INSTANCE / "rsa-start.sh").read_text(encoding="utf-8")
        assert text.index('test -s "$TMP/compose.yaml"') < text.index('mv "$TMP/compose.yaml"')
        assert text.index('test -s "$TMP/Caddyfile"') < text.index('mv "$TMP/Caddyfile"')

    def test_the_start_script_reads_the_version_rather_than_being_told_it(self) -> None:
        text = (INSTANCE / "rsa-start.sh").read_text(encoding="utf-8")
        assert "RSA_IMAGE_PARAMETER" in text
        assert "RSA_IDENTITIES_PARAMETER" in text

    def test_the_image_reaches_the_api_so_health_can_report_it(
        self, instance_compose: dict[str, Any]
    ) -> None:
        """A rollback that cannot be checked from the running thing is a rollback nobody
        can confirm happened."""
        assert "RSA_IMAGE" in instance_compose["services"]["api"]["environment"]

    def test_the_running_image_is_written_where_compose_reads_it_for_down_too(self) -> None:
        """ExecStop runs without ExecStart's environment, and `docker compose down` still
        interpolates the image."""
        text = (INSTANCE / "rsa-start.sh").read_text(encoding="utf-8")
        assert "RSA_IMAGE=%s" in text


class TestWhatTheInternetCanReach:
    """The instance publishes one thing. Everything else is on the compose network.

    /chat writes to the database on the caller's behalf. It was reachable from any address
    in allowed_cidrs before this; now it is not routable from outside at all.
    """

    def test_only_caddy_publishes_ports(self, instance_compose) -> None:
        publishing = {
            name for name, service in instance_compose["services"].items() if service.get("ports")
        }
        assert publishing == {"caddy"}

    def test_caddy_publishes_exactly_eighty_and_four_four_three(self, instance_compose) -> None:
        ports = {str(p).split(":")[0] for p in instance_compose["services"]["caddy"]["ports"]}
        assert ports == {"80", "443"}

    def test_the_certificate_outlives_the_container(self, instance_compose) -> None:
        """Reissuing on every start would meet Let's Encrypt's rate limit within a week."""
        volumes = instance_compose["services"]["caddy"]["volumes"]
        assert any(str(v).startswith("caddy_data:") for v in volumes)

    def test_the_proxy_routes_only_the_operator_routes_to_the_api(self) -> None:
        """Comments are stripped first: one of them names the routes to explain why they
        are absent, and a test that read comments would forbid saying so."""
        lines = [
            line
            for line in (INSTANCE / "Caddyfile").read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("#")
        ]
        directives = chr(10).join(lines)
        assert "reverse_proxy api:8000" in directives
        for write_route in ("/chat", "/tickets", "/approvals", "/session"):
            assert write_route not in directives, write_route

    def test_the_name_and_the_contact_have_no_defaults(self, instance_compose) -> None:
        environment = instance_compose["services"]["caddy"]["environment"]
        for key in ("RSA_DOMAIN", "RSA_ACME_EMAIL"):
            assert environment[key].startswith("${") and ":?" in environment[key]


class TestTheDnsUpdater:
    def test_it_runs_before_the_registry_login(self) -> None:
        """Let's Encrypt has to resolve this machine, not whichever had the address last."""
        start = (INSTANCE / "rsa-start.sh").read_text(encoding="utf-8")
        assert start.index("update-dns") < start.index("get-login-password")

    def test_it_uses_imdsv2(self) -> None:
        """The instance requires a token; a v1 read would simply fail."""
        text = (INSTANCE / "update-dns.sh").read_text(encoding="utf-8")
        assert "api/token" in text
        assert "X-aws-ec2-metadata-token:" in text

    def test_it_refuses_to_write_an_empty_address(self) -> None:
        text = (INSTANCE / "update-dns.sh").read_text(encoding="utf-8")
        assert 'if [ -z "$ADDRESS" ]' in text

    def test_it_waits_for_the_change(self) -> None:
        text = (INSTANCE / "update-dns.sh").read_text(encoding="utf-8")
        assert "resource-record-sets-changed" in text
