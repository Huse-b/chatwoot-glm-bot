# Chatwoot GLM 客服机器人

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-64%20passed-brightgreen)](#8-测试)

**English**: A zero-dependency Chatwoot chatbot that polls conversations via the Chatwoot Application API and replies with multi-turn LLM responses (Zhipu GLM, OpenAI-compatible endpoint). Falls back to fixed greeting replies when no API key is configured.

官网访客在 Chatwoot 浮窗发的消息，机器人主动轮询读取后，调用智谱 GLM 生成**多轮智能回复**回发访客；未配置大模型密钥时自动降级为固定文案问候回复。

**纯 Python 标准库实现，零第三方依赖**，单文件部署，支持 Windows / macOS / Linux。

## ✨ 特性

- **两种回复模式**：配置 GLM Key 后对所有访客消息生成多轮智能回复（携带最近 12 条上下文）；不配置则仅对「您好/你好」回固定文案，互不影响
- **无需公网回调**：轮询模式主动拉取消息，机器人可部署在任何能访问 Chatwoot 的内网机器上
- **真人接管自动让位**：客服接管会话后机器人立即停止回复，绝不和真人抢话
- **可靠送达**：SQLite 去重占位防重复回复；429 限流自动退避；401/403 立即停机提示，不静默漏回复
- **凭据安全**：Windows DPAPI 加密存储 / 环境变量注入，密钥从不进日志、不进代码
- **断线恢复**：游标与进度持久化，断网恢复后自动续读未处理消息
- **常驻部署**：macOS launchd（开机自启+崩溃拉起）与 Windows 计划任务脚本齐全
- **云端就绪**：Chatwoot 迁移到 https 域名只需一个参数，代码零改动

## 🏗 系统架构

```text
网站页面（Chatwoot 原生浮窗，官方嵌入代码）
        │ 访客消息
        ▼
Chatwoot 服务器（任意可部署位置：内网服务器 / 云主机 / Docker）
        │ Application API（REST）
        ▼
chatwoot_test_bot.py（轮询模式常驻进程，本仓库）
        │ 组装上下文（系统提示词 + 最近 12 条会话消息）
        ▼
智谱 GLM 开放平台（open.bigmodel.cn，OpenAI 兼容接口，默认 glm-4-flash）
        │ 生成回复
        ▼
机器人回发 Chatwoot → 访客在浮窗看到回复
```

| 组件 | 说明 |
|---|---|
| Chatwoot 服务器 | 消息收发中枢（[chatwoot.com](https://www.chatwoot.com/) 开源客服系统，已在 4.x 版本验证） |
| 轮询机器人 | 本仓库核心，读消息用**用户令牌**、发回复用**机器人令牌** |
| GLM 大模型 | [智谱开放平台](https://open.bigmodel.cn/)，默认免费模型 `glm-4-flash`，可换 `glm-4-air` / `glm-4-plus` |

> 为什么用轮询而不是 Webhook？轮询模式下机器人是**主动出站连接**，不需要 Chatwoot 能反向访问机器人所在机器——内网机器、NAT 后面都能跑，无需公网 IP、端口映射或内网穿透。

## 🚀 快速开始

### 0. 准备

- Python **3.9+**
- 一个运行中的 Chatwoot 实例（[官方部署文档](https://www.chatwoot.com/docs/self-hosted)），并已创建网站浮窗收件箱
- （可选，启用智能回复）[智谱开放平台](https://open.bigmodel.cn/) API Key

### 1. 获取三个凭据

| 凭据 | 获取方式 | 用途 |
|---|---|---|
| 用户 Access Token | Chatwoot 后台 → 个人设置 → Access Token | 读取会话与消息 |
| 机器人 Access Token | Chatwoot 后台 → 应用 → Agent Bots 创建，或管理员 API 生成 | 以机器人身份发送回复 |
| GLM API Key | [智谱开放平台](https://open.bigmodel.cn/) 控制台（形如 `id.secret`） | 生成智能回复 |

### 2. 录入凭据并启动

```bash
# 录入 Chatwoot 两个令牌（Windows 用 DPAPI 加密存储；macOS/Linux 建议用环境变量，见下文）
python chatwoot_test_bot.py --save-credentials

# 录入 GLM Key（不录入则运行在固定文案模式）
python chatwoot_test_bot.py --save-llm-key

# 启动轮询（--inbox 换成你的网站浮窗收件箱 ID，后台「设置→收件箱」可查）
python chatwoot_test_bot.py --polling --inbox 7
```

若 Chatwoot 不在默认地址，加参数指定：

```bash
python chatwoot_test_bot.py --polling --inbox 7 --chatwoot-host chat.example.com
python chatwoot_test_bot.py --polling --inbox 7 --chatwoot-host 192.168.1.100:3000   # 内网 host:port
```

### 3. 验证

启动日志出现「GLM 已启用（模型 glm-4-flash）」即智能回复模式就绪。用网站浮窗发一条消息，应收到 GLM 生成回复；再追问一句，验证多轮上下文连贯。

> **Windows 粘贴注意**：隐藏输入不支持 Ctrl+V（控制字符会被程序拒绝），请用**鼠标右键**或 **Shift+Insert** 粘贴令牌。

### macOS / Linux：用环境变量代替加密文件

```bash
export CHATWOOT_HOST=chat.example.com          # 可选，默认见代码常量
export CHATWOOT_USER_TOKEN=你的用户令牌
export CHATWOOT_BOT_TOKEN=你的机器人令牌
export GLM_API_KEY=你的智谱Key
export LLM_SYSTEM_PROMPT=你的业务客服提示词      # 可选，自定义回复人设
python3 chatwoot_test_bot.py --polling --inbox 7
```

## 🎯 回复逻辑

| 条件 | 行为 |
|---|---|
| 已配置 GLM Key | 回复所有**公开的入站访客消息**；每次携带系统提示词 + 最近 12 条多轮上下文 |
| 未配置 GLM Key | 仅对「您好 / 你好」回复固定欢迎文案（保护未配密钥的部署） |
| GLM 调用失败 | 自动降级发送兜底文案并记日志，访客不会收到空白 |
| 会话被真人接管 / 已解决 / 静音 | 机器人自动让位，不再回复 |

### 自定义客服人设（系统提示词）

默认提示词是通用的「网站在线客服助手」。注入你自己的业务介绍，三种方式任选：

```bash
# 方式一：环境变量（推荐，优先级最高）
export LLM_SYSTEM_PROMPT="你是 XX 公司官网客服。核心产品有……不确定的报价不要编造，引导留联系方式。"

# 方式二：直接修改代码顶部 LLM_SYSTEM_PROMPT 常量

# 方式三：写成 launchd/计划任务的环境变量，随服务自动生效
```

提示词建议包含：业务介绍、回复语言要求（跟随访客）、不确定信息不编造、引导人工跟进的边界。

## ⚙️ 全部配置

### 命令行参数

| 参数 | 说明 |
|---|---|
| `--polling --inbox <ID>` | 轮询模式（生产推荐） |
| `--chatwoot-host <host[:port] / https://域名>` | Chatwoot 地址；`https://` 前缀自动启用 TLS |
| `--llm-model <模型名>` | GLM 模型，默认 `glm-4-flash` |
| `--save-credentials` / `--save-llm-key` | 交互录入并加密保存凭据 |
| （无参数） | 旧回调模式：监听 18787 端口接收 Webhook 推送，仅适合本机联调 |

### 环境变量

| 变量 | 说明 | 优先级 |
|---|---|---|
| `CHATWOOT_USER_TOKEN` | 用户令牌（读） | 高于加密文件 |
| `CHATWOOT_BOT_TOKEN` | 机器人令牌（写） | 高于加密文件 |
| `GLM_API_KEY` | 智谱 GLM Key | 高于加密文件 |
| `LLM_SYSTEM_PROMPT` | 自定义系统提示词 | 高于代码默认值 |
| `CHATWOOT_SCHEME` / `CHATWOOT_HOST` / `CHATWOOT_PORT` | Chatwoot 地址 | 高于代码常量，低于命令行 |

## 🔐 凭据与安全

- **Windows**：令牌经 **DPAPI**（用户级密钥）加密存入 `chatwoot_test_bot.credentials`，仅当前 Windows 账户可解密，拷贝到其他机器无法还原
- **macOS / Linux**：用环境变量注入；launchd 部署时 plist 权限 600，仅本用户可读
- 录入时做**形状校验**（长度、空白、控制字符、非 ASCII），拦截粘贴不完整事故
- 密钥**从不打印、从不写日志、从不进数据库**
- 所有网络请求经令牌认证；错误日志脱敏，只输出接口名与截断的错误字段
- 凭据泄露处置：到 Chatwoot / 智谱控制台吊销重发，重新录入即可

## 📦 生产部署

### 方式一：macOS launchd（推荐常驻）

机器人所在 macOS 机器（与 Chatwoot 可不同机器）：

```bash
# 1. 同步仓库到 ~/chatwoot-odoo-bot/
# 2. 从 Windows 迁移已录好的凭据（解密只在内存，经 SSH 加密通道，不落明文）
python migrate_creds_mac.py        # Windows 上执行；目标地址用 CHATWOOT_MIGRATE_REMOTE 环境变量指定
# 3. 完成：LaunchAgent com.chatwoot.odoo.poller 已注册（开机自启 + 崩溃自动拉起）
```

运维命令：

```bash
launchctl list | grep chatwoot                        # 状态
tail -f ~/chatwoot-odoo-bot/chatwoot_test_bot.log     # 日志（每 300 秒心跳）
launchctl unload ~/Library/LaunchAgents/com.chatwoot.odoo.poller.plist   # 停止
```

### 方式二：Windows 计划任务

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_service.ps1 -Start   # 安装并启动（需先录入凭据）
powershell -NoProfile -ExecutionPolicy Bypass -File .\check_status.ps1             # 状态 / 最近日志 / 累计回复数
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall_service.ps1        # 卸载
```

> 注意：设备休眠期间不轮询，长时间无人值守请调整电源计划。

## ☁️ Chatwoot 迁移到云端

机器人代码零改动，只换目标地址：

```bash
python3 chatwoot_test_bot.py --polling --inbox 7 --chatwoot-host https://chat.example.com
```

检查清单：① 云端域名 HTTPS 就绪、机器人可出公网访问；② 改地址重启进程（启动日志打印实际目标）；③ 同实例凭据不用重录，换实例则重新生成录入；④ 官网浮窗嵌入代码里的 Chatwoot 地址同步更新。

## 🧪 测试

全部离线（不打真实 Chatwoot、不打真实 GLM、不需要网络）：

```bash
python -m unittest discover -p "test_*.py"
```

当前 **64 项全绿**（macOS 上 DPAPI 用例自动跳过），覆盖：会话分页、增量读取、去重占位、真人接管、限流退避、鉴权停机、DPAPI 往返、隐藏输入防护、GLM 请求构造与失败降级、提示词环境变量、地址解析与 TLS 切换、非法配置启动期拒绝。

## 📄 文件说明

```text
chatwoot_test_bot.py     # 机器人本体（全部逻辑，单文件零依赖）
deploy_creds.py          # macOS 端 launchd 部署（配合凭据迁移脚本使用）
migrate_creds_mac.py     # Windows → macOS 凭据迁移（SSH 加密通道）
check_attempts.py        # 运维：查询回复处理记录
install_service.ps1      # Windows 计划任务安装
uninstall_service.ps1    # Windows 计划任务卸载
check_status.ps1         # Windows 任务状态查看
test_chatwoot_*.py       # 测试套件（6 个文件）
```

运行产物（不入库）：`*.credentials`（加密凭据）、`*.sqlite3`（游标与去重）、`*.log*`（滚动日志）。

## ❓ 常见问题

<details>
<summary><b>机器人在线但不回复？</b></summary>

依次检查：会话是否已被真人接管（接管后机器人让位）；消息是否为 private；进程是否存活（看日志心跳）；收件箱 ID 是否传对。
</details>

<details>
<summary><b>日志出现「GLM 返回 HTTP 401」？</b></summary>

Key 无效。确认是**智谱**的 Key（形如 `id.secret`，不是其他平台的 `sk-` 开头格式）；重跑 `--save-llm-key`，Windows 下用鼠标右键粘贴。
</details>

<details>
<summary><b>录入密钥提示形状异常 / 含控制字符？</b></summary>

粘贴不完整或 Ctrl+V 进了控制字符。重新运行录入命令，Windows 用鼠标右键 / Shift+Insert，macOS 用 Cmd+V。
</details>

<details>
<summary><b>同一消息收到两条回复？</b></summary>

存在两个轮询进程（如手动前台进程与常驻服务并存）。停掉多余进程——SQLite 占位能防同进程重复，防不了两个进程。
</details>

<details>
<summary><b>英文提问收到中文回复？</b></summary>

系统提示词已要求跟随访客语言；若仍出现，确认运行的是本仓库最新代码并重启进程。
</details>

<details>
<summary><b>能接别的大模型吗？</b></summary>

当前走智谱 GLM 的 OpenAI 兼容接口（`/api/paas/v4/chat/completions`）。理论上任何 OpenAI 兼容端点改 `LLM_HOST` / `LLM_PATH` 常量即可接入，鉴权格式为标准 Bearer Token。
</details>

<details>
<summary><b>消息会重复回复吗？断网/崩溃会漏回复吗？</b></summary>

SQLite 原子占位防同进程重放；断网恢复自动续读。极端故障（发送结果未知、进程崩溃在占位后发送前）优先避免重复回复，可能漏发需人工核对——设计取舍详见代码注释，不宣称 exactly-once。
</details>

## 🗺 Roadmap

- [x] 轮询模式自动回复
- [x] GLM 多轮智能回复
- [x] 常驻部署（launchd / Windows 计划任务）与心跳日志
- [x] 云端 https 迁移接口
- [ ] 意图识别分流（问候固定文案 / 知识类检索 / 商机引导 / 转人工）
- [ ] 知识库 RAG（检索增强回复）
- [ ] 多收件箱并行支持

欢迎 Issue 与 PR。

## 🤝 贡献

1. Fork → 新建分支 → 提交 PR
2. 提交前运行 `python -m unittest discover -p "test_*.py"` 确保全绿
3. 新增功能请附带离线测试用例（不打真实服务）
4. **严禁**在 Issue / PR / 代码中出现真实令牌、密钥或生产环境地址

## 📜 许可证

[MIT](LICENSE)
