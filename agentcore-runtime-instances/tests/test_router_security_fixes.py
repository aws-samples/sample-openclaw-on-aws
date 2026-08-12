"""Unit tests for lambda/router security fixes.

Exercises the actual changed logic paths: signature verification
correctness, allowlist enforcement (fail-closed + whitespace trimming),
session-id determinism/collision-safety, and rate-limit behavior.

Run: cd agentcore-runtime-instances && source .venv/bin/activate && \
     python3 -m pytest tests/ -v
"""
import hashlib
import importlib
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAMBDA_DIR = os.path.join(REPO_ROOT, "lambda", "router")
sys.path.insert(0, LAMBDA_DIR)


def _reload_with_env(module_name, env):
    """Reload a module with a specific os.environ so its module-level
    constants (read once at import time) reflect the test's env vars."""
    if module_name in sys.modules:
        del sys.modules[module_name]
    with patch.dict(os.environ, env, clear=False):
        return importlib.import_module(module_name)


# ---------------------------------------------------------------------------
# Fix #2 + #5: Telegram allowlist -- fail closed, whitespace trimming
# ---------------------------------------------------------------------------

class TestTelegramValidation:
    def test_validate_webhook_fails_closed_when_secret_unset(self):
        tg = _reload_with_env("adapters.telegram", {"WEBHOOK_SECRET_TOKEN": ""})
        assert tg.validate_webhook({"headers": {}}) is False

    def test_validate_webhook_fails_closed_on_wrong_secret(self):
        tg = _reload_with_env("adapters.telegram", {"WEBHOOK_SECRET_TOKEN": "correct"})
        event = {"headers": {"x-telegram-bot-api-secret-token": "wrong"}}
        assert tg.validate_webhook(event) is False

    def test_validate_webhook_passes_with_correct_secret(self):
        tg = _reload_with_env("adapters.telegram", {"WEBHOOK_SECRET_TOKEN": "correct"})
        event = {"headers": {"x-telegram-bot-api-secret-token": "correct"}}
        assert tg.validate_webhook(event) is True

    def test_allowed_user_ids_trims_whitespace(self):
        tg = _reload_with_env(
            "adapters.telegram", {"ALLOWED_USER_IDS": "111, 222 ,333"}
        )
        assert tg.ALLOWED_USER_IDS == ["111", "222", "333"]

    def test_allowed_user_ids_empty_string_produces_empty_list(self):
        tg = _reload_with_env("adapters.telegram", {"ALLOWED_USER_IDS": ""})
        assert tg.ALLOWED_USER_IDS == []

    def test_parse_inbound_rejects_unauthorized_user(self):
        tg = _reload_with_env(
            "adapters.telegram",
            {"ALLOWED_USER_IDS": "111", "TELEGRAM_BOT_TOKEN": "fake"},
        )
        with patch.object(tg, "send_message"):
            event = {
                "body": json.dumps(
                    {
                        "message": {
                            "chat": {"id": 1},
                            "from": {"id": 999},
                            "text": "hello",
                            "message_id": 1,
                        }
                    }
                )
            }
            assert tg.parse_inbound(event) is None

    def test_parse_inbound_accepts_authorized_user(self):
        tg = _reload_with_env(
            "adapters.telegram",
            {"ALLOWED_USER_IDS": "999", "TELEGRAM_BOT_TOKEN": "fake"},
        )
        event = {
            "body": json.dumps(
                {
                    "message": {
                        "chat": {"id": 1},
                        "from": {"id": 999},
                        "text": "hello",
                        "message_id": 1,
                    }
                }
            )
        }
        result = tg.parse_inbound(event)
        assert result is not None
        assert result["user_id"] == "999"

    def test_allowed_user_ids_with_leading_space_now_matches(self):
        """Regression test for the original whitespace bug: '111, 222'
        used to produce ['111', ' 222'] (untrimmed), so a message from
        user 222 (no leading space in the actual from.id, always a plain
        int/str) would fail to match ' 222'. Confirms the fix."""
        tg = _reload_with_env(
            "adapters.telegram",
            {"ALLOWED_USER_IDS": "111, 222", "TELEGRAM_BOT_TOKEN": "fake"},
        )
        event = {
            "body": json.dumps(
                {
                    "message": {
                        "chat": {"id": 1},
                        "from": {"id": 222},
                        "text": "hello",
                        "message_id": 1,
                    }
                }
            )
        }
        result = tg.parse_inbound(event)
        assert result is not None, "user 222 should be authorized after trim fix"


