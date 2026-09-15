# -*- coding: utf-8 -*-
"""core 包：跨境卖家 AI 多平台文案智造引擎 v3 的核心逻辑。

模块分工（含按评审返工新增模块）：
- privacy_gate.py  医疗隐私门禁：产品字段白名单 + 本地敏感内容检查（命中患者标识即阻断）
- config_check.py  启动期配置校验：所有引用（平台/语种/市场/免责声明）必须存在
- llm_client.py    统一模型路由：.env 凭证 / 单层分类重试 / 出站隐私门禁 / token 按 tag 归因
- agents.py        5 个 Agent + 结构化产物契约 + 严格 schema 校验 + 合规 fail-closed 合并
- market_rules.py  市场规则：市场选择独立于语言；按市场输出准入/数据主权待核清单
- pipeline.py      编排：状态机（可交付/审核未通过/失败/超时）、deadline、降级、交付门禁
- value_calc.py    业务价值计算器（两侧同验收标准口径；零成功不可估算）
- exporter.py      导出：发布稿只消费审核后安全版（不回拼原始字段）；RTL 包裹
"""
from pathlib import Path

import yaml

# 引擎根目录与配置目录
ENGINE_ROOT = Path(__file__).resolve().parents[1]   # hackathon_round2/
CONFIG_DIR = ENGINE_ROOT / "config"
SAMPLES_DIR = ENGINE_ROOT / "samples"
OUTPUT_DIR = ENGINE_ROOT / "output_runs"

# 项目根（.env 所在）：E:/AI新青年/
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = PROJECT_ROOT / ".env"

# 已加载配置的进程级缓存（避免每次调用重复读盘）
_yaml_cache = {}


def load_env(env_path: Path = None) -> dict:
    """极简 .env 解析（KEY=VALUE，忽略注释与空行）。凭证只允许来自这里。"""
    path = env_path or ENV_PATH
    data = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip()
    return data


def load_yaml_config(name: str) -> dict:
    """读取 config/ 下的 YAML（带缓存）。name 不带扩展名，如 'platforms'。"""
    if name not in _yaml_cache:
        path = CONFIG_DIR / f"{name}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}")
        with open(path, "r", encoding="utf-8") as f:
            _yaml_cache[name] = yaml.safe_load(f) or {}
    return _yaml_cache[name]


def ensure_output_dir() -> Path:
    """运行产物输出目录（结果 JSON / 生成的图片都落在这里）。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR
