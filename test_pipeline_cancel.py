# -*- coding: utf-8 -*-
"""Pipeline 取消标志单测（ARCHITECTURE_V2.md §8 / 批次 F1）。

验证：调用 request_cancel() 后 run() 提前结束、未开始的组合标记 skipped、meta.cancelled=True，
且无残留线程 / 不崩溃。复用 test_engine 的 MockTransport 客户端（返回合法空 JSON）。
"""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI

from core import load_env
from core.llm_client import LLMClient
from core.pipeline import Pipeline


def completion(usage=True):
    data = {"id": "response-test", "object": "chat.completion", "created": 1,
            "model": "test", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "{}"}}]}
    if usage:
        data["usage"] = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    return data


class PipelineCancelTests(unittest.TestCase):
    def client(self, handler):
        with patch("core.llm_client.load_env", return_value={}):
            client = LLMClient(api_key="synthetic-test-credential", base_url="https://gateway.invalid/v1")
        client.client.close()
        client.client = OpenAI(api_key="synthetic-test-credential", base_url=client.base_url,
                               max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(client.client.close)
        return client

    def test_request_cancel_before_run_ends_early_and_marks_cancelled(self):
        product = json.loads(
            (Path(__file__).parent / "samples/product_medical_appliance.json").read_text(encoding="utf-8"))
        c = self.client(lambda r: httpx.Response(200, json=completion()))
        pipeline = Pipeline(c, product, ["en", "ar"], ["tiktok", "instagram"])
        # run 之前请求取消：run 应在阶段 3 将所有待执行组合标记 skipped，并置 meta.cancelled。
        pipeline.request_cancel()
        result = pipeline.run()
        self.assertTrue(result["meta"].get("cancelled"))
        self.assertEqual(len(result["results"]), 4)  # 2 语种 × 2 平台
        for item in result["results"]:
            self.assertEqual(item["status"], "skipped_deadline")
        self.assertEqual(result["counts"]["delivered"], 0)
        # 取消后不应再发起新的 chat 调用（仅阶段1/2 的极少几次被 CallStopped 中断）
        self.assertLessEqual(c.usage_summary()["total_tokens"], 10)

    def test_no_cancel_runs_normally(self):
        product = json.loads(
            (Path(__file__).parent / "samples/product_medical_appliance.json").read_text(encoding="utf-8"))
        c = self.client(lambda r: httpx.Response(200, json=completion()))
        pipeline = Pipeline(c, product, ["en"], ["tiktok"])
        result = pipeline.run()  # 未取消，应正常结束
        self.assertFalse(result["meta"].get("cancelled"))


if __name__ == "__main__":
    unittest.main()
