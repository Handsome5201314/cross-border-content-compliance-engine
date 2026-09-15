# -*- coding: utf-8 -*-
"""积分业务单元测试：内存 SQLite，覆盖赠送 / 预扣 / 退还 / 充值 / 并发双花。"""
import threading
import unittest

import dao.db as db_mod
from dao import users_dao
from credits import pricing, service as credits_service


class CreditsServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db_mod.set_test_db_path(":memory:")
        db_mod.Database.reset()
        db_mod.Database.get().init_schema()

    def setUp(self):
        conn = db_mod.Database.get()._conn
        conn.execute("DELETE FROM credit_ledger")
        conn.execute("DELETE FROM users")
        conn.commit()

    def _make_user(self, name="u", credits=0):
        return users_dao.UserDAO.create(name, "h", credits=credits)

    # ----------------------------------------------------------- 赠送
    def test_grant_signup(self):
        uid = self._make_user()
        bal = credits_service.grant_signup(uid)
        self.assertEqual(bal, pricing.SIGNUP_BONUS)
        self.assertEqual(credits_service.get_balance(uid), pricing.SIGNUP_BONUS)
        ledger = credits_service.get_ledger(uid)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["type"], "grant")
        self.assertEqual(ledger[0]["balance_after"], pricing.SIGNUP_BONUS)

    # ----------------------------------------------------------- 预扣 + 一致性
    def test_pre_deduct_consumes_and_ledger_consistent(self):
        uid = self._make_user(credits=pricing.SIGNUP_BONUS)
        cost = 3 * pricing.PER_LANGUAGE  # 3 语种
        bal = credits_service.pre_deduct(uid, cost)
        self.assertEqual(bal, pricing.SIGNUP_BONUS - cost)
        ledger = credits_service.get_ledger(uid)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["type"], "consume")
        self.assertEqual(ledger[0]["change"], -cost)
        self.assertEqual(ledger[0]["balance_after"], bal)
        # 余额与流水末尾余额强一致
        self.assertEqual(credits_service.get_balance(uid), ledger[0]["balance_after"])

    def test_pre_deduct_insufficient_raises(self):
        uid = self._make_user(credits=5)
        with self.assertRaises(credits_service.InsufficientCredits):
            credits_service.pre_deduct(uid, 10)
        # 余额不变
        self.assertEqual(credits_service.get_balance(uid), 5)
        self.assertEqual(len(credits_service.get_ledger(uid)), 0)

    # ----------------------------------------------------------- 退还复原
    def test_refund_restores_balance(self):
        uid = self._make_user(credits=pricing.SIGNUP_BONUS)
        cost = 4 * pricing.PER_LANGUAGE
        credits_service.pre_deduct(uid, cost, task_id=1)
        self.assertEqual(credits_service.get_balance(uid), pricing.SIGNUP_BONUS - cost)
        credits_service.refund(uid, 1, cost)
        self.assertEqual(credits_service.get_balance(uid), pricing.SIGNUP_BONUS)
        ledger = credits_service.get_ledger(uid)
        types = sorted(r["type"] for r in ledger)
        self.assertEqual(types, ["consume", "refund"])

    # ----------------------------------------------------------- 充值 + 审计
    def test_recharge_adds_and_requires_note(self):
        admin = self._make_user("admin", credits=0)
        target = self._make_user("vip", credits=0)
        with self.assertRaises(ValueError):
            credits_service.recharge(admin, "vip", 50, "")  # 备注必填
        new_bal = credits_service.recharge(admin, "vip", 50, "活动赠送")
        self.assertEqual(new_bal, 50)
        self.assertEqual(credits_service.get_balance(target), 50)
        ledger = credits_service.get_ledger(target)
        self.assertEqual(ledger[0]["type"], "recharge")
        self.assertEqual(ledger[0]["operator_id"], admin)
        self.assertEqual(ledger[0]["note"], "活动赠送")

    def test_recharge_unknown_user(self):
        admin = self._make_user("admin", credits=0)
        with self.assertRaises(ValueError):
            credits_service.recharge(admin, "ghost", 10, "x")

    # ----------------------------------------------------------- 并发双花防护
    def test_concurrent_pre_deduct_no_overdraft(self):
        uid = self._make_user(credits=100)
        errors = []
        def worker():
            try:
                credits_service.pre_deduct(uid, 20)
            except credits_service.InsufficientCredits:
                errors.append(1)
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 100 / 20 = 5 次成功，5 次因余额不足失败；最终余额为 0
        self.assertEqual(credits_service.get_balance(uid), 0)
        self.assertEqual(len(errors), 5)
        ledger = credits_service.get_ledger(uid)
        self.assertEqual(len(ledger), 5)
        # 每笔流水 balance_after 单调递减且 >= 0
        balances = [r["balance_after"] for r in reversed(ledger)]
        self.assertEqual(balances, [80, 60, 40, 20, 0])


if __name__ == "__main__":
    unittest.main()
