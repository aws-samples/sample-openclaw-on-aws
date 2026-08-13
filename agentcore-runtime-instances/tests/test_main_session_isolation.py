"""Unit tests for container/main.py security fixes.

Covers:
  - Session-id sanitization (_sanitize_session_id): allowlist regex enforced
    before an AgentCore session id is used to build an S3 key prefix.
    Confirms path traversal, absolute paths, embedded nulls, and
    overlong inputs are rejected (fall back to a fixed safe segment)
    rather than being silently stripped and used.

Run: cd agentcore-runtime-instances && source .venv/bin/activate && \
     python3 -m pytest tests/ -v
"""
import importlib
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTAINER_DIR = os.path.join(REPO_ROOT, "container")
sys.path.insert(0, CONTAINER_DIR)


@pytest.fixture()
def main_module():
    """Import container/main.py without running its module-level side
    effects against a real gateway (there's no gateway in the test env).

    main.py's only unconditional module-level side effects are: creating a
    BedrockAgentCoreApp() instance, installing SIGTERM/SIGINT handlers, and
    defining functions/constants. It does not start subprocesses or hit the
    network at import time (that only happens inside the request handler /
    _ensure_session_workspace), so a plain import is safe here.
    """
    if "main" in sys.modules:
        del sys.modules["main"]
    module = importlib.import_module("main")
    yield module
    if "main" in sys.modules:
        del sys.modules["main"]


class TestSanitizeSessionId:
    def test_valid_alphanumeric_session_id_is_preserved(self, main_module):
        assert main_module._sanitize_session_id("abc123") == "abc123"

    def test_valid_with_hyphen_and_underscore_is_preserved(self, main_module):
        sid = "session-id_ABC-123"
        assert main_module._sanitize_session_id(sid) == sid

    def test_valid_uuid_like_session_id_is_preserved(self, main_module):
        sid = "e7f1a2b3-4c5d-6e7f-8091-a2b3c4d5e6f7"
        assert main_module._sanitize_session_id(sid) == sid

    def test_valid_128_char_session_id_is_preserved(self, main_module):
        sid = "a" * 128
        assert main_module._sanitize_session_id(sid) == sid

    def test_none_falls_back_to_safe_default(self, main_module):
        assert main_module._sanitize_session_id(None) == main_module._FALLBACK_SESSION_PREFIX

    def test_empty_string_falls_back_to_safe_default(self, main_module):
        assert main_module._sanitize_session_id("") == main_module._FALLBACK_SESSION_PREFIX

    def test_path_traversal_falls_back_to_safe_default(self, main_module):
        assert main_module._sanitize_session_id("../../etc") == main_module._FALLBACK_SESSION_PREFIX

    def test_absolute_path_falls_back_to_safe_default(self, main_module):
        assert main_module._sanitize_session_id("/etc/passwd") == main_module._FALLBACK_SESSION_PREFIX

    def test_embedded_null_byte_falls_back_to_safe_default(self, main_module):
        assert main_module._sanitize_session_id("abc\x00def") == main_module._FALLBACK_SESSION_PREFIX

    def test_overlong_input_falls_back_to_safe_default(self, main_module):
        assert main_module._sanitize_session_id("a" * 129) == main_module._FALLBACK_SESSION_PREFIX

    def test_forward_slash_falls_back_to_safe_default(self, main_module):
        assert main_module._sanitize_session_id("foo/bar") == main_module._FALLBACK_SESSION_PREFIX

    def test_dot_dot_without_slash_still_falls_back(self, main_module):
        # ".." alone contains only allowlisted chars would be wrong -- "."
        # is NOT in the allowlist, so this must fall back regardless.
        assert main_module._sanitize_session_id("..") == main_module._FALLBACK_SESSION_PREFIX

    def test_shell_metacharacters_fall_back_to_safe_default(self, main_module):
        for sid in ["abc;rm -rf /", "abc$(whoami)", "abc`id`", "abc|cat"]:
            assert main_module._sanitize_session_id(sid) == main_module._FALLBACK_SESSION_PREFIX

    def test_non_string_input_falls_back_to_safe_default(self, main_module):
        assert main_module._sanitize_session_id(12345) == main_module._FALLBACK_SESSION_PREFIX

    def test_fallback_value_itself_is_allowlist_safe(self, main_module):
        """The fallback constant must itself pass the allowlist regex --
        otherwise a bug in the fallback could reintroduce the same class of
        S3-prefix issue it's meant to prevent."""
        assert main_module._SESSION_ID_RE.match(main_module._FALLBACK_SESSION_PREFIX)


class TestSessionPrefixIsolation:
    def test_different_valid_session_ids_produce_different_prefixes(self, main_module):
        sid_a = main_module._sanitize_session_id("tenant-a-session")
        sid_b = main_module._sanitize_session_id("tenant-b-session")
        prefix_a = f"sessions/{sid_a}"
        prefix_b = f"sessions/{sid_b}"
        assert prefix_a != prefix_b

    def test_malicious_and_missing_session_ids_collapse_to_same_safe_fallback(self, main_module):
        """Different malicious inputs must not each carve out their own
        (still attacker-influenced) S3 path -- they all collapse onto the
        one fixed fallback segment."""
        assert (
            main_module._sanitize_session_id("../../etc")
            == main_module._sanitize_session_id("/etc/passwd")
            == main_module._sanitize_session_id(None)
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
