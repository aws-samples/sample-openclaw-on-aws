"""Unit tests for container/.openclaw/exec-approvals.json allowlist hardening.

OpenClaw's allowlist matcher evaluates `argPattern` as a JS RegExp against
argv[1:] joined with a single space (see openclaw's
exec-command-resolution matchArgPattern()). Python's `re` module is
close enough to JS regex semantics for these patterns (no lookbehind
differences here), so these tests exercise the same argPattern strings
using Python's re against representative argv joins, to catch regressions
without needing a Node.js harness.

These tests validate:
  - The exec-approvals.json file is valid JSON and matches OpenClaw's
    allowlist entry schema shape (id/pattern/argPattern only).
  - Each hardened entry's argPattern allows the intended safe usage and
    denies the specific RCE-primitive argv shapes called out in the fix.

Run: cd agentcore-runtime-instances && source .venv/bin/activate && \
     python3 -m pytest tests/ -v
"""
import json
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOWLIST_PATH = os.path.join(
    REPO_ROOT, "container", ".openclaw", "exec-approvals.json"
)


@pytest.fixture(scope="module")
def allowlist():
    with open(ALLOWLIST_PATH) as fh:
        data = json.load(fh)
    return data["agents"]["main"]["allowlist"]


def _entry(allowlist, pattern):
    matches = [e for e in allowlist if e["pattern"] == pattern]
    assert len(matches) == 1, f"expected exactly one entry for pattern={pattern!r}"
    return matches[0]


def _argv_matches(arg_pattern, argv_after_executable):
    """Mirror OpenClaw's matchArgPattern(): args joined with a single
    space, regex tested against that joined string."""
    args_string = " ".join(argv_after_executable)
    return re.search(arg_pattern, args_string) is not None


class TestAllowlistSchema:
    def test_file_is_valid_json(self, allowlist):
        assert isinstance(allowlist, list)

    def test_only_schema_fields_present(self, allowlist):
        allowed_keys = {
            "id", "pattern", "source", "commandText", "argPattern",
            "lastUsedAt", "lastUsedCommand", "lastResolvedPath",
        }
        for entry in allowlist:
            assert set(entry.keys()) <= allowed_keys, (
                f"entry {entry!r} has keys outside OpenClaw's "
                "ExecApprovalsAllowlistEntrySchema (additionalProperties: false)"
            )

    def test_every_entry_has_an_id_and_pattern(self, allowlist):
        for entry in allowlist:
            assert entry.get("id")
            assert entry.get("pattern")

    def test_hardened_interpreters_all_have_arg_pattern(self, allowlist):
        for pattern_name in ["git", "npm", "node", "python3", "pip", "pip3"]:
            entry = _entry(allowlist, pattern_name)
            assert entry.get("argPattern"), (
                f"{pattern_name} must have an argPattern restricting its "
                "argv shape; a bare command-name allowlist entry with no "
                "argPattern permits arbitrary arguments."
            )

    def test_low_risk_readonly_entries_remain_unrestricted(self, allowlist):
        # These are read-only/build tools with no code-eval surface; no
        # argPattern needed.
        for pattern_name in ["pytest", "cat", "ls", "grep", "find", "mkdir", "cp", "mv"]:
            entry = _entry(allowlist, pattern_name)
            assert "argPattern" not in entry


