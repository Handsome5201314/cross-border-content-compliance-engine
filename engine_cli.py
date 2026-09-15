# -*- coding: utf-8 -*-
"""CLI 入口：端到端跑一遍引擎，产出 run_result.json。

用法（验收口径）：
  C:/Python314/python.exe engine_cli.py --product samples/product_medical_appliance.json --platforms tiktok,instagram --langs en,ar

演示预算（对应评审 P0-4）：
- 默认小批次现场真跑；全量 6 平台 × 7 语种用 precompute_full.py 离线预生成；
- 默认不生图，--with-image 才做图像生成；重试仅一层且分类（永久错误立即失败）；
- --deadline 控制整批预算（默认 900s，超时未执行的组合标记 skipped_deadline 如实展示）；
- 失败项可用 --rerun-failed <result.json> 单独重跑。
"""
import argparse
import json
import sys
import time
from pathlib import Path

# Windows 控制台中文/Unicode 输出兜底
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 旧终端不支持时忽略
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import load_yaml_config                      # noqa: E402
from core.config_check import validate_all_configs     # noqa: E402
from core.llm_client import LLMClient, mask_key        # noqa: E402
from core.pipeline import Pipeline, STATUS_LABELS      # noqa: E402
from core.privacy_gate import PrivacyViolation, validate_product  # noqa: E402


def parse_args():
    ap = argparse.ArgumentParser(description="跨境卖家 AI 多平台文案智造引擎 v3")
    ap.add_argument("--product", "-p", default="samples/product_medical_appliance.json",
                    help="产品 JSON 文件路径")
    ap.add_argument("--out", "-o", default="run_result.json", help="输出 JSON 路径")
    ap.add_argument("--platforms", default=None, help="逗号分隔平台（默认取产品文件 target_platforms）")
    ap.add_argument("--langs", "--languages", dest="languages", default=None,
                    help="逗号分隔语种（默认取产品文件 target_languages）")
    ap.add_argument("--compliance-level", default=None, choices=["strict", "standard", "loose"],
                    help="合规等级（默认取产品文件，再默认 strict）")
    ap.add_argument("--main-model", default=None, help="主力模型覆盖（默认 qwen3.7-plus）")
    ap.add_argument("--fast-model", default=None, help="快速模型覆盖（默认 qwen3.6-flash）")
    ap.add_argument("--review-model", default=None, help="合规审查模型覆盖（默认 glm-5.2 跨厂商交叉审）")
    ap.add_argument("--max-workers", type=int, default=4, help="并行度（默认 4）")
    ap.add_argument("--deadline", type=float, default=900.0,
                    help="整批时间预算秒数（默认 900；超时未执行的组合标记 skipped_deadline）")
    ap.add_argument("--with-image", action="store_true",
                    help="生成 1 张示例图（默认不生图，P0-4 演示时间预算）")
    ap.add_argument("--rerun-failed", default=None,
                    help="从已有结果 JSON 重跑非可交付项（如 --rerun-failed run_result.json）")
    return ap.parse_args()


