"""The files that are copied onto the demo instance.

None of this runs here, which is exactly why it is worth a test. A mistake in these files
shows up as an instance that boots and does nothing, ten minutes and one `terraform apply`
after it was made. The checks are the ones that catch that class of mistake without an
instance: the two compose files describing the same stack, the cloud-init template naming
every file it is given, and shell scripts that a Linux shell can actually run.
"""

from __future__ import annotations

import base64
import gzip
import re
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
INSTANCE = REPO / "deploy" / "instance"
TEMPLATE = INSTANCE / "cloud-init.sh.tftpl"
DEMO_TF = REPO / "deploy" / "terraform" / "demo" / "main.tf"

SERVICES = ("db", "migrate", "seed", "api", "ui", "mcp")
# Caddy runs only on the instance: development reaches the UI on the loopback address and
# has nothing to get a certificate for.
INSTANCE_ONLY = ("caddy",)


def number(pattern: str, text: str) -> int:
    """The one number a pattern names, or a failure that says the pattern found nothing."""
    found = re.search(pattern, text)
    assert found is not None, pattern
    return int(found.group(1))


def load(path: Path) -> dict[str, Any]:
    content = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(content, dict), f"{path} must contain a YAML mapping"
    assert all(isinstance(key, str) for key in content), f"{path} must use string keys"
    return content


@pytest.fixture(scope="module")
def local_compose() -> dict[str, Any]:
    return load(REPO / "compose.yaml")


@pytest.fixture(scope="module")
def instance_compose() -> dict[str, Any]:
    return load(INSTANCE / "compose.yaml")


def names_given(compose: dict[str, Any], service: str) -> set[str]:
    """The environment variable names one service is given.

    Compose accepts two shapes and this project uses both: a mapping where the value is
    written here, and a list, whose bare entries are passed through from the environment
    only when it sets them. Reading names out of either keeps "both files give the same
    variables" a statement about the variables rather than about the YAML.
    """
    given = compose["services"][service]["environment"]
    if isinstance(given, dict):
        return set(given)
    return {str(entry).split("=", 1)[0] for entry in given}


def value_given(compose: dict[str, Any], service: str, name: str) -> str | None:
    """What one service is given for `name`, or None when it is passed through.

    A bare list entry has no value here by design: compose takes it from the environment
    and leaves it out when nothing sets it.
    """
    given = compose["services"][service]["environment"]
    if isinstance(given, dict):
        return None if given.get(name) is None else str(given[name])
    for entry in given:
        head, sep, tail = str(entry).partition("=")
        if head == name:
            return tail if sep else None
    raise KeyError(name)


class TestTheTwoComposeFiles:
    """Development builds the image; the instance pulls it. Everything else must match.

    They are separate files because the differences are real, and separate files drift.
    """

    def test_they_define_the_same_services(
        self, local_compose: dict[str, Any], instance_compose: dict[str, Any]
    ) -> None:
        assert set(local_compose["services"]) == set(SERVICES)
        assert set(instance_compose["services"]) == set(SERVICES) | set(INSTANCE_ONLY)

    @pytest.mark.parametrize("service", SERVICES)
    def test_each_shared_service_runs_the_same_command(
        self, service: str, local_compose: dict[str, Any], instance_compose: dict[str, Any]
    ) -> None:
        assert local_compose["services"][service].get("command") == instance_compose["services"][
            service
        ].get("command")

    @pytest.mark.parametrize("service", ("migrate", "seed", "api", "ui", "mcp"))
    def test_each_service_is_given_the_same_settings(
        self, service: str, local_compose: dict[str, Any], instance_compose: dict[str, Any]
    ) -> None:
        """The names, not the values: the database host is the same, the key is not."""
        assert names_given(local_compose, service) == names_given(instance_compose, service)

    @pytest.mark.parametrize("service", ("db", "api", "ui", "mcp"))
    def test_each_long_running_service_has_a_health_check(
        self, service: str, instance_compose: dict[str, Any]
    ) -> None:
        """`docker compose up --wait` returns when health checks pass.

        Without one, "the container started" is what the boot waits for, and the instance
        reports itself ready before the application answers.
        """
        assert "healthcheck" in instance_compose["services"][service]

    def test_migrations_run_once_and_are_not_restarted(
        self, instance_compose: dict[str, Any]
    ) -> None:
        migrate = instance_compose["services"]["migrate"]
        assert migrate["restart"] == "no"
        for dependent in ("api", "ui", "mcp"):
            condition = instance_compose["services"][dependent]["depends_on"]["migrate"]
            assert condition["condition"] == "service_completed_successfully"

    def test_the_instance_file_names_no_key(self, instance_compose: dict[str, Any]) -> None:
        """Every value must be a substitution, never a literal.

        The key reaches the instance from Parameter Store at boot. A default here would put
        it in the repository, and a placeholder that looks like a key is worse than none.
        """
        for service in ("migrate", "seed", "api", "ui"):
            value = value_given(instance_compose, service, "DASHSCOPE_API_KEY")
            assert value is not None
            assert value.startswith("${") and value.endswith("}")

    def test_the_instance_file_requires_an_image_rather_than_defaulting(
        self, instance_compose: dict[str, Any]
    ) -> None:
        for service in ("migrate", "seed", "api", "ui", "mcp"):
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

    def test_every_placeholder_is_supplied(
        self, placeholders: set[str], supplied: set[str]
    ) -> None:
        assert placeholders - supplied == set()

    def test_nothing_is_supplied_that_the_template_ignores(
        self, placeholders: set[str], supplied: set[str]
    ) -> None:
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


