"""Browser BYOK endpoints must be selected from operator-controlled HTTPS origins."""
import os
from urllib.parse import urlsplit

DEFAULT_UI_GATEWAYS = {
    "https://token-plan.cn-beijing.maas.aliyuncs.com",
    "https://dashscope.aliyuncs.com",
}


def gateway_origin(url):
    if not isinstance(url, str) or "\\" in url or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError("网关 URL 格式不合法")
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment):
        raise ValueError("网关必须使用 HTTPS，且不能含用户凭证、查询参数或片段")
    port = parts.port
    return "https://" + parts.hostname.lower() + (f":{port}" if port and port != 443 else "")


def validate_ui_gateway(url):
    allowed = set(DEFAULT_UI_GATEWAYS)
    for configured in os.environ.get("HACKATHON_UI_GATEWAY_ORIGINS", "").split(","):
        if configured.strip():
            allowed.add(gateway_origin(configured.strip()))
    if gateway_origin(url) not in allowed:
        raise ValueError("该网关未由管理员加入允许列表")
