# -*- coding: utf-8 -*-
"""V3 边界测试（FastAPI TestClient）：替换原 Streamlit AppTest。

保留 V2 三条边界精神：
1. 首页 / 静态资源 / 预生成 demo 不读凭证、不调用 provider，免登录可用；
2. 绝不读取任何平台保留头（X-modelscope-* / X-studio-* / Authorization）
   来选网关——V3 压根不接收这类头，自定义网关无从注入；
3. 生成接口无会话直接拦截（401），永不波及凭证读取路径。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import dao.db as _db  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import api.main as _api_main  # noqa: E402


class AppBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 独立测试数据库 + 固定种子管理员凭据（避免与同进程其它测试串库）
        cls.tmp = tempfile.mkdtemp(prefix="cce_test_")
        _db.Database.reset()
        _db.set_test_db_path(os.path.join(cls.tmp, "app.db"))
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "adminpass"
        # 进入上下文触发 lifespan（seed_admin + recover_orphan_tasks），贴近真实启动
        cls.client = TestClient(_api_main.app).__enter__()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.client.__exit__(None, None, None)
        except Exception:
            pass

    def test_home_static_and_assets_load_without_auth(self):
        # 首页（index.html）与静态资源免登录可达，且页面含标题
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<title>", r.text)
        r2 = self.client.get("/assets/icons.js")
        self.assertEqual(r2.status_code, 200)
        # 访客无会话：/api/auth/me 直接 401，不进入任何凭证读取路径
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 401)

    def test_generate_is_blocked_without_session(self):
        # 无会话 POST /api/generate → 401（先于任何引擎/凭证读取）
        r = self.client.post("/api/generate", json={
            "product_name": "测试产品", "languages": ["en"], "platforms": ["tiktok"],
        })
        self.assertEqual(r.status_code, 401)

    def test_precomputed_demo_available_without_auth(self):
        # 访客价值看板 / 预生成产物免登录、不写用户目录
        r = self.client.get("/api/demo/precomputed")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("counts", body)
        params = self.client.get("/api/value-board/params")
        self.assertEqual(params.status_code, 200)
        self.assertIn("per_language", params.json())

    def test_platform_reserved_headers_are_not_consumed(self):
        # V3 全部端点不读取任何平台保留头；即便携带也当成普通请求处理。
        # 无会话下自定义网关头不会改变 401 结果（网关无从注入）。
        headers = {
            "X-modelscope-xxx": "https://untrusted.invalid/v1",
            "X-studio-token": "synthetic",
            "Authorization": "Bearer synthetic",
        }
        me = self.client.get("/api/auth/me", headers=headers)
        self.assertEqual(me.status_code, 401)
        gen = self.client.post("/api/generate", json={"product_name": "x"}, headers=headers)
        self.assertEqual(gen.status_code, 401)


if __name__ == "__main__":
    unittest.main()
