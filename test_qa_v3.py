# -*- coding: utf-8 -*-
"""QA 对抗测试 V3 API 面（software-qa-engineer / 严过关）。

针对全新攻击面（FastAPI 26 端点 + 签名 Cookie cce_session + SSE），覆盖：
- IDOR 跨用户越权（任务详情/结果/文件下载）
- admin 端点越权（普通用户 403；伪造 role=admin 但无 SECRET 签名 → 401）
- 会话安全（篡改 Cookie / 过期签名 / 无 Cookie → 401；登出后旧 Cookie 复用）
- 免登录端点边界（demo/value-board 可访问；/api/generate 未登录 401）
- 上传安全（路径穿越文件名被中和）
- SSE 未授权（未登录/非属主拒绝）
- 金额类（普通用户充值 → 403）

运行：C:/Python314/python.exe -m unittest test_qa_v3 -v
方式：FastAPI TestClient 进程内；FakePipeline 打桩不出网；SQLite 用临时文件。
"""
import json
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

from fastapi.testclient import TestClient  # noqa: E402

import api.generation as _generation  # noqa: E402
import api.main as _api_main  # noqa: E402
import api.session as _session  # noqa: E402

from itsdangerous import TimestampSigner  # noqa: E402

SECRET = "qa-test-secret-not-leaked"


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
        self._release.wait(timeout=1.0)
        return {"meta": {"cancelled": self.cancelled}}


def _patch_pipeline():
    return patch.multiple(
        _generation,
        create_llm_client=lambda: object(),
        build_pipeline=lambda *a, **k: FakePipeline(),
    )


