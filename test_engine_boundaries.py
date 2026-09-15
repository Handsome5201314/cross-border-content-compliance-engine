"""Offline regressions: synthetic credentials and an in-process OpenAI HTTP transport only."""
import base64
import json
import io
import os
import socket
import http.client
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI
from PIL import Image

from core import load_env
from core.llm_client import LLMClient, mask_key


def completion(usage=True):
    data = {"id": "response-test", "object": "chat.completion", "created": 1,
            "model": "test", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "{}"}}]}
    if usage:
        data["usage"] = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    return data


class BoundaryTests(unittest.TestCase):
    def client(self, handler):
        with patch("core.llm_client.load_env", return_value={}):
            client = LLMClient(api_key="synthetic-test-credential", base_url="https://gateway.invalid/v1")
        client.client.close()
        client.client = OpenAI(api_key="synthetic-test-credential", base_url=client.base_url,
                               max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(client.client.close)
        return client

    def test_explicit_credentials_do_not_read_file(self):
        with patch("core.llm_client.load_env", side_effect=AssertionError("file touched")):
            c = LLMClient(api_key="synthetic", base_url="https://gateway.invalid/v1")
            c.client.close()

    def test_partial_credentials_never_use_platform_key(self):
        with patch("core.llm_client.load_env", side_effect=AssertionError("file touched")):
            with self.assertRaises(RuntimeError):
                LLMClient(base_url="https://untrusted.invalid")

    def test_ui_gateway_policy_rejects_arbitrary_endpoints(self):
        from core.gateway_policy import validate_ui_gateway
        with patch.dict(os.environ, {}, clear=True):
            for url in ["http://127.0.0.1", "https://untrusted.invalid/v1",
                        "https://dashscope.aliyuncs.com@127.0.0.1/v1",
                        "https://dashscope.aliyuncs.com/v1?credential=private"]:
                with self.assertRaises(ValueError):
                    validate_ui_gateway(url)
            validate_ui_gateway("https://dashscope.aliyuncs.com/compatible-mode/v1")

    def test_schema_failure_does_not_echo_model_response(self):
        from core.agents import BaseAgent, SchemaError
        data = completion()
        data["choices"][0]["message"]["content"] = "synthetic-private-response"
        c = self.client(lambda r: httpx.Response(200, json=data))
        with self.assertRaises(SchemaError) as error:
            BaseAgent(c)._chat_json("JSON", "hello")
        self.assertNotIn("synthetic-private-response", str(error.exception))
        self.assertEqual(c.usage_summary()["total_tokens"], 10)

    def test_env_empty_override_and_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            path = Path(tmp) / "settings"
            path.write_text("HACKATHON_API_KEY=synthetic\nHACKATHON_BASE_URL=https://example.invalid\nUNRELATED=private\n")
            os.environ["HACKATHON_API_KEY"] = " "
            data = load_env(path)
            self.assertEqual(data["HACKATHON_API_KEY"], "")
            self.assertNotIn("UNRELATED", data)

    def test_env_only_never_opens_file(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, "read_text", side_effect=AssertionError):
            self.assertEqual(load_env(allow_file=False), {})

    def test_mask_has_no_key_characters(self):
        self.assertNotIn("synthetic", mask_key("synthetic-test-credential"))

    def test_missing_usage_is_unknown_and_fails(self):
        c = self.client(lambda r: httpx.Response(200, json=completion(False)))
        with self.assertRaises(RuntimeError):
            c.chat("test", [{"role": "user", "content": "hello"}])
        self.assertEqual(c.usage_summary()["unknown_usage_calls"], 1)
        self.assertFalse(c.usage_summary()["usage_complete"])
        self.assertEqual(len(c.attempt_records), 1)

    def test_invalid_usage_is_not_accepted(self):
        for value in [-1, "3", True, None]:
            data = completion()
            data["usage"]["prompt_tokens"] = value
            c = self.client(lambda r: httpx.Response(200, json=data))
            with self.assertRaises(RuntimeError):
                c.chat("test", [{"role": "user", "content": "hello"}])

    def test_timeout_not_retried_and_error_redacted(self):
        requests = []
        def handler(req):
            requests.append(req)
            raise httpx.ReadTimeout("synthetic-private-detail", request=req)
        c = self.client(handler)
        with self.assertRaises(RuntimeError) as error:
            c.chat("test", [{"role": "user", "content": "hello"}])
        self.assertEqual(len(requests), 1)
        self.assertNotIn("synthetic-private-detail", str(error.exception))
        self.assertEqual(c.usage_summary()["unknown_usage_calls"], 1)

    def test_parameter_fallback_counts_toward_two_attempt_limit(self):
        requests = []
        def handler(req):
            requests.append(json.loads(req.content))
            if len(requests) == 1:
                return httpx.Response(400, json={"error": {"message": "unknown parameter enable_thinking"}})
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        c = self.client(handler)
        with self.assertRaises(RuntimeError):
            c.chat("qwen3.7-plus", [{"role": "user", "content": "hello"}])
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(c.attempt_records), 2)
        self.assertNotIn("enable_thinking", requests[-1])

    def test_permanent_error_and_structured_content_do_not_retry(self):
        requests = []
        c = self.client(lambda r: (requests.append(r) or httpx.Response(401, json={"error": {"message": "synthetic-private-detail"}})))
        with self.assertRaises(RuntimeError) as error:
            c.chat("test", [{"role": "user", "content": "hello"}])
        self.assertNotIn("synthetic-private-detail", str(error.exception))
        self.assertEqual(len(requests), 1)
        with self.assertRaises(ValueError):
            c.chat("test", [{"role": "user", "content": [{"type": "text", "text": "hello"}]}])
        self.assertEqual(len(requests), 1)

    def test_task_isolation_and_deadline(self):
        requests = []
        c = self.client(lambda r: (requests.append(r) or httpx.Response(200, json=completion())))
        a, b = c.new_task(), c.new_task()
        a.chat("test", [{"role": "user", "content": "hello"}])
        self.assertEqual(b.usage_summary()["total_tokens"], 0)
        self.assertNotEqual(a.task_id, b.task_id)
        b.set_deadline(time.monotonic() - 1)
        with self.assertRaises(RuntimeError):
            b.chat("test", [{"role": "user", "content": "hello"}])
        self.assertEqual(len(requests), 1)

    def test_image_failure_does_not_generate_again_or_overwrite(self):
        for payload in [{"url": "http://127.0.0.1/private"}, {"b64_json": "!!!"}]:
            requests = []
            c = self.client(lambda r: (requests.append(r) or httpx.Response(200, json={"created": 1, "data": [payload]})))
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "image.png"
                path.write_bytes(b"previous")
                self.assertFalse(c.generate_image("a flower", path)["ok"])
                self.assertEqual(path.read_bytes(), b"previous")
            self.assertEqual(len(requests), 1)
            self.assertEqual(len(c.attempt_records), 1)

    def test_successful_call_and_default_output_limit(self):
        requests = []
        c = self.client(lambda r: (requests.append(json.loads(r.content)) or httpx.Response(200, json=completion())))
        content, record = c.chat("test", [{"role": "user", "content": "hello"}], tag="one")
        self.assertEqual((content, record["total_tokens"]), ("{}", 5))
        self.assertGreater(requests[0]["max_tokens"], 0)
        self.assertTrue(c.usage_summary()["usage_complete"])

    def test_atomic_concurrent_output(self):
        from core.output_io import atomic_write
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            payloads = [str(i).encode() * 10000 for i in range(12)]
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(lambda data: atomic_write(path, data), payloads))
            self.assertIn(path.read_bytes(), payloads)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["report.json"])

    def test_atomic_publish_failure_preserves_old_output(self):
        from core.output_io import atomic_write
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_bytes(b"old")
            with patch("core.output_io.os.replace", side_effect=OSError("busy")):
                with self.assertRaises(OSError):
                    atomic_write(path, b"new")
            self.assertEqual(path.read_bytes(), b"old")
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_pipeline_forks_client_and_retains_rerun_usage(self):
        from core.pipeline import Pipeline
        product = json.loads((Path(__file__).parent / "samples/product_medical_appliance.json").read_text(encoding="utf-8"))
        c = self.client(lambda r: httpx.Response(200, json=completion()))
        first = Pipeline(c, product, ["en"], ["tiktok"])
        first.client.chat("test", [{"role": "user", "content": "hello"}])
        result = {"results": [], "meta": {"platforms": ["tiktok"], "languages": ["en"]}}
        first.finalize(result)
        second = Pipeline(c, product, ["en"], ["tiktok"])
        self.assertEqual(second.client.usage_summary()["total_tokens"], 0)
        second.client.chat("test", [{"role": "user", "content": "hello"}])
        second.finalize(result)
        second.finalize(result)
        self.assertEqual(result["usage"]["total_tokens"], 10)
        self.assertEqual(len(result["usage_runs"]), 2)

    def test_cancel_after_response_blocks_followup(self):
        c = self.client(lambda r: httpx.Response(200, json=completion()))
        c.chat("test", [{"role": "user", "content": "hello"}])
        c.cancel()
        with self.assertRaises(RuntimeError):
            c.chat("test", [{"role": "user", "content": "hello"}])
        self.assertEqual(c.usage_summary()["attempted_calls"], 1)
        self.assertEqual(c.usage_summary()["total_tokens"], 5)

    def test_image_valid_png_is_published_once(self):
        data = io.BytesIO()
        Image.new("RGB", (2, 2)).save(data, format="PNG")
        encoded = base64.b64encode(data.getvalue()).decode("ascii")
        c = self.client(lambda r: httpx.Response(200, json={"created": 1, "data": [{"b64_json": encoded}]}))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "image.png"
            self.assertTrue(c.generate_image("a flower", path)["ok"])
            self.assertEqual(path.read_bytes(), data.getvalue())
        self.assertEqual(c.usage_summary()["attempted_calls"], 1)
        self.assertFalse(c.usage_summary()["usage_complete"])

    def test_private_image_destinations_are_blocked(self):
        from core.image_io import download_image
        for url in ["file:///private", "http://example.com/a", "https://user:pass@example.com/a",
                    "https://example.com:444/a", "https://example.com/\\a", "https://example.com/a\n"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                download_image(url)
        for ip in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"]:
            with patch("core.image_io.socket.getaddrinfo", return_value=[(2, 1, 6, "", (ip, 443))]), \
                    patch("core.image_io._PinnedHTTPS") as conn:
                with self.assertRaises(ValueError):
                    download_image("https://example.com/image")
                conn.assert_not_called()

    def test_image_redirect_and_oversize_rejected_before_read(self):
        from core.image_io import download_image, MAX_IMAGE_BYTES
        for status, length in [(302, "1"), (200, str(MAX_IMAGE_BYTES + 1))]:
            with patch("core.image_io.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]), \
                    patch("core.image_io._PinnedHTTPS") as conn:
                response = conn.return_value.getresponse.return_value
                response.status = status
                response.getheader.return_value = length
                with self.assertRaises(ValueError):
                    download_image("https://example.com/image")
                response.read1.assert_not_called()
                conn.return_value.close.assert_called_once()

    def test_https_download_valid_image_uses_real_http_parser(self):
        from core.image_io import download_image
        data = io.BytesIO()
        Image.new("RGB", (2, 2)).save(data, format="PNG")
        payload = data.getvalue()
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        writer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload)
        response = http.client.HTTPResponse(reader)
        response.begin()
        self.addCleanup(response.close)
        with patch("core.image_io.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]), \
                patch("core.image_io._PinnedHTTPS") as conn:
            conn.return_value.getresponse.return_value = response
            self.assertEqual(download_image("https://example.com/image"), payload)

    def test_pipeline_deadline_blocks_later_stages(self):
        from core.pipeline import Pipeline
        product = json.loads((Path(__file__).parent / "samples/product_medical_appliance.json").read_text(encoding="utf-8"))
        requests = []
        def handler(req):
            requests.append(req)
            pipeline.client.set_deadline(time.monotonic() - 1)
            return httpx.Response(200, json=completion())
        c = self.client(handler)
        pipeline = Pipeline(c, product, ["en"], ["tiktok"])
        result = pipeline.run()
        self.assertEqual(len(requests), 1)
        self.assertEqual(result["usage"]["total_tokens"], 5)
        self.assertEqual(result["counts"]["delivered"], 0)

    def test_provider_metadata_cannot_bypass_text_gate(self):
        requests = []
        c = self.client(lambda r: (requests.append(r) or httpx.Response(200, json=completion())))
        for model in ["Patient: SYNTHETIC_PERSON", "bad\nmodel", "", "a" * 200]:
            with self.assertRaises(ValueError):
                c.chat(model, [{"role": "user", "content": "hello"}])
            with self.assertRaises(ValueError):
                c.generate_image("a flower", Path("unused.png"), model=model)
        with self.assertRaises(ValueError):
            c.chat("test", [{"role": "user", "content": "hello"}], temperature="private")
        self.assertEqual(requests, [])

    def test_invalid_review_values_do_not_appear_in_errors(self):
        from core.agents import validate_compliance_response, SchemaError
        for data in [
            {"risk_level": "synthetic-private-response"},
            {"risk_level": "low", "signal_verdicts": [{"term": "synthetic-private-response"}]},
            {"risk_level": "low", "signal_verdicts": [], "llm_findings": [{"extra": "synthetic-private-response"}]},
            {"risk_level": "low", "signal_verdicts": [], "llm_findings": [{"term": "word", "severity": "synthetic-private-response"}]},
        ]:
            with self.assertRaises(SchemaError) as error:
                validate_compliance_response(data)
            self.assertNotIn("synthetic-private-response", str(error.exception))

    def test_work_package_error_does_not_echo_schema_instance(self):
        from jsonschema.exceptions import ValidationError
        from core.package_cli import run_batch
        product = json.loads((Path(__file__).parent / "samples/product_medical_appliance.json").read_text(encoding="utf-8"))
        c = self.client(lambda r: httpx.Response(200, json=completion()))
        with patch("core.package_cli.SiteAgent.run", side_effect=ValidationError("synthetic-private-response")):
            result = run_batch(product, "site", ["en"], c)
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertNotIn("synthetic-private-response", result["results"][0]["error"])

    def test_default_sdk_does_not_follow_redirects(self):
        with patch("core.llm_client.load_env", side_effect=AssertionError("file touched")):
            c = LLMClient(api_key="synthetic", base_url="https://gateway.invalid/v1")
            self.addCleanup(c.client.close)
            self.assertFalse(c.client._client.follow_redirects)


if __name__ == "__main__":
    unittest.main()
