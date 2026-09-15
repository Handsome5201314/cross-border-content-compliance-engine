# 跨境卖家 AI 多平台文案智造引擎 v3（按评审返工版）

## R2-05：独立站与跨境直播

本轮为新增内容包，不改既有社媒生成链路。独立站用于获客官网文案，不是商城；
直播模块输出脚本、口播与互动话术，不负责推流。产品事实仍来自输入 JSON 和隐私白名单。

运行示例（在本目录执行，凭证仍只读项目根 .env）：

```powershell
C:/Python314/python.exe site_cli.py --product samples/product_medical_appliance.json --langs en,ar
C:/Python314/python.exe live_cli.py --product samples/product_medical_appliance.json --langs en,ar,id
C:/Python314/python.exe site_cli.py --product samples/product_consumer_demo.json --langs en,ar
C:/Python314/python.exe live_cli.py --product samples/product_consumer_demo.json --langs en,ar,id
C:/Python314/python.exe -m unittest test_deep_modules -v
```

- 两模块支持 en/ar/id/th/vi/ms；--out 可指定 JSON 路径，默认输出 output/{module}_{product文件名}.json。
- 独立站 content：hero、audiences、solution、capabilities、evidence、compliance、faq、cta、seo、notices。
- 直播 content：title、capabilities、timeline、interaction、notices。时间轴固定八段，时长为正整数秒。
- 结果沿用 meta/product/results/counts/usage 外层；每项含 platform/module/language/status/error，
  新模块业务内容在 content，不冒充旧社媒 final_copy；旧社媒导出器不处理新模块。
- 逐句审查覆盖所有发布字符串（含 CTA/SEO/FAQ/演示动作）；每条保留路径、原句、信号和裁决。
  风险项提供 risk_sentence → reason → replacement；无信号但语义有风险也可被识别。
- 首次结构失败最多请求一次显式修复，成功后记录 warning；隐私阻断不修复、不外发。
  审查回复漏句或缺少正则裁决时，最多请求一次显式补审；仍不完整即失败，不自动放行。
  风险改写后必须整稿再审；仍有风险不提供 content 或 HTML，不为演示凑交付。
- planned 能力保留状态和本地化 roadmap 标签；医疗免责声明强制进入直播合规段口播。
  消费品保留虚构声明，采用消费品准入待核清单而不是医疗器械准入报告。
- 阿语结果 rtl=true；只有 delivered 项生成同名 _{language}.html，所有 HTML 内容转义。
  运行失败清理同名旧预览。SEO 超限按既有软告警口径处理，发布前仍须精简至目标字符数。
- Web：运行原 app.py，展开顶部“独立站与跨境直播内容包”，读取 output/ 中最新文件，
  可查看安全稿、审查记录、耗时与用量并下载审计 JSON；页面不自动触发付费生成。
  历史稿读取时按当前规则复核，不通过则拒绝预览。目标语稿件混入中文、微信被替换为其他平台均拒绝交付。
- JSON 含未发布风险原句，是审计工作包，不可直接作为发布稿。HTML 才只包含审核后内容。
  CTA 仅是交接给官网运营方的文案，不含假按钮、假链接或未实现的留资后端。
- 准入/数据主权都是 pending_verification；不联网核实法规，不把本地部署等同于合规通过。
  内容与翻译仍须专业人员和当地业务方复核，产品事实并未被外部证实。
- 新增逻辑：core/site_agent.py、live_agent.py、package_agent.py、package_cli.py、package_view.py；
  配置：config/site_blocks.yaml、live_script.yaml、package_copy.yaml。新增依赖 jsonschema。
- 真实产物复核：C:/Python314/python.exe verify_deep_outputs.py output/文件名.json；
  验证用量归因、最终审查与发布稿逐字一致、规划状态篡改拒绝、口播声明删改拒绝及 RTL。


> AI+跨境黑客松巅峰赛 · 复赛交付 | 场景：真实产品出海营销
> **真实产品**：301 儿科住院医师 AI 助手（软件）+ 智能胸卡（硬件）→ 东南亚/中东/俄语区备选
> 差异化：不只是「写得多」，更是「写得合规」——双合规门禁（营销文案合规 + 市场准入/数据主权待核清单）

---

## 0. 环境搭建（快速开始 · 复赛评审重点）

本仓库是「跨境内容合规引擎」的代码与交付物集合。本地跑通只需三步：

### 0.1 克隆与依赖
```bash
git clone <你的 GitCode 仓库地址> hackathon_round2
cd hackathon_round2
C:/Python314/python.exe -m pip install -r requirements.txt
```
> 依赖：pyyaml / openai / streamlit / jsonschema（见 `requirements.txt`）。Python 需用 3.14（`C:/Python314/python.exe`）。

