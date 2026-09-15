# -*- coding: utf-8 -*-
"""启动期配置校验（对应评审 P1-11 / P2-15）：所有引用必须在启动时校验存在，未知不视为通过。

校验内容：
- platforms.yaml：每个平台必须有 name/tone/fields 合同（titles/descriptions/bullets/hashtags/extras，
  unit 只能是 char|word，计数为非负整数）；
- languages.yaml：每个语种必须有 name/native_name/rtl/localization_notes；
- compliance.yaml：正则全部可编译；正则模式必须为 signal（命中=待审信号）；
  language_market_map 的键必须都在 languages.yaml、值必须都在 markets 里；
  每个语种必须有非空免责声明；levels 必须含 strict/standard/loose；
  每个市场必须有 name/regulator/rules/access_notes/data_sovereignty_notes。

返回错误列表（空列表 = 通过）。CLI/UI/流水线初始化时调用，有错误直接终止。
"""
from pathlib import Path

from . import CONFIG_DIR

VALID_UNITS = {"char", "word"}
VALID_LEVELS = {"strict", "standard", "loose"}


def _load(name: str) -> dict:
    import yaml
    path = CONFIG_DIR / f"{name}.yaml"
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def validate_platforms(cfg: dict) -> list:
    errors = []
    if not isinstance(cfg, dict) or not cfg:
        return ["platforms.yaml 为空或不是映射"]
    for key, spec in cfg.items():
        if not isinstance(spec, dict):
            errors.append(f"platforms.{key} 不是映射")
            continue
        for must in ("name", "tone", "fields"):
            if not spec.get(must):
                errors.append(f"platforms.{key} 缺少 {must}")
        fields = spec.get("fields") or {}
        for fkey in ("titles", "descriptions", "bullets"):
            fs = fields.get(fkey)
            if not isinstance(fs, dict):
                errors.append(f"platforms.{key}.fields.{fkey} 缺失或不是映射")
                continue
            if fs.get("unit") not in VALID_UNITS:
                errors.append(f"platforms.{key}.fields.{fkey}.unit 非法: {fs.get('unit')}")
            for cnt_key in ("count", "max"):
                v = fs.get(cnt_key, 0)
                if not isinstance(v, int) or v < 0:
                    errors.append(f"platforms.{key}.fields.{fkey}.{cnt_key} 必须是非负整数: {v}")
        ht = fields.get("hashtags") or {}
        cmin, cmax = ht.get("count_min", 0), ht.get("count_max", 0)
        if not (isinstance(cmin, int) and isinstance(cmax, int) and 0 <= cmin <= cmax):
            errors.append(f"platforms.{key}.fields.hashtags 数量区间非法: [{cmin},{cmax}]")
        if not isinstance(fields.get("extras") or {}, dict):
            errors.append(f"platforms.{key}.fields.extras 必须是映射")
    return errors


def validate_languages(cfg: dict) -> list:
    errors = []
    if not isinstance(cfg, dict) or not cfg:
        return ["languages.yaml 为空或不是映射"]
    for key, spec in cfg.items():
        if not isinstance(spec, dict):
            errors.append(f"languages.{key} 不是映射")
            continue
        for must in ("name", "native_name"):
            if not spec.get(must):
                errors.append(f"languages.{key} 缺少 {must}")
        if not isinstance(spec.get("rtl"), bool):
            errors.append(f"languages.{key}.rtl 必须是布尔")
        if not isinstance(spec.get("localization_notes"), list) or not spec["localization_notes"]:
            errors.append(f"languages.{key} 缺少 localization_notes")
    return errors