class V3AdversarialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="cce_qa3_")
        _db.Database.reset()
        _db.set_test_db_path(os.path.join(cls.tmp, "app.db"))
        # 已知 SESSION_SECRET，便于构造「无 SECRET 签名」的伪造 Cookie 对照
        os.environ["SESSION_SECRET"] = SECRET
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "adminpass"
        _session._signer = None  # 强制用上面的 SECRET 重建签名器

        cls._pp = _patch_pipeline()
        cls._pp.start()
        cls.userA = TestClient(_api_main.app).__enter__()
        cls.userB = TestClient(_api_main.app).__enter__()
        cls.admin = TestClient(_api_main.app).__enter__()

        # A 注册登录
        ra = cls.userA.post("/api/auth/register", json={"username": "qa_a", "password": "pwA123456"})
        assert ra.status_code == 201, ra.text
        cls.uidA = ra.json()["user_id"]
        # B 注册登录
        rb = cls.userB.post("/api/auth/register", json={"username": "qa_b", "password": "pwB123456"})
        assert rb.status_code == 201, rb.text
        cls.uidB = rb.json()["user_id"]
        # 管理员登录
        ra2 = cls.admin.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
        assert ra2.status_code == 200, ra2.text

    @classmethod
    def tearDownClass(cls):
        cls._pp.stop()
        for c in (cls.userA, cls.userB, cls.admin):
            try:
                c.__exit__(None, None, None)
            except Exception:
                pass
        os.environ.pop("SESSION_SECRET", None)

    # -------------------------------------------------- 工具
    @classmethod
    def _cancel_active(cls, client):
        """清理可能残留的进行中任务，避免「每用户单 worker」409 互相阻塞。"""
        try:
            rec = client.get("/api/tasks/recent")
            if rec.status_code != 200:
                return
            for t in rec.json().get("tasks", []):
                if t.get("status") in ("pending", "running"):
                    client.post("/api/generate/%d/cancel" % int(t["task_id"]))
        except Exception:
            pass

    @classmethod
    def _make_task(cls, client, uid):
        # 先清理该用户残留的进行中任务（状态泄漏：上一用例可能留下活跃任务）
        cls._cancel_active(client)
        # 等待残留 worker 真正退出（cancel 后最多 ~1s 收尾，否则 create 会 409）
        deadline = time.time() + 4.0
        while time.time() < deadline and _generation.has_active_worker(uid):
            time.sleep(0.02)
        body = {"product_name": "QA产品", "product_name_en": "QA", "languages": ["en", "ar"],
                "platforms": ["tiktok"], "compliance_level": "strict"}
        g = client.post("/api/generate", json=body)
        assert g.status_code == 201, g.text
        return g.json()["task_id"]

    @classmethod
    def _wait_idle(cls, uid, timeout=4.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not _generation.has_active_worker(uid):
                return
            time.sleep(0.02)

    def _forged_cookie(self, uid, role, secret="WRONG-SECRET"):
        """用「非服务端 SECRET」签发的伪造 Cookie（签名必失败）。"""
        payload = json.dumps({"uid": int(uid), "role": str(role)}).encode()
        return TimestampSigner(secret).sign(payload).decode()

    # ================================================== IDOR 跨用户越权
    def test_idor_task_detail(self):
        tid = self._make_task(self.userA, self.uidA)
        self._wait_idle(self.uidA)
        # B 访问 A 的任务详情 → 必须 404（不泄露存在性）
        r = self.userB.get("/api/tasks/%d" % tid)
        self.assertEqual(r.status_code, 404, r.text)
        # A 自己访问 → 200
        self.assertEqual(self.userA.get("/api/tasks/%d" % tid).status_code, 200)

    def test_idor_task_result(self):
        tid = self._make_task(self.userA, self.uidA)
        self._wait_idle(self.uidA)
        # A 的结果下载（终态合成）
        ra = self.userA.get("/api/tasks/%d/result" % tid)
        self.assertIn(ra.status_code, (200, 404))
        # B 访问 A 的结果 → 404
        rb = self.userB.get("/api/tasks/%d/result" % tid)
        self.assertEqual(rb.status_code, 404, rb.text)

    def test_idor_file_download(self):
        # A 上传文件
        up = self.userA.post("/api/space/upload",
                             files={"file": ("a_secret.txt", b"secret-a", "text/plain")})
        self.assertEqual(up.status_code, 200, up.text)
        fid = up.json()["file_id"]
        # A 能下载
        self.assertEqual(self.userA.get("/api/space/file/%s?kind=inputs" % fid).status_code, 200)
        # B 用同一 file_id 下载 → 只能落在 B 自己的目录（404，拿不到 A 的内容）
        rb = self.userB.get("/api/space/file/%s?kind=inputs" % fid)
        self.assertEqual(rb.status_code, 404, rb.text)
        # 直接枚举他人的文件名空间也只作用于自身目录
        self.assertNotEqual(rb.content, b"secret-a")

    def test_idor_sse_stream_non_owner(self):
        tid = self._make_task(self.userA, self.uidA)
        # B 订阅 A 的任务流 → 404
        r = self.userB.get("/api/generate/%d/stream" % tid)
        self.assertEqual(r.status_code, 404, r.text)

    # ================================================== admin 端点越权
    def test_admin_endpoints_forbidden_for_normal_user(self):
        eps = ["/api/admin/users", "/api/admin/audit", "/api/admin/task-ledger"]
        for ep in eps:
            self.assertEqual(self.userA.get(ep).status_code, 403, ep)
        # 写操作也 403
        self.assertEqual(
            self.userA.post("/api/admin/recharge",
                            json={"username": "qa_a", "amount": 10, "note": "x"}).status_code, 403)
        self.assertEqual(
            self.userA.post("/api/admin/users/%d/disable" % self.uidB).status_code, 403)

    def test_admin_forged_role_cookie_rejected(self):
        # 用「不知道 SECRET」签发的 role=admin Cookie → 签名失败 → 401
        forged = self._forged_cookie(self.uidB, "admin", secret="WRONG-SECRET")
        c = TestClient(_api_main.app)
        r = c.get("/api/admin/users", cookies={"cce_session": forged})
        self.assertEqual(r.status_code, 401, r.text)

    def test_admin_valid_signature_with_user_role_is_403(self):
        # 真实签名但 role=user → 403（不是 401，也不是 200）
        good_user = _session.issue_session(self.uidB, "user")
        c = TestClient(_api_main.app)
        r = c.get("/api/admin/users", cookies={"cce_session": good_user})
        self.assertEqual(r.status_code, 403, r.text)

    # ================================================== 会话安全
    def test_no_cookie_401(self):
        c = TestClient(_api_main.app)
        self.assertEqual(c.get("/api/tasks/recent").status_code, 401)
        self.assertEqual(c.get("/api/credits/balance").status_code, 401)
        self.assertEqual(c.get("/api/space/stats").status_code, 401)

    def test_tampered_cookie_401(self):
        # 取 A 的合法 Cookie，篡改一个字符 → 签名失败 → 401
        good = _session.issue_session(self.uidA, "user")
        tampered = good[:-1] + ("A" if good[-1] != "A" else "B")
        c = TestClient(_api_main.app)
        r = c.get("/api/tasks/recent", cookies={"cce_session": tampered})
        self.assertEqual(r.status_code, 401, r.text)

    def test_expired_signature_401(self):
        # 构造 TTL 之外的过期时间戳（用已知 SECRET 正确签名，仅时间戳陈旧）→ SignatureExpired → 401
        from itsdangerous.encoding import base64_encode, int_to_bytes
        sep = "."
        payload = json.dumps({"uid": self.uidA, "role": "user"}).encode()
        old_ts = int(time.time()) - 8 * 86400
        rv = payload + sep.encode() + base64_encode(int_to_bytes(old_ts))
        mac = TimestampSigner(SECRET).get_signature(rv)
        expired_token = (rv + sep.encode() + mac).decode()
        c = TestClient(_api_main.app)
        r = c.get("/api/tasks/recent", cookies={"cce_session": expired_token})
        self.assertEqual(r.status_code, 401, r.text)

    def test_logout_invalidates_old_cookie(self):
        """要求：登出后旧 Cookie 必须失效（401）。当前为无状态签名 Cookie，需服务端吊销才成立。"""
        c = TestClient(_api_main.app)
        login = c.post("/api/auth/login", json={"username": "qa_a", "password": "pwA123456"})
        self.assertEqual(login.status_code, 200)
        old_cookie = login.cookies.get("cce_session")
        self.assertIsNotNone(old_cookie)
        # 登出
        self.assertEqual(c.post("/api/auth/logout").status_code, 200)
        # 用旧 Cookie 再次访问 → 期望 401（服务端已吊销）
        r = c.get("/api/tasks/recent", cookies={"cce_session": old_cookie})
        self.assertEqual(r.status_code, 401,
                         "登出后旧 Cookie 仍可复用（无服务端会话吊销）——应为 401")

    # ================================================== 免登录端点边界
    def test_public_endpoints_no_login(self):
        c = TestClient(_api_main.app)
        self.assertEqual(c.get("/api/demo/precomputed").status_code, 200)
        self.assertEqual(c.get("/api/value-board/params").status_code, 200)

    def test_generate_requires_login(self):
        c = TestClient(_api_main.app)
        body = {"product_name": "x", "languages": ["en"], "platforms": ["tiktok"]}
        self.assertEqual(c.post("/api/generate", json=body).status_code, 401)
        # SSE 未登录 → 401
        self.assertEqual(c.get("/api/generate/1/stream").status_code, 401)

    # ================================================== 上传安全
    def test_upload_path_traversal_neutralized(self):
        # 文件名含穿越成分 → 必须被中和在用户自身目录内（不逃逸）
        up = self.userA.post("/api/space/upload",
                             files={"file": ("../../evil.txt", b"pwned", "text/plain")})
        self.assertEqual(up.status_code, 200, up.text)
        fid = up.json()["file_id"]
        # 穿越成分必须被中和：文件名不得含目录分隔符或父目录符号
        self.assertNotIn("..", fid)
        self.assertNotIn("/", fid)
        self.assertNotIn("\\", fid)
        # 文件名最终应落回 basename（secure_filename 剥离路径成分），且不含路径前缀
        self.assertTrue(fid.endswith("evil.txt") or fid == "evil.txt",
                        "文件名未回到用户目录内的 basename：%s" % fid)
        # 该文件只能从 A 自己的 inputs 目录下载，内容正确（未逃逸到他人目录）
        dl = self.userA.get("/api/space/file/%s?kind=inputs" % fid)
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl.content, b"pwned")

    def test_upload_empty_rejected(self):
        up = self.userA.post("/api/space/upload",
                             files={"file": ("empty.txt", b"", "text/plain")})
        self.assertEqual(up.status_code, 422, up.text)

    # ================================================== 金额类越权
    def test_recharge_by_normal_user_forbidden(self):
        r = self.userB.post("/api/admin/recharge",
                            json={"username": "qa_a", "amount": 999, "note": "恶意充值"})
        self.assertEqual(r.status_code, 403, r.text)
        # 确认 B 并未成功给 A 充值
        bal = self.userA.get("/api/credits/balance")
        self.assertLessEqual(bal.json()["credits"], 1000)


if __name__ == "__main__":
    unittest.main()
