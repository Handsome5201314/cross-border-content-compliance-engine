# 跨境内容合规引擎 V3 —— FastAPI + Slate Pro 静态前端 + Docker
# 监听 0.0.0.0:7860（同源 SPA，后端 API 与静态前端同端口，规避 CORS 与平台保留头冲突）
# 注意：V2 的 app.py（Streamlit）保留在仓库内但镜像不启动。

FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖先行（利用层缓存）
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 业务层 + V3 壳层 + 静态前端（app.py 一并保留但不启动）
COPY . .

# 健康检查：探测首页可达
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7860/').status==200 else 1)" || exit 1

# 生产部署必须注入 SESSION_SECRET 与 ADMIN_*；缺失则启动拒绝（见 api/session.py §7-2）
EXPOSE 7860
ENV HOST=0.0.0.0 PORT=7860

CMD ["python", "-m", "api.main"]
