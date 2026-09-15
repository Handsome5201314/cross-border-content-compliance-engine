# GitCode 推送步骤（明早照做，约 5 分钟）

> 本地仓库**已就绪**，只剩 push 和授权两步必须你手动。

## 当前状态
- commit：`8ffddee`
- 入库文件：204 个，`.git` 约 23 MB
- 凭证检查：**已通过**。`.gitignore` 已拦住 `.env` / `*.key` / `*secret*` / `*token*`，提交前扫描确认无凭证入暂存区。
- 已排除：视频（35.9MB）、`.trellis`、`__pycache__`、各类日志

---

## 1. 在 GitCode 建空仓库
- 打开 https://gitcode.com → 新建仓库
- 名称建议：`cross-border-content-compliance-engine`
- ⚠️ **不要**勾选「初始化 README / LICENSE」，否则会和本地历史冲突
- 建好后复制地址，形如：
  `https://gitcode.com/<你的用户名>/cross-border-content-compliance-engine.git`

## 2. 推送（本机执行）
```bat
cd E:\AI新青年\output\hackathon_round2
git remote add origin https://gitcode.com/<你的用户名>/cross-border-content-compliance-engine.git
git branch -M master
git push -u origin master
```
23MB，通常 1–3 分钟。提示登录就用 GitCode 账号 + 令牌。

推送前想再保险一次，跑这个（输出应为空）：
```bat
git ls-files | findstr /I ".env secret .key token"
```

## 3. 授权评审账号 `air__Heaven`
仓库 → **设置 → 成员管理 → 添加成员** → 输入 `air__Heaven` → 给**只读**权限即可。
> 即便仓库设为公开，也建议显式加一次，确保评审账号一定能访问。

## 4. 回填链接到提交材料
把仓库地址填进 `SUBMISSION_TEMPLATE_CONTENT.md`：
- 第七节「GitCode 代码仓库地址」
- 第七节「测试账号或体验地址」
- 「在线链接填写表」——**按模板要求设成超链接**：`[跨境内容合规引擎 · GitCode](https://gitcode.com/...)`

## 5. 重新导出 PDF
Word 打开 `破界AI_跨境内容合规引擎_复赛作品.docx` → 改链接 → **另存为 PDF**。
（已有的 `破界AI_跨境内容合规引擎_复赛作品.pdf` 是 499,447 字节 / 约 12 页，可直接覆盖）

## 6. 提交到 CSDN
https://marketing.csdn.net/questions/Q2608200953083853278

---

## 顺带待办（同一批做完）
- [ ] 演示视频上传拿公开链接 —— 成片在 `final\破界AI_跨境内容合规引擎_复赛演示.mp4`（150 秒 / 35.9MB）。B站登录态已失效需重登；不想折腾就直接传百度网盘拿分享链接，同样能填进「在线链接填写表」。
- [ ] 模板明确允许「无法线上则打包 zip」→ 兜底包已备好：`破界AI_跨境内容合规引擎_提交包.zip`（59 文件 / 2.58MB / 无凭证）
