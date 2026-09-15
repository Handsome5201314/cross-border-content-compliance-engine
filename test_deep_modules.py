import unittest

from core.package_agent import split_sentences, validate_review, render_preview, scan_content, apply_replacements, validate_language
from core.privacy_gate import validate_product, PrivacyViolation
from core.site_agent import SiteAgent
from core.live_agent import LiveAgent
from core import load_yaml_config
from jsonschema import Draft202012Validator


class ReviewContractTests(unittest.TestCase):
    def test_sentence_scan_covers_arabic_and_english(self):
        text = "The best choice! Is it guaranteed? أمان مطلق؟ لا."
        rows = split_sentences({"spoken": text})
        self.assertEqual(len(rows), 4)
        self.assertEqual("".join(row["text"] for row in rows), text)

    def test_missing_coverage_is_rejected(self):
        rows = split_sentences({"spoken": "One. Two."})
        with self.assertRaises(ValueError):
            validate_review({"checked_ids": [rows[0]["id"]], "findings": []}, rows)

    def test_signal_requires_verdict(self):
        rows = [{"id": "s0", "text": "Best!", "signals": [{"term": "Best"}]}]
        with self.assertRaises(ValueError):
            validate_review({"checked_ids": ["s0"], "findings": []}, rows)

    def test_no_unsafe_preview(self):
        with self.assertRaises(ValueError):
            render_preview({"status": "review_blocked", "language": "ar", "content": {}})

    def test_html_is_escaped_and_rtl(self):
        rendered = render_preview({"status": "delivered", "language": "ar", "rtl": True, "content": {"hero": {"headline": "<script>alert(1)</script>"}}})
        self.assertIn('dir="rtl"', rendered)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_all_module_languages_have_notices(self):
        copy = load_yaml_config("package_copy")
        for agent in (SiteAgent(None), LiveAgent(None)):
            Draft202012Validator.check_schema(agent.schema({}))
            for language in agent.cfg["languages"]:
                self.assertTrue(copy["roadmap"][language])
                self.assertTrue(copy["medical_notice"][language])
                self.assertTrue(copy["demo_notice"][language])

    def test_spoken_contract_rejects_missing_markers(self):
        validator = Draft202012Validator(LiveAgent(None).schema({})["properties"]
                                         ["timeline"]["items"]["properties"]["spoken"])
        self.assertFalse(validator.is_valid("Just speech without stage directions."))
        self.assertTrue(validator.is_valid("[pause] This is **a draft**."))

    def test_live_duration_must_be_positive_integer(self):
        validator = Draft202012Validator(LiveAgent(None).schema({})["properties"]
                                         ["timeline"]["items"]["properties"]["duration_seconds"])
        for duration in [0, -1, 2.5, "90", True]:
            self.assertFalse(validator.is_valid(duration))
        self.assertTrue(validator.is_valid(90))

    def test_site_faq_requires_eight_to_ten_questions(self):
        validator = Draft202012Validator(SiteAgent(None).schema({})["properties"]["faq"])
        question = {"topic": "deployment", "question": "Where?", "answer": "To be confirmed."}
        for count in [0, 7, 11]:
            self.assertFalse(validator.is_valid([question] * count))
        self.assertTrue(validator.is_valid([question] * 8))

    def test_mixed_language_and_changed_product_channel_are_rejected(self):
        for text in ["Clinical knowledge沉淀", "Support via WhatsApp"]:
            with self.assertRaises(ValueError):
                validate_language({"spoken": text}, {"deployment": "微信随访"})
        validate_language({"spoken": "Follow-up drafts via WeChat"}, {"deployment": "微信随访"})

    def test_source_whitelist_still_blocks_unknown_fields(self):
        with self.assertRaises(PrivacyViolation):
            validate_product({"clinical_record": "private"})

    def test_arabic_and_percent_signals(self):
        rows = scan_content({"spoken": "100% safe. أمان مطلق؟ Guaranteed cure!"})
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["signals"] for row in rows))

    def test_negative_context_needs_semantic_verdict(self):
        rows = scan_content({"spoken": "Not a cure."})
        self.assertTrue(rows[0]["signals"])
        verdict = {"checked_ids": ["s0"], "findings": [
            {"id": "s0", "verdict": "benign", "reason": "否定语境", "replacement": ""}]}
        self.assertEqual(validate_review(verdict, rows), verdict)

    def test_replacements_preserve_adjacent_sentences_and_input(self):
        content = {"spoken": "Best! Keep this. Guaranteed!"}
        rows = split_sentences(content)
        findings = [{"id": "s0", "verdict": "violation", "replacement": "A tool."},
                    {"id": "s2", "verdict": "violation", "replacement": " Please verify."}]
        rewritten = apply_replacements(content, rows, findings)
        self.assertEqual(rewritten["spoken"], "A tool. Keep this. Please verify.")
        self.assertEqual(content["spoken"], "Best! Keep this. Guaranteed!")

    def test_empty_replacement_and_unknown_id_are_rejected(self):
        rows = scan_content({"spoken": "Best!"})
        for identifier, replacement in [("s0", ""), ("unknown", "A tool.")]:
            with self.assertRaises(ValueError):
                validate_review({"checked_ids": ["s0"], "findings": [{"id": identifier,
                    "verdict": "violation", "reason": "绝对化", "replacement": replacement}]}, rows)


if __name__ == "__main__":
    unittest.main()