# ---------------------------------------------------------------------------
# Fix #2 + #5: Slack -- fail closed, allowlist enforcement
# ---------------------------------------------------------------------------

class TestSlackValidation:
    def test_validate_webhook_fails_closed_when_secret_unset(self):
        sl = _reload_with_env("adapters.slack", {"SLACK_SIGNING_SECRET": ""})
        assert sl.validate_webhook({"headers": {}, "body": "{}"}) is False

    def test_validate_webhook_passes_with_correct_signature(self):
        import hmac

        secret = "test-signing-secret"
        sl = _reload_with_env("adapters.slack", {"SLACK_SIGNING_SECRET": secret})
        body = '{"type":"event_callback"}'
        timestamp = str(int(time.time()))
        sig_basestring = f"v0:{timestamp}:{body}"
        computed = "v0=" + hmac.HMAC(
            secret.encode(), sig_basestring.encode(), hashlib.sha256
        ).hexdigest()
        event = {
            "headers": {
                "x-slack-request-timestamp": timestamp,
                "x-slack-signature": computed,
            },
            "body": body,
        }
        assert sl.validate_webhook(event) is True

    def test_validate_webhook_rejects_stale_timestamp(self):
        import hmac

        secret = "test-signing-secret"
        sl = _reload_with_env("adapters.slack", {"SLACK_SIGNING_SECRET": secret})
        body = '{"type":"event_callback"}'
        stale_timestamp = str(int(time.time()) - 400)  # >300s old
        sig_basestring = f"v0:{stale_timestamp}:{body}"
        computed = "v0=" + hmac.HMAC(
            secret.encode(), sig_basestring.encode(), hashlib.sha256
        ).hexdigest()
        event = {
            "headers": {
                "x-slack-request-timestamp": stale_timestamp,
                "x-slack-signature": computed,
            },
            "body": body,
        }
        assert sl.validate_webhook(event) is False

    def test_parse_inbound_rejects_unauthorized_user(self):
        sl = _reload_with_env(
            "adapters.slack", {"SLACK_ALLOWED_USER_IDS": "U111"}
        )
        event = {
            "body": json.dumps(
                {
                    "type": "event_callback",
                    "event": {"type": "message", "user": "U999", "text": "hi", "channel": "C1"},
                }
            )
        }
        assert sl.parse_inbound(event) is None

    def test_parse_inbound_accepts_authorized_user(self):
        sl = _reload_with_env(
            "adapters.slack", {"SLACK_ALLOWED_USER_IDS": "U999"}
        )
        event = {
            "body": json.dumps(
                {
                    "type": "event_callback",
                    "event": {"type": "message", "user": "U999", "text": "hi", "channel": "C1"},
                }
            )
        }
        result = sl.parse_inbound(event)
        assert result is not None
        assert result["user_id"] == "U999"

    def test_parse_inbound_empty_allowlist_denies_everyone(self):
        """Fail-closed: no allowlist configured means no one is authorized,
        not everyone."""
        sl = _reload_with_env("adapters.slack", {"SLACK_ALLOWED_USER_IDS": ""})
        event = {
            "body": json.dumps(
                {
                    "type": "event_callback",
                    "event": {"type": "message", "user": "U999", "text": "hi", "channel": "C1"},
                }
            )
        }
        assert sl.parse_inbound(event) is None


# ---------------------------------------------------------------------------
# Fix #1 + #5: Discord -- real Ed25519 verification, fail closed, allowlist
# ---------------------------------------------------------------------------

