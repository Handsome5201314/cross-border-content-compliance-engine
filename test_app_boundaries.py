"""Streamlit UI smoke checks without reading credentials or calling a provider."""
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


class AppBoundaryTests(unittest.TestCase):
    def test_home_and_incomplete_custom_credentials(self):
        with patch("core.llm_client.load_env", side_effect=AssertionError("credentials must not be read")):
            app = AppTest.from_file(str(Path(__file__).parent / "app.py"), default_timeout=20).run()
            self.assertFalse(app.exception)
            app.checkbox(key="u_force").check().run()
            button = next(b for b in app.button if "开始生成" in b.label)
            button.click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any("同时填写" in error.value for error in app.error))

    def test_arbitrary_custom_gateway_is_blocked(self):
        with patch("core.llm_client.load_env", side_effect=AssertionError("credentials must not be read")):
            app = AppTest.from_file(str(Path(__file__).parent / "app.py"), default_timeout=20).run()
            app.text_input(key="u_key").set_value("synthetic-test-credential")
            app.text_input(key="u_base").set_value("https://untrusted.invalid/v1")
            app.checkbox(key="u_force").check().run()
            next(b for b in app.button if "开始生成" in b.label).click().run()
            self.assertFalse(app.exception)
            self.assertTrue(any("允许列表" in error.value for error in app.error))

    def test_precomputed_interaction_preserves_session_usage(self):
        source = {"meta": {"platforms": [], "languages": []}, "results": [],
                  "counts": {}, "usage": {"total_tokens": 5}, "value": {}}
        original_read = Path.read_text
        original_exists = Path.exists
        def read(path, *args, **kwargs):
            return json.dumps(source) if path.name == "precomputed_full.json" else original_read(path, *args, **kwargs)
        def exists(path):
            return True if path.name == "precomputed_full.json" else original_exists(path)
        with patch.object(Path, "read_text", read), patch.object(Path, "exists", exists):
            app = AppTest.from_file(str(Path(__file__).parent / "app.py"), default_timeout=20).run()
            next(r for r in app.radio if r.label == "选择模式").set_value("precomputed").run()
            self.assertFalse(app.exception)
            result = app.session_state["result"]
            result["usage"]["total_tokens"] = 11
            result["usage_runs"] = [{"task_id": "synthetic-run", "total_tokens": 6}]
            app.run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["result"]["usage"]["total_tokens"], 11)
            self.assertEqual(len(app.session_state["result"]["usage_runs"]), 1)


if __name__ == "__main__":
    unittest.main()
