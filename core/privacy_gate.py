# -*- coding: utf-8 -*-
"""医疗隐私门禁（P0-2 红线）：产品字段白名单 + 本地敏感内容检查。

三条硬规则：
1. 产品输入只允许白名单内的公开营销字段，未知字段直接拒绝（防病历/内部资料混入）；
2. 所有发往云端模型的内容必须先过本地敏感内容检查：
   命中患者标识（床号/住院号/病历号/姓名标签/身份证/手机号等）或疑似 PHI
   → 本地阻断并报错，绝不用云端模型去脱敏（阻断是确定性本地动作，不是优化项）；
3. LLMClient.chat() 内置最后一道防线：任何出站消息命中即拒绝发送（纵深防御，
   即使某条调用路径漏了上游检查也拦得住）。

注意区分两件事（UI/README 同步声明）：
- 被营销的产品（某三甲医院儿科住院医师 AI 助手）是全本地部署，患者数据不出院——这是卖点；
- 本营销引擎自身会调用云端大模型——所以输入必须先过本门禁，两者不得混同。
"""
import re

# ---------------- 产品字段白名单（营销输入契约，与 samples/product_medical_appliance.json 一致） ----------------
APPROVED_FIELDS = {
    "schema_version", "product_name", "product_name_en", "brand", "form", "deployment",
    "capabilities", "tech_stack", "red_lines", "selling_angles",
    "target_buyers", "target_markets", "target_languages", "target_platforms",
    "compliance_level", "pricing_positioning", "marketing_notes",
}

# ---------------- 敏感内容规则（本地正则；命中即阻断，不给云端） ----------------
# 设计原则：只匹配「患者标识结构」（标签+值 / 特定位数编号），不匹配普通营销词汇，
# 避免把合法的 B2B 营销文案误杀；宁可漏掉模糊 PHI 也不把整篇营销文拦死。
_SENSITIVE_PATTERNS = [
    # 中文患者编号：标签 + 字母数字值（"床号 12"、"住院号:20260908001"）
    (r"(?:床号|住院号|病历号|门诊号|就诊号|就诊卡号|医保卡号|档案号)\s*[:：]?\s*[A-Za-z0-9\-]{2,}",
     "疑似床号/住院号/病历号等患者编号"),
    # 中文患者姓名：人物标签（可带"姓名"）+ 冒号 + 值
    (r"(?:患者|病人|患儿|产妇|家属|死者)\s*(?:姓名)?\s*[:：]\s*\S+",
     "疑似患者姓名标识"),
    # 裸"姓名："标签 + 中文短值（病历表单常见结构）
    (r"姓名\s*[:：]\s*[一-龥]{2,4}(?![一-龥])",
     "疑似患者姓名标识"),
    # 身份证号（18 位含校验位 X；15 位旧式）
    (r"\b\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b",
     "疑似身份证号"),
    (r"\b\d{15}\b", "疑似 15 位编号（旧式身份证/病案号）"),
    # 手机号 / 座机 / 国际号码
    (r"\b1[3-9]\d{9}\b", "疑似手机号"),
    (r"\b\d{3,4}-\d{7,8}\b", "疑似座机号"),
    (r"\+\d{1,3}[-\s]?\d{7,12}", "疑似国际电话号码"),
    # 病历结构段（主诉/现病史/查体等）——营销资料不应出现病历结构
    (r"(?:主诉|现病史|既往史|婚育史|家族史|月经史|查体|专科检查|辅助检查)\s*[:：]",
     "疑似病历结构内容（PHI）"),
    # 英文患者标识
    (r"Patient(?:\s*Name)?\s*[:：]\s*\S+", "疑似患者姓名标识（英文）"),
    (r"\b(?:MRN|Hospital\s*(?:No\.?|Number|ID)|Admission\s*(?:No\.?|Number)|Medical\s*Record\s*(?:No\.?|Number))\s*[:：]?\s*[A-Za-z0-9\-]{2,}",
     "疑似患者编号（英文）"),
    (r"\bBed\s*(?:No\.?|Number)?\s*[:：]?\s*[A-Za-z]?\d{1,3}\b", "疑似床号（英文）"),
]


class PrivacyViolation(Exception):
    """隐私门禁阻断异常：消息只含命中原因，不含原文片段（避免敏感内容二次扩散）。"""


_COMPILED = [(re.compile(p, re.IGNORECASE), reason) for p, reason in _SENSITIVE_PATTERNS]


def scan_sensitive(text: str) -> list:
    """本地敏感内容扫描。返回命中列表 [{reason, hit}...]（不含大段原文，仅命中文本前缀）。"""
    findings = []
    if not text:
        return findings
    for rx, reason in _COMPILED:
        for m in rx.finditer(text):
            findings.append({"reason": reason, "hit": m.group(0)[:24]})
    return findings


def assert_text_clean(text: str, context: str = "") -> None:
    """断言文本干净：命中患者标识/疑似 PHI 即抛 PrivacyViolation（本地阻断，不外发）。"""
    findings = scan_sensitive(text)
    if findings:
        reasons = "；".join(sorted({f["reason"] for f in findings}))
        raise PrivacyViolation(
            f"[隐私门禁] {context or '输入'} 命中 {len(findings)} 项患者标识/疑似 PHI，"
            f"已本地阻断（绝不外发云端脱敏）。命中类型：{reasons}"
        )


def validate_product(product: dict) -> dict:
    """产品输入门禁：字段白名单 + 全字段敏感扫描。通过则返回白名单内字段的净化副本。

    - 未知字段 → 直接拒绝（列出允许字段），防止把病历/内部资料整个塞进来；
    - 任何字段文本命中患者标识 → 拒绝。
    """
    if not isinstance(product, dict):
        raise PrivacyViolation("[隐私门禁] 产品输入必须是 JSON 对象")
    unknown = [k for k in product if k not in APPROVED_FIELDS]
    if unknown:
        raise PrivacyViolation(
            f"[隐私门禁] 产品输入含未批准字段: {sorted(unknown)}。"
            f"只允许营销公开字段: {sorted(APPROVED_FIELDS)}"
        )
    _scan_obj(product, "product")
    return {k: product[k] for k in product if k in APPROVED_FIELDS}


def _scan_obj(obj, path: str) -> None:
    """递归扫描对象内全部字符串。"""
    if isinstance(obj, str):
        assert_text_clean(obj, path)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan_obj(v, f"{path}[{i}]")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _scan_obj(v, f"{path}.{k}")
