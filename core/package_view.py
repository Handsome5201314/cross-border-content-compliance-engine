import json

import streamlit as st

from . import ENGINE_ROOT, load_yaml_config
from .package_agent import render_preview
from .site_agent import SiteAgent
from .live_agent import LiveAgent
from jsonschema.exceptions import ValidationError
from .privacy_gate import PrivacyViolation


def show_packages():
    labels = load_yaml_config("package_copy")["ui"]
    with st.expander(labels["title"], expanded=False):
        paths = sorted((ENGINE_ROOT / "output").glob("*.json"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
        if not paths:
            st.info(labels["empty"])
            return
        selected = st.selectbox(labels["select"], paths, format_func=lambda path: path.name)
        try:
            result = json.loads(selected.read_text(encoding="utf-8"))
            if result["meta"]["module"] not in {"site", "live"}:
                raise ValueError("非深水模块内容包")
            st.caption(result["product"]["product_name"])
            st.write(labels["generated"], result["meta"]["generated_at"])
            st.write(labels["counts"], result["counts"])
            st.write(labels["usage"], result["usage"])
            st.warning(labels["warning"])
            agent = {"site": SiteAgent, "live": LiveAgent}[result["meta"]["module"]](None)
            for item in result["results"]:
                st.subheader(f"{item['module']} / {item['language']} / {item['status']}")
                if item["status"] == "delivered":
                    try:
                        agent.validate_delivery(item, result["product"])
                    except (ValueError, ValidationError, PrivacyViolation, KeyError, IndexError) as error:
                        st.error(f"{labels['stale']}: {error}")
                    else:
                        st.iframe(render_preview(item), height=650)
                        for warning in item["warnings"]:
                            st.warning(warning)
                else:
                    st.error(item["error"])
                rounds = item.get("compliance", {}).get("rounds", [])
                risks = [{labels["round"]: index + 1,
                          labels["risk_sentence"]: finding["risk_sentence"],
                          labels["reason"]: finding["reason"],
                          labels["replacement"]: finding["replacement"]}
                         for index, review in enumerate(rounds)
                         for finding in review["findings"] if finding["verdict"] == "violation"]
                if risks:
                    st.write(labels["risk_table"])
                    st.dataframe(risks, hide_index=True)
                elif rounds:
                    st.caption(labels["no_risks"])
                with st.expander(labels["audit"]):
                    st.json(item.get("compliance", {}))
            st.download_button(labels["download"], json.dumps(result, ensure_ascii=False, indent=2),
                               file_name=selected.name, mime="application/json", key="deep-package-download")
        except (OSError, ValueError, KeyError, TypeError) as error:
            st.error(f"{labels['error']}: {error}")
