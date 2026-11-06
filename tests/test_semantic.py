from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from webqa.config import DEFAULTS, validate_config
from webqa.semantic import (
    PageObservation,
    SemanticAnalyzer,
    SemanticFinding,
)
from webqa.storage import Storage


class SemanticAnalyzerTest(unittest.TestCase):
    def test_failed_list_data_and_stuck_loading_are_distinct_findings(self):
        observation = PageObservation(
            url="https://example.test/zh/network/leaderboard",
            title="排行榜",
            body_text="排行榜 暂时读不到数据 公开接口没有响应，可以稍后重试",
            main_text="暂时读不到数据 公开接口没有响应，可以稍后重试",
            table_count=1,
            table_data_rows=0,
            loading_indicators=["skeleton"],
            loading_wait_timed_out=True,
        )
        result = SemanticAnalyzer().evaluate(observation)
        findings = {item.classification: item for item in result.findings}
        self.assertEqual("LIST", result.understanding.primary_type)
        self.assertEqual("SUSPECTED", result.status)
        self.assertTrue(findings["CONTENT_UNAVAILABLE"].bug_eligible)
        self.assertTrue(findings["DATA_MISSING"].bug_eligible)
        self.assertTrue(findings["STUCK_LOADING"].bug_eligible)

    def test_reasonable_empty_state_is_not_reported_as_missing_data(self):
        observation = PageObservation(
            url="https://example.test/search?q=none",
            title="搜索",
            body_text="搜索 没有匹配结果 请修改关键词",
            main_text="没有匹配结果 请修改关键词",
            table_count=1,
        )
        result = SemanticAnalyzer().evaluate(observation)
        classes = {item.classification for item in result.findings}
        self.assertNotIn("DATA_MISSING", classes)
        self.assertNotIn("ORACLE_UNCERTAIN", classes)

    def test_unexplained_empty_collection_requires_review_not_bug(self):
        observation = PageObservation(
            url="https://example.test/items",
            title="Items",
            body_text="Items Name Status",
            main_text="Name Status",
            table_count=1,
        )
        result = SemanticAnalyzer().evaluate(observation)
        finding = next(item for item in result.findings if item.classification == "ORACLE_UNCERTAIN")
        self.assertFalse(finding.bug_eligible)
        self.assertIn(finding.confidence, {"low", "medium"})
        self.assertEqual("NEEDS_REVIEW", result.status)

    def test_documentation_mentioning_failure_language_is_not_auto_confirmed(self):
        observation = PageObservation(
            url="https://example.test/docs/api-errors",
            title="API error handling guide",
            body_text=("When the API is unavailable, show a retry control. " * 80),
            main_text=("When the API is unavailable, show a retry control. " * 80),
        )
        result = SemanticAnalyzer().evaluate(observation)
        finding = next(
            item
            for item in result.findings
            if item.classification == "CONTENT_UNAVAILABLE"
        )
        self.assertEqual("medium", finding.confidence)
        self.assertFalse(finding.bug_eligible)

    def test_structured_oracle_is_applied_by_route(self):
        rules = [
            {
                "source": "requirement:catalog-1",
                "conditions": {"path_regex": "/catalog"},
                "expected_result": {"must_contain": "商品目录", "min_items": 1},
                "confidence": "high",
            }
        ]
        observation = PageObservation(
            url="https://example.test/catalog",
            title="Catalog",
            body_text="Catalog",
            main_text="Catalog",
            table_count=1,
        )
        result = SemanticAnalyzer(expected_behavior=rules).evaluate(observation)
        finding = next(item for item in result.findings if item.classification == "BUSINESS_MISMATCH")
        self.assertTrue(finding.bug_eligible)
        self.assertEqual("requirement:catalog-1", finding.oracle_source)

    def test_natural_language_oracle_is_not_silently_treated_as_verified(self):
        result = SemanticAnalyzer(
            expected_behavior=["页面内容与控件语义一致"]
        ).evaluate(
            PageObservation(
                url="https://example.test/article",
                body_text="示例正文",
                main_text="示例正文",
            )
        )
        finding = next(
            item
            for item in result.findings
            if item.classification == "ORACLE_UNCERTAIN"
        )
        self.assertFalse(finding.bug_eligible)
        self.assertEqual("NEEDS_REVIEW", result.status)

    def test_requested_but_unavailable_model_never_silently_passes(self):
        result = SemanticAnalyzer(
            {"model": {"enabled": True, "provider": "python"}}
        ).evaluate(
            PageObservation(
                url="https://example.test/article",
                title="Article",
                body_text="A readable article.",
                main_text="A readable article.",
            )
        )
        self.assertEqual("MODEL_UNAVAILABLE", result.coverage)
        self.assertEqual("NEEDS_REVIEW", result.status)
        self.assertIn(
            "SEMANTIC_CHECK_UNAVAILABLE",
            {item.classification for item in result.findings},
        )

    def test_model_finding_can_flag_unreadable_copy_without_auto_confirming_low_confidence(self):
        class CopyModel:
            def evaluate(self, observation, understanding, rules):
                return [
                    SemanticFinding(
                        "COPY_QUALITY",
                        "语句不通顺",
                        "正文应自然可读",
                        "句子缺少必要成分",
                        "low",
                        "minor",
                        "semantic.model",
                    )
                ]

        result = SemanticAnalyzer(model=CopyModel()).evaluate(
            PageObservation(
                url="https://example.test/article",
                body_text="示例正文",
                main_text="示例正文",
            )
        )
        finding = next(item for item in result.findings if item.classification == "COPY_QUALITY")
        self.assertFalse(finding.bug_eligible)
        self.assertEqual("NEEDS_REVIEW", result.status)