class TestDiscordValidation:
    @staticmethod
    def _make_keypair():
        from nacl.signing import SigningKey

        signing_key = SigningKey.generate()
        verify_key = signing_key.verify_key
        public_key_hex = verify_key.encode().hex()
        return signing_key, public_key_hex

    def test_validate_webhook_fails_closed_when_public_key_unset(self):
        dc = _reload_with_env("adapters.discord", {"DISCORD_PUBLIC_KEY": ""})
        assert dc.validate_webhook({"headers": {}, "body": "{}"}) is False

    def test_validate_webhook_fails_closed_on_invalid_public_key(self):
        dc = _reload_with_env("adapters.discord", {"DISCORD_PUBLIC_KEY": "not-hex!!"})
        assert dc.validate_webhook({"headers": {}, "body": "{}"}) is False

    def test_validate_webhook_passes_with_correct_signature(self):
        signing_key, public_key_hex = self._make_keypair()
        dc = _reload_with_env("adapters.discord", {"DISCORD_PUBLIC_KEY": public_key_hex})

        body = '{"type":1}'
        timestamp = "1234567890"
        message = (timestamp + body).encode("utf-8")
        signature = signing_key.sign(message).signature.hex()

        event = {
            "headers": {
                "x-signature-ed25519": signature,
                "x-signature-timestamp": timestamp,
            },
            "body": body,
        }
        assert dc.validate_webhook(event) is True

    def test_validate_webhook_rejects_tampered_body(self):
        """Signature was computed over the original body; verifying against
        a different body must fail -- this is the core Ed25519 guarantee
        the fix relies on."""
        signing_key, public_key_hex = self._make_keypair()
        dc = _reload_with_env("adapters.discord", {"DISCORD_PUBLIC_KEY": public_key_hex})

        original_body = '{"type":1}'
        tampered_body = '{"type":2}'
        timestamp = "1234567890"
        message = (timestamp + original_body).encode("utf-8")
        signature = signing_key.sign(message).signature.hex()

        event = {
            "headers": {
                "x-signature-ed25519": signature,
                "x-signature-timestamp": timestamp,
            },
            "body": tampered_body,
        }
        assert dc.validate_webhook(event) is False

    def test_validate_webhook_rejects_wrong_signer(self):
        """A signature from a different keypair than the configured
        DISCORD_PUBLIC_KEY must be rejected."""
        _, public_key_hex = self._make_keypair()
        wrong_signing_key, _ = self._make_keypair()
        dc = _reload_with_env("adapters.discord", {"DISCORD_PUBLIC_KEY": public_key_hex})

        body = '{"type":1}'
        timestamp = "1234567890"
        message = (timestamp + body).encode("utf-8")
        signature = wrong_signing_key.sign(message).signature.hex()

        event = {
            "headers": {
                "x-signature-ed25519": signature,
                "x-signature-timestamp": timestamp,
            },
            "body": body,
        }
        assert dc.validate_webhook(event) is False

    def test_validate_webhook_missing_headers_fails_closed(self):
        _, public_key_hex = self._make_keypair()
        dc = _reload_with_env("adapters.discord", {"DISCORD_PUBLIC_KEY": public_key_hex})
        assert dc.validate_webhook({"headers": {}, "body": "{}"}) is False

    def test_parse_inbound_rejects_unauthorized_user(self):
        dc = _reload_with_env(
            "adapters.discord", {"DISCORD_ALLOWED_USER_IDS": "111"}
        )
        event = {
            "body": json.dumps(
                {
                    "type": 2,
                    "data": {"options": [{"name": "message", "value": "hi"}]},
                    "member": {"user": {"id": "999"}},
                    "channel_id": "C1",
                    "id": "I1",
                    "token": "T1",
                }
            )
        }
        assert dc.parse_inbound(event) is None

    def test_parse_inbound_accepts_authorized_user(self):
        dc = _reload_with_env(
            "adapters.discord", {"DISCORD_ALLOWED_USER_IDS": "999"}
        )
        event = {
            "body": json.dumps(
                {
                    "type": 2,
                    "data": {"options": [{"name": "message", "value": "hi"}]},
                    "member": {"user": {"id": "999"}},
                    "channel_id": "C1",
                    "id": "I1",
                    "token": "T1",
                }
            )
        }
        result = dc.parse_inbound(event)
        assert result is not None
        assert result["user_id"] == "999"

    def test_parse_inbound_ping_bypasses_allowlist(self):
        """Discord's PING (type=1) verification handshake must still work
        even with an empty allowlist -- it has no user_id and is handled
        before the allowlist check."""
        dc = _reload_with_env("adapters.discord", {"DISCORD_ALLOWED_USER_IDS": ""})
        event = {"body": json.dumps({"type": 1})}
        result = dc.parse_inbound(event)
        assert result == {"_ping": True}


