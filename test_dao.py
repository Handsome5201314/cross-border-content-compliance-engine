# -*- coding: utf-8 -*-
"""DAO 单元测试：使用内存 SQLite（:memory:），不触碰文件系统。"""
import unittest

import dao.db as db_mod
from dao import users_dao, credits_dao, tasks_dao, audit_dao


class DaoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db_mod.set_test_db_path(":memory:")
        db_mod.Database.reset()
        db_mod.Database.get().init_schema()

    def setUp(self):
        # 每个用例清空四张表，保证隔离。
        conn = db_mod.Database.get()._conn
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM credit_ledger")
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM users")
        conn.commit()

    # ----------------------------------------------------------- users
    def test_create_and_get(self):
        uid = users_dao.UserDAO.create("alice", "hash", role="user")
        self.assertGreater(uid, 0)
        u = users_dao.UserDAO.get_by_username("alice")
        self.assertIsNotNone(u)
        self.assertEqual(u["role"], "user")
        self.assertEqual(u["status"], "active")
        self.assertEqual(u["credits"], 0)
        self.assertEqual(users_dao.UserDAO.get_by_id(uid)["username"], "alice")

    def test_exists_username(self):
        users_dao.UserDAO.create("bob", "h")
        self.assertTrue(users_dao.UserDAO.exists_username("bob"))
        self.assertFalse(users_dao.UserDAO.exists_username("carol"))

    def test_set_status_and_role(self):
        uid = users_dao.UserDAO.create("bob", "h")
        users_dao.UserDAO.set_status(uid, "disabled")
        self.assertEqual(users_dao.UserDAO.get_by_id(uid)["status"], "disabled")
        users_dao.UserDAO.set_role(uid, "admin")
        self.assertEqual(users_dao.UserDAO.get_by_id(uid)["role"], "admin")

    def test_add_credits_in_transaction(self):
        uid = users_dao.UserDAO.create("carol", "h")
        new = users_dao.UserDAO.add_credits(uid, 100)
        self.assertEqual(new, 100)
        self.assertEqual(users_dao.UserDAO.get_credits(uid), 100)
        new = users_dao.UserDAO.add_credits(uid, -30)
        self.assertEqual(new, 70)
        self.assertEqual(users_dao.UserDAO.get_credits(uid), 70)

    def test_list_all(self):
        users_dao.UserDAO.create("a", "h")
        users_dao.UserDAO.create("b", "h", credits=5)
        self.assertEqual(len(users_dao.UserDAO.list_all()), 2)
        self.assertEqual(len(users_dao.UserDAO.list_all(only_active=True)), 2)
        uid = users_dao.UserDAO.get_by_username("b")["user_id"]
        users_dao.UserDAO.set_status(uid, "disabled")
        self.assertEqual(len(users_dao.UserDAO.list_all(only_active=True)), 1)

    # ----------------------------------------------------------- credits ledger
    def test_ledger_append_and_query(self):
        uid = users_dao.UserDAO.create("d", "h", credits=100)
        credits_dao.CreditDAO.append(uid, 100, "grant", balance_after=100)
        credits_dao.CreditDAO.append(uid, -20, "consume", task_id=1, balance_after=80)
        rows = credits_dao.CreditDAO.ledger_for_user(uid)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["type"], "consume")
        self.assertEqual(rows[0]["balance_after"], 80)
        grants = credits_dao.CreditDAO.ledger_for_user(uid, type="grant")
        self.assertEqual(len(grants), 1)
        all_rows = credits_dao.CreditDAO.ledger_all()
        self.assertEqual(len(all_rows), 2)

    # ----------------------------------------------------------- tasks
    def test_task_lifecycle(self):
        uid = users_dao.UserDAO.create("e", "h")
        tid = tasks_dao.TaskDAO.create(uid, "产品A", "Product A", ["en", "ar"],
                                        ["tiktok", "instagram"], "strict")
        self.assertGreater(tid, 0)
        tasks_dao.TaskDAO.set_running(tid)
        self.assertEqual(tasks_dao.TaskDAO.get(tid)["status"], "running")
        summary = {"delivered": 1, "review_blocked": 0, "failed": 0, "skipped_deadline": 1}
        tasks_dao.TaskDAO.finish(tid, "completed", credit_cost=10, credit_refunded=0,
                                  result_summary=summary, result_path="/x/result.json")
        t = tasks_dao.TaskDAO.get(tid)
        self.assertEqual(t["status"], "completed")
        self.assertEqual(t["credit_cost"], 10)
        self.assertEqual(t["language_count"], 2)
        self.assertEqual(t["platform_count"], 2)
        self.assertEqual(t["result_summary"]["delivered"], 1)

    def test_recent_tasks(self):
        uid = users_dao.UserDAO.create("f", "h")
        for i in range(3):
            tasks_dao.TaskDAO.create(uid, f"p{i}", f"P{i}", ["en"], ["tiktok"], "strict")
        rows = tasks_dao.TaskDAO.list_recent(uid, limit=20)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["product_name"], "p2")

    # ----------------------------------------------------------- audit
    def test_audit_append_and_list(self):
        admin = users_dao.UserDAO.create("admin", "h", role="admin")
        target = users_dao.UserDAO.create("victim", "h")
        audit_dao.AuditDAO.append(admin, "disable_user", target, detail={"reason": "abuse"})
        rows = audit_dao.AuditDAO.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "disable_user")
        self.assertEqual(rows[0]["target_user_id"], target)
        self.assertEqual(rows[0]["detail"]["reason"], "abuse")
        by_target = audit_dao.AuditDAO.list(target_user_id=target)
        self.assertEqual(len(by_target), 1)


if __name__ == "__main__":
    unittest.main()
