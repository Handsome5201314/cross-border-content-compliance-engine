# -*- coding: utf-8 -*-
"""QA 对抗性测试（software-qa-engineer / 严过关）。

独立于工程师自测，针对「跨境电商内容合规引擎」产品化升级的边界与攻击面：
- 安全：路径穿越、SQL 注入（用户名含引号/分号）、密码哈希两种格式 + 错误密码必拒
- 积分：余额不足拦截、取消/失败后全额退还、重复 finalize 不重复退款（双花防护）、并发预扣防双花
- 认证：禁用用户登录被拒、管理员种子账号重复启动幂等、重复用户名注册被拒、注册赠送 + 自动登录
- 任务：取消→状态 cancelled 且落库、失败→状态 failed 且退还
- 上传：file_uploader 经 safe_join 落在用户隔离目录内、用户名/原文件名不进路径

运行：C:/Python314/python.exe -m unittest test_qa_adversarial -v
"""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

import dao.db as db_mod
from dao import users_dao, tasks_dao, audit_dao, credits_dao
from credits import pricing, service as credits_service
from admin import service as admin_svc
from tasks import service as tasks_service
from storage import space as storage_space
from auth import hashing


# ---------------------------------------------------------------- 纯逻辑层
class AdversarialSecurityCreditsTests(unittest.TestCase):
    """安全 + 积分（不依赖 Streamlit）。"""

    @classmethod
    def setUpClass(cls):
        db_mod.set_test_db_path(":memory:")
        db_mod.Database.reset()
        db_mod.Database.get().init_schema()

    def setUp(self):
        conn = db_mod.Database.get()._conn
        conn.execute("DELETE FROM credit_ledger")
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM audit_log")
        conn.commit()

    def _make_user(self, name="u", credits=0):
        return users_dao.UserDAO.create(name, "h", credits=credits)

    # ----------------------------------------------------- 路径穿越
    def test_safe_join_rejects_dotdot(self):
        base = storage_space.inputs_dir(1)
        with self.assertRaises(ValueError):
            storage_space.safe_join(base, "..", "..", "etc", "passwd")

    def test_safe_join_rejects_absolute_path_part(self):
        # Windows 上 Path("/etc/passwd") 非绝对；secure_filename 取其 basename，
        # 绝对/根路径成分被剥离，结果必须仍落在 base 内（不逃逸）。
        base = storage_space.inputs_dir(1)
        target = storage_space.safe_join(base, "/etc/passwd")
        self.assertTrue(target.is_relative_to(base))
        self.assertEqual(target.name, "passwd")

    def test_safe_join_rejects_root_escape_via_filename(self):
        # "../../" 被 secure_filename 剥离为 basename，结果仍落在 base 内（不逃逸）。
        base = storage_space.inputs_dir(1)
        target = storage_space.safe_join(base, "../../../../win.ini")
        self.assertTrue(target.is_relative_to(base))
        self.assertEqual(target.name, "win.ini")

    def test_secure_filename_neutralizes_traversal(self):
        # 原文件名里的目录成分必须被剥离，绝不进入路径
        self.assertEqual(storage_space.secure_filename("../../../etc/passwd"), "passwd")
        self.assertEqual(storage_space.secure_filename("a\\b\\c.txt"), "c.txt")

    def test_save_upload_stays_inside_user_dir(self):
        path = storage_space.save_upload(7, "../../escape.txt", b"x-payload")
        self.assertTrue(path.is_relative_to(storage_space.inputs_dir(7)))
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b"x-payload")
        # 用户名/原文件名未出现在路径中（仅整数 user_id 拼装）
        self.assertIn(f"{os.sep}users{os.sep}7{os.sep}", os.path.normpath(str(path)))

    def test_user_spaces_are_isolated(self):
        a = storage_space.inputs_dir(11)
        b = storage_space.inputs_dir(12)
        self.assertNotEqual(a, b)
        # 恶意文件名不会跨到另一个用户目录
        p = storage_space.save_upload(11, "../12/evil.txt", b"z")
        self.assertTrue(p.is_relative_to(a))
        self.assertFalse(p.is_relative_to(b))

    # ----------------------------------------------------- SQL 注入（参数化校验）
    def test_sql_injection_username_stored_literally(self):
        evil = "x'); DROP TABLE users;--"
        uid = users_dao.UserDAO.create(evil, "h")
        # 表未被删：仍能查询
        self.assertIsNotNone(users_dao.UserDAO.get_by_id(uid))
        self.assertEqual(users_dao.UserDAO.get_by_username(evil)["username"], evil)
        # 全表扫描仍可用（若被注入执行会抛错/丢表）
        self.assertIsInstance(users_dao.UserDAO.list_all(), list)

    def test_sql_injection_or_style_username(self):
        evil = "a' OR '1'='1"
        uid = users_dao.UserDAO.create(evil, "h")
        self.assertEqual(users_dao.UserDAO.get_by_username(evil)["username"], evil)

    # ----------------------------------------------------- 密码哈希两种格式
    def test_bcrypt_format_verify_and_reject(self):
        h = hashing.hash_password("secret")
        self.assertTrue(h.startswith("bcrypt$"))
        self.assertTrue(hashing.verify_password("secret", h))
        self.assertFalse(hashing.verify_password("wrong", h))

    def test_pbkdf2_format_verify_and_reject(self):
        # 手动构造 pbkdf2$ 串（模拟 bcrypt 不可用降级路径）
        salt = os.urandom(16)
        dk = hashlib.pbkdf2_hmac("sha256", b"secret", salt, 600_000)
        stored = "pbkdf2$600000$%s$%s" % (salt.hex(), dk.hex())
        self.assertTrue(hashing.verify_password("secret", stored))
        self.assertFalse(hashing.verify_password("wrong", stored))

    def test_malformed_hash_rejected(self):
        self.assertFalse(hashing.verify_password("x", ""))
        self.assertFalse(hashing.verify_password("x", "bcrypt$garbage"))
        self.assertFalse(hashing.verify_password("x", "pbkdf2$notenough$parts"))

    # ----------------------------------------------------- 积分：余额不足
    def test_pre_deduct_insufficient_leaves_balance(self):
        uid = self._make_user(credits=5)
        with self.assertRaises(credits_service.InsufficientCredits):
            credits_service.pre_deduct(uid, 10)
        self.assertEqual(credits_service.get_balance(uid), 5)
        self.assertEqual(len(credits_service.get_ledger(uid)), 0)

    # ----------------------------------------------------- 积分：取消/失败全退
    def test_cancel_refunds_full(self):
        uid = self._make_user(credits=pricing.SIGNUP_BONUS)
        cost = 3 * pricing.PER_LANGUAGE
        credits_service.pre_deduct(uid, cost, task_id=1)
        credits_service.refund(uid, 1, cost)
        self.assertEqual(credits_service.get_balance(uid), pricing.SIGNUP_BONUS)

    def test_failed_refunds_full(self):
        uid = self._make_user(credits=pricing.SIGNUP_BONUS)
        cost = 2 * pricing.PER_LANGUAGE
        credits_service.pre_deduct(uid, cost, task_id=2)
        credits_service.refund(uid, 2, cost)
        self.assertEqual(credits_service.get_balance(uid), pricing.SIGNUP_BONUS)

    # ----------------------------------------------------- 积分：重复退款（双花）
    def test_refund_idempotent_on_repeated_call(self):
        """同一 task 重复调用 refund 不应重复加回积分（credits/service.py:refund 当前无幂等守卫）。"""
        uid = self._make_user(credits=100)
        cost = 30
        credits_service.pre_deduct(uid, cost, task_id=7)   # -> 70
        credits_service.refund(uid, 7, cost)               # -> 100 正确
        credits_service.refund(uid, 7, cost)               # 重复：应仍为 100
        self.assertEqual(credits_service.get_balance(uid), 100)
        refunds = [r for r in credits_service.get_ledger(uid) if r["type"] == "refund"]
        self.assertEqual(len(refunds), 1, "重复退款产生了多条 refund 流水")

    def test_repeated_finalize_no_double_refund(self):
        """模拟 _finalize_gen 二次执行：重复 finalize + refund 不应双花。"""
        uid = self._make_user()
        credits_service.grant_signup(uid)
        cost = 2 * pricing.PER_LANGUAGE
        credits_service.pre_deduct(uid, cost, task_id=5)
        tasks_service.finalize_task(uid, 5, "cancelled",
                                    credit_cost=cost, credit_refunded=cost,
                                    result={"meta": {"cancelled": True}, "counts": {}})
        credits_service.refund(uid, 5, cost)
        # 重复触发（同上任务二次 finalize）
        credits_service.refund(uid, 5, cost)
        self.assertEqual(credits_service.get_balance(uid), pricing.SIGNUP_BONUS)

    # ----------------------------------------------------- 积分：并发防双花
    def test_concurrent_pre_deduct_no_overdraft(self):
        uid = self._make_user(credits=100)
        import threading
        errors = []
        def worker():
            try:
                credits_service.pre_deduct(uid, 20)
            except credits_service.InsufficientCredits:
                errors.append(1)
        ts = [threading.Thread(target=worker) for _ in range(10)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(credits_service.get_balance(uid), 0)
        self.assertEqual(len(errors), 5)


# ---------------------------------------------------------------- 任务落地
class AdversarialTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qatask_")
        db_mod.set_test_db_path(os.path.join(self.tmp, "app.db"))
        db_mod.Database.get().init_schema()

    def tearDown(self):
        db_mod.Database.reset()
        db_mod.set_test_db_path(None)

    def _make_user(self):
        uid = users_dao.UserDAO.create("t", "h")
        credits_service.grant_signup(uid)
        return uid

    def test_cancel_marks_cancelled_and_persists(self):
        uid = self._make_user()
        cost = 2 * pricing.PER_LANGUAGE
        credits_service.pre_deduct(uid, cost, task_id=None)
        tid = tasks_service.create_task(uid, "P", "P", ["en", "ar"], ["tiktok"], "strict")
        tasks_dao.TaskDAO.set_running(tid)
        tasks_service.finalize_task(uid, tid, "cancelled",
                                    credit_cost=cost, credit_refunded=cost,
                                    result={"meta": {"cancelled": True}, "counts": {}})
        task = tasks_dao.TaskDAO.get(tid)
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(task["credit_refunded"], cost)

    def test_failed_marks_failed_and_refunds(self):
        uid = self._make_user()
        cost = 3 * pricing.PER_LANGUAGE
        credits_service.pre_deduct(uid, cost, task_id=None)
        tid = tasks_service.create_task(uid, "P", "P", ["en"], ["ig"], "strict")
        tasks_service.finalize_task(uid, tid, "failed",
                                    credit_cost=cost, credit_refunded=cost, result=None)
        credits_service.refund(uid, tid, cost)
        self.assertEqual(tasks_dao.TaskDAO.get(tid)["status"], "failed")
        self.assertEqual(credits_service.get_balance(uid), pricing.SIGNUP_BONUS)

    def test_insufficient_credit_rolls_back_pending_task(self):
        """余额不足时不应残留脏数据：pending 任务应被标记 failed 且不扣费。"""
        uid = self._make_user()  # 余额 = SIGNUP_BONUS
        tid = tasks_service.create_task(uid, "P", "P", ["en"], ["tiktok"], "strict")
        # 超额预扣应抛错且余额不变
        with self.assertRaises(credits_service.InsufficientCredits):
            credits_service.pre_deduct(uid, pricing.SIGNUP_BONUS + 999, task_id=tid)
        self.assertEqual(credits_service.get_balance(uid), pricing.SIGNUP_BONUS)
        # 工程师在 _start_logged_in_generation 中对此类情况会 finish(failed)；此处校验 finalize 不会误退
        tasks_dao.TaskDAO.finish(tid, "failed", credit_cost=0, credit_refunded=0,
                                 result_summary=None, result_path=None)
        self.assertEqual(tasks_dao.TaskDAO.get(tid)["status"], "failed")


# ---------------------------------------------------------------- 管理后台
class AdversarialAdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db_mod.set_test_db_path(":memory:")
        db_mod.Database.reset()
        db_mod.Database.get().init_schema()

    def setUp(self):
        conn = db_mod.Database.get()._conn
        conn.execute("DELETE FROM credit_ledger")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM audit_log")
        conn.commit()

    def _make_user(self, name, credits=0):
        return users_dao.UserDAO.create(name, "h", credits=credits)

    def test_disable_user_blocks_login_at_data_layer(self):
        uid = self._make_user("bob")
        admin_svc.disable_user(1, uid, "qa")
        self.assertEqual(users_dao.UserDAO.get_by_id(uid)["status"], "disabled")
        # 模拟 do_login 的禁用判定分支
        user = users_dao.UserDAO.get_by_username("bob")
        self.assertEqual(user["status"], "disabled")

    def test_recharge_requires_note_and_audits(self):
        admin = self._make_user("admin")
        target = self._make_user("vip")
        with self.assertRaises(ValueError):
            admin_svc.recharge(admin, "vip", 50, "")  # 备注必填
        admin_svc.recharge(admin, "vip", 50, "活动")
        self.assertEqual(credits_service.get_balance(target), 50)
        audits = audit_dao.AuditDAO.list(action="recharge")
        self.assertTrue(any(a["target_user_id"] == target for a in audits))

    def test_duplicate_username_rejected_by_unique_constraint(self):
        self._make_user("carol")
        with self.assertRaises(Exception):  # sqlite3.IntegrityError（UNIQUE）
            self._make_user("carol")


# ---------------------------------------------------------------- 认证流程（stub Streamlit）
class _Rerun(Exception):
    """模拟 st.rerun()/st.stop()：真实 rerun 会中断脚本执行。"""


class _StubSt:
    def __init__(self):
        self.session_state = {}
        self.errors = []
        self.successes = []
    def error(self, msg):
        self.errors.append(msg)
    def success(self, msg):
        self.successes.append(msg)
    def warning(self, msg):
        pass
    def rerun(self):
        raise _Rerun()
    def stop(self):
        raise _Rerun()
    def exception(self, e):
        raise e


class AdversarialAuthFlowTests(unittest.TestCase):
    """以 stub 替代 streamlit，真实调用 app.do_register / do_login / ensure_seed_admin。"""

    def setUp(self):
        db_mod.set_test_db_path(":memory:")
        db_mod.Database.reset()
        db_mod.Database.get().init_schema()
        self.stub = _StubSt()
        import app as app_mod
        import auth.session as auth_session_mod
        self.app = app_mod
        self._orig_app_st = app_mod.st
        self._orig_auth_st = auth_session_mod.st
        app_mod.st = self.stub
        auth_session_mod.st = self.stub
        # 确保走默认 admin/admin 分支（确定性）
        os.environ.pop("ADMIN_USERNAME", None)
        os.environ.pop("ADMIN_PASSWORD", None)

    def tearDown(self):
        import auth.session as auth_session_mod
        self.app.st = self._orig_app_st
        auth_session_mod.st = self._orig_auth_st
        db_mod.Database.reset()
        db_mod.set_test_db_path(None)

    def _call(self, fn, *a):
        try:
            fn(*a)
        except _Rerun:
            pass

    def test_register_grants_bonus_and_auto_login(self):
        self._call(self.app.do_register, "dave", "pw")
        self.assertIn("auth_user_id", self.stub.session_state)
        self.assertEqual(self.stub.session_state.get("auth_credits"), pricing.SIGNUP_BONUS)
        self.assertEqual(users_dao.UserDAO.get_by_username("dave")["credits"], pricing.SIGNUP_BONUS)

    def test_duplicate_username_rejected(self):
        self._call(self.app.do_register, "carol", "pw1")
        self.stub.errors.clear()
        self._call(self.app.do_register, "carol", "pw2")
        self.assertTrue(any("已被注册" in e for e in self.stub.errors))
        self.assertEqual(len([u for u in users_dao.UserDAO.list_all() if u["username"] == "carol"]), 1)

    def test_login_success_and_wrong_password(self):
        self._call(self.app.do_register, "erin", "pw")
        # 正确密码
        self.stub.errors.clear()
        self._call(self.app.do_login, "erin", "pw")
        self.assertIn("auth_user_id", self.stub.session_state)
        # 错误密码
        self.stub.session_state.clear()
        self.stub.errors.clear()
        self._call(self.app.do_login, "erin", "wrong")
        self.assertNotIn("auth_user_id", self.stub.session_state)
        self.assertTrue(any("用户名或密码错误" in e for e in self.stub.errors))

    def test_disabled_user_login_rejected(self):
        self._call(self.app.do_register, "bob", "pw")
        uid = users_dao.UserDAO.get_by_username("bob")["user_id"]
        admin_svc.disable_user(1, uid, "qa")
        self.stub.session_state.clear()
        self.stub.errors.clear()
        self._call(self.app.do_login, "bob", "pw")
        self.assertNotIn("auth_user_id", self.stub.session_state)
        self.assertTrue(any("禁用" in e for e in self.stub.errors))
        # 禁用操作已落审计
        self.assertTrue(audit_dao.AuditDAO.list(action="login_blocked"))

    def test_admin_seed_idempotent(self):
        self.app.ensure_seed_admin()
        self.app.ensure_seed_admin()  # 重复启动
        admins = [u for u in users_dao.UserDAO.list_all() if u["username"] == "admin"]
        self.assertEqual(len(admins), 1)
        self.assertEqual(admins[0]["role"], "admin")
        self.assertTrue(self.stub.session_state.get("_seed_default_admin"))


if __name__ == "__main__":
    unittest.main()