# ---------------------------------------------------------------------------
# Fix #6: per-user session-id derivation -- determinism, collision-safety,
# length constraint
# ---------------------------------------------------------------------------

class TestSessionIdDerivation:
    def _get_core(self):
        return _reload_with_env(
            "core",
            {
                "AGENTCORE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:123:runtime/x",
                "SESSION_ID": "fallback-session-id-at-least-33-chars-long",
            },
        )

    def test_deterministic_for_same_input(self):
        core = self._get_core()
        id1 = core.derive_session_id("telegram", "12345")
        id2 = core.derive_session_id("telegram", "12345")
        assert id1 == id2

    def test_different_users_get_different_sessions(self):
        core = self._get_core()
        id_a = core.derive_session_id("telegram", "111")
        id_b = core.derive_session_id("telegram", "222")
        assert id_a != id_b

    def test_different_channels_same_user_id_get_different_sessions(self):
        """A Telegram user and a Discord user with numerically identical
        user_id strings must not collide onto the same AgentCore session --
        this is exactly the kind of cross-tenant bleed fix #6 closes."""
        core = self._get_core()
        id_telegram = core.derive_session_id("telegram", "12345")
        id_discord = core.derive_session_id("discord", "12345")
        assert id_telegram != id_discord

    def test_meets_agentcore_minimum_length(self):
        """AgentCore requires runtimeSessionId >= 33 chars (enforced
        elsewhere via deploy-channel-router.sh's --session-id check)."""
        core = self._get_core()
        session_id = core.derive_session_id("telegram", "1")  # shortest realistic input
        assert len(session_id) >= 33

    def test_falls_back_to_session_id_when_user_id_empty(self):
        core = self._get_core()
        result = core.derive_session_id("telegram", "")
        assert result == core.SESSION_ID


# ---------------------------------------------------------------------------
# Fix #8: per-user rate limiting -- cooldown enforcement, fail-open safety
# ---------------------------------------------------------------------------

