from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from webqa.config import DEFAULTS, apply_test_profile, validate_config
from webqa.contracts import evaluate_api_contracts, validate_json_schema
from webqa.invariants import select_invariant
from webqa.list_sampling import apply_list_sampling, normalized_collection_url
from webqa.pagepatrol import _sanitize_report_files
from webqa.planner_rules import plan_field_cases
from webqa.safety import Redactor
from webqa.storage import Storage


class ContractTest(unittest.TestCase):
    def test_api_contract_checks_status_content_schema_required_and_enum(self):
        responses = [
            {
                "method": "GET",
                "url": "https://example.test/api/items",
                "status": 200,
                "content_type": "application/json; charset=utf-8",
                "json": {"data": [{"id": 1, "status": "unexpected"}]},
            }
        ]
        settings = {
            "enabled": True,
            "automatic_http_errors": True,
            "rules": [
                {
                    "key": "items-contract",
                    "url_regex": "/api/items$",
                    "statuses": [200],
                    "content_types": ["application/json"],
                    "required_fields": ["data.*.id", "meta.total"],
                    "enums": {"data.*.status": ["active", "inactive"]},
                    "json_schema": {
                        "type": "object",
                        "required": ["data", "meta"],
                        "properties": {"data": {"type": "array"}},
                    },
                }
            ],
        }
        results = evaluate_api_contracts(responses, settings)
        titles = {item["title"] for item in results}
        self.assertIn("JSON Schema 校验失败", titles)
        self.assertIn("API 响应缺少必填字段", titles)
        self.assertIn("API 字段超出允许枚举", titles)

    def test_required_api_is_only_demanded_in_configured_or_initial_phase(self):
        settings = {
            "enabled": True,
            "rules": [{"key": "required", "url_regex": "/api/required", "required": True}],
        }
        self.assertTrue(evaluate_api_contracts([], settings, phase="INITIAL_PAGE"))
        self.assertFalse(evaluate_api_contracts([], settings, phase="STATE_AFTER_ACTION"))
        scoped = {
            "enabled": True,
            "rules": [
                {
                    "key": "scoped",
                    "page_url_regex": "/leaderboard$",
                    "url_regex": "/api/required",
                    "required": True,
                }
            ],
        }
        self.assertFalse(
            evaluate_api_contracts(
                [], scoped, phase="INITIAL_PAGE", page_url="https://example.test/home"
            )
        )
        self.assertTrue(
            evaluate_api_contracts(
                [], scoped, phase="INITIAL_PAGE", page_url="https://example.test/leaderboard"
            )
        )

    def test_json_schema_fallback_contract(self):
        errors = validate_json_schema(
            {"status": "bad"},
            {
                "type": "object",
                "required": ["status", "items"],
                "properties": {"status": {"enum": ["ok"]}},
            },
        )
        self.assertTrue(errors)