class TestASettingCanBeSetWhereTheServiceRuns:
    """ "Configurable" has to mean configurable where it runs.

    Every one of these reads an environment variable, and the compose files name every
    variable the container is given - so a setting missing from them could be set on a
    developer's machine and nowhere else. `RSA_APPROVAL_TTL_SECONDS` was documented in
    `config.py` as required to be configurable, and on the instance it was not: the
    architecture document's wording and the deployment disagreed, and the deployment won.

    The test is written against `Settings` rather than a list of names, so a setting added
    later is either passed through or deliberately excluded, and cannot quietly become a
    third thing.
    """

    # Read by the evaluation loop and the benchmark harness, which run from a checkout
    # and never inside this stack.
    EVALUATION_ONLY: ClassVar[set[str]] = {
        "RSA_JUDGE_BASE_URL",
        "RSA_JUDGE_API_KEY",
        "RSA_JUDGE_MODEL",
        "RSA_AGENT_BASE_URL",
        "RSA_AGENT_API_KEY",
        "RSA_USER_BASE_URL",
        "RSA_USER_API_KEY",
        "RSA_USER_MODEL",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_HOST",
    }

    @staticmethod
    def aliases() -> set[str]:
        from retail_support_agent.config import Settings

        found = set()
        for field in Settings.model_fields.values():
            alias = field.validation_alias
            if isinstance(alias, str):
                found.add(alias)
        return found

    @pytest.mark.parametrize("which", ("local", "instance"))
    def test_every_setting_the_service_reads_can_be_given_to_it(
        self, which: str, local_compose: dict[str, Any], instance_compose: dict[str, Any]
    ) -> None:
        compose = local_compose if which == "local" else instance_compose
        given = names_given(compose, "api")
        missing = self.aliases() - given - self.EVALUATION_ONLY
        assert missing == set(), f"the API cannot be given: {sorted(missing)}"

    def test_the_pass_through_entries_carry_no_value(self, local_compose: dict[str, Any]) -> None:
        """A value here would be a second copy of the application's default, and the two
        would drift. Bare, compose omits the variable entirely when nothing sets it."""
        entries = local_compose["services"]["api"]["environment"]
        bare = {entry for entry in entries if "=" not in str(entry)}
        assert "RSA_APPROVAL_TTL_SECONDS" in bare
        assert "RSA_SESSION_CACHE_SIZE" in bare

    def test_the_layer_refuses_to_have_its_own_values_overridden(self) -> None:
        """The demo layer derives the database URL, the image and the pool. A setting
        passed in that named one of those would disagree with what the layer wrote, and
        which won would depend on the order compose read them in."""
        variables = (REPO / "deploy" / "terraform" / "demo" / "variables.tf").read_text(
            encoding="utf-8"
        )
        block = variables[variables.index('variable "settings"') :]
        for derived in ("RSA_IMAGE", "RSA_AUTH_ISSUER", "RSA_DATABASE_URL", "RSA_IDENTITIES"):
            assert derived in block

    def test_the_settings_reach_the_file_the_units_read(self) -> None:
        """systemd gives `rsa-start` this file's contents, and compose resolves a bare
        entry from there. A value written anywhere else would never reach the container."""
        template = TEMPLATE.read_text(encoding="utf-8")
        start = template.index("cat > /etc/rsa-demo.env")
        first_line_end = template.index(chr(10), start)
        block = template[start : template.index("ENVFILE", first_line_end)]
        assert "${settings}" in block


