from __future__ import annotations

import unittest
import tempfile
import copy
from pathlib import Path

from webqa.config import DEFAULTS, validate_config
from webqa.pagepatrol import run_pagepatrol
from webqa.safety import (
    Redactor,
    classify_action,
    classify_exception,
    decide_recovery,
)


class SafetyTest(unittest.TestCase):
    def test_redactor_removes_secrets_and_personal_data(self):
        redactor = Redactor({})
        value = redactor.redact_value(
            {
                "url": "https://example.test/account?token=top-secret&view=full",
                "authorization": "Bearer abc.def",
                "message": "user@example.com 13800138000 api_key=secret-value",
                "json_log": '{"refresh_token":"json-secret"}',
            }
        )
        rendered = str(value)
        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("abc.def", rendered)
        self.assertNotIn("user@example.com", rendered)
        self.assertNotIn("13800138000", rendered)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("json-secret", rendered)
        self.assertIn("view=full", value["url"])
        sentence = redactor.redact_text("外域链接 https://example.test/path，只记录不进入")
        self.assertEqual("外域链接 https://example.test/path，只记录不进入", sentence)
        legacy = redactor.redact_text(
            "外域链接 https://example.test/path%EF%BC%8C%E5%8F%AA%E8%AE%B0%E5%BD%95%E4%B8%8D%E8%BF%9B%E5%85%A5"
        )
        self.assertEqual("外域链接 https://example.test/path，只记录不进入", legacy)

    def test_action_risk_requires_explicit_stateful_authorization(self):
        saved = classify_action({"tag": "button", "label": "保存设置"}, {})
        self.assertEqual("STATEFUL", saved.level)
        self.assertFalse(saved.allowed)
        self.assertFalse(saved.retry_safe)
        allowed = classify_action(
            {"tag": "button", "label": "保存设置"},
            {"allow_stateful_actions": True},
        )
        self.assertTrue(allowed.allowed)
        self.assertFalse(allowed.retry_safe)
        destructive = classify_action(
            {"tag": "button", "label": "Delete account"},
            {"allow_destructive_actions": True},
            ["delete"],
        )
        self.assertEqual("DESTRUCTIVE", destructive.level)
        self.assertFalse(destructive.allowed)
        link = classify_action({"tag": "a", "role": "link", "href": "/help", "label": "Help"}, {})
        self.assertEqual("READ_ONLY", link.level)
        self.assertTrue(link.retry_safe)

    def test_exception_classification_and_safe_recovery(self):
        auth = classify_exception(
            RuntimeError("request failed"),
            {"document_responses": [{"url": "https://example.test", "status": 401}]},
            "open",
        )
        self.assertEqual("AUTH_BLOCKED", auth.category)
        limited = classify_exception(RuntimeError("HTTP 429"), {}, "open")
        self.assertEqual("RATE_LIMITED", limited.category)
        stateful = classify_action(
            {"tag": "button", "label": "保存"},
            {"allow_stateful_actions": True},
        )
        unknown = decide_recovery(
            classify_exception(TimeoutError("timeout"), {}, "action"),
            stateful,
            action_attempted=True,
            state_changed=False,
            attempts=1,
            retry_limit=2,
        )
        self.assertEqual("UNKNOWN_COMMIT", unknown.status)
        self.assertFalse(unknown.retry)
        readonly = classify_action({"tag": "a", "href": "/next", "label": "Next"}, {})
        retry = decide_recovery(
            classify_exception(TimeoutError("timeout"), {}, "action"),
            readonly,
            action_attempted=True,
            state_changed=False,
            attempts=1,
            retry_limit=2,
        )
        self.assertEqual("RETRY", retry.status)
        self.assertTrue(retry.retry)

    def test_sensitive_target_is_not_sent_to_pagepatrol_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            result = run_pagepatrol(
                {
                    "safety": {"redaction_enabled": True, "allow_sensitive_pagepatrol": False},
                    "pagepatrol": {"enabled": True, "executable": "missing.exe"},
                },
                Path(temp),
                "https://example.test/account?access_token=secret-value",
                1,
            )
        self.assertEqual("DISABLED_SENSITIVE", result["status"])
        self.assertNotIn("secret-value", str(result))

    def test_sensitive_target_storage_requires_explicit_authorization(self):
        config = copy.deepcopy(DEFAULTS)
        config["target"] = "https://example.test/account?session=secret-value"
        with self.assertRaisesRegex(ValueError, "敏感参数"):
            validate_config(config)
        config["safety"]["allow_sensitive_target_storage"] = True
        validate_config(config)

    def test_record_only_is_default_and_load_only_remains_available(self):
        config = copy.deepcopy(DEFAULTS)
        self.assertEqual("record_only", config["traversal"]["child_page_check"])
        shared = config["deduplication"]["shared_controls"]
        self.assertTrue(shared["enabled"])
        self.assertEqual("verify_then_reuse", shared["policy"])
        self.assertEqual("high", shared["min_confidence"])
        validate_config(config)
        config["traversal"]["child_page_check"] = "load_only"
        validate_config(config)


if __name__ == "__main__":
    unittest.main()

