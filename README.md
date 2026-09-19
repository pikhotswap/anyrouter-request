# 中转站模型探针 — 使用说明

一个本地网页工具，用于探测任意 OpenAI 协议兼容中转站上的模型是否真实可用。

## 启动

- macOS：双击「双击启动监测面板.command」（首次如被拦截，右键 → 打开一次），会自动启动服务并打开浏览器。
- Windows：双击「双击启动监测面板.bat」。需先安装 Python 3（python.org 或 Microsoft Store），脚本会自动检测。
- 其它系统 / 手动启动：`python3 relay_monitor.py`（Windows 为 `python relay_monitor.py`），然后浏览器访问 http://localhost:8765 （换端口：`PORT=9000 python3 relay_monitor.py`）。

仅需系统自带 Python 3（无需安装任何第三方库）。

## 使用流程

1. 点「＋ 添加中转站」，填写 API 地址（填到 `/v1`，只填根域名会自动补 `/v1`）、API Key。
2. 点「📋 拉取模型列表」，在下拉框选择要探测的模型（列表里没有的可用「✏️ 手动输入…」）。
3. 点「▶ 探测一次」验证，或点「⏱ 持续监测」定时轮询。
4. 模型可用时会弹窗 + 系统通知 + 响铃提醒。

## 主要选项

- **上游格式**：Chat Completions / Responses（原生）/ Anthropic Messages，按中转站支持的协议选择。
- **完全模拟 Codex 终端请求**：强制 Responses + 流式，携带 codex CLI 的完整请求指纹（结构化 input、instructions、reasoning、originator、session-id 等请求头）。UA 下拉留空时自动使用 `codex_cli_rs/0.42.0`。
- **User-Agent**：内置 claude-cli / claude-code / Kilo-Code / codex 等预设，支持自定义；选 codex 系 UA 时自动附带 `originator: codex_cli_rs` 头。
- **双间隔**：探活间隔（失败时用，默认 120 秒）+ 保活间隔（成功后自动切换，默认 120 秒），不通时勤问、通了省着问。
- **代理**：可填本地代理端口（如 Clash 的 `http://127.0.0.1:7890`），拉列表和探测都走代理；留空直连。
- **会话 ID**：每个中转站自动生成并复用，收到 400 自动更换，避免被上游按坏会话持续拒绝。

## 行为说明

- 每次探测是真实请求：`max_tokens: 50`、提示词 "hi"，消耗很小；单次超时 60 秒。
- 401/403 判定为 Key 无效并停止该卡片的持续监测；429 视为服务在线（限流）。
- 所有配置保存在脚本旁的 `relay_monitor_config.json`（首次保存时自动生成），换浏览器、重开页面都不丢。
- 面板里的 Key 以明文存在本机配置文件中，请勿把该文件分享给他人。

## 文件清单

- `relay_monitor.py` — 主程序（服务端 + 内嵌网页，单文件，无第三方依赖）
- `双击启动监测面板.command` — macOS 双击启动器
- `双击启动监测面板.bat` — Windows 双击启动器
