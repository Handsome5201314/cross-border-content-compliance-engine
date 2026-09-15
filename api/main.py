# -*- coding: utf-8 -*-
"""FastAPI 应用入口（ARCHITECTURE_V3.md §2.1 / §4）。

- 路由先注册，最后把 ui/ 静态目录挂到 ``/``（html=True，SPA 同源同端口，
  天然规避 CORS 与平台保留头冲突）。
- 启动 lifespan：seed_admin（环境变量注入）+ recover_orphan_tasks（容器重启后
  running/pending 孤儿任务置 failed 并退还预扣，refund 幂等守卫防双花）。
- 全局异常处理器：中文友好提示，绝不外泄 traceback。
- 启动：``python -m api.main``（Docker CMD）或 ``uvicorn api.main:app``。
"""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from admin import service as admin_service
from api.routers import admin as admin_router_mod
from api.routers import auth as auth_router_mod
from api.routers import credits as credits_router_mod
from api.routers import generate as generate_router_mod
from api.routers import space as space_router_mod
from api.routers import tasks as tasks_router_mod
from auth.hashing import hash_password
from credits import service as credits_service
from dao import users_dao
from dao.db import Database
from tasks import service as tasks_service

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("cce.main")

ROOT = Path(__file__).resolve().parents[1]
UI_DIR = ROOT / "ui"

# 启动期状态（供日志与 /api/auth/me 告警展示）
_seed_default_admin = False


def seed_admin() -> None:
    """首次启动自动建种子管理员（ADMIN_USERNAME/ADMIN_PASSWORD 环境变量）。

    未注入时本地回退 admin/admin 并告警（仅限本地；生产必须注入强口令）。
    """
    global _seed_default_admin
    username = (os.environ.get("ADMIN_USERNAME") or "").strip()
    password = (os.environ.get("ADMIN_PASSWORD") or "").strip()
    if not username or not password:
        username, password = "admin", "admin"
        _seed_default_admin = True
        logger.warning("未注入 ADMIN_USERNAME/ADMIN_PASSWORD：使用默认凭据 admin/admin（仅限本地开发）。")
    if not users_dao.UserDAO.exists_username(username):
        users_dao.UserDAO.create(username, hash_password(password), role="admin")
        logger.info("种子管理员已创建：%s", username)


def recover_orphan_tasks() -> None:
    """容器重启后：running/pending 孤儿任务置 failed 并退还预扣积分（§7-3）。

    退款走 ``credits_service.refund``（task_id 幂等守卫）：即使 worker 重启前已完成
    退款也不会双花。落库审计（operator_id=0 表示系统）。
    """
    db = Database.get()
    orphans = db.query_all(
        "SELECT task_id, user_id FROM tasks WHERE status IN ('pending','running')")
    if not orphans:
        return
    for row in orphans:
        task_id, uid = int(row["task_id"]), int(row["user_id"])
        consume = db.query_one(
            "SELECT -change AS amt FROM credit_ledger "
            "WHERE task_id = ? AND type = 'consume' LIMIT 1", (task_id,))
        amount = int(consume["amt"]) if consume and consume["amt"] else 0
        refunded = 0
        if amount > 0:
            _, ok = credits_service.refund(uid, task_id, amount)
            refunded = amount if ok else 0
        tasks_service.finalize_task(uid, task_id, "failed", credit_cost=amount,
                                    credit_refunded=refunded, result=None)
        from dao import audit_dao
        audit_dao.AuditDAO.append(0, "orphan_task_recovered", uid,
                                  detail={"task_id": task_id, "refunded": refunded})
        logger.info("孤儿任务已回收 task=%s user=%s 退款=%s", task_id, uid, refunded)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Database.get().init_schema()
    seed_admin()
    recover_orphan_tasks()
    yield


app = FastAPI(title="跨境内容合规引擎", version="3.0", lifespan=lifespan)

# 说明：SPA 与 API 同源同端口（7860），无需 CORS；如需跨域调试可挂 CORSMiddleware（预留）。

app.include_router(auth_router_mod.router)
app.include_router(auth_router_mod.demo_router())
app.include_router(generate_router_mod.router)
app.include_router(tasks_router_mod.router)
app.include_router(credits_router_mod.router)
app.include_router(space_router_mod.router)
app.include_router(admin_router_mod.router)


@app.exception_handler(RequestValidationError)
async def on_validation_error(_request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": "请求参数不合法，请检查表单填写"})


@app.exception_handler(Exception)
async def on_unexpected(_request: Request, exc: Exception):
    logger.exception("未处理异常: %s", type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误，请稍后重试"})


# 路由注册完毕后再挂静态目录（兜底 / 与 /assets/*）。
if UI_DIR.exists():
    app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
else:
    logger.warning("ui/ 目录不存在，静态前端不可用：%s", UI_DIR)


if __name__ == "__main__":
    import uvicorn
    host = (os.environ.get("HOST") or "0.0.0.0").strip()
    port = int((os.environ.get("PORT") or "7860").strip())
    uvicorn.run(app, host=host, port=port)
