# -*- coding: utf-8 -*-
"""一键自检 + 端到端验收脚本（按评审 P2-15 重写）。

检查项：
  0. 依赖真实 import（importlib.import_module 实际执行，不用 find_spec 冒充可用）+ requirements.txt 存在
  1. 全部 py 文件语法检查（py_compile）
  2. 配置按各自 schema 校验（config_check.validate_all_configs，不再用统一键数误报）
  3. 合规正则：编译 + 逐条语义回归（100% 漏报修复 / 否定语境=信号而非定罪 / 干净文案 0 命中）
  4. 隐私门禁回归：患者标识样例全阻断；内置产品样例通过；未知字段拒绝
  5. 合规合并回归：空报告→SchemaError（review_failed 方向）；模型 risk_level=high 不被冲淡；
     benign 裁决的否定语境不定罪
  6. 零成功价值回归：可交付=0 → estimable=False、无节省/外推数字
  7. 导出残留回归：原始 localized 含 #cure，发布稿导出只消费 final_copy（安全版），#cure 不回流
  8. Key 安全自检：覆盖任务书指定的 sk-sp- 前缀与通用 sk- 前缀，只输出位置不输出内容
  9. 真实 API 冒烟（一次极短调用）
 10. 端到端 CLI（2 平台 × 2 语种，默认不生图，含 deadline）→ run_result_verify.json

用法：C:/Python314/python.exe _verify_all.py [--skip-e2e]
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

FAILED = []


def step(name):
    print(f"\n{'=' * 60}\n[{name}]\n{'=' * 60}")


def check(ok: bool, msg: str):
    print(("  [ OK ] " if ok else "  [FAIL] ") + msg)
    if not ok:
        FAILED.append(msg)


# ---------------- 0. 依赖（真实 import，P0-1） ----------------
step("0. 依赖检查（真实 import）")
import importlib  # noqa: E402

for pkg in ("yaml", "openai", "streamlit", "PIL"):
    try:
        mod = importlib.import_module(pkg)
        check(True, f"依赖 {pkg} 实际导入成功（版本 {getattr(mod, '__version__', '?')}）")
    except Exception as e:  # noqa: BLE001
        check(False, f"依赖 {pkg} 导入失败: {e}")
check((ROOT / "requirements.txt").exists(), "requirements.txt 存在")

# ---------------- 1. 语法 ----------------
step("1. Python 语法检查")
import py_compile  # noqa: E402

PY_FILES = ["core/__init__.py", "core/privacy_gate.py", "core/config_check.py",
            "core/llm_client.py", "core/agents.py", "core/market_rules.py",
            "core/pipeline.py", "core/value_calc.py", "core/exporter.py",
            "core/image_io.py", "core/output_io.py", "core/gateway_policy.py", "core/usage.py",
            "core/package_agent.py", "core/package_cli.py",
            "engine_cli.py", "app.py", "precompute_full.py"]
for f in PY_FILES:
    try:
        py_compile.compile(str(ROOT / f), doraise=True)
        check(True, f"{f} 语法 OK")
    except py_compile.PyCompileError as e:
        check(False, f"{f} 语法错误: {e}")

# ---------------- 2. 配置（各自 schema，P2-15） ----------------
step("2. 配置解析 + 各自 schema 校验")
import yaml  # noqa: E402
from core.config_check import validate_all_configs  # noqa: E402

for name in ("platforms", "languages", "compliance"):
    try:
        with open(ROOT / "config" / f"{name}.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        check(isinstance(cfg, dict) and bool(cfg), f"{name}.yaml 解析 OK（{len(cfg)} 个顶层键）")
    except Exception as e:  # noqa: BLE001
        check(False, f"{name}.yaml 解析失败: {e}")
cfg_errors = validate_all_configs()
check(not cfg_errors, f"配置交叉校验通过（引用完整性/免责声明/市场映射）"
      + (f"；错误: {cfg_errors[:3]}" if cfg_errors else ""))
try:
    product = json.loads((ROOT / "samples" / "product_medical_appliance.json").read_text(encoding="utf-8"))
    check(product.get("capabilities") and all(
        c.get("status") in ("implemented", "planned") and c.get("evidence") for c in product["capabilities"]),
        f"产品 JSON：{len(product.get('capabilities', []))} 项能力均带状态与依据")
    check(not product.get("certifications_claimed"), "产品 JSON：无认证宣称字段（无证据不写）")
except Exception as e:  # noqa: BLE001
    check(False, f"产品 JSON 解析失败: {e}")
    product = {}

# ---------------- 3. 合规正则语义回归（P1-12） ----------------
step("3. 合规正则：编译 + 语义回归")
from core.agents import ComplianceAgent  # noqa: E402

with open(ROOT / "config" / "compliance.yaml", encoding="utf-8") as f:
    comp_cfg = yaml.safe_load(f)
probe = ComplianceAgent.__new__(ComplianceAgent)  # 不走 __init__（无 client）
probe.cfg = comp_cfg
probe._compiled = [(re.compile(i["pattern"], re.IGNORECASE), i["severity"], i["reason"])
                   for i in comp_cfg["global_rules"]["forbidden_patterns"]]

hits_100 = probe.regex_scan("100% safe")
check(len(hits_100) >= 1, f"'100% safe' 命中 {len(hits_100)} 项（词边界漏报已修复，预期>=1）")
hits_100b = probe.regex_scan("100%effective")
check(len(hits_100b) >= 1, f"'100%effective' 命中 {len(hits_100b)} 项（紧贴写法也命中）")
hits_neg = probe.regex_scan("Not a cure. We do not guarantee results.")
check(len(hits_neg) >= 2, f"否定语境 'Not a cure / do not guarantee' 命中 {len(hits_neg)} 项（命中=待审信号）")
check(all(h["source"] == "regex_signal" for h in hits_neg),
      "命中项 source=regex_signal（待审信号，不是确定违规）")
clean = probe.regex_scan("AI assistant supports clinical documentation, data stays in-hospital.")
check(len(clean) == 0, f"干净文案命中 {len(clean)} 项（预期 0）")
for tc in comp_cfg["global_rules"].get("test_cases", []):
    got = len(probe.regex_scan(tc["text"])) > 0
    check(got == tc["expect_hit"], f"配置用例回归: {tc['text'][:40]!r} expect_hit={tc['expect_hit']} 实际={got}")

# ---------------- 4. 隐私门禁回归（P0-2） ----------------
step("4. 隐私门禁回归")
from core.privacy_gate import PrivacyViolation, scan_sensitive, validate_product  # noqa: E402

SENSITIVE_SAMPLES = [
    "床号 12 的患儿今天出院",
    "住院号:20260908001，体温 38.5",
    "患者姓名：张三丰",
    "身份证号 11010119900307867X",
    "联系电话 13812345678",
    "Patient: John Smith, Bed 3",
    "主诉：发热咳嗽 3 天",
]
n_blocked = sum(1 for s in SENSITIVE_SAMPLES if scan_sensitive(s))
check(n_blocked == len(SENSITIVE_SAMPLES),
      f"敏感样例 {n_blocked}/{len(SENSITIVE_SAMPLES)} 被本地检出（命中即阻断，绝不外发）")
try:
    validate_product(product)
    check(True, "内置真实产品样例通过白名单 + 敏感扫描（无误杀）")
except PrivacyViolation as e:
    check(False, f"内置产品样例被门禁误杀: {e}")
try:
    validate_product({**product, "secret_field": "x"})
    check(False, "未知字段未被拒绝（白名单失效）")
except PrivacyViolation:
    check(True, "未知字段被白名单拒绝")

# ---------------- 5. 合规合并回归（P0-3 fail-closed） ----------------
step("5. 合规合并 fail-closed 回归")
from core.agents import SchemaError, merge_compliance  # noqa: E402

try:
    merge_compliance({}, [])
    check(False, "空合规报告未抛 SchemaError（曾导致空报告判 pass）")
except SchemaError:
    check(True, "空合规报告 → SchemaError（上游判 review_failed，不再判 pass）")
# 模型判 high 但 findings 空：不允许被冲淡为 pass
try:
    merged = merge_compliance(
        {"risk_level": "high", "signal_verdicts": [], "llm_findings": [],
         "safe_titles": ["t"], "safe_descriptions": ["d"], "safe_bullets": [],
         "safe_hashtags": [], "safe_extras": {}}, [])
    check(merged["merged_risk_level"] == "high",
          f"risk_level=high 且 findings 空 → 合并仍为 high（实际 {merged['merged_risk_level']}）")
except SchemaError as e:
    check(False, f"合法响应被误判 SchemaError: {e}")
# 否定语境 benign 裁决：不定罪
signals = [{"source": "regex_signal", "term": "cure", "severity": "high", "reason": "x"},
           {"source": "regex_signal", "term": "guarantee", "severity": "high", "reason": "y"}]
merged = merge_compliance(
    {"risk_level": "pass", "signal_verdicts": [
        {"term": "cure", "verdict": "benign", "reason": "否定语境 Not a cure"},
        {"term": "guarantee", "verdict": "benign", "reason": "否定语境 do not guarantee"}],
     "llm_findings": [], "safe_titles": ["t"], "safe_descriptions": ["d"],
     "safe_bullets": [], "safe_hashtags": [], "safe_extras": {}}, signals)
check(merged["merged_risk_level"] == "pass" and len(merged["benign_verdicts"]) == 2,
      f"否定语境 benign 裁决 → 不定罪（实际 {merged['merged_risk_level']}，benign {len(merged['benign_verdicts'])}）")
# 信号未裁决 → 保守按违规
merged2 = merge_compliance(
    {"risk_level": "pass", "signal_verdicts": [], "llm_findings": [],
     "safe_titles": ["t"], "safe_descriptions": ["d"], "safe_bullets": [],
     "safe_hashtags": [], "safe_extras": {}}, signals)
check(merged2["merged_risk_level"] == "high" and len(merged2["unresolved_signals"]) == 2,
      f"未裁决信号 → 保守 high（实际 {merged2['merged_risk_level']}）")

# ---------------- 6. 零成功价值回归（P1-7） ----------------
step("6. 零成功价值回归")
from core.value_calc import compute_value  # noqa: E402

v0 = compute_value(delivered_count=0, total_combos=4, blocked_count=2, failed_count=2,
                   num_platforms=2, num_languages=2, engine_seconds=900, engine_cost_cny=1.0,
                   markets_covered=[])
check(v0.get("estimable") is False and "无法估算" in v0.get("note", ""),
      f"零成功 → estimable=False 且提示无法估算（note: {v0.get('note', '')[:40]}）")
check("savings" not in v0 and "monthly_projection" not in v0,
      "零成功 → 无节省/月度外推字段（禁止外推）")
v1 = compute_value(delivered_count=4, total_combos=4, blocked_count=0, failed_count=0,
                   num_platforms=2, num_languages=2, engine_seconds=900, engine_cost_cny=1.0,
                   markets_covered=["ID", "TH"])
check(v1.get("estimable") and v1["savings"]["capacity_multiplier"] < 50,
      f"正常 4 条 → 可估算且产能倍数合理（×{v1['savings']['capacity_multiplier']}，旧版会出现 252×）")
check("可调假设" in v1.get("assumption_note", ""), "口径说明标注为可调假设（非行业估值/实际成本）")

# ---------------- 7. 导出残留回归（P1-9） ----------------
step("7. 导出残留回归（#cure 不回流）")
from core.exporter import to_markdown  # noqa: E402

fake_result = {
    "meta": {"generated_at": "2026-09-09T00:00:00", "engine": "test", "platforms": ["tiktok"],
             "languages": ["en"], "compliance_level": "strict"},
    "analysis": {"selling_points": []},
    "counts": {"total": 1, "delivered": 1, "review_blocked": 0, "failed": 0, "skipped_deadline": 0},
    "usage": {"wall_time_s": 1.0, "total_tokens": 1, "total_calls": 1, "cost_estimate_cny": 0.01,
              "by_model": {}},
    "results": [{
        "combo_id": "tiktok__en", "platform": "tiktok", "language": "en",
        "status": "delivered", "error": None,
        "localized": {"titles": ["Not a cure but helpful"],
                      "descriptions": ["assists doctors"],
                      "hashtags": ["#cure"], "bullets": []},
        "compliance": {"merged_risk_level": "pass", "signal_findings": [],
                       "confirmed_findings": [], "benign_verdicts": [],
                       "residual_findings": [], "disclaimer": "draft note"},
        "final_copy": {"titles": ["Not a cure but helpful"],
                       "descriptions": ["assists doctors"],
                       "bullets": [], "hashtags": [], "extras": {}, "cta": "",
                       "disclaimer": "AI draft, needs physician review"},
        "asset_prompt": {"prompt_en": "x"},
    }],
    "market_report": {}, "value": None, "engine_notices": {},
}
md = to_markdown(fake_result)
check("#cure" not in md, "发布稿导出不含原始 #cure 标签（导出只消费 final_copy 安全版）")
check("AI draft, needs physician review" in md, "发布稿包含免责声明")
check("#cure" in json.dumps(fake_result["results"][0]["localized"], ensure_ascii=False),
      "对照：原始 localized 中确实存在 #cure（证明上面的回归有效）")

# ---------------- 8. Key 安全（覆盖 sk-sp- 前缀，只输出位置） ----------------
step("8. Key 安全自检（全目录扫描，覆盖 sk-sp- 前缀）")
KEY_PATTERNS = [re.compile(r"sk-sp-[A-Za-z0-9_\-]{8,}"), re.compile(r"sk-[A-Za-z0-9]{20,}")]
leaks = []
for p in ROOT.rglob("*"):
    if p.suffix.lower() in {".py", ".yaml", ".json", ".md", ".txt", ".jsonl"} and p.is_file():
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        for rx in KEY_PATTERNS:
            m = rx.search(text)
            if m:
                leaks.append(f"{p.relative_to(ROOT)}（偏移 {text.find(m.group(0))}，长度 {len(m.group(0))}，内容不打印）")
                break
check(not leaks, "无文件含 sk-sp-/sk- 开头的 Key 字面量" + (f"；泄漏位置: {leaks}" if leaks else ""))
if "--skip-e2e" in sys.argv:
    print("  [SKIP] 离线模式不读取真实凭证文件")
else:
    from core import load_env
    check(bool(load_env().get("HACKATHON_API_KEY")), "模型凭证已配置（不显示内容）")

# ---------------- 9/10. API 冒烟 + 端到端 ----------------
if "--skip-e2e" in sys.argv:
    step("9/10. API 冒烟 + 端到端")
    print("  [SKIP] --skip-e2e 已指定，跳过 API 与端到端")
else:
    step("9. 真实 API 冒烟")
    try:
        from core.llm_client import LLMClient, mask_key  # noqa: E402
        client = LLMClient()
        print(f"    网关: {client.base_url}")
        print(f"    Key : {mask_key(client.api_key)}")
        content, rec = client.chat("qwen3.6-flash", [{"role": "user", "content": "回复两个字：可用"}],
                                   label="连通性")
        check(True, f"API 冒烟 OK（{rec['latency_s']}s, tokens={rec['total_tokens']}）回复: {content.strip()[:20]}")
    except Exception as e:  # noqa: BLE001
        check(False, f"API 冒烟失败: {e}")

    step("10. 端到端 CLI（2 平台 × 2 语种，默认不生图）")
    t0 = time.time()
    r = subprocess.run(
        [sys.executable, str(ROOT / "engine_cli.py"),
         "--product", str(ROOT / "samples" / "product_medical_appliance.json"),
         "--platforms", "tiktok,instagram",
         "--langs", "en,ar",
         "--deadline", "600",
         "--out", str(ROOT / "run_result_verify.json")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT))
    print(r.stdout[-3000:] if r.stdout else "")
    if r.stderr:
        print("[stderr]", r.stderr[-800:])
    check(r.returncode == 0, f"CLI 退出码 {r.returncode}（总耗时 {time.time() - t0:.0f}s）")
    out_file = ROOT / "run_result_verify.json"
    if out_file.exists():
        try:
            res = json.loads(out_file.read_text(encoding="utf-8"))
            counts = res.get("counts", {})
            statuses = {x.get("status") for x in res.get("results", [])}
            check(counts.get("total") == 4, f"结果共 {counts.get('total')} 条（2 平台×2 语种）")
            check(statuses <= {"delivered", "review_blocked", "failed", "skipped_deadline"},
                  f"状态枚举合法: {sorted(statuses)}")
            check(res.get("usage", {}).get("total_tokens", 0) > 0,
                  f"token 统计: {res['usage']['total_tokens']} / API 估算 ¥{res['usage']['cost_estimate_cny']}")
            check(counts.get("delivered", 0) >= 1,
                  f"可交付 {counts.get('delivered', 0)} 条（审核未通过 {counts.get('review_blocked', 0)}"
                  f" / 失败 {counts.get('failed', 0)}——如实统计，不为凑数放行）")
            delivered = [x for x in res["results"] if x["status"] == "delivered"]
            if delivered:
                fc = delivered[0].get("final_copy") or {}
                check(bool(fc.get("disclaimer")),
                      "可交付项含确定性免责声明（AI 输出为草稿需医生审核）")
            val = res.get("value") or {}
            if counts.get("delivered", 0) == 0:
                check(val.get("estimable") is False, "零可交付 → 价值不可估算（无外推数字）")
            else:
                check(val.get("estimable") is True and val["savings"]["capacity_multiplier"] < 50,
                      f"价值口径合理（产能 ×{val['savings']['capacity_multiplier']}）")
        except Exception as e:  # noqa: BLE001
            check(False, f"结果文件解析失败: {e}")

print(f"\n{'=' * 60}\n汇总: {'全部通过 ✅' if not FAILED else f'{len(FAILED)} 项失败 ❌'}")
for m in FAILED:
    print("  FAIL:", m)
sys.exit(1 if FAILED else 0)