def _print_result_summary(result: dict, out_path: Path):
    counts = result.get("counts", {})
    usage = result["usage"]
    print("\n" + "=" * 64)
    print(f"结果: 可交付 {counts.get('delivered', 0)} / 审核未通过 {counts.get('review_blocked', 0)}"
          f" / 失败 {counts.get('failed', 0)} / 超时跳过 {counts.get('skipped_deadline', 0)}"
          f"（共 {counts.get('total', 0)} 条）")
    if counts.get("review_blocked") or counts.get("failed") or counts.get("skipped_deadline"):
        print("未交付项（如实展示）：")
        for r in result["results"]:
            if r["status"] != "delivered":
                print(f"  - {r['platform']}×{r['language']} [{r['status']}] {str(r.get('error'))[:140]}")
    print(f"总耗时  : {usage['wall_time_s']}s")
    print(f"总调用  : {usage['total_calls']} 次 / 总 tokens: {usage['total_tokens']}")
    for m, st in usage["by_model"].items():
        print(f"  - {m:20s} {st['calls']} 次 / {st['total_tokens']} tokens")
    print(f"API 成本估算: ¥{usage['cost_estimate_cny']}（{usage['price_note']}）")
    print(f"结果文件: {out_path}")

    v = result.get("value") or {}
    print("\n----- 业务价值（口径：两侧同验收标准，参数为可调假设） -----")
    if not v.get("estimable"):
        print(f"{v.get('note', '无有效产出，无法估算')}")
    else:
        print(f"人工侧    : {v['manual_side']['hours_total']} 小时 / ¥{v['manual_side']['cost_cny']:,.0f}")
        print(f"AI 侧     : 人工 {v['ai_side']['manual_hours_total']} 小时 + API ¥{v['ai_side']['api_cost_cny_estimate']}"
              f" = 总 ¥{v['ai_side']['total_cost_cny']:,.0f}（引擎墙钟 {v['ai_side']['engine_wall_seconds']}s）")
        print(f"节省      : {v['savings']['hours_saved']} 小时 / ¥{v['savings']['cost_saved_cny']:,.0f}")
        print(f"产能提升  : ×{v['savings']['capacity_multiplier']}")
        print(f"市场覆盖  : {v['markets_count']} 个（{', '.join(v['markets_covered'])}，按实际可交付计）")
    print(f"口径说明  : {v.get('assumption_note', '')[:120]}…")

    delivered = [r for r in result["results"] if r["status"] == "delivered"]
    if delivered:
        demo = delivered[0]
        fc = demo.get("final_copy") or {}
        print("\n----- 样例摘录（真实输出片段） -----")
        print(f"[{demo['platform']} × {demo['language']}]")
        if fc.get("titles"):
            print("标题:", fc["titles"][0][:120])
        if fc.get("descriptions"):
            print("正文:", fc["descriptions"][0][:200].replace("\n", " / "))
        comp = demo.get("compliance") or {}
        print(f"合规: {comp.get('merged_risk_level')}（待审信号 {len(comp.get('signal_findings', []))}"
              f" / 确认违规 {len(comp.get('confirmed_findings', []))}"
              f" / benign 保留 {len(comp.get('benign_verdicts', []))}"
              f" / 残留 {len(comp.get('residual_findings', []))}）")


def main():
    args = parse_args()
    t0 = time.time()

    root = Path(__file__).resolve().parent
    product_path = Path(args.product)
    if not product_path.is_absolute():
        product_path = root / product_path

    # ---------- 输入（P0-2 隐私门禁：白名单 + 本地敏感扫描） ----------
    if not product_path.exists():
        sys.exit(f"[FATAL] 产品文件不存在: {product_path}")
    try:
        product = validate_product(json.loads(product_path.read_text(encoding="utf-8")))
    except PrivacyViolation as e:
        sys.exit(str(e))
    print(f"[隐私门禁] 通过：字段白名单校验 + 本地敏感内容扫描（0 命中）")

    # ---------- 配置启动校验（P1-11：引用缺失直接失败，不静默跳过） ----------
    cfg_errors = validate_all_configs()
    if cfg_errors:
        sys.exit("[FATAL] 配置校验失败:\n  - " + "\n  - ".join(cfg_errors))
    print("[配置校验] 通过：平台/语种/市场/免责声明引用完整")

    platforms_cfg = load_yaml_config("platforms")
    languages_cfg = load_yaml_config("languages")

    # ---------- 单条重跑模式 ----------
    if args.rerun_failed:
        return _rerun_failed(args, root)

    platforms = args.platforms.split(",") if args.platforms else product.get("target_platforms")
    languages = args.languages.split(",") if args.languages else product.get("target_languages")
    platforms = [p.strip() for p in platforms if p.strip()]
    languages = [l.strip() for l in languages if l.strip()]
    bad = {"platforms": [p for p in platforms if p not in platforms_cfg],
           "languages": [l for l in languages if l not in languages_cfg]}
    if bad["platforms"] or bad["languages"]:
        sys.exit(f"[FATAL] 未知平台/语种: {bad}（合法: {sorted(platforms_cfg)} / {sorted(languages_cfg)}）")
    if not platforms or not languages:
        sys.exit("[FATAL] 无有效平台/语种")
    level = args.compliance_level or product.get("compliance_level", "strict")

    models = {}
    if args.main_model:
        models["main"] = args.main_model
    if args.fast_model:
        models["fast"] = args.fast_model
    if args.review_model:
        models["review"] = args.review_model

    print("=" * 64)
    print("跨境卖家 AI 多平台文案智造引擎 v3（按评审返工版）")
    print("=" * 64)
    print(f"产品      : {product.get('product_name')}")
    print(f"平台({len(platforms)})   : {', '.join(platforms)}")
    print(f"语种({len(languages)})   : {', '.join(languages)}")
    print(f"合规等级  : {level}｜整批预算: {args.deadline:.0f}s｜并行: {args.max_workers}")
    print(f"模型路由  : main={models.get('main', 'qwen3.7-plus')} fast={models.get('fast', 'qwen3.6-flash')} "
          f"review={models.get('review', 'glm-5.2')}")
    print("[声明] 被营销产品为全本地部署（数据不出院）；本营销引擎自身调用云端大模型，输入已过隐私门禁")
    print(f"生图      : {'开启（--with-image）' if args.with_image else '关闭（默认，--with-image 开启）'}")

    try:
        client = LLMClient()
    except RuntimeError as e:
        sys.exit(str(e))
    print(f"网关      : {client.base_url}")
    print(f"API Key   : {mask_key(client.api_key)}")

    # ---------- 图像生成（仅显式开启） ----------
    image_result = {"attempted": False, "ok": False, "model": None, "path": None, "error": None}
    if args.with_image:
        print("\n[生图] 尝试生成 1 张示例图…")
        smoke_prompt = ("A pediatric resident doctor wearing a small smart badge device in a hospital ward, "
                        "warm lighting, clean modern clinical environment, no visible patient faces, "
                        "photorealistic, soft blue tones")
        from core import ensure_output_dir
        out_dir = ensure_output_dir()
        image_result = {"attempted": True, **client.generate_image(
            smoke_prompt, out_dir / "sample_image.png")}
        if image_result["ok"]:
            print(f"[生图] 成功: {image_result['model']} -> {image_result['path']}")
        else:
            print(f"[生图] 失败（如实标注，不影响文案主流程）: {image_result['error']}")

    # ---------- 主流水线 ----------
    def progress(stage, done, total, msg):
        print(f"  [{stage}] ({done}/{total}) {msg}", flush=True)

    print(f"\n开始生成：{len(platforms)} 平台 × {len(languages)} 语种 = "
          f"{len(platforms) * len(languages)} 条文案（完成一条打印一条）\n")
    try:
        pipeline = Pipeline(client, product, languages, platforms, level,
                            models=models, max_workers=args.max_workers,
                            batch_deadline_s=args.deadline)
        result = pipeline.run(progress_cb=progress)
    except (ValueError, RuntimeError) as e:  # noqa: BLE001 配置/门禁错误如实暴露
        sys.exit(f"[FATAL] {e}")
    result["image"] = image_result

    # ---------- 落盘 ----------
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = root / out_path
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    _print_result_summary(result, out_path)
    print(f"\n墙钟总耗时（含生图与重试）: {time.time() - t0:.1f}s")
    return 0