class TestGitArgPattern:
    def test_safe_subcommands_allowed(self, allowlist):
        pat = _entry(allowlist, "git")["argPattern"]
        for argv in [
            ["status"], ["log", "--oneline"], ["diff"], ["clone", "https://example.com/x.git"],
            ["commit", "-m", "msg"], ["checkout", "main"], ["push", "origin", "main"],
        ]:
            assert _argv_matches(pat, argv), f"git {argv} should be allowed"

    def test_dash_c_config_injection_denied(self, allowlist):
        pat = _entry(allowlist, "git")["argPattern"]
        assert not _argv_matches(pat, ["-c", "core.sshCommand=evil", "clone", "https://x"])

    def test_git_config_subcommand_denied(self, allowlist):
        """`git config` is deliberately excluded from the safe subcommand
        set -- it's exactly how core.sshCommand / core.hooksPath get set
        persistently for later RCE."""
        pat = _entry(allowlist, "git")["argPattern"]
        assert not _argv_matches(pat, ["config", "core.sshCommand", "evil"])

    def test_upload_pack_rce_denied(self, allowlist):
        pat = _entry(allowlist, "git")["argPattern"]
        assert not _argv_matches(
            pat, ["clone", "--upload-pack=touch /tmp/pwned", "https://example.com/x.git"]
        )

    def test_receive_pack_rce_denied(self, allowlist):
        pat = _entry(allowlist, "git")["argPattern"]
        assert not _argv_matches(
            pat, ["push", "--receive-pack=touch /tmp/pwned", "origin", "main"]
        )

    def test_exec_flag_denied(self, allowlist):
        pat = _entry(allowlist, "git")["argPattern"]
        assert not _argv_matches(pat, ["--exec=evil", "log"])

    def test_unrecognized_subcommand_denied(self, allowlist):
        """Only the declared safe subcommand set matches; anything else
        (submodule, filter-branch, gc, apply, am, format-patch, etc.) is an
        allowlist miss."""
        pat = _entry(allowlist, "git")["argPattern"]
        assert not _argv_matches(pat, ["submodule", "update", "--init"])


class TestNpmArgPattern:
    def test_install_ci_test_allowed(self, allowlist):
        pat = _entry(allowlist, "npm")["argPattern"]
        for argv in [["install"], ["install", "left-pad"], ["ci"], ["test"]]:
            assert _argv_matches(pat, argv), f"npm {argv} should be allowed"

    def test_exec_denied(self, allowlist):
        pat = _entry(allowlist, "npm")["argPattern"]
        assert not _argv_matches(pat, ["exec", "evil-package"])

    def test_arbitrary_run_script_denied(self, allowlist):
        pat = _entry(allowlist, "npm")["argPattern"]
        assert not _argv_matches(pat, ["run", "postinstall"])


class TestNodeArgPattern:
    def test_running_a_script_file_allowed(self, allowlist):
        pat = _entry(allowlist, "node")["argPattern"]
        assert _argv_matches(pat, ["script.js"])
        assert _argv_matches(pat, ["/app/tool.js", "--flag"])

    def test_inline_eval_e_denied(self, allowlist):
        pat = _entry(allowlist, "node")["argPattern"]
        assert not _argv_matches(pat, ["-e", "require('child_process').exec('evil')"])

    def test_inline_eval_eval_denied(self, allowlist):
        pat = _entry(allowlist, "node")["argPattern"]
        assert not _argv_matches(pat, ["--eval", "evil()"])

    def test_inline_print_p_denied(self, allowlist):
        pat = _entry(allowlist, "node")["argPattern"]
        assert not _argv_matches(pat, ["-p", "1+1"])


class TestPython3ArgPattern:
    def test_running_a_script_file_allowed(self, allowlist):
        pat = _entry(allowlist, "python3")["argPattern"]
        assert _argv_matches(pat, ["script.py"])

    def test_module_invocation_allowed(self, allowlist):
        pat = _entry(allowlist, "python3")["argPattern"]
        assert _argv_matches(pat, ["-m", "pytest", "tests/"])

    def test_inline_eval_c_denied(self, allowlist):
        pat = _entry(allowlist, "python3")["argPattern"]
        assert not _argv_matches(pat, ["-c", "import os; os.system('evil')"])

    def test_inline_eval_command_denied(self, allowlist):
        pat = _entry(allowlist, "python3")["argPattern"]
        assert not _argv_matches(pat, ["--command", "evil"])


class TestPipArgPattern:
    def test_install_and_related_subcommands_allowed(self, allowlist):
        for pattern_name in ["pip", "pip3"]:
            pat = _entry(allowlist, pattern_name)["argPattern"]
            for argv in [["install", "requests"], ["uninstall", "requests"],
                         ["list"], ["show", "requests"], ["freeze"], ["download", "requests"]]:
                assert _argv_matches(pat, argv), f"{pattern_name} {argv} should be allowed"

    def test_unrecognized_subcommand_denied(self, allowlist):
        for pattern_name in ["pip", "pip3"]:
            pat = _entry(allowlist, pattern_name)["argPattern"]
            assert not _argv_matches(pat, ["config", "set", "global.index-url", "http://evil"])


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
