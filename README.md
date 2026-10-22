# doubao_eye 👁️

**给没有多模态能力的大模型装上一双眼睛。**

doubao_eye 是一个 MCP (Model Context Protocol) 服务：把豆包网页版的免登录多模态能力
（识图、读文档）封装成 `look` / `read` 等工具，让任何纯文本模型（DeepSeek、Qwen-turbo、GLM 文本版等）的 agent 也能"看"图片、读文件、理解屏幕截图。

```
你的 agent（无视觉模型） ──MCP──▶ doubao_eye ──▶ 豆包网页版（免登录）
      "这张图里有什么？"                    图片/文件 + 问题 → 文字回答
```

## 特性

- **零登录**：使用豆包网页版的匿名（免登录）通道，无需扫码、无需账号、无 Cookie 维护
- **零守护进程**：stdio 传输，MCP 客户端调用时才启动；浏览器也是第一次调用工具时才拉起，
  空闲 10 分钟自动关闭，平时内存占用为 0
- **轻量**：不依赖 Playwright/Chromium 下载 —— 裸 CDP 直驱系统自带的 Edge/Chrome
  headless 实例，Python 侧仅 `mcp` + `httpx` + `websockets` 三个依赖
- **反反代**：所有请求经页面内 `window.fetch` 发出，由豆包自己的前端 hook 自动注入
  `a_bogus` 签名与 msToken —— 无需自行破解签名算法（2026 年新版防护下 httpx 直连
  / X-Bogus 旧方案已全部失效）
- **风控自愈**：瞬时限流自动退避重试；设备指纹被拉黑时自动重置 profile 换新身份重试

## 工具

| 工具 | 用途 | 输入 |
|---|---|---|
| `look` | 看图并回答 | 单图 `image` 或多图 `images` 列表（一次最多 9 张）+ 问题 |
| `read` | 读文件并回答（PDF/Word/Excel/PPT/代码等 60+ 格式） | 单文件 `file` 或多文件 `files` 列表（最多 9 个）+ 问题 |
| `watch` | 看视频（实验性，见下） | 同上 |
| `status` | 查看服务状态（不启动浏览器） | — |

**批量是推荐用法**：`look(images=[...])` / `read(files=[...])` 把多个附件合并进
**一次**豆包对话 —— 匿名通道对连续对话有间隔限制（上传不受限），批量 N 个附件
只需等一次间隔，逐个调用则要等 N 次。

**回答质量**：服务会自动给问题加"盲人视角 + 精简"前缀（实测：OCR 完整度不变，
但砍掉豆包的解释/总结/反问废话，输出 token 减少约 2/3，并自动附带空间位置
描述）。专家模式需登录，免登录只能用快速模式，此为快速模式下的最优解。

> `watch` 说明：豆包网页版聊天的上传接口目前仅接受文档与图片格式，不支持视频附件，
> 因此 `watch` 对视频文件会返回明确的替代建议（抽帧后用 `look` 的 `images` 批量看）。

## 安装

```bash
pip install -e .          # 或 uv pip install -e .
```

要求：Python 3.10+，系统装有 Microsoft Edge 或 Chrome（Win11/macOS/Linux 常见路径
自动探测；也可用环境变量 `DOUBAO_EYE_BROWSER` 指定路径）。

mcp SDK **1.x 与 2.x 均支持**：2.x 用 `MCPServer`，1.x 自动回退到等价的
`FastMCP`（现有全局装 mcp 1.x 的环境无需升级，不影响其他依赖 1.x 的工具链）。
兼容性测试：`python tests/test_mcp_compat.py`。

### Linux 服务器 / Docker

Linux 下支持三类环境，`--no-sandbox` 按需自动添加（桌面环境不加，避免
不必要的 sandbox 降级）：

| 环境 | 判定 | 处理 |
|---|---|---|
| 桌面 Linux（有 `DISPLAY`/`WAYLAND_DISPLAY`） | 有显示服务 | 正常启动，无需配置 |
| 无头服务器（无显示服务） | 环境变量缺失 | 自动加 `--no-sandbox` |
| root 用户 / Docker 容器 | `geteuid()==0` 或 `/.dockerenv` 存在 | 自动加 `--no-sandbox` |

```bash
# Ubuntu/Debian 安装浏览器（任选其一）
sudo apt install chromium-browser        # 或 google-chrome-stable / microsoft-edge-stable
pip install -e .
python -m doubao_eye                      # stdio 模式由 MCP 客户端拉起，无需手动运行
```

特殊场景强制覆盖：`DOUBAO_EYE_NO_SANDBOX=true`（强制加）/ `=false`（强制不加）。

> 注意：Linux 上 WebGL 会报告 `Mesa/llvmpipe` 或具体 GPU 字符串——这在 Linux
> 上是真实用户常态，不是指纹泄漏；但免登录通道在 Linux 指纹下的通过率建议
> 部署后自行跑一轮 `look` 验证。

## 接入 MCP 客户端

### Claude Code

```bash
claude mcp add doubao-eye -- python -m doubao_eye
```