class TestRateLimiting:
    def _get_core_with_mocked_dynamo(self):
        core = _reload_with_env(
            "core",
            {
                "AGENTCORE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:123:runtime/x",
                "COLDSTART_TABLE": "test-table",
                "REQUEST_COOLDOWN_SECONDS": "5",
            },
        )
        mock_dynamo = MagicMock()
        core._dynamodb_client = mock_dynamo
        return core, mock_dynamo

    def test_no_rate_limit_without_coldstart_table(self):
        core = _reload_with_env(
            "core",
            {
                "AGENTCORE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:123:runtime/x",
                "COLDSTART_TABLE": "",
            },
        )
        assert core.is_rate_limited("telegram", "111") is False

    def test_no_rate_limit_without_user_id(self):
        core, _ = self._get_core_with_mocked_dynamo()
        assert core.is_rate_limited("telegram", "") is False

    def test_first_request_not_rate_limited(self):
        core, mock_dynamo = self._get_core_with_mocked_dynamo()
        mock_dynamo.get_item.return_value = {}  # no prior item
        assert core.is_rate_limited("telegram", "111") is False
        mock_dynamo.put_item.assert_called_once()

    def test_repeat_request_within_cooldown_is_rate_limited(self):
        core, mock_dynamo = self._get_core_with_mocked_dynamo()
        mock_dynamo.get_item.return_value = {
            "Item": {"last_success_epoch": {"N": str(int(time.time()))}}
        }
        assert core.is_rate_limited("telegram", "111") is True

    def test_request_after_cooldown_expires_is_not_rate_limited(self):
        core, mock_dynamo = self._get_core_with_mocked_dynamo()
        mock_dynamo.get_item.return_value = {
            "Item": {"last_success_epoch": {"N": str(int(time.time()) - 10)}}
        }
        assert core.is_rate_limited("telegram", "111") is False

    def test_fails_open_on_dynamodb_error(self):
        """A rate-limit check failure must never block legitimate traffic --
        this is a cost guard, not the authorization boundary."""
        core, mock_dynamo = self._get_core_with_mocked_dynamo()
        mock_dynamo.get_item.side_effect = Exception("DynamoDB unavailable")
        assert core.is_rate_limited("telegram", "111") is False

    def test_different_users_have_independent_cooldowns(self):
        core, mock_dynamo = self._get_core_with_mocked_dynamo()
        now = int(time.time())

        def get_item_side_effect(**kwargs):
            key = kwargs["Key"]["session_id"]["S"]
            if "111" in key:
                return {"Item": {"last_success_epoch": {"N": str(now)}}}
            return {}

        mock_dynamo.get_item.side_effect = get_item_side_effect
        assert core.is_rate_limited("telegram", "111") is True
        assert core.is_rate_limited("telegram", "222") is False


# ---------------------------------------------------------------------------
# Fix #6 (regression guard): invoke_with_retry / invoke_agent must accept
# and use an explicit session_id rather than always falling back to the
# global SESSION_ID -- this is the actual wiring the per-user fix depends on.
# ---------------------------------------------------------------------------

class TestInvokeUsesExplicitSessionId:
    def test_invoke_agent_uses_explicit_session_id_not_global_default(self):
        core = _reload_with_env(
            "core",
            {
                "AGENTCORE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:123:runtime/x",
                "SESSION_ID": "global-fallback-should-not-be-used-here",
            },
        )
        mock_agentcore = MagicMock()
        mock_stream = MagicMock()
        mock_stream.read.return_value = b'data: {"result": "ok"}\n\n'
        mock_agentcore.invoke_agent_runtime.return_value = {"response": mock_stream}
        core._agentcore_client = mock_agentcore

        explicit_session_id = "per-user-derived-session-id-1234567890"
        core.invoke_agent("hello", session_id=explicit_session_id)

        call_kwargs = mock_agentcore.invoke_agent_runtime.call_args.kwargs
        assert call_kwargs["runtimeSessionId"] == explicit_session_id
        assert call_kwargs["runtimeSessionId"] != "global-fallback-should-not-be-used-here"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# Regression guard: index.py's handle_webhook/handle_worker changes (rate-
# limit check + session_id derivation wired in) must not break existing
# special-case handling (Discord PING, Slack URL verification challenge,
# cold-start notice dispatch) that fix #6/#8 touched the surrounding code of
# but did not intend to change.
# ---------------------------------------------------------------------------

