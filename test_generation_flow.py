# -*- coding: utf-8 -*-
"""生成流程集成测试（Batch F2 / D1 / E 业务逻辑层）。

覆盖登录用户「后台生成」的计费与落库闭环（§8.2）：
- 预扣积分 → 建任务 → 生成结束按状态 finalize_task + 退还（cancel/failed 全额退，completed 不退）；
- 结果 JSON 经 storage 落用户隔离目录（路径仅整数 user_id 拼装）；
- 余额一致性、ledger 流水、task 状态、result_path 真实可查。

不含 Streamlit 线程轮询（UI 轮询在 app.py 内由 _render_generation_monitor 实现，
pipeline 取消逻辑由 test_pipeline_cancel 覆盖）。
"""
import os
import shutil
import tempfile
import unittest

from dao.db import set_test_db_path, Database
from dao import users_dao, tasks_dao, credits_dao
from credits import pricing, service as credits_service
from tasks import service as tasks_service
from storage import space as storage_space


class GenerationFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="genflow_")
        set_test_db_path(os.path.join(self.tmp, "app.db"))
        Database.get().init_schema()

    def tearDown(self):
        Database.reset()
        set_test_db_path(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_user(self) -> int:
        uid = users_dao.UserDAO.create("alice", "hash", role="user")
        credits_service.grant_signup(uid)
        return uid

    def test_cancelled_refunds_and_marks_task(self):
        uid = self._make_user()
        self.assertEqual(credits_service.get_balance(uid), pricing.SIGNUP_BONUS)

        cost = 2 * pricing.PER_LANGUAGE
        credits_service.pre_deduct(uid, cost)  # 预扣
        self.assertEqual(credits_service.get_balance(uid), pricing.SIGNUP_BONUS - cost)

        task_id = tasks_service.create_task(uid, "P", "P", ["en", "ar"], ["tiktok"], "strict")
        tasks_dao.TaskDAO.set_running(task_id)

        # 模拟取消：finalize 标记 cancelled + 退还
        result = {"meta": {"cancelled": True}, "counts": {"delivered": 0}}
        tasks_service.finalize_task(uid, task_id, "cancelled",
                                    credit_cost=cost, credit_refunded=cost, result=result)
        credits_service.refund(uid, task_id, cost)

        # 余额复原
        self.assertEqual(credits_service.get_balance(uid), pricing.SIGNUP_BONUS)
        # 任务状态与计费字段
        task = tasks_dao.TaskDAO.get(task_id)
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(task["credit_cost"], cost)
        self.assertEqual(task["credit_refunded"], cost)
        # 结果 JSON 真实落盘于隔离目录
        self.assertTrue(task["result_path"])
        self.assertTrue(os.path.exists(task["result_path"]))
        self.assertIn(f"{os.sep}users{os.sep}1{os.sep}", os.path.normpath(task["result_path"]))

    def test_failed_refunds(self):
        uid = self._make_user()
        cost = 3 * pricing.PER_LANGUAGE
        credits_service.pre_deduct(uid, cost)
        task_id = tasks_service.create_task(uid, "P", "P", ["en", "ar", "fr"], ["ig"], "strict")
        tasks_service.finalize_task(uid, task_id, "failed",
                                    credit_cost=cost, credit_refunded=cost, result=None)
        credits_service.refund(uid, task_id, cost)
        self.assertEqual(credits_service.get_balance(uid), pricing.SIGNUP_BONUS)
        self.assertEqual(tasks_dao.TaskDAO.get(task_id)["status"], "failed")

    def test_completed_no_refund(self):
        uid = self._make_user()
        cost = 2 * pricing.PER_LANGUAGE
        credits_service.pre_deduct(uid, cost)
        task_id = tasks_service.create_task(uid, "P", "P", ["en", "ar"], ["tiktok"], "strict")
        result = {"meta": {"cancelled": False}, "counts": {"delivered": 2}}
        tasks_service.finalize_task(uid, task_id, "completed",
                                    credit_cost=cost, credit_refunded=0, result=result)
        # 成功不退还
        self.assertEqual(credits_service.get_balance(uid), pricing.SIGNUP_BONUS - cost)
        self.assertEqual(tasks_dao.TaskDAO.get(task_id)["status"], "completed")
        self.assertEqual(tasks_dao.TaskDAO.get(task_id)["credit_refunded"], 0)

    def test_ledger_records_consume_and_refund(self):
        uid = self._make_user()
        cost = 1 * pricing.PER_LANGUAGE
        credits_service.pre_deduct(uid, cost)
        task_id = tasks_service.create_task(uid, "P", "P", ["en"], ["tiktok"], "strict")
        tasks_service.finalize_task(uid, task_id, "cancelled",
                                    credit_cost=cost, credit_refunded=cost, result=None)
        credits_service.refund(uid, task_id, cost)
        ledger = credits_dao.CreditDAO.ledger_for_user(uid, limit=50)
        types = [r["type"] for r in ledger]
        self.assertIn("consume", types)
        self.assertIn("refund", types)
        # 余额最终正确
        self.assertEqual(credits_service.get_balance(uid), pricing.SIGNUP_BONUS)

    def test_recent_tasks_persists(self):
        uid = self._make_user()
        tasks_service.create_task(uid, "ProdA", "ProdA", ["en"], ["tiktok"], "strict")
        tasks_service.create_task(uid, "ProdB", "ProdB", ["ar"], ["ig"], "strict")
        recent = tasks_service.recent_tasks(uid, limit=10)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["product_name"], "ProdB")  # 倒序


if __name__ == "__main__":
    unittest.main()
