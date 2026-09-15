# 现有引擎安全边界与服务接入

2026-09-15。用户授权直接修复参赛引擎，优先处理现有代码风险；企业租户、多成员共享积分和 PostgreSQL 是后续服务的确认决策。本轮没有新增认证、企业账本、任务数据库或上线部署。

## 当前行为

- 凭证按显式成对参数→环境变量→本地文件读取；显式空环境变量不回退，文件只返回两个允许字段。服务使用 `LLMClient(..., allow_env_file=False)` 或显式注入成对凭证，避免开发机文件回退。Key 显示不保留任何原字符。网关必须是 HTTPS，不能把凭证放入 URL。
- 浏览器自定义网关默认只允许百炼的 `token-plan.cn-beijing.maas.aliyuncs.com` 和 `dashscope.aliyuncs.com`。管理员可通过逗号分隔的 `HACKATHON_UI_GATEWAY_ORIGINS` 增加可信 HTTPS origin；配置不得包含不受信任域名、内部服务或临时 DNS。仅同源允许不代表路径授权，生产还须配置出站代理/网络防火墙。CLI 的成对凭证配置属于本机操作者可信输入。
- 当前消息契约是 `role/content` 文本消息；多模态、tool_calls、额外字段明确拒绝，避免绕过本地隐私检查。模型名限定 1–128 位 ASCII 标识符并过隐私扫描，temperature/size 只接受约定的数值形式；既有 Agent 均使用此契约。SDK 禁止自动跟随重定向。
- 每个逻辑 chat 最多两个 provider attempts，包括参数回退。仅明确 400 enable_thinking 参数拒绝或 429 可重试；超时、连接错误、5xx 不自动重放。默认输出上限 8192 tokens，显式上限为 1–32768 整数。
- 原始响应 JSON 中的 token 字段必须是非负整数且总和相符；缺失/字符串/布尔/负数不会当成免费成功。合法用量在后续解析失败前记录。上游错误与非法 JSON 不回显响应正文。
- 每个 Pipeline/工作包批次有独立 task_id 和统计；重跑通过 usage_runs 保留过去消耗，重复 finalize 不重复相加。预生成模式仅首次或显式重新加载文件，不再每次页面交互覆盖本会话结果。total_calls 保留“已有合法文本用量记录数”的兼容含义；attempted_calls 才是实际尝试数。attempt_records 含未知/拒绝/已知状态；cost_estimate_cny 只统计已知文本混合单价估算。usage_complete=false 表示未知费用；billing_ready 始终为 false。历史快照缺失用量标识时也不可作完整账单。
- 每次外部 attempt 前检查本任务取消/单调时钟 deadline，包括后续审查、JSON 修复及兼容重试。只阻止新调用；已进入 SDK 的请求无法撤回，线程池会等待在途请求，SDK timeout 也不是整个进程的硬截止期限。完成的响应即使晚到仍保留已知消耗。没有持久化任务取消 API。
- 图像仅调用明确选择的模型；最多一次明确 size 参数兼容回退。生成成功后的下载/解码/落盘失败绝不再次生成。图像费用显式为未知，不计入文本估算。
- 下载只允许无凭证的公网 HTTPS/443，校验全部解析地址后用已验证数字 IP 连接，TLS 校验原 hostname；不读取代理设置、不跟随重定向。最多 10 MiB、2500 万像素，严格 base64 和 PNG/JPEG/WebP 文件校验。DNS 解析耗时仍受操作系统控制；生产需受控 DNS 和网络超时策略。
- JSON、HTML 和图片使用同目录随机暂存文件、flush/fsync、原子替换；Windows 共享冲突有限重试后明确失败。失败保留旧文件并清理本次暂存，UI/CLI 默认图片使用 UUID 文件名。

## 尚未具备的服务能力

`output_io` 只处理可信本地 CLI 路径，不提供多租户授权、句柄相对沙箱或数据库事务，也不能保证 JSON 和多个 HTML 文件整体原子发布；同名并发发布仍是最后一次成功写入获胜。严禁把 HTTP 用户路径直接传给它。多租户文件必须由资源 ID 查询企业归属后进入安全存储层，以不可变版本、staging/ready 和恢复对账发布，详见 `../ui-redesign/ARCHITECTURE_FIXES.md`。

模型 attempts 当前在进程内。进程崩溃可丢失诊断，不能用于积分预留/退款或 exactly-once 承诺。服务须在每次请求前持久化调用意图、预留最坏费用，使用 PostgreSQL 企业账户行锁、幂等操作、outbox、worker 租约/fencing 与对账状态；未知用量必须留待核对。企业成员退出不销毁企业余额或账本。

Streamlit 仍是演示应用，当前没有多租户认证、企业配额、服务端共享额度防滥用能力。不要作为公开共享平台密钥服务部署。本轮没有真实付费模型验收，不能据离线通过推断当前网关对 token 上限、size 参数和图片 URL 契约的兼容性。

## 验证入口

```powershell
C:/Python314/python.exe -B -m unittest test_engine_boundaries test_deep_modules test_app_boundaries -v
C:/Python314/python.exe -B _verify_all.py --skip-e2e
C:/Python314/python.exe -B engine_cli.py --help
C:/Python314/python.exe -B site_cli.py --help
C:/Python314/python.exe -B live_cli.py --help
```

边界测试使用合成凭证、httpx.MockTransport、临时目录及本地 socketpair，不调用真实模型。Streamlit AppTest 检查首页、缺失自定义凭证、任意网关阻断和预生成模式保留统计。最终联合测试 46/46 通过，包含原有深水模块 15 项、新增引擎边界 28 项、AppTest 3 项；离线总自检与三个 CLI 的 `--help` 均退出 0。AppTest 输出现有 Streamlit 上下文/弃用提示，无 UI exception。独立只读补丁复核的 4 条遗漏已复现并补齐。

`--skip-e2e` 明确跳过真实凭证读取与网络验收；完整真实 CLI/模型流程未运行。自检脚本原被 `_*.py` 忽略，本轮添加精确例外使其修正可被版本控制。更详细的本轮结果写在本地工作记录 `.trellis/tasks/engine-boundary-fixes/README.md`。
