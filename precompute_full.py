# -*- coding: utf-8 -*-
"""离线预生成全量产物（6 平台 × 7 语种 = 42 条），供现场演示「加载预生成全量」模式。

对应评审 P0-4 / 任务书演示策略：现场不做全量真跑（时间不可控），
提前离线跑一次全量并存盘、标注生成时间与真实用量；UI/展示读这份缓存。

运行（建议非现场时段执行，预计数分钟到十几分钟，--deadline 给足预算）：
  C:/Python314/python.exe precompute_full.py
  可选：--workers 6 --deadline 3600

产物：samples/precomputed_full.json（含 generated_at / usage / value / 全部 results）
"""
import argparse
import json
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import load_yaml_config                                # noqa: E402
from core.config_check import validate_all_configs               # noqa: E402
from core.llm_client import LLMClient                            # noqa: E402
from core.pipeline import Pipeline                               # noqa: E402
from core.privacy_gate import PrivacyViolation, validate_product # noqa: E402

ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / "samples" / "precomputed_full.json"


def main():
    ap = argparse.ArgumentParser(description="离线预生成全量产物（6 平台 × 7 语种）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--deadline", type=float, default=3600.0, help="整批预算秒数（默认 3600）")
    ap.add_argument("--platforms", default=None, help="逗号分隔（默认产品全部 6 平台）")
    ap.add_argument("--langs", "--languages", dest="languages", default=None)
    args = ap.parse_args()

    errors = validate_all_configs()
    if errors:
        sys.exit("[FATAL] 配置校验失败:\n  - " + "\n  - ".join(errors))
    try:
        product = validate_product(json.loads(
            (ROOT / "samples" / "product_medical_appliance.json").read_text(encoding="utf-8")))
    except PrivacyViolation as e:
        sys.exit(str(e))

    cfg = load_yaml_config("platforms")
    platforms = (args.platforms.split(",") if args.platforms else product.get("target_platforms"))
    languages = (args.languages.split(",") if args.languages else product.get("target_languages"))
    platforms = [p.strip() for p in platforms if p.strip() in cfg]
    languages = [l.strip() for l in languages if l.strip() in load_yaml_config("languages")]
    print(f"预生成全量：{len(platforms)} 平台 × {len(languages)} 语种 = "
          f"{len(platforms) * len(languages)} 条（预算 {args.deadline:.0f}s，workers={args.workers}）")

    try:
        client = LLMClient()
    except RuntimeError as e:
        sys.exit(str(e))

    def progress(stage, done, total, msg):
        print(f"  [{stage}] ({done}/{total}) {msg}", flush=True)

    t0 = time.time()
    pipeline = Pipeline(client, product, languages, platforms, "strict",
                        max_workers=args.workers, batch_deadline_s=args.deadline)
    result = pipeline.run(progress_cb=progress)
    result["meta"]["precomputed"] = {
        "purpose": "离线预生成全量产物，供现场演示「加载预生成全量」模式",
        "wall_seconds": round(time.time() - t0, 1),
    }

    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = result.get("counts", {})
    print("\n" + "=" * 64)
    print(f"预生成完成：可交付 {counts.get('delivered', 0)} / 审核未通过 {counts.get('review_blocked', 0)}"
          f" / 失败 {counts.get('failed', 0)} / 超时 {counts.get('skipped_deadline', 0)}")
    print(f"耗时 {result['usage']['wall_time_s']}s / tokens {result['usage']['total_tokens']}"
          f" / API 估算 ¥{result['usage']['cost_estimate_cny']}")
    print(f"生成时间标注: {result['meta']['generated_at']}")
    print(f"已写入: {OUT_PATH}")
    if counts.get("delivered", 0) < len(platforms) * len(languages):
        print("提示：存在未交付项，可用 engine_cli.py --rerun-failed "
              f"{OUT_PATH} 重跑非可交付项后重新预生成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
