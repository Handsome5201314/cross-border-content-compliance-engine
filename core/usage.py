"""Aggregate diagnostic snapshots by run ID, preserving previous rerun consumption."""
import copy


def update_result_usage(result, client):
    current = client.usage_summary()
    runs = result.setdefault("usage_runs", [])
    previous = result.get("usage")
    if not runs and previous and previous.get("task_id") != current.get("task_id"):
        runs.append(copy.deepcopy(previous))
    for index, run in enumerate(runs):
        if run.get("task_id") == current.get("task_id"):
            runs[index] = current
            break
    else:
        runs.append(current)
    combined = {}
    for key in ("total_calls", "total_tokens", "prompt_tokens", "completion_tokens",
                "attempted_calls", "unknown_usage_calls", "cost_estimate_cny", "wall_time_s"):
        combined[key] = sum(run.get(key, 0) for run in runs)
    combined["cost_estimate_cny"] = round(combined["cost_estimate_cny"], 4)
    combined["wall_time_s"] = round(combined["wall_time_s"], 1)
    for group in ("by_model", "by_tag"):
        combined[group] = {}
        for run in runs:
            for name, values in run.get(group, {}).items():
                target = combined[group].setdefault(name, {})
                for key, value in values.items():
                    target[key] = target.get(key, 0) + value
    combined["usage_complete"] = all(run.get("usage_complete", False) for run in runs)
    combined["billing_ready"] = False
    combined["price_note"] = "各次运行已知文本用量的估算合计，非账单；历史未计量/未知用量及图片费用不在估算内。"
    combined["task_id"] = current.get("task_id")
    result["usage"] = combined
    if hasattr(client, "attempts"):
        by_id = {r["attempt_id"]: r for r in result.get("attempt_records", [])}
        by_id.update({r["attempt_id"]: r for r in client.attempts()})
        result["attempt_records"] = list(by_id.values())
    return combined