### 0.2 配置凭证（只写变量名，禁止写字面量）
引擎运行时从仓库根目录（或上层目录）的 `.env` 读取网关地址与密钥：
```ini
# .env
HACKATHON_BASE_URL=<网关基础地址>
HACKATHON_API_KEY=<你的 API Key>
```
> 这两个变量名是唯一需要的；**任何代码 / 日志 / 文档都不含 Key 字面量**，Key 在终端以 `sk-****` 掩码显示（`core/llm_client.py` 的 `mask_key`）。

### 0.3 运行
```bash
# 一键离线自检（不调 API，约数秒）—— 提交物 Checklist #4 的证据来源
C:/Python314/python.exe _verify_all.py --skip-e2e

# Web 演示（三种模式，含离线预生成全量）
C:/Python314/python.exe -m streamlit run app.py

# 命令行端到端（需 .env 凭证；site/live 为独立站 / 跨境直播内容包）
C:/Python314/python.exe engine_cli.py --product samples/product_medical_appliance.json --platforms tiktok,instagram --langs en,ar
C:/Python314/python.exe site_cli.py   --product samples/product_medical_appliance.json --langs en,ar
C:/Python314/python.exe live_cli.py  --product samples/product_medical_appliance.json --langs en,ar,id
```
> 双击 `start_demo.bat` 可在 Windows 上一键起 Streamlit（自动检测并安装依赖）。

完整运行说明、状态机与口径声明见下方「二、怎么跑」「三、数据流」「四、重要口径声明」。

---

## 一、本轮返工总览（对照 Codex 评审 16 条）

| 评审项 | 修法（一句话） | 落点 |
|---|---|---|
| P0-1 依赖 | requirements.txt + 验收脚本真实 import | `requirements.txt`、`_verify_all.py` 步骤0 |
| P0-2 隐私门禁 | 字段白名单 + 本地敏感扫描（命中即阻断）+ 出站消息统一拦截 + UI/CLI 显式区分「产品本地部署 vs 引擎上云」 | `core/privacy_gate.py`、`core/llm_client.py`、`app.py` 顶部声明 |
| P0-3 合规 fail-closed | Agent 响应严格 schema 校验；空报告/缺字段→review_failed；状态分 可交付/审核未通过/失败/超时，只有可交付计入产出 | `core/agents.py`（merge_compliance/SchemaError）、`core/pipeline.py` |
| P0-4 时间预算 | 默认小批次真跑 + 全量离线预生成（precompute_full.py）+ 单层分类重试 + 整批 deadline + 逐条呈现 + 失败项单条重跑 + 默认不生图 | `core/pipeline.py`、`core/llm_client.py`、`precompute_full.py`、`engine_cli.py` |
| P1-5 产品事实 | 按 V1B 重建：13 项能力全部带 状态(已实现/规划中)+依据；无认证字段、无性能数字 | `samples/product_medical_appliance.json` |
| P1-6/7 价值口径 | 两侧同验收标准：人工=研究+平台撰写(不按语言重复)+本地化+终审；AI=资料准备+API+逐条复核+返修；零成功→「无有效产出，无法估算」；市场按实际可交付计 | `core/value_calc.py` |
| P1-8 平台契约 | titles[]/descriptions[]/bullets[]/hashtags[]/extras{} 贯穿三 Agent；长度单位统一 char（避开泰语分词歧义）；对最终内容逐字段硬校验、无容忍倍率 | `config/platforms.yaml`、`core/agents.py` |
| P1-9 导出残留 | 安全版覆盖全部发布字段（含 safe_hashtags/safe_extras）；发布稿导出只消费 final_copy | `core/agents.py`、`core/exporter.py` |
| P1-10 免责声明 | 分语种声明为确定性配置（启动校验非空），流水线追加、不经模型；含「AI 草稿需医生审核」 | `config/compliance.yaml`、`core/pipeline.py` |
| P1-11 市场规则 | 市场独立于语言（语种∩目标市场）；启动校验所有引用；按市场输出 广告风险/准入待核/数据主权待核，恒为 pending_verification | `core/market_rules.py`、`core/config_check.py` |
| P1-12 正则规则 | 修 `100%` 词边界漏报；命中一律为待审信号，语义层逐条裁决（否定语境 benign 保留）；未裁决按违规保守处理 | `config/compliance.yaml`、`core/agents.py` |
| P2-13 token 归因 | 每次调用带组合 tag，聚合各 Agent 返回的 usage records，不做全局差值 | `core/llm_client.py`、`core/pipeline.py` |
| P2-14 导出快照 | 看板参数重算 value 后，页面与 JSON/Markdown 导出用同一份快照 | `app.py`、`core/exporter.py` |
| P2-15 验收脚本 | 分配置 schema 校验；Key 扫描覆盖 sk-sp- 前缀只输出位置；新增 空报告/零成功/导出残留/100%/否定语境/隐私门禁 回归用例 | `_verify_all.py` |
| P2-16 RTL | ar 语种提供 dir=rtl + html 转义的安全阅读预览，保留纯文本复制 | `app.py`（rtl_preview）、`core/exporter.py` |