def validate_compliance(cfg: dict, languages_cfg: dict) -> list:
    import re
    errors = []
    if not isinstance(cfg, dict) or not cfg:
        return ["compliance.yaml 为空或不是映射"]
    gr = cfg.get("global_rules") or {}
    if gr.get("mode") != "signal":
        errors.append("compliance.global_rules.mode 必须为 signal（正则命中=待审信号，不直接定罪）")
    patterns = gr.get("forbidden_patterns") or []
    if not patterns:
        errors.append("compliance.global_rules.forbidden_patterns 为空")
    for i, item in enumerate(patterns):
        try:
            re.compile(item["pattern"], re.IGNORECASE)
        except Exception as e:  # noqa: BLE001
            errors.append(f"forbidden_patterns[{i}] 正则编译失败: {e}")
        if item.get("severity") not in ("high", "medium", "low"):
            errors.append(f"forbidden_patterns[{i}].severity 非法: {item.get('severity')}")

    # 免责声明：每个语种都必须有非空声明（严格模式的确定性配置，对应 P1-10）
    disclaimers = cfg.get("disclaimers") or {}
    for lang in languages_cfg:
        if not (disclaimers.get(lang) or "").strip():
            errors.append(f"compliance.disclaimers.{lang} 缺失或为空（严格模式要求非空）")

    # 语种->市场映射：键必须存在于 languages，值必须存在于 markets（不许静默跳过，对应 P1-11）
    markets = cfg.get("markets") or {}
    lmap = cfg.get("language_market_map") or {}
    for lang, mkts in lmap.items():
        if lang not in languages_cfg:
            errors.append(f"language_market_map 引用了未知语种: {lang}")
        if not isinstance(mkts, list) or not mkts:
            errors.append(f"language_market_map.{lang} 为空")
            continue
        for m in mkts:
            if m not in markets:
                errors.append(f"language_market_map.{lang} 引用了不存在的市场: {m}")
    for lang in languages_cfg:
        if lang not in lmap:
            errors.append(f"语种 {lang} 缺少 language_market_map 映射")

    levels = cfg.get("levels") or {}
    if set(levels) < VALID_LEVELS:
        errors.append(f"compliance.levels 必须包含 {sorted(VALID_LEVELS)}，实际: {sorted(levels)}")
    for key, lv in levels.items():
        if not isinstance(lv.get("append_disclaimer"), bool) or \
           not isinstance(lv.get("require_disclaimer"), bool):
            errors.append(f"levels.{key} 缺少 append_disclaimer/require_disclaimer 布尔配置")

    # 每个市场：广告规则 + 准入待核 + 数据主权待核（未知≠通过，对应 P1-11）
    for code, m in markets.items():
        if not isinstance(m, dict):
            errors.append(f"markets.{code} 不是映射")
            continue
        for must in ("name", "regulator", "rules", "access_notes", "data_sovereignty_notes"):
            if not m.get(must):
                errors.append(f"markets.{code} 缺少 {must}")

    notices = cfg.get("engine_notices") or {}
    for must in ("deployment_clarification", "legal", "privacy"):
        if not notices.get(must):
            errors.append(f"engine_notices.{must} 缺失")
    return errors


def validate_all_configs() -> list:
    """全量配置校验。返回错误列表；空列表 = 通过。任何引用缺失都在启动期暴露。"""
    errors = []
    try:
        platforms = _load("platforms")
        errors += [f"[platforms] {e}" for e in validate_platforms(platforms)]
    except Exception as e:  # noqa: BLE001
        errors.append(f"[platforms] 读取失败: {e}")
    try:
        languages = _load("languages")
        errors += [f"[languages] {e}" for e in validate_languages(languages)]
    except Exception as e:  # noqa: BLE001
        errors.append(f"[languages] 读取失败: {e}")
    try:
        compliance = _load("compliance")
        errors += [f"[compliance] {e}" for e in validate_compliance(compliance, languages)]
    except Exception as e:  # noqa: BLE001
        errors.append(f"[compliance] 读取失败: {e}")
    return errors


def assert_configs_ok() -> None:
    """有配置错误直接抛异常（CLI/UI 用）。"""
    errors = validate_all_configs()
    if errors:
        raise RuntimeError("配置校验失败（启动即暴露，不做静默跳过）:\n  - " + "\n  - ".join(errors))
