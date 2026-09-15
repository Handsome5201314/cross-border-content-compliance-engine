# -*- coding: utf-8 -*-
"""V3 API 端到端测试（FastAPI TestClient）。

覆盖 26 个端点的核心契约：
- 认证：register/login/logout/me
- 演示：demo/precomputed、value-board/params
- 生成：generate / stream / cancel
- 任务：recent / {id} / {id}/result
- 积分：balance / ledger
- 空间：stats / upload / files / file/{id}
- 管理：users / users/{id}/disable|enable / recharge / audit / task-ledger

数据库与存储隔离用独立临时目录；Pipeline 工厂打桩，不出网。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import dao.db as _db  # noqa: E402
from credits import pricing as _pricing  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import api.generation as _generation  # noqa: E402
import api.main as _api_main  # noqa: E402


# ── Pipeline 打桩（避免真实出网，且可控存活时长以便测试单 worker 守卫 / 取消）──
class FakePipeline:
    def __init__(self, *a, **k):
        self.cancelled = False
        self._release = threading.Event()

    def request_cancel(self):
        self.cancelled = True
        self._release.set()

    def run(self, progress_cb=None, item_cb=None):
        if progress_cb:
            progress_cb("selling", 1, 1, "生成售卖点")
        if item_cb:
            item_cb("tiktok", "en", {
                "localized": {"titles": ["Demo"]},
                "compliance": {"confirmed_findings": [], "safe_copy": {"titles": ["Ok"]}, "disclaimer": "draft"},
                "final_copy": {"titles": ["Final"]},
            })
        # 保持活跃 ~1s（或直至被取消），用于验证「同用户单 worker」守卫与取消
        self._release.wait(timeout=1.0)
        return {"meta": {"cancelled": self.cancelled}}


def _patch_pipeline():
    return patch.multiple(
        _generation,
        create_llm_client=lambda: object(),
        build_pipeline=lambda *a, **k: FakePipeline(),
    )


class ApiEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 独立临时数据库（在 import 之后、构造 TestClient 之前切换，确保与同进程其它测试隔离）
        cls.tmp = tempfile.mkdtemp(prefix="cce_api_")
        _db.Database.reset()
        _db.set_test_db_path(os.path.join(cls.tmp, "app.db"))
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "adminpass"
        # 显式进入 TestClient 上下文以触发 lifespan（seed_admin + recover_orphan_tasks）
        cls.user = TestClient(_api_main.app).__enter__()
        cls.admin = TestClient(_api_main.app).__enter__()
        # 注册并登录普通用户
        u_name = "tester_%d" % int(time.time())
        cls.user_name = u_name
        r = cls.user.post("/api/auth/register", json={"username": u_name, "password": "pw123456"})
        assert r.status_code == 201, r.text
        cls.user_id = r.json()["user_id"]
        # 管理员登录（seed_admin 已用 admin/adminpass 建种子管理员）
        ra = cls.admin.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
        assert ra.status_code == 200, ra.text
        cls._pp = _patch_pipeline()
        cls._pp.start()

    @classmethod
    def tearDownClass(cls):
        cls._pp.stop()
        try:
            cls.user.__exit__(None, None, None)
            cls.admin.__exit__(None, None, None)
        except Exception:
            pass

    @classmethod
    def _wait_idle(cls, uid, timeout=4.0):
        """等待该用户的生成 worker 完全退出（TestClient 同进程，可直接读注册表）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not _generation.has_active_worker(uid):
                return True
            time.sleep(0.02)
        return not _generation.has_active_worker(uid)

    # ── 认证 ──
    def test_auth_me_and_logout(self):
        me = self.user.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["username"], self.user_name)
        # 注册已发放 signup 奖励，且未被扣减（此前无生成）
        self.assertTrue(me.json()["credits"] >= _pricing.SIGNUP_BONUS, me.json())
        # 重复注册 → 409
        dup = self.user.post("/api/auth/register", json={"username": self.user_name, "password": "x"})
        self.assertEqual(dup.status_code, 409)
        # 错误密码 → 401
        bad = self.user.post("/api/auth/login", json={"username": self.user_name, "password": "wrong"})
        self.assertEqual(bad.status_code, 401)
        # 登出 → 200 且 me 变 401
        self.user.post("/api/auth/logout")
        self.assertEqual(self.user.get("/api/auth/me").status_code, 401)
        # 重新登录以便后续用例
        self.user.post("/api/auth/login", json={"username": self.user_name, "password": "pw123456"})

    # ── 演示 ──
    def test_demo_endpoints(self):
        r = self.user.get("/api/demo/precomputed")
        self.assertEqual(r.status_code, 200)
        self.assertIn("counts", r.json())
        p = self.user.get("/api/value-board/params")
        self.assertEqual(p.status_code, 200)
        self.assertIn("per_language", p.json())

    # ── 生成：成功路径 + SSE 终态 + 单 worker 守卫 ──
    def test_generate_flow_and_stream(self):
        self._wait_idle(self.user_id)
        body = {
            "product_name": "测试产品 A", "product_name_en": "Test A",
            "form": "软件", "deployment": "", "marketing_notes": "禁止编造",
            "target_markets": ["印尼", "泰国"], "languages": ["en", "id", "th"],
            "platforms": ["tiktok", "landing_page"], "compliance_level": "strict",
        }
        g = self.user.post("/api/generate", json=body)
        self.assertEqual(g.status_code, 201, g.text)
        task_id = g.json()["task_id"]
        self.assertGreater(g.json()["cost"], 0)
        # worker 已活跃（FakePipeline 阻塞 ~1s）
        self.assertTrue(_generation.has_active_worker(self.user_id))
        # 同用户再建 → 409（单 worker 守卫）
        g2 = self.user.post("/api/generate", json=body)
        self.assertEqual(g2.status_code, 409)
        # 等待任务自然完成到终态
        self._wait_idle(self.user_id)
        t = self.user.get("/api/tasks/%d" % task_id).json()
        self.assertIn(t["status"], ("completed", "cancelled", "failed"))
        # 订阅 SSE（worker 已退出 → 由落库状态合成终态事件）
        s = self.user.get("/api/generate/%d/stream" % task_id)
        self.assertEqual(s.status_code, 200)
        self.assertIn("text/event-stream", s.headers.get("content-type", ""))
        events = [ln[6:] for ln in s.text.split("\n\n") if ln.startswith("data: ")]
        parsed = [__import__("json").loads(e) for e in events]
        types = [e.get("type") for e in parsed]
        self.assertIn("bye", types)
        self.assertTrue(any(t in ("done", "cancelled", "failed") for t in types))

    def test_generate_cancel(self):
        self._wait_idle(self.user_id)
        body = {"product_name": "取消测试", "languages": ["en"], "platforms": ["tiktok"]}
        g = self.user.post("/api/generate", json=body)
        self.assertEqual(g.status_code, 201, g.text)
        task_id = g.json()["task_id"]
        # 立即取消（worker 仍处于阻塞窗口）
        c = self.user.post("/api/generate/%d/cancel" % task_id)
        self.assertEqual(c.status_code, 200, c.text)
        self._wait_idle(self.user_id)
        t = self.user.get("/api/tasks/%d" % task_id).json()
        self.assertEqual(t["status"], "cancelled")

    def test_generate_insufficient_credits(self):
        self._wait_idle(self.user_id)
        # 余额清零后再生成 → 402（pre_deduct 在 worker 启动前即失败）
        uid = self.user_id
        conn = _db.Database.get()
        with conn.transaction() as c:
            c.execute("UPDATE users SET credits = 0 WHERE user_id = ?", (uid,))
        body = {"product_name": "没钱测试", "languages": ["en"], "platforms": ["tiktok"]}
        g = self.user.post("/api/generate", json=body)
        self.assertEqual(g.status_code, 402, g.text)
        # 恢复余额
        with conn.transaction() as c:
            c.execute("UPDATE users SET credits = ? WHERE user_id = ?",
                      (_pricing.SIGNUP_BONUS, uid))

    def test_generate_validation(self):
        bad = self.user.post("/api/generate", json={"product_name": "x", "languages": [], "platforms": []})
        self.assertEqual(bad.status_code, 422)

    # ── 任务 ──
    def test_tasks_recent_detail_result(self):
        rec = self.user.get("/api/tasks/recent?limit=10")
        self.assertEqual(rec.status_code, 200)
        tasks = rec.json()["tasks"]
        self.assertTrue(len(tasks) >= 1)
        tid = tasks[0]["task_id"]
        det = self.user.get("/api/tasks/%d" % tid)
        self.assertEqual(det.status_code, 200)
        # 已完成任务可下载结果；未完成的 404 也接受
        res = self.user.get("/api/tasks/%d/result" % tid)
        self.assertIn(res.status_code, (200, 404))
        if res.status_code == 200:
            self.assertIn("attachment", res.headers.get("content-disposition", ""))

    # ── 积分 ──
    def test_credits_balance_ledger(self):
        b = self.user.get("/api/credits/balance")
        self.assertEqual(b.status_code, 200)
        self.assertIsInstance(b.json()["credits"], int)
        l = self.user.get("/api/credits/ledger?limit=50")
        self.assertEqual(l.status_code, 200)
        self.assertIn("ledger", l.json())

    # ── 空间 ──
    def test_space_stats_upload_files(self):
        st = self.user.get("/api/space/stats")
        self.assertEqual(st.status_code, 200)
        self.assertIn("uploads", st.json())
        # 上传
        up = self.user.post("/api/space/upload",
                            files={"file": ("hello.txt", b"hello world", "text/plain")})
        self.assertEqual(up.status_code, 200, up.text)
        # 列举 inputs
        fl = self.user.get("/api/space/files?kind=inputs")
        self.assertEqual(fl.status_code, 200)
        names = [f["name"] for f in fl.json()["files"]]
        self.assertTrue(any("hello" in n for n in names))
        # 下载
        dl = self.user.get("/api/space/file/%s?kind=inputs" % up.json()["file_id"])
        self.assertEqual(dl.status_code, 200)

    # ── 管理后台 ──
    def test_admin_endpoints(self):
        u = self.admin.get("/api/admin/users")
        self.assertEqual(u.status_code, 200)
        users = u.json()["users"]
        self.assertTrue(any(x["username"] == self.user_name for x in users))
        # 审计
        a = self.admin.get("/api/admin/audit?limit=50")
        self.assertEqual(a.status_code, 200)
        # 任务流水
        tl = self.admin.get("/api/admin/task-ledger?limit=50")
        self.assertEqual(tl.status_code, 200)
        # 充值
        rc = self.admin.post("/api/admin/recharge", json={
            "username": self.user_name, "amount": 500, "note": "测试充值"})
        self.assertEqual(rc.status_code, 200, rc.text)
        # 禁用 → 被禁用用户登录被拒
        uid_target = next(x["user_id"] for x in users if x["username"] == self.user_name)
        dis = self.admin.post("/api/admin/users/%d/disable" % uid_target)
        self.assertEqual(dis.status_code, 200)
        blocked = self.user.post("/api/auth/login", json={"username": self.user_name, "password": "pw123456"})
        self.assertEqual(blocked.status_code, 401)
        # 禁用自己 → 409
        self_admin = next(x["user_id"] for x in users if x["username"] == "admin")
        self.assertEqual(self.admin.post("/api/admin/users/%d/disable" % self_admin).status_code, 409)
        # 重新启用
        en = self.admin.post("/api/admin/users/%d/enable" % uid_target)
        self.assertEqual(en.status_code, 200)

    def test_admin_requires_admin_role(self):
        # 普通用户访问 admin 端点 → 403
        self.assertEqual(self.user.get("/api/admin/users").status_code, 403)

    def test_admin_recharge_unknown_user(self):
        r = self.admin.post("/api/admin/recharge", json={
            "username": "no_such_user_xyz", "amount": 10, "note": "x"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
