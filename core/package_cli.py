import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import ENGINE_ROOT, load_yaml_config
from .llm_client import LLMClient
from .config_check import validate_all_configs
from .live_agent import LiveAgent
from .market_rules import build_market_report
from .package_agent import render_preview
from .privacy_gate import validate_product
from .site_agent import SiteAgent


def run_batch(product, module, languages, client):
    product = validate_product(product)
    config_errors = validate_all_configs()
    if config_errors:
        raise ValueError(f"配置校验失败: {config_errors}")
    agent = {"site": SiteAgent, "live": LiveAgent}[module](client)
    if not languages or len(languages) != len(set(languages)) or not set(languages) <= set(agent.cfg["languages"]):
        raise ValueError("语种为空、重复或不支持")
    start = time.monotonic()

    def run_language(language):
        try:
            return agent.run(product, language)
        except Exception as error:
            return {"platform": module, "module": module, "language": language,
                    "status": "failed", "error": f"{type(error).__name__}: {str(error)[:1500]}",
                    "content": None, "rtl": load_yaml_config("languages")[language]["rtl"],
                    "warnings": [], "compliance": {"rounds": []}, "markets": product["target_markets"]}

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run_language, languages))
    compliance = load_yaml_config("compliance")
    labels = load_yaml_config("package_copy")
    market_report = build_market_report(product["target_markets"], results, compliance)
    if product["compliance_level"] != "strict":
        market_report = {market: {"status": "pending_verification",
                                  "access_pending": [labels["consumer_access_pending"]],
                                  "data_sovereignty_pending": [labels["consumer_data_pending"]]}
                         for market in product["target_markets"]}
    notices = {**compliance["engine_notices"], "deployment_clarification": labels["engine_notice"]}
    return {"meta": {"schema_version": "3.0", "module": module, "languages": languages,
                     "generated_at": datetime.now(timezone.utc).isoformat(),
                     "elapsed_s": round(time.monotonic() - start, 2)},
            "product": product, "results": results,
            "counts": {"total": len(results), **{status: sum(item["status"] == status for item in results)
                       for status in ["delivered", "review_blocked", "failed", "skipped_deadline"]}},
            "usage": client.usage_summary(), "usage_records": list(client.usage_records),
            "market_report": market_report,
            "engine_notices": notices}


def main(module):
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=f"{module} 深水内容包真实生成")
    parser.add_argument("--product", default=str(ENGINE_ROOT / "samples/product_medical_appliance.json"))
    parser.add_argument("--langs", default="en,ar")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    product_path = Path(args.product)
    output = args.out or ENGINE_ROOT / "output" / f"{module}_{product_path.stem}.json"
    try:
        product = validate_product(json.loads(product_path.read_text(encoding="utf-8")))
        result = run_batch(product, module, [lang.strip() for lang in args.langs.split(",")], LLMClient())
    except (ValueError, OSError, RuntimeError) as error:
        print(f"启动失败: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(".json.tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(output)
    for item in result["results"]:
        preview = output.with_name(f"{output.stem}_{item['language']}.html")
        if item["status"] == "delivered":
            preview.write_text(render_preview(item), encoding="utf-8")
        elif preview.exists():
            preview.unlink()
        print(f"{module}/{item['language']}: {item['status']} {item['error'] or ''}")
    print(json.dumps({"output": str(output), "counts": result["counts"],
                      "elapsed_s": result["meta"]["elapsed_s"], "usage": result["usage"]}, ensure_ascii=False))
    return 0 if all(item["status"] == "delivered" for item in result["results"]) else 1
