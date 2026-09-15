# -*- coding: utf-8 -*-
"""用户文件空间隔离（ARCHITECTURE_V2.md §7）。

安全底线（§7.3 / 任务书硬约束 #8）：
1. 路径只由服务端整数 user_id / task_id 拼装，用户名字符串绝不进入路径。
2. safe_join 规范化校验：外部输入（上传文件名）先 secure_filename，再校验不穿越 base。
3. 上传文件落盘：<inputs_dir>/<uuid4_hex>_<secure_filename>，原始文件名仅作展示。
4. 结果落盘用 atomic_write（参考 core.output_io.atomic_write）。
"""
import json
import re
import secrets
from pathlib import Path

from dao.db import data_root
from core.output_io import atomic_write, atomic_write_text


# 用户数据根：<data_root>/users/{user_id}/...（仅整数 user_id 拼装）
_USER_ROOT = data_root() / "users"

# 允许文件名保留的字符集（参考 werkzeug secure_filename 子集，零依赖实现）。
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def secure_filename(name: str, max_len: int = 120) -> str:
    """剥离目录成分并仅保留安全字符；空名回退为 'file'。"""
    name = Path(str(name)).name
    name = _SAFE_RE.sub("_", name)
    if not name:
        name = "file"
    return name[:max_len]


# ---------------------------------------------------------------- 目录拼装（仅整数 ID）
def user_space(user_id: int) -> Path:
    return _USER_ROOT / str(int(user_id))


def inputs_dir(user_id: int) -> Path:
    return user_space(user_id) / "inputs"


def outputs_dir(user_id: int) -> Path:
    return user_space(user_id) / "outputs"


def task_dir(user_id: int, task_id: int) -> Path:
    return user_space(user_id) / "tasks" / str(int(task_id))


def _ensure(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# ---------------------------------------------------------------- 防穿越
def safe_join(base: Path, *parts: str) -> Path:
    """将文件名等外部输入拼到 base 内并校验不穿越。返回解析后的绝对路径。

    拒绝：包含 '..'、绝对路径、或解析后落在 base 之外的任何路径。原始文件名仅作展示，
    永不用于路径跳转（§7.3-2）。
    """
    base = Path(base).resolve()
    base.mkdir(parents=True, exist_ok=True)
    sanitized = [secure_filename(p) for p in parts]
    target = (base / Path(*sanitized)).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"路径穿越被拒绝：{target} 不在 {base} 内")
    return target


# ---------------------------------------------------------------- 读写
def write_result(user_id: int, task_id: int, result: dict) -> Path:
    """原子写单任务结果 JSON 到用户隔离目录。"""
    d = _ensure(task_dir(user_id, task_id))
    path = d / "result.json"
    atomic_write_text(path, json.dumps(result, ensure_ascii=False, indent=2))
    return path


def read_result(user_id: int, task_id: int) -> dict | None:
    path = task_dir(user_id, task_id) / "result.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    return None


def save_upload(user_id: int, original_filename: str, data: bytes) -> Path:
    """上传文件落 inputs_dir；以 uuid 前缀 + 安全文件名命名，用户名/原文件名不进路径。"""
    d = _ensure(inputs_dir(user_id))
    safe = secure_filename(original_filename)
    path = d / f"{secrets.token_hex(8)}_{safe}"
    atomic_write(path, data)
    return path


def save_output(user_id: int, filename: str, data: bytes) -> Path:
    """生成产物落 outputs_dir（如导出的图片）。"""
    d = _ensure(outputs_dir(user_id))
    safe = secure_filename(filename)
    path = d / f"{secrets.token_hex(8)}_{safe}"
    atomic_write(path, data)
    return path


def count_files(directory: Path) -> int:
    """真实统计目录下（递归）的文件数，替换假 12/3/9。"""
    directory = Path(directory)
    if not directory.exists():
        return 0
    return sum(1 for p in directory.rglob("*") if p.is_file())
