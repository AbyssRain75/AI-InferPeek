# AI InferPeek

面向 Windows 的轻量级本地 AI 运行状态聚合看板。实时监控硬件负载、模型服务与 AI 编程 Agent 的会话进度，助你随时掌握显存余量、任务工作状态与 Token 消耗。

提供**置顶桌面悬浮窗**、**系统托盘常驻**与**完整交互看板**（支持应用窗口或浏览器访问）。

[![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-blue)](https://github.com/AbyssRain75/AI-InferPeek)
[![Python](https://img.shields.io/badge/Python-3.10%2B-brightgreen)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## 核心特性

- 📊 **系统与多卡监控**：CPU、物理内存、多 GPU 核心利用率与显存（含共享显存）；整机 CPU 封装功耗（RAPL）及 GPU 功耗；CPU/GPU 实时温度与风扇转速；支持多显卡切换与按进程查看硬件占用。
- ⚡ **本地模型服务感知**：
  - **Ollama**：实时查看已加载模型、显存/内存分配、生成速率（Tokens/s）及累计请求统计。
  - **ComfyUI**：实时获取排队与正在执行的任务、主模型与 LoRA 配置、单任务耗时统计。
- 🤖 **AI 编程与 Agent 会话跟踪**：
  - 原生聚合 **Claude Code**、**Codex**（含 ChatGPT 本机执行任务）、**DeepSeek Harness (`dsh`)**、**Grok Build**、**OpenCode**、**OpenClaw**、**Antigravity**。
  - 自动识别会话状态（工作中 / 等待确认 / 已完成 / 疑似中断），实时统计上下文大小、Token 消耗明细与耗时。
  - 额外支持套餐额度与余额跟踪：Claude Code 周期配额（Beta）、DeepSeek Harness 余额预警与今日花费、Antigravity 模型额度（Beta）。
- 🪟 **轻量多形态交互**：
  - **桌面悬浮窗**：支持精简小窗（核心数据直显）、透明度调节、鼠标穿透、屏幕吸附、主题切换及开机自启。
  - **系统托盘**：常驻系统托盘，低资源静默运行，支持快速控制悬浮窗与面板。
  - **完整看板**：多维度动态曲线图、详细会话列表、服务检测说明与可视化设置。
- 🔒 **本地优先与隐私保护**：纯本地只读分析，**无需配置任何模型 API Key**，不修改、不上传用户的代码与对话原文。

---

## 快速开始

### 桌面版（推荐）

1. 前往 [Releases](https://github.com/AbyssRain75/AI-InferPeek/releases) 页面，下载最新版本的 `AIInferPeek-<版本>.zip`。
2. 解压整个压缩包，进入 `AIInferPeek` 文件夹，双击运行 `AIInferPeek.exe`（exe 须与同目录下的 `_internal` 文件夹放在一起）。
3. 启动后桌面将显示置顶悬浮窗，并在任务栏托盘区常驻图标。程序单实例运行。

> **环境说明**：
> - 适用于 64 位 Windows 10 / 11，已内置独立运行环境，无需预先安装 Python。
> - 桌面窗口依赖系统内置的 Microsoft Edge WebView2 运行时（Windows 11 已自带；部分 Windows 10 若缺失会弹出安装指引，安装前亦可先通过托盘使用浏览器看板）。
> - **快捷操作**：悬浮窗右上角可一键切换小窗精简模式、唤出完整看板或隐藏；托盘图标左键切换悬浮窗显示/隐藏，右键可进行设置、调整透明度、吸附边缘或完全退出。

---

### 源码运行（网页版 / 桌面版）

需要 Windows 系统及 Python 3.10 或更高版本环境（安装时请勾选 `Add python.exe to PATH`）。

#### 方式一：双击启动脚本（网页版）
克隆或下载解压源码后，双击根目录下的 `启动.bat`：
- 首次运行将自动创建 `.venv` 虚拟环境并安装所需依赖。
- 准备就绪后自动启动服务并使用默认浏览器打开 [本地看板](http://127.0.0.1:8765)。在命令行窗口中按 `Ctrl+C` 即可停止。

#### 方式二：命令行手动运行

在源码根目录下打开 PowerShell 执行：

```powershell
# 1. 创建并激活虚拟环境
python -m venv .venv

# 2. 安装基础运行依赖
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. 启动 Web 网页版看板
.\.venv\Scripts\python.exe -m aiinferpeek
```

若需从源码启动带悬浮窗和托盘的**桌面版**，安装桌面依赖后启动即可：

```powershell
# 安装桌面端界面依赖
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt

# 启动桌面端入口
.\.venv\Scripts\python.exe -m aiinferpeek.desktop
```

---

## 支持的服务与工具

启动对应服务或工具后，看板会自动感知本机进程与常用端口，通常在数秒内建立连接并显示：

| 服务或工具 | 监控指标与内容 | 说明 |
|---|---|---|
| **Ollama** | 已加载模型、占用大小、GPU/CPU 分配、生成速率（Tokens/s）、请求统计 | 生成速率与请求统计由本机 Ollama 进程与 `server.log` 解析；局域网远程服务仅显示已加载模型 |
| **ComfyUI** | 执行中与排队队列、单任务耗时、主模型及 LoRA 节点信息 | 不显示节点逐步采样进度；空闲时展示上一任务所用模型 |
| **Claude Code** | 运行中会话、执行状态、工作目录、模型、上下文与 Token 用量；5 小时/每周额度（Beta） | 需支持在 `.claude/sessions/` 记录会话的版本；套餐配额读取自 Claude 桌面端缓存数据（可能存在数小时延迟；未安装桌面端时不显示） |
| **Codex** | 最近活跃会话、模型、上下文、Token 用量与最近请求额度 | 额度信息来源于最近一次 API 请求返回的数据 |
| **ChatGPT 本机任务** | 任务执行状态（工作中 / 空闲）、回合运行耗时 | 仅监控由本机 Codex `exec-server` 执行的任务；无法读取云端模型、对话原文及用量，亦不判定中断原因 |
| **DeepSeek Harness (`dsh`)** | 最近活跃会话、模型、上下文、Token 消耗及待确认状态；账户余额与今日花费，支持余额预警 | 底层数据未区分异常退出状态，中断回合可能显示为“已完成”；余额数据依赖“余额小鲸鱼”插件（`dsh-whale-widget`）本地账本 |
| **Grok Build** | 最近活跃会话、模型、上下文、Token 用量及操作确认状态 | Token 用量在回合（Turn）执行完成时同步刷新 |
| **OpenCode** | 最近活跃会话、模型、上下文与 Token 用量（含推理 Token） | 需 2.0 及以上版本；等待用户确认或提问时显示为“工作中”；子会话不单独列出 |
| **OpenClaw** | 各 Agent 活跃会话、聊天渠道、模型、上下文与 Token 用量 | 等待确认时显示为“工作中”；网关运行于 WSL 内部时无法跨系统读取；源码版需 Python 3.14+ 以解析压缩存储的历史回复用量 |
| **Antigravity** | 最近活跃对话、模型、上下文、Token 用量与待确认状态；分模型额度（Beta） | 需 2.x 版本；模型每次回复完成后刷新用量；额度读取自后台服务本地接口（若版本升级导致接口变更可能受限） |

> **提示**：Codex、DeepSeek Harness、Grok Build、OpenCode、OpenClaw 和 Antigravity 默认展示**最近 3 小时**内有活动的会话；单次回合发起后若超过 30 分钟无新记录，将提示“可能已中断”。

---

### 硬件与显卡支持说明

- **NVIDIA 显卡**：基于官方 NVML 接口深度采集，支持核心利用率、显存分配、真实温度、运行功耗、核心频率与风扇转速。
- **AMD / Intel 显卡**：通过 Windows 系统底层图形接口（D3DKMT / DXGI）采集核心利用率与显存；温度、频率取决于驱动上报能力，未提供时显示为“—”；功耗项暂不提供瓦数。
- **核心显卡与多显卡**：核显展示借用系统物理内存的“共享显存”；完整看板列出本机全部 GPU，悬浮窗优先展示独立显卡（多独显分别展示）。

### CPU 温度与功耗配置

- **CPU 封装功耗**：基于 Windows 原生 RAPL（运行平均功率限制）能量计采集，无需管理员权限（Intel 处理器广泛支持）；若环境不支持则自动回退至 LibreHardwareMonitor 读数。
- **CPU 温度获取**：因 Windows 系统未向普通应用程序开放 CPU 温度接口（任务管理器亦无此指标），需配合运行开源工具 [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)：
  1. 以管理员身份运行 LibreHardwareMonitor。
  2. 打开菜单 **Options → Remote Web Server**，勾选 **Run**（端口保持默认 `8085`，无需启用 Authentication 认证）。
  3. 建议在 **Options → Remote Web Server → Interface / Port** 中将监听接口设为 `127.0.0.1`（仅限本机访问，保障网络安全）。
  > 开启后数秒内看板与悬浮窗即可感知并显示 CPU 温度；设置页面也提供了实时检测状态与官方下载入口。

---

## 设置与个性化配置

在完整看板右上角或托盘菜单中打开“设置”，可以：
- 自定义开启或关闭特定工具的监控。
- 修改指定工具的会话存储目录及 Ollama 日志路径。
- 手动添加远程服务地址（如局域网内其他主机的 Ollama、ComfyUI 服务）。
- 设置当前设备名称，便于在多设备环境中区分看板。
- 设置 DeepSeek Harness 余额预警阈值。

保存后即时生效。

### 配置文件模式

除界面设置外，也可通过编辑配置文件管理。将 [aiinferpeek.example.json](aiinferpeek.example.json) 复制为 `aiinferpeek.json`（源码版置于根目录，exe 版置于 exe 同级目录）：

```json
{
  "device_name": "工作站主机",
  "endpoints": [
    { "kind": "ollama", "url": "http://192.168.1.20:11434" }
  ],
  "disabled_tools": ["grok-build"],
  "balance_warning": 10.0,
  "ollama_log": null,
  "claude_dir": null,
  "codex_dir": null,
  "dsh_dir": null,
  "grok_dir": null,
  "opencode_dir": null,
  "openclaw_dir": null,
  "antigravity_dir": null
}
```

- `balance_warning`：余额预警阈值（数值型，低于该金额时悬浮窗将突出提醒）。
- `endpoints`：用于添加无法通过本机自动发现的服务，`kind` 可为 `ollama` 或 `comfyui`。
- `disabled_tools`：禁用不需要监控的工具名称列表（可选值：`ollama`、`comfyui`、`claude-code`、`codex`、`dsh`、`grok-build`、`opencode`、`openclaw`、`antigravity`）。
- 路径项填 `null` 表示采用系统默认或对应环境变量指定的路径。

### 网页版命令行参数

```powershell
.\.venv\Scripts\python.exe -m aiinferpeek --port 8765 --interval 1 --history 600
```

| 参数 | 默认值 | 作用说明 |
|---|---|---|
| `--port` | `8765` | 看板监听端口 |
| `--interval` | `1` | 数据采样间隔（秒） |
| `--history` | `600` | 历史走势数据保留时长（秒） |
| `--no-browser` | 否 | 启动后不自动唤起默认浏览器 |
| `--config` | 根目录 `aiinferpeek.json` | 指定外部配置文件路径 |
| `--host` | `127.0.0.1` | 监听地址（默认仅允许本机访问） |
| `--version` | — | 显示版本号并退出 |

---

## 数据与隐私说明

- **纯只读采集**：硬件数据采集自系统标准 API，服务与会话状态采集自本机公开端口、进程信息或本地日志/会话文件。程序绝不修改各工具的原始数据。
- **过滤与脱敏**：本地会话日志中可能含有对话上下文或提示词，AI InferPeek 仅提取展示监控所需的摘要信息（标题、工作路径、模型名、状态与用量），其余内容即刻丢弃，不留存、不分析。*在公开发布截图或汇报问题前，请留意会话标题或项目路径是否包含敏感隐私信息。*
- **网络隔离保障**：服务默认仅监听 `127.0.0.1`（本机回环）。若通过 `--host 0.0.0.0` 开放至局域网，局域网内其他用户将可直接查阅监控数据；为防范未授权修改，在此模式下 Web 端的配置修改功能将自动受到限制。

---

## 常见问题 (FAQ)

<details>
<summary><b>看板中没有检测到某个工具？</b></summary>

在完整看板中点击展开底部的“没看到某个工具？”，可以查看各个工具的进程、端口与默认数据路径的实时自检结果。若曾手动更改过工具的配置目录，请在设置中指定实际路径。此外，工具尚未启动、服务未开始监听端口或数据版本不兼容均可能导致无法识别。
</details>

<details>
<summary><b>部分指标显示为“—”？</b></summary>

显示“—”表示系统底层接口、硬件驱动或上游工具未提供对应数值，并不代表数值为零。这常见于非 NVIDIA 显卡的温度与频率、未运行 LibreHardwareMonitor 时的 CPU 温度，以及 ChatGPT 本机任务的云端模型与用量。
</details>

<details>
<summary><b>画面数据似乎停止更新？</b></summary>

看板会自动高亮标出连接断开、数据停滞或单项服务过期的状态。为降低无谓资源占用，当浏览器标签页处于后台、窗口最小化或悬浮窗隐藏时，前端渲染引擎将自动进入节能挂起状态，重新切回前台后将瞬间同步至最新数据。
</details>

<details>
<summary><b>默认端口 8765 被占用如何处理？</b></summary>

桌面版启动时若检测到 8765 端口已被占用，会自动递增并绑定至临近的空闲端口；网页版可通过启动参数 `--port 8766` 自行指定可用端口。
</details>

<details>
<summary><b>如何升级或彻底卸载？</b></summary>

- **版本升级**：完全退出旧版本程序后，使用新版本解压出来的 `AIInferPeek` 文件夹整体替换即可。若在 exe 同级目录下自定义了 `aiinferpeek.json`，请记得复制到新目录；悬浮窗个性化设置存放于系统用户配置中，不受文件夹更换影响。
- **开机自启迁移**：开机自启记录的是当时 exe 的具体文件路径。若移动了文件夹位置，托盘菜单中的“开机自动启动”勾选状态将重置，重新勾选一次即可刷新路径登记。
- **彻底卸载**：在托盘菜单中取消“开机自动启动”，选择“退出”完全关闭，随后删除软件文件夹。若需清除本地配置与缓存，可删除 `%APPDATA%\AIInferPeek` 及 `%LOCALAPPDATA%\AIInferPeek` 目录。
</details>

---

## 配置文件与日志位置

| 存储项 | 物理存储路径 |
|---|---|
| **工具监控与端口配置** | 源码根目录或 exe 同级目录下的 `aiinferpeek.json` |
| **悬浮窗、托盘与偏好设置** | `%APPDATA%\AIInferPeek\settings.json` |
| **桌面端本地缓存数据** | `%APPDATA%\AIInferPeek\webview\` |
| **桌面端运行日志（exe 版）** | `%LOCALAPPDATA%\AIInferPeek\aiinferpeek.log` |

---

## 问题反馈

欢迎通过 [GitHub Issues](https://github.com/AbyssRain75/AI-InferPeek/issues) 提交缺陷报告或改进建议。反馈时建议附带：程序版本、Windows 系统版本、显卡型号与驱动版本、相关工具版本以及简要复现步骤。上传截图或日志前请注意隐藏个人敏感路径与会话信息。

---

## 开源许可证

本项目源码采用 [MIT License](LICENSE) 授权。桌面版 exe 分发包内集成的第三方组件均遵循其原软件开源许可证，完整版权声明与许可正文参见包内的 `THIRD-PARTY-NOTICES.txt`。