class SemanticPersistenceTest(unittest.TestCase):
    def test_oracle_and_evaluation_are_auditable(self):
        with tempfile.TemporaryDirectory() as temp:
            config = copy.deepcopy(DEFAULTS)
            config["target"] = "https://example.test/"
            config["output"]["database"] = str(Path(temp) / "semantic.sqlite3")
            storage = Storage(config["output"]["database"])
            run_id, _ = storage.create_or_resume_run(config, new_run=True)
            page = storage.pending_pages(run_id, 0)[0]
            rule_id = storage.upsert_business_rule(
                "example.test",
                "catalog-rule",
                "目录至少一项",
                "ACTIVE",
                {"rule": {"expected": {"min_items": 1}, "confidence": "high"}},
            )
            evaluation_id, findings = storage.add_semantic_evaluation(
                run_id,
                int(page["id"]),
                "INITIAL_PAGE",
                page["url"],
                {"body_text": "Catalog", "body_chars": 7},
                {
                    "status": "SUSPECTED",
                    "coverage": "DETERMINISTIC_ONLY",
                    "understanding": {"primary_type": "LIST"},
                    "rules": [{"rule_key": "catalog-rule"}],
                    "findings": [
                        {
                            "classification": "BUSINESS_MISMATCH",
                            "title": "目录为空",
                            "expected": "至少一项",
                            "actual": "0 项",
                            "confidence": "high",
                            "severity": "major",
                            "oracle_source": "requirement",
                            "bug_eligible": True,
                            "fingerprint": "finding-a",
                            "evidence": {},
                        }
                    ],
                },
            )
            self.assertGreater(rule_id, 0)
            self.assertGreater(evaluation_id, 0)
            self.assertEqual("SUSPECTED", findings[0]["status"])
            self.assertEqual("6", storage.rows("SELECT value FROM schema_meta WHERE key='version'")[0]["value"])
            storage.close_thread_connection()

    def test_semantic_configuration_requires_the_stage_when_marked_required(self):
        config = copy.deepcopy(DEFAULTS)
        config["semantic"]["enabled"] = False
        with self.assertRaisesRegex(ValueError, "required"):
            validate_config(config)

    def test_model_secrets_cannot_be_persisted_in_yaml_options(self):
        config = copy.deepcopy(DEFAULTS)
        config["semantic"]["model"].update(
            {
                "enabled": True,
                "provider": "python",
                "adapter": "example.adapter:create",
                "options": {"api_key": "secret-value"},
            }
        )
        with self.assertRaisesRegex(ValueError, "不得保存"):
            validate_config(config)


if __name__ == "__main__":
    unittest.main()