class ProfileAndGenerationTest(unittest.TestCase):
    def test_smoke_profile_applies_bounded_traversal(self):
        config = copy.deepcopy(DEFAULTS)
        apply_test_profile(config, "smoke")
        self.assertEqual("smoke", config["testing"]["profile"])
        self.assertEqual(40, config["traversal"]["max_pages"])
        self.assertEqual(80, config["traversal"]["max_actions_per_page"])
        validate_config(config)

    def test_resume_cannot_silently_change_test_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            smoke = copy.deepcopy(DEFAULTS)
            smoke["target"] = "https://example.test/"
            apply_test_profile(smoke, "smoke")
            storage = Storage(Path(temp) / "webqa.sqlite3")
            storage.create_or_resume_run(smoke, new_run=True)
            full = copy.deepcopy(DEFAULTS)
            full["target"] = smoke["target"]
            apply_test_profile(full, "full")
            with self.assertRaisesRegex(ValueError, "测试模式"):
                storage.create_or_resume_run(full, new_run=False)
            storage.close_thread_connection()

    def test_form_planner_covers_select_checkbox_and_boundaries(self):
        select_cases = plan_field_cases(
            {
                "tag": "select",
                "constraints": {
                    "required": True,
                    "options": [
                        {"value": "", "label": "Choose", "disabled": False},
                        {"value": "a", "label": "A", "disabled": False},
                    ],
                },
            },
            "case-",
        )
        self.assertEqual({"empty", "valid"}, {item["case_type"] for item in select_cases})
        checkbox_cases = plan_field_cases(
            {"tag": "input", "type": "checkbox", "constraints": {"required": True}},
            "case-",
        )
        self.assertEqual({"empty", "valid"}, {item["case_type"] for item in checkbox_cases})
        text_cases = plan_field_cases(
            {
                "tag": "input",
                "type": "text",
                "constraints": {"minlength": "2", "maxlength": "4", "pattern": "[a-z]+"},
            },
            "case-",
        )
        types = {item["case_type"] for item in text_cases}
        self.assertTrue({"minlength", "below_minlength", "maxlength", "over_maxlength", "pattern_mismatch"} <= types)
        implicit_text_cases = plan_field_cases(
            {"tag": "input", "type": "", "constraints": {"required": True}},
            "case-",
        )
        self.assertEqual(
            {"empty", "valid"},
            {item["case_type"] for item in implicit_text_cases},
        )

    def test_invariant_auto_detection_is_conservative(self):
        settings = {"auto_detect": True, "rules": []}
        self.assertEqual(
            "reversible",
            select_invariant({"label": "Menu", "expanded": "false"}, settings)["type"],
        )
        self.assertEqual(
            "idempotent",
            select_invariant({"label": "Overview", "role": "tab"}, settings)["type"],
        )
        self.assertEqual(
            "same_collection_members",
            select_invariant({"label": "Sort by date"}, settings)["type"],
        )
        self.assertEqual(
            "non_increasing_count",
            select_invariant({"label": "筛选状态"}, settings)["type"],
        )
        self.assertEqual(
            "stable_structure",
            select_invariant({"label": "Language"}, settings)["type"],
        )
        self.assertIsNone(select_invariant({"label": "Save", "role": "button"}, settings))

    def test_list_sampling_selects_head_tail_and_pagination_points(self):
        claims: set[tuple[str, str]] = set()

        def claim(group_key, slot):
            value = (group_key, slot)
            if value in claims:
                return False
            claims.add(value)
            return True

        controls = [
            {
                "label": f"Project {index}",
                "href": f"https://example.test/projects/{index}",
                "collection_key_hint": "projects-list",
                "collection_item_index": index,
                "pagination_first_page": 1,
                "pagination_current_page": 1,
                "pagination_last_page": 10,
            }
            for index in range(8)
        ]
        controls.extend(
            {
                "label": str(page),
                "href": f"https://example.test/projects?page={page}",
                "pagination_key_hint": "projects-pages",
                "pagination_kind": "page",
                "pagination_page_number": page,
                "pagination_first_page": 1,
                "pagination_current_page": 1,
                "pagination_last_page": 10,
            }
            for page in (1, 2, 10)
        )
        planned, records = apply_list_sampling(
            controls,
            "https://example.test/projects?page=1",
            {"enabled": True, "item_detail_samples": 2, "pagination_samples": 3},
            {"list_item_samples": 2, "pagination_samples": 3},
            claim,
        )
        selected_items = [item for item in planned if item.get("list_item_sample")]
        selected_pages = [item for item in planned if item.get("pagination_sample")]
        self.assertEqual(["Project 0"], [item["label"] for item in selected_items])
        self.assertEqual([10], [item["pagination_page_number"] for item in selected_pages])
        self.assertEqual(2, len(records))
        pagination_record = next(item for item in records if item["sample_kind"] == "PAGINATION")
        self.assertEqual(
            [{"label": "推导分页 5", "href": "https://example.test/projects?page=5", "page": 5, "direct": True}],
            pagination_record["direct_targets"],
        )

        tail_controls = [
            {
                "label": f"Project {index + 72}",
                "href": f"https://example.test/projects/{index + 72}",
                "collection_key_hint": "projects-list",
                "collection_item_index": index,
                "pagination_first_page": 1,
                "pagination_current_page": 10,
                "pagination_last_page": 10,
            }
            for index in range(8)
        ]
        tail_planned, _ = apply_list_sampling(
            tail_controls,
            "https://example.test/projects?page=10",
            {"enabled": True, "item_detail_samples": 2, "pagination_samples": 3},
            {"list_item_samples": 2, "pagination_samples": 3},
            claim,
        )
        self.assertEqual(
            ["Project 79"],
            [item["label"] for item in tail_planned if item.get("list_item_sample")],
        )
        self.assertEqual(
            "https://example.test/projects",
            normalized_collection_url("https://example.test/projects?page=10"),
        )


class PagePatrolImportTest(unittest.TestCase):
    def test_nested_pagepatrol_findings_are_extracted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_json = root / "report.json"
            report_html = root / "report.html"
            report_json.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "viewport": {"name": "desktop", "width": 1280, "height": 800},
                                "findings": [
                                    {
                                        "check": "overlap",
                                        "category": "layout",
                                        "severity": "error",
                                        "message": "Controls overlap",
                                        "selector": "#submit",
                                        "details": {},
                                    },
                                    {
                                        "check": "large-page",
                                        "category": "performance",
                                        "severity": "warning",
                                        "message": "Large page",
                                        "details": {},
                                    },
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            report_html.write_text("<html>report</html>", encoding="utf-8")
            result = _sanitize_report_files(report_json, report_html, Redactor({}))
            self.assertEqual({"error": 1, "warning": 1, "info": 0}, result["finding_counts"])
            self.assertEqual(2, len(result["findings"]))
            self.assertTrue(all(item.get("fingerprint") for item in result["findings"]))
            self.assertNotIn("viewport", result)


if __name__ == "__main__":
    unittest.main()
