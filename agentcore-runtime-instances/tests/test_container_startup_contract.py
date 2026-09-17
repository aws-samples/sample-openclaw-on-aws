"""Unit tests for the container startup contract on AgentCore Runtime Instances.

Regression guards for two host behaviours that make an otherwise-working image
fail with nothing but "Container exited before becoming ready":

  1. The host wraps the image entrypoint as
     `/bin/sh -c "exec '<entrypoint>' >/tmp/.agent_stdout 2>/tmp/.agent_stderr"`
     and does NOT honour the image WORKDIR, so a relative entrypoint such as
     `./start.sh` resolves against `/` and fails with "not found".

  2. The host starts the workload on an id-mapped overlay mount, where the
     container's uid 0 holds no DAC override over files owned by another uid.
     A root-time `mkdir`/`chown` of an `agent`-owned OPENCLAW_HOME therefore
     fails with EPERM, and under `set -e` the entrypoint dies in well under a
     second -- before the host's log drainer attaches, which is why no
     application output reaches CloudWatch.

  3. Legacy (version 1) exec approvals must be migrated before the gateway
     starts, because `openclaw doctor` cannot take the gateway-lifecycle lock
     once the gateway owns it.

Run: cd agentcore-runtime-instances && source .venv/bin/activate && \
     python3 -m pytest tests/ -v
"""
import json
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERFILE_PATH = os.path.join(REPO_ROOT, "container", "Dockerfile")
START_SH_PATH = os.path.join(REPO_ROOT, "container", "start.sh")
MAIN_PY_PATH = os.path.join(REPO_ROOT, "container", "main.py")
APPROVALS_PATH = os.path.join(
    REPO_ROOT, "container", ".openclaw", "exec-approvals.json"
)


@pytest.fixture(scope="module")
def dockerfile():
    with open(DOCKERFILE_PATH) as fh:
        return fh.read()


@pytest.fixture(scope="module")
def start_sh():
    with open(START_SH_PATH) as fh:
        return fh.read()


@pytest.fixture(scope="module")
def main_py():
    with open(MAIN_PY_PATH) as fh:
        return fh.read()


class TestEntrypointIsAbsolute:
    def test_entrypoint_uses_an_absolute_path(self, dockerfile):
        entrypoints = re.findall(r"^ENTRYPOINT\s+(.+)$", dockerfile, re.MULTILINE)
        assert entrypoints, "Dockerfile declares no ENTRYPOINT"
        for raw in entrypoints:
            argv = json.loads(raw)
            assert argv[0].startswith("/"), (
                f"ENTRYPOINT {argv[0]!r} is relative. The Runtime Instances host "
                "ignores WORKDIR when wrapping the entrypoint, so a relative path "
                "fails with 'not found' and the container exits immediately."
            )

    def test_entrypoint_target_matches_a_copied_path(self, dockerfile):
        argv = json.loads(re.findall(r"^ENTRYPOINT\s+(.+)$", dockerfile, re.MULTILINE)[-1])
        assert argv[0] == "/app/start.sh", (
            "start.sh is copied into the /app WORKDIR; the absolute ENTRYPOINT "
            "must point at that same location."
        )


class TestContainerRunsAsNonRoot:
    def test_dockerfile_declares_a_non_root_user(self, dockerfile):
        users = re.findall(r"^USER\s+(\S+)$", dockerfile, re.MULTILINE)
        assert users, (
            "Dockerfile declares no USER. Starting as root and dropping "
            "privileges later does not work on Runtime Instances: the id-mapped "
            "mount denies uid 0 any override over agent-owned paths."
        )
        assert users[-1] not in ("root", "0"), f"container would run as {users[-1]!r}"

    def test_home_is_owned_by_the_runtime_user_at_build_time(self, dockerfile):
        assert re.search(r"chown\s+-R\s+agent:agent\s+/home/agent", dockerfile), (
            "OPENCLAW_HOME must be chowned to `agent` at build time, because the "
            "entrypoint can no longer do it at runtime."
        )

    def test_entrypoint_does_not_unconditionally_chown_openclaw_home(self, start_sh):
        """A bare `chown` at the top level dies with EPERM under an id-mapped
        mount. It is only acceptable inside an `id -u` = 0 guard, which exists
        for local `docker run --user 0` use."""
        for line in start_sh.splitlines():
            stripped = line.strip()
            if stripped.startswith("chown ") and "OPENCLAW_HOME" in stripped:
                indented = line.startswith((" ", "\t"))
                assert indented, (
                    "chown of OPENCLAW_HOME must be guarded by a root check, "
                    f"not run unconditionally: {stripped!r}"
                )

    def test_entrypoint_reports_the_failing_command(self, start_sh):
        assert "trap _start_failed ERR" in start_sh, (
            "Without an ERR trap the host reports only 'Container exited before "
            "becoming ready' and the real failure never reaches CloudWatch."
        )


class TestExecApprovalsMigration:
    def test_shipped_approvals_file_is_the_legacy_schema(self):
        """Documents why the migration hook is needed. If this file is ever
        reshaped to the current schema, the hook becomes a no-op rather than
        wrong -- but this assertion should then be updated deliberately."""
        with open(APPROVALS_PATH) as fh:
            assert json.load(fh).get("version") == 1

    def test_migration_runs_before_the_gateway_starts(self, main_py):
        migrate = main_py.index("_migrate_legacy_exec_approvals()\n")
        start = main_py.index("_start_gateway()\n", migrate)
        assert migrate < start, (
            "`openclaw doctor` needs the gateway-lifecycle lock; once the "
            "gateway holds it the migration aborts with "
            "StateDatabaseCoordinatorContentionError."
        )

    def test_migration_is_non_interactive(self, main_py):
        assert '"--non-interactive"' in main_py and '"--repair"' in main_py, (
            "doctor must not prompt: there is no TTY in the container."
        )