def _rerun_failed(args, root: Path) -> int:
    """从已有结果 JSON 单独重跑非可交付项（P0-4：失败项单独重跑）。"""
    src = Path(args.rerun_failed)
    if not src.is_absolute():
        src = root / src
    if not src.exists():
        sys.exit(f"[FATAL] 结果文件不存在: {src}")
    result = json.loads(src.read_text(encoding="utf-8"))
    ctx = result.get("context") or {}
    meta = result.get("meta") or {}
    todo = [r for r in result.get("results", []) if r.get("status") != "delivered"]
    if not todo:
        print("没有需要重跑的项（全部可交付）")
        return 0
    print(f"待重跑 {len(todo)} 项: {[r['combo_id'] for r in todo]}")
    try:
        client = LLMClient()
        pipeline = Pipeline(client, result.get("product") or {}, meta.get("languages", []),
                            meta.get("platforms", []), meta.get("compliance_level", "strict"),
                            models=meta.get("models"), batch_deadline_s=args.deadline)
    except (ValueError, RuntimeError, PrivacyViolation) as e:
        sys.exit(f"[FATAL] 重建流水线失败: {e}")
    platform_copies = ctx.get("platform_copies") or {}
    new_results = []
    for r in result.get("results", []):
        if r.get("status") == "delivered":
            new_results.append(r)
            continue
        p, l = r["platform"], r["language"]
        print(f"  重跑 {p}×{l} …", flush=True)
        item = pipeline._run_combo(p, l, platform_copies.get(p))
        label = STATUS_LABELS.get(item["status"], item["status"])
        print(f"  重跑 {p}×{l} → {label}"
              + (f"（{str(item.get('error'))[:120]}）" if item.get("error") else ""))
        new_results.append(item)
    result["results"] = new_results
    result = pipeline.finalize(result)
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = root / out_path
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_result_summary(result, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
