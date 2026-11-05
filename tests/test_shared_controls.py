from __future__ import annotations

import copy
import unittest

from webqa.browser import enrich_controls


class SharedControlFingerprintTest(unittest.TestCase):
    def _control(self) -> dict:
        return {
            "role": "button",
            "tag": "button",
            "type": "button",
            "label": "AI assistant",
            "accessible_name": "AI assistant",
            "aria": "AI assistant",
            "title": "",
            "placeholder": "",
            "href": "",
            "section": "",
            "selector": "#assistant",
            "stable_id": "assistant",
            "testid": "",
            "name": "",
            "shared_scope": "fixed",
            "shared_container_signature": "div||assistant-shell||AI assistant",
            "shared_forced": False,
            "expanded": None,
            "pressed": None,
            "checked": None,
            "selected": None,
            "icon_only": False,
        }

    def test_selector_and_coordinates_do_not_change_shared_identity(self):
        first = self._control()
        second = copy.deepcopy(first)
        second["selector"] = "body > div:nth-of-type(5) > button"
        first_result = enrich_controls("https://example.test/start", [first])[0]
        second_result = enrich_controls("https://example.test/child", [second])[0]
        self.assertNotEqual(first_result["control_key"], second_result["control_key"])
        self.assertEqual(first_result["shared_control_key"], second_result["shared_control_key"])
        self.assertEqual("high", first_result["shared_confidence"])

    def test_page_business_control_is_not_shared(self):
        control = self._control()
        control["shared_scope"] = ""
        control["shared_container_signature"] = ""
        result = enrich_controls("https://example.test/start", [control])[0]
        self.assertEqual("", result["shared_control_key"])
        self.assertEqual("none", result["shared_confidence"])

    def test_different_link_targets_do_not_share_result(self):
        first = self._control()
        second = copy.deepcopy(first)
        first.update({"tag": "a", "role": "link", "href": "https://example.test/login"})
        second.update({"tag": "a", "role": "link", "href": "https://example.test/logout"})
        first_result = enrich_controls("https://example.test/start", [first])[0]
        second_result = enrich_controls("https://example.test/child", [second])[0]
        self.assertNotEqual(first_result["shared_control_key"], second_result["shared_control_key"])


if __name__ == "__main__":
    unittest.main()
