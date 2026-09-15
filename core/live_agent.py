from .package_agent import PackageAgent, TEXT, obj, array, capability_schema


class LiveAgent(PackageAgent):
    def __init__(self, client):
        super().__init__(client, "live", "live_script")

    def schema(self, product):
        return obj(title=TEXT, capabilities=capability_schema(),
                   timeline={"type": "array", "minItems": 8, "maxItems": 8,
                             "items": obj(stage={"enum": self.cfg["stages"]}, title=TEXT,
                                          duration_seconds={"type": "integer", "minimum": 1},
                                          talking_points=array(TEXT),
                                          spoken={"type": "string", "pattern": r"(?s)^(?=.*\[pause\])(?=.*\*\*.+?\*\*).+$"},
                                          action=TEXT)},
                   interaction=obj(prompts=array(TEXT), objections=array(
                       obj(topic={"enum": self.cfg["objection_topics"]}, question=TEXT, answer=TEXT), 6)),
                   notices=array(TEXT, 0))

    def validate_content(self, content, product, language):
        super().validate_content(content, product, language)
        if [segment["stage"] for segment in content["timeline"]] != self.cfg["stages"]:
            raise ValueError("直播时间轴顺序不完整")
        topics = [item["topic"] for item in content["interaction"]["objections"]]
        if sorted(topics) != sorted(self.cfg["objection_topics"]):
            raise ValueError("异议应答主题缺失或重复")
        compliance_spoken = content["timeline"][5]["spoken"]
        for notice in self.notices(product, language):
            if notice not in compliance_spoken:
                raise ValueError("合规段口播缺少必需声明原文")
        if not any("[pause]" in segment["spoken"] and "**" in segment["spoken"]
                   for segment in content["timeline"]):
            raise ValueError("口播缺少停顿与重音标记")