class TestTheShellScripts:
    @pytest.mark.parametrize(
        "name",
        (
            "rsa-start.sh",
            "update-dns.sh",
            "archive.sh",
            "restore.sh",
            "rsa-stop.sh",
            "cloud-init.sh.tftpl",
            "../../docker/entrypoint.sh",
        ),
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
        assert "RSA_IMAGE" in names_given(instance_compose, "api")

    def test_the_running_image_is_written_where_compose_reads_it_for_down_too(self) -> None:
        """ExecStop runs without ExecStart's environment, and `docker compose down` still
        interpolates the image."""
        text = (INSTANCE / "rsa-start.sh").read_text(encoding="utf-8")
        assert "RSA_IMAGE=%s" in text


class TestUserDataStillFits:
    """EC2 caps user data at 16 KB, and nine base64 blobs now go into it.

    This was roomy until M6.9 added three scripts. Past the limit, the failure is an apply
    that refuses with a size nothing in the repository mentions, hours after the file that
    caused it was written.
    """

    LIMIT = 16384

    def rendered(self) -> bytes:
        """The template with every file blob substituted, as Terraform would render it."""
        text = TEMPLATE.read_text(encoding="utf-8")
        for name, path in re.findall(
            r'(\w+)\s*=\s*filebase64\("\$\{path\.module\}/\.\./\.\./instance/([^"]+)"\)',
            DEMO_TF.read_text(encoding="utf-8"),
        ):
            blob = base64.b64encode((INSTANCE / path).read_bytes()).decode("ascii")
            text = text.replace(f"${{{name}}}", blob)
        # Everything else is a short value; a placeholder of its own length is close
        # enough for a limit this far from the measurement.
        return re.sub(r"\$\{\w+\}", "placeholder-value-x", text).encode("utf-8")

    def test_it_fits_inside_the_limit(self) -> None:
        assert len(gzip.compress(self.rendered())) < self.LIMIT

    def test_there_is_still_room_for_another_script(self) -> None:
        """Failing at the limit itself would fail on the change that has no room left,
        which is a change too late to be useful."""
        assert len(gzip.compress(self.rendered())) < self.LIMIT * 0.9


class TestArchivingOnTheWayDown:
    """The database outlives the machine, or the audit trail is not a record (M6.9)."""

    def test_the_unit_archives_before_it_stops_the_stack(self) -> None:
        """`docker compose down` is what stops PostgreSQL, so the dump has to come first
        and both have to be one command: systemd stops at the first ExecStop that fails."""
        unit = (INSTANCE / "rsa.service").read_text(encoding="utf-8")
        assert "ExecStop=/usr/local/bin/rsa-stop" in unit
        assert "docker compose" not in unit

        stop = "\n".join(
            line
            for line in (INSTANCE / "rsa-stop.sh").read_text(encoding="utf-8").splitlines()
            if not line.startswith("#")
        )
        assert stop.index("rsa-archive") < stop.index("docker compose")

    def test_a_failed_archive_does_not_block_the_stop(self) -> None:
        """A machine that will not shut down bills by the hour, which is the failure M6.1's
        whole shape exists to avoid. A missed archive costs one idle period."""
        stop = (INSTANCE / "rsa-stop.sh").read_text(encoding="utf-8")
        directives = [line for line in stop.splitlines() if line.startswith("set ")]
        assert directives == ["set -u"]
        assert "if ! timeout" in stop

    def test_the_stop_is_bounded_and_the_unit_waits_that_long(self) -> None:
        unit = (INSTANCE / "rsa.service").read_text(encoding="utf-8")
        stop = (INSTANCE / "rsa-stop.sh").read_text(encoding="utf-8")
        timeout = number(r"TimeoutStopSec=(\d+)", unit)
        archive = number(r"RSA_ARCHIVE_TIMEOUT_SECONDS:-(\d+)", stop)
        assert archive < timeout
        # EC2 allows about four minutes of graceful shutdown before it pulls the power.
        assert timeout <= 240

    def test_the_dump_is_written_whole_before_it_is_uploaded(self) -> None:
        """Streaming into S3 uploads a truncated dump as a complete object when the dump
        fails half way, and a truncated archive that lists fine is the failure this whole
        step exists to prevent."""
        text = (INSTANCE / "archive.sh").read_text(encoding="utf-8")
        assert "set -euo pipefail" in text
        assert text.index('test -s "$TMP/dump.sql.gz"') < text.index("aws s3 cp")

    def test_the_archive_goes_where_the_role_can_write(self) -> None:
        """M6.4 scoped the instance role to one prefix, so the demo cannot reach the
        Terraform state sitting in the same bucket."""
        text = (INSTANCE / "archive.sh").read_text(encoding="utf-8")
        assert 'PREFIX="${RSA_ARCHIVE_PREFIX:-archives}"' in text
        assert "RSA_ARCHIVE_BUCKET:?" in text
        assert "RSA_ARCHIVE_BUCKET=${archive_bucket}" in TEMPLATE.read_text(encoding="utf-8")

    @pytest.mark.parametrize("name", ("archive.sh", "restore.sh"))
    def test_each_script_brings_its_own_environment(self, name: str) -> None:
        """Two callers, one with the unit's environment and one without.

        ExecStop inherits `EnvironmentFile`; a Systems Manager session, which is how
        `demo.py archive` and `demo.py restore` reach the machine, inherits nothing. Found
        by running it: `docker compose` refused to interpolate the stack definition and
        failed on a variable the unit would have supplied.
        """
        text = (INSTANCE / name).read_text(encoding="utf-8")
        assert ". /etc/rsa-demo.env" in text
        assert "set -a" in text

    def test_the_whole_database_is_dumped(self) -> None:
        """Not a subset: choosing which tables matter is a decision that has to stay right
        forever, and the whole thing is a few hundred kilobytes."""
        text = (INSTANCE / "archive.sh").read_text(encoding="utf-8")
        assert "--table" not in text
        assert "--exclude-table" not in text
        assert "--clean" in text and "--if-exists" in text


class TestRestoringIt:
    def test_it_restores_in_one_transaction(self) -> None:
        """The outcome is the archive or the database that was already there. A
        half-applied restore leaves a schema that matches neither."""
        text = (INSTANCE / "restore.sh").read_text(encoding="utf-8")
        assert "--single-transaction" in text
        assert "ON_ERROR_STOP=1" in text

    def test_nothing_writes_while_the_database_is_replaced(self) -> None:
        text = (INSTANCE / "restore.sh").read_text(encoding="utf-8")
        assert text.index("stop api ui") < text.index("gunzip -c")

    def test_the_writers_come_back_whatever_happens(self) -> None:
        """A restore that fails and leaves the demo with no API is worse than the state it
        was trying to fix."""
        text = (INSTANCE / "restore.sh").read_text(encoding="utf-8")
        assert "trap finish EXIT" in text
        assert "start api ui" in text

    def test_a_corrupt_download_is_refused_before_it_is_applied(self) -> None:
        text = (INSTANCE / "restore.sh").read_text(encoding="utf-8")
        assert text.index("gzip -t") < text.index("stop api ui")

    def test_it_refuses_when_there_is_nothing_to_restore(self) -> None:
        text = (INSTANCE / "restore.sh").read_text(encoding="utf-8")
        assert '[ "$KEY" = "None" ]' in text


class TestWhatTheInternetCanReach:
    """The instance publishes one thing. Everything else is on the compose network.

    /chat writes to the database on the caller's behalf. It was reachable from any address
    in allowed_cidrs before this; now it is not routable from outside at all.
    """

    def test_only_caddy_publishes_ports_beyond_the_machine(
        self, instance_compose: dict[str, Any]
    ) -> None:
        """Anything else may publish only on the loopback address. A mapping without an
        address binds every interface, so it counts as public here."""
        public = {
            name
            for name, service in instance_compose["services"].items()
            for port in service.get("ports", [])
            if not str(port).startswith("127.0.0.1:")
        }
        assert public == {"caddy"}

    def test_only_the_watchers_route_is_on_the_loopback_address(
        self, instance_compose: dict[str, Any]
    ) -> None:
        """The idle watcher runs on the host and asks 127.0.0.1:8000/idle. Since M6.6 the
        API published nothing, so the watcher got no answer and the instance stopped only
        at the uptime cap. Exactly that one port comes back, and nothing more."""
        loopback = {
            (name, str(port))
            for name, service in instance_compose["services"].items()
            for port in service.get("ports", [])
            if str(port).startswith("127.0.0.1:")
        }
        assert loopback == {("api", "127.0.0.1:8000:8000")}

    def test_the_watcher_asks_where_the_api_is_published(
        self, instance_compose: dict[str, Any]
    ) -> None:
        """Read, not imported: what matters is the default the unit runs with, since the
        instance's /etc/rsa-idle.env sets no URL."""
        watcher = (INSTANCE / "idle_shutdown.py").read_text(encoding="utf-8")
        assert 'DEFAULT_URL = "http://127.0.0.1:8000/idle"' in watcher
        assert "RSA_IDLE_URL" not in (INSTANCE / "cloud-init.sh.tftpl").read_text(encoding="utf-8")
        assert "127.0.0.1:8000:8000" in instance_compose["services"]["api"]["ports"]

    def test_caddy_publishes_exactly_eighty_and_four_four_three(
        self, instance_compose: dict[str, Any]
    ) -> None:
        ports = {str(p).split(":")[0] for p in instance_compose["services"]["caddy"]["ports"]}
        assert ports == {"80", "443"}

    def test_the_certificate_outlives_the_container(self, instance_compose: dict[str, Any]) -> None:
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

    def test_the_name_and_the_contact_have_no_defaults(
        self, instance_compose: dict[str, Any]
    ) -> None:
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


class TestTheMcpServer:
    """M10: the MCP server in both stacks, behind the proxy on the instance."""

    @staticmethod
    def caddy_directives() -> str:
        lines = (INSTANCE / "Caddyfile").read_text(encoding="utf-8").splitlines()
        return chr(10).join(line for line in lines if not line.strip().startswith("#"))

    def test_the_image_has_the_role_the_service_runs(
        self, local_compose: dict[str, Any], instance_compose: dict[str, Any]
    ) -> None:
        entrypoint = (REPO / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
        assert "  mcp)" in entrypoint
        assert "python -m retail_support_agent.mcp_server" in entrypoint
        for compose in (local_compose, instance_compose):
            assert compose["services"]["mcp"]["command"] == ["mcp"]

    def test_it_is_given_no_model_key(
        self, local_compose: dict[str, Any], instance_compose: dict[str, Any]
    ) -> None:
        """It calls no model. A key it does not need is a key it can leak."""
        for compose in (local_compose, instance_compose):
            assert "DASHSCOPE_API_KEY" not in names_given(compose, "mcp")

    def test_on_the_instance_it_answers_only_to_the_demos_name(
        self, instance_compose: dict[str, Any]
    ) -> None:
        assert value_given(instance_compose, "mcp", "RSA_MCP_ALLOWED_HOSTS") == (
            "${RSA_DOMAIN:?RSA_DOMAIN is required}"
        )
        assert value_given(instance_compose, "mcp", "RSA_MCP_URL") == (
            "https://${RSA_DOMAIN:?RSA_DOMAIN is required}/mcp"
        )

    def test_on_the_instance_it_requires_the_user_pool(
        self, instance_compose: dict[str, Any]
    ) -> None:
        for name in ("RSA_AUTH_ISSUER", "RSA_AUTH_AUDIENCE"):
            value = value_given(instance_compose, "mcp", name)
            assert value is not None and value.startswith(f"${{{name}:?")

    def test_it_reads_the_same_scope_settings_as_the_api(
        self, instance_compose: dict[str, Any]
    ) -> None:
        shared = {
            "RSA_SESSION_CACHE_SIZE",
            "RSA_SCOPE_REDACT_FIELDS",
            "RSA_SCOPE_WRITE_STATUSES",
            "RSA_AUTH_JWKS_URL",
            "RSA_AUTH_TOKEN_USE",
        }
        assert shared <= names_given(instance_compose, "mcp")
        assert shared <= names_given(instance_compose, "api")

    def test_locally_it_starts_only_when_asked(self, local_compose: dict[str, Any]) -> None:
        """A developer machine has no user pool, and the server refuses to start without
        one; in the default profile it would fail every `compose up`."""
        assert local_compose["services"]["mcp"]["profiles"] == ["mcp"]

    def test_the_proxy_routes_mcp_and_its_metadata(self) -> None:
        directives = self.caddy_directives()
        for path in ("/mcp", "/.well-known/oauth-protected-resource/mcp"):
            assert f"handle {path} {{\n\t\treverse_proxy mcp:8765" in directives

    def test_the_proxy_does_not_route_its_operator_routes(self) -> None:
        """/health and /idle on the MCP server answer without a token."""
        directives = self.caddy_directives()
        assert directives.count("reverse_proxy mcp:8765") == 2
        for path in ("/health", "/idle"):
            block = directives[directives.index(f"handle {path} {{") :]
            assert block.split("}", 1)[0].strip().endswith("reverse_proxy api:8000")