## 二、怎么跑

### 环境

- Python：`C:/Python314/python.exe`（3.14.x）
- 凭证：项目根 `E:\AI新青年\.env` 的 `HACKATHON_API_KEY` / `HACKATHON_BASE_URL`（只从这里读，任何文件不含 Key 字面量）
- 依赖：`C:/Python314/python.exe -m pip install -r requirements.txt`（pyyaml / openai / streamlit）

### 一键自检（含 8 组离线回归用例 + API 冒烟 + 端到端）

```bash
cd E:\AI新青年\output\hackathon_round2
C:/Python314/python.exe _verify_all.py            # 全量
C:/Python314/python.exe _verify_all.py --skip-e2e # 只跑离线检查（不调 API）
```

### CLI 端到端（默认小批次口径）

```bash
C:/Python314/python.exe engine_cli.py --product samples/product_medical_appliance.json --platforms tiktok,instagram --langs en,ar
# --deadline 900 整批预算；--with-image 才生图（默认不生）
# 失败项单独重跑：C:/Python314/python.exe engine_cli.py --rerun-failed run_result.json
```

### 离线预生成全量（演示用，非现场时段执行一次）

```bash
C:/Python314/python.exe precompute_full.py          # 6 平台 × 7 语种 → samples/precomputed_full.json
```

### Web Demo（三种演示模式）

```bash
C:/Python314/python.exe -m streamlit run app.py
```

1. **小批次真跑**（默认 3 平台 × 2 语种）：完成一条显示一条；失败项如实展示且可单条重跑；
2. **加载预生成全量**：读取 `samples/precomputed_full.json`，标注生成时间/耗时/tokens/成本；
3. **单条重跑**：任意未交付卡片上的「🔄 单条重跑」按钮。

## 三、数据流与状态机

```
产品 JSON ──► 隐私门禁(白名单+敏感扫描) ──► Pipeline
                                            │ 卖点拆解(失败→确定性降级,不终止)
                                            ▼
                              平台适配 ×N（结构化契约 titles/descriptions/bullets/hashtags/extras）
                                            ▼
              组合级（并行，tag=平台__语种 归因 token）：本地化 → 正则待审信号
                                            ▼
              合规裁决（跨厂商模型）：signal_verdicts(violation|benign) + 安全版(全字段)
                                            ▼
              交付门禁（fail-closed）：风险 high / 未裁决残留 / 字段合同违规 → review_blocked
                                            ▼
              delivered（可交付）：免责声明确定性追加 → 素材 Prompt
                                            ▼
              统计：usage(按模型/按tag) + 市场报告(准入/数据主权待核) + 价值账(零成功不外推)
```

状态：`delivered`（可交付，唯一计入产出与价值）/ `review_blocked`（审核未通过，可单条重跑）/
`failed`（生成失败）/ `skipped_deadline`（整批预算耗尽未执行）——全部如实展示。

## 四、重要口径声明（现场答辩用）

1. **本地产品 ≠ 本地引擎**：被营销产品全本地部署、数据不出院（卖点）；本引擎生成文案会调用云端大模型，输入已过本地隐私门禁。
2. **正则命中 ≠ 违规**：正则层只产生待审信号，由语义层裁决；否定语境（Not a cure）判 benign 并保留；未裁决信号保守按违规。
3. **合规输出不构成法律意见**：准入/数据主权均为「待核清单」，状态恒为 pending_verification（未知不视为通过）。
4. **价值账参数为可调假设**：不是行业估值、不是实际成本；API 费用为按单价表的估算值。
5. **无证据不写**：产品无认证信息 → 文案禁止出现任何认证宣称；无性能数字 → 禁止出现毫秒级/准确率等数字。

## 五、已知限制与风险（如实）

1. **图像模型未验证**：`--with-image` / UI 显式按钮才会调用；失败如实上报，不影响文案主流程。
2. **成本单价是估算**：网关未公布价格，`PRICE_CNY_PER_MTOK` 为可调估值。
3. **合规规则库是工程化整理**：未联网核验各国法规原文；正式商用需逐国核对。
4. **长度合同可能触发 review_blocked**：Google Ads 30 字符等硬限制对模型输出很紧，安全版超限会被门禁拦下（可用单条重跑修复）——这是有意的 fail-closed 设计。
5. **预生成全量已随仓库提交**：`samples/precomputed_full.json` 已入库（42 条真实组合：18 可交付 / 6 审核未过 / 18 失败，如实统计），Web 演示「加载预生成全量」模式开箱即用，无需现场重跑；如需刷新再执行 `precompute_full.py`。