class TestIndexHandlerRegression:
    def _get_index(self, env=None):
        env = env or {}
        base_env = {
            "AGENTCORE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:123:runtime/x",
            "AWS_LAMBDA_FUNCTION_NAME": "test-fn",
        }
        base_env.update(env)
        with patch.dict(os.environ, base_env, clear=False):
            with patch("boto3.client") as mock_boto_client:
                mock_boto_client.return_value = MagicMock()
                if "index" in sys.modules:
                    del sys.modules["index"]
                if "core" in sys.modules:
                    del sys.modules["core"]
                for mod in list(sys.modules):
                    if mod.startswith("adapters"):
                        del sys.modules[mod]
                import index
                return index

    def test_discord_ping_bypasses_ratelimit_and_session_derivation(self):
        """Discord's PING handshake (type=1) has no user_id and must be
        answered immediately -- it must not be dropped by the new
        rate-limit check or fail while deriving a session id from an
        empty user_id."""
        idx = self._get_index({"DISCORD_PUBLIC_KEY": "", "DISCORD_ALLOWED_USER_IDS": ""})
        with patch.object(idx.dc_adapter, "validate_webhook", return_value=True):
            event = {
                "rawPath": "/webhook/discord",
                "headers": {},
                "body": json.dumps({"type": 1}),
            }
            result = idx.handle_webhook(event)
            assert result["statusCode"] == 200
            assert json.loads(result["body"]) == {"type": 1}

    def test_slack_url_verification_challenge_still_works(self):
        idx = self._get_index({"SLACK_SIGNING_SECRET": ""})
        with patch.object(idx.sl_adapter, "validate_webhook", return_value=True):
            event = {
                "rawPath": "/webhook/slack",
                "headers": {},
                "body": json.dumps(
                    {"type": "url_verification", "challenge": "abc123"}
                ),
            }
            result = idx.handle_webhook(event)
            assert result["statusCode"] == 200
            assert json.loads(result["body"]) == {"challenge": "abc123"}

    def test_unauthorized_webhook_returns_403(self):
        idx = self._get_index()
        with patch.object(idx.tg_adapter, "validate_webhook", return_value=False):
            event = {"rawPath": "/webhook/telegram", "headers": {}, "body": "{}"}
            result = idx.handle_webhook(event)
            assert result["statusCode"] == 403

    def test_authorized_message_triggers_async_worker_invoke_with_session_id(self):
        """End-to-end regression check for fix #6: a legitimate authorized
        message must still trigger the async self-invoke (existing
        behavior preserved), and the worker payload must now carry the
        derived per-user session_id (new behavior)."""
        idx = self._get_index({"COLDSTART_TABLE": ""})
        with patch.object(idx.tg_adapter, "validate_webhook", return_value=True), \
             patch.object(
                 idx.tg_adapter,
                 "parse_inbound",
                 return_value={
                     "chat_id": 1,
                     "user_id": "999",
                     "message_text": "hello",
                     "message_id": 1,
                     "channel": "telegram",
                 },
             ), \
             patch.object(idx.tg_adapter, "send_typing"):
            event = {"rawPath": "/webhook/telegram", "headers": {}, "body": "{}"}
            result = idx.handle_webhook(event)
            assert result["statusCode"] == 200

            # Existing behavior preserved: async self-invoke happened.
            idx.lambda_client.invoke.assert_called_once()
            call_kwargs = idx.lambda_client.invoke.call_args.kwargs
            assert call_kwargs["InvocationType"] == "Event"

            # New behavior: worker payload carries a derived session_id
            # distinct from the raw user_id, tying back to fix #6.
            payload = json.loads(call_kwargs["Payload"])
            assert payload["session_id"] is not None
            assert "telegram-999" in payload["session_id"]

    def test_rate_limited_message_does_not_invoke_worker(self):
        """Fix #8 regression check: a rate-limited request must short-
        circuit before the async self-invoke, not after."""
        idx = self._get_index({"COLDSTART_TABLE": "test-table"})
        with patch.object(idx.tg_adapter, "validate_webhook", return_value=True), \
             patch.object(
                 idx.tg_adapter,
                 "parse_inbound",
                 return_value={
                     "chat_id": 1,
                     "user_id": "999",
                     "message_text": "hello",
                     "message_id": 1,
                     "channel": "telegram",
                 },
             ), \
             patch("index.is_rate_limited", return_value=True):
            event = {"rawPath": "/webhook/telegram", "headers": {}, "body": "{}"}
            result = idx.handle_webhook(event)
            assert result["statusCode"] == 200
            idx.lambda_client.invoke.assert_not_called()
