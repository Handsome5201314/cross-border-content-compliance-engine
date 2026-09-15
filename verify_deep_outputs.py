import argparse
import copy
import json
from pathlib import Path

from core.live_agent import LiveAgent
from core.site_agent import SiteAgent
from core.package_agent import scan_content, render_preview, validate_review
from core.privacy_gate import validate_product


def verify(path):
    report = json.loads(path.read_text(encoding="utf-8"))
    product = validate_product(report["product"])
    agent = {"site": SiteAgent, "live": LiveAgent}[report["meta"]["module"]](None)
    records = report["usage_records"]
    assert sum(record["total_tokens"] for record in records) == report["usage"]["total_tokens"]
    assert len(records) == report["usage"]["total_calls"]
    delivered = 0
    for item in report["results"]:
        if item["status"] != "delivered":
            assert item["content"] is None
            continue
        delivered += 1
        agent.validate_delivery(item, product)
        agent.validate_content(item["content"], product, item["language"])
        latest = item["compliance"]["rounds"][-1]
        assert scan_content(item["content"]) == latest["sentences"], "发布内容与审查快照不同"
        verdict = {"checked_ids": latest["checked_ids"], "findings": [
            {key: finding[key] for key in ["id", "verdict", "reason", "replacement"]}
            for finding in latest["findings"]]}
        validate_review(verdict, latest["sentences"])
        assert all(finding["verdict"] == "benign" for finding in latest["findings"])
        assert ('dir="rtl"' in render_preview(item)) == (item["language"] == "ar")
        altered = copy.deepcopy(item["content"])
        planned = next((cap for cap in altered["capabilities"] if cap["status"] == "planned"), None)
        if planned:
            planned["status"] = "implemented"
            try:
                agent.validate_content(altered, product, item["language"])
            except ValueError:
                pass
            else:
                raise AssertionError("规划能力改成上市未被拦截")
        if agent.module == "live":
            altered = copy.deepcopy(item["content"])
            for notice in agent.notices(product, item["language"]):
                altered["timeline"][5]["spoken"] = altered["timeline"][5]["spoken"].replace(notice, "Review details.")
            try:
                agent.validate_content(altered, product, item["language"])
            except ValueError:
                pass
            else:
                raise AssertionError("删除强制口播声明未被拦截")
    assert delivered == report["counts"]["delivered"]
    print(f"{path.name}: {delivered}/{len(report['results'])} delivered; usage and review snapshot verified")
    return delivered


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="验证真实生成快照与发布门禁，不调用 API")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.paths:
        verify(path)
