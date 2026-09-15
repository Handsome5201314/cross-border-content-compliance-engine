from .package_agent import PackageAgent, TEXT, obj, array, capability_schema


class SiteAgent(PackageAgent):
    def __init__(self, client):
        super().__init__(client, "site", "site_blocks")

    def schema(self, product):
        return obj(hero=obj(headline=TEXT, subtitle=TEXT, cta=TEXT),
                   audiences=array(obj(buyer=TEXT, pain=TEXT)),
                   solution=TEXT, capabilities=capability_schema(),
                   evidence=array(obj(claim=TEXT, source=TEXT)),
                   compliance=obj(data_sovereignty=TEXT, market_access=TEXT),
                   faq={"type": "array", "minItems": 8, "maxItems": 10,
                        "items": obj(topic=TEXT, question=TEXT, answer=TEXT)},
                   cta=obj(label=TEXT, next_step=TEXT),
                   seo=obj(title=TEXT, description=TEXT, keywords=array(TEXT)),
                   notices=array(TEXT, 0))

    def validate_content(self, content, product, language):
        super().validate_content(content, product, language)
        topics = [item["topic"] for item in content["faq"]]
        if not set(self.cfg["faq_topics"]) <= set(topics) or len(set(topics)) != len(topics):
            raise ValueError("FAQ 主题缺失或重复")

    def warnings(self, content):
        return [f"SEO {key} 长度 {len(content['seo'][key])} 超过 {limit} 字符，需运营精简"
                for key, limit in self.cfg["seo_limits"].items() if len(content["seo"][key]) > limit]