或项目 `.mcp.json`：

```json
{
  "mcpServers": {
    "doubao-eye": {
      "command": "python",
      "args": ["-m", "doubao_eye"]
    }
  }
}
```

### Claude Desktop / 其他 MCP 客户端

```json
{
  "mcpServers": {
    "doubao-eye": {
      "command": "doubao-eye"
    }
  }
}
```

> Windows 下若用 `uvx`/`npx` 包装启动，命令需要 `cmd /c` 前缀（MCP 客户端常见问题，与本服务无关）。

## 使用示例

对 agent 说：

> 用 look 看一下 D:\screenshots\error.png，告诉我报错信息是什么

> 用 read 读一下 ./report.pdf，总结三个要点

agent 会自动调用对应工具并把豆包的回答整合进上下文。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DOUBAO_EYE_BROWSER` | 自动探测 | Edge/Chrome 可执行文件路径 |
| `DOUBAO_EYE_HEADLESS` | `true` | 浏览器是否无头运行 |
| `DOUBAO_EYE_IDLE_TIMEOUT` | `600` | 浏览器空闲自动关闭秒数（0=不关闭） |
| `DOUBAO_EYE_TIMEOUT` | `150` | 等待豆包回答的最长秒数 |
| `DOUBAO_EYE_MIN_INTERVAL` | `15` | 两次对话的最小间隔秒数（主动限速） |
| `DOUBAO_EYE_BACKOFF` | `45` | 撞上限流后的退避重试秒数 |
| `DOUBAO_EYE_LOG` | `INFO` | 日志级别（日志走 stderr，不干扰 MCP stdio 协议） |

## 工作原理

```
MCP 客户端 ──stdio──▶ doubao_eye (Python)
                        │ 懒启动、空闲关闭
                        ▼
                 Edge headless（系统浏览器，独立 profile）
                        │ 页面内 window.fetch
                        │  （豆包前端 hook 自动注入 a_bogus / msToken）
                        ▼
                 doubao.com/chat/ 匿名会话
```

1. **浏览器层**（`browser.py`）：以 `--remote-debugging-port` 启动系统 Edge 的独立
   headless 实例，通过 WebSocket 说 CDP 协议（Page/Runtime 域），不依赖任何浏览器
   自动化框架。与真实浏览器逐项对照消除 headless 指纹差异（曾因此被 710022004
   持续拉黑）：UA 覆盖同时带 client hints 元数据（否则 Sec-CH-UA 头整组丢失）、
   不禁用 GPU（软件渲染的 WebGL 报告 "Microsoft Basic Render Driver" 是自动化
   强信号）、保留原生 plugins/Notification、AudioContext 挂起对齐真实手势策略、
   platformVersion 按真实系统 build 计算；stealth 脚本只做最小必要修补
2. **协议层**（`doubao.py`）：在页面上下文执行 JS ——
   上传四步（`prepare_upload` 拿匿名 STS → 浏览器内 `crypto.subtle` 做 AWS V4 签名 →
   TOS 直传 → `CommitImageUpload`），文件走 `prepare_upload` 返回的文件服务、图片走
   UI 硬编码的图片服务；文件发送前先 `pre_handle_v2` 触发服务端解析；对话走
   `/chat/completion` SSE 流（新事件格式 `STREAM_MSG_NOTIFY` 累积文本），附件与文本
   按 UI 真实结构分两条 message
3. **服务层**（`server.py`）：MCP 工具注册、本地/URL/data-URI/tos-URI 输入解析、
   多附件批量合并、图片尺寸探测、空闲监控、主动限速与风控自愈

## 已知限制

- 免登录通道由豆包风控管控：实测指纹干净时 **12 秒间隔连发 5 次全部成功**（10 秒
  以上即正常人类节奏）；服务默认主动限速 15 秒，漏网撞上 710022002 时自动退避
  重试，设备指纹被拉黑（710022004）时自动重置 profile。**把多个附件合并进一次
  调用**仍是最有效的省间隔手段
- 视频不支持附件输入（豆包网页版限制，非本服务问题）
- 单文件 ≤ 50MB、图片 ≤ 20MB、单次调用最多 9 个附件
- 速率与能力随豆包策略变化，无 SLA —— 适合个人开发者的轻量场景，不建议生产关键路径

## 常见问题

**Q: 会在后台常驻吗？**
不会。MCP 客户端（如 Claude Code）需要时才 spawn 进程，会话结束即退出；浏览器实例
更是在第一次实际工具调用时才启动，空闲 10 分钟自动关闭。

**Q: 它会动我用着的浏览器吗？**
不会。它启动的是一个独立的 headless Edge 实例（独立 profile 目录），与你日常使用的
浏览器窗口、账号、Cookie 完全隔离。

**Q: 和登录版有什么差别？**
免登录通道无需任何账号配置（开箱即用），但豆包可能对匿名用户施加更严格的频率限制；
如需更高配额可自行研究登录态接入（本版本聚焦免登录零配置路线）。

## License

Apache-2.0
