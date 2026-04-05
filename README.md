# astrbot_plugin_browser_tool

AstrBot 插件：为 LLM 提供真实浏览器操作能力。底层使用 Playwright，同时支持**本地启动浏览器**和**连接远程浏览器（WebSocket / CDP）**。

## 功能

注册一个 LLM tool `browse_webpage`，支持以下动作：

| 动作 | 说明 | 必填参数 |
|---|---|---|
| `goto` | 导航到 URL，返回标题、可读文本摘要、链接、表单元素 | `url` |
| `get_content` | 获取当前页面内容（`text` 或 `html`） | — |
| `screenshot` | 截取 JPEG 截图并以 base64 返回 | — |
| `click` | 点击选择器匹配的元素 | `selector` |
| `fill` | 向输入框填写内容 | `selector`, `value` |
| `select` | 在下拉列表中选择选项 | `selector`, `value` |
| `evaluate` | 在页面中执行 JavaScript | `script` |
| `wait` | 等待选择器对应元素出现 | `selector` |
| `close_session` | 关闭并释放当前会话的浏览器 | — |

浏览器会话按会话来源（`unified_msg_origin`）隔离，跨多次调用保留状态（先 `goto` 再 `click`/`fill` 等）。

## 安装

1. 在 AstrBot 插件市场搜索并安装，或将此目录放入 `data/plugins/`。
2. 安装 Python 依赖：`pip install playwright`
3. 安装浏览器可执行文件（本地模式必须）：
   ```bash
   playwright install chromium
   # 或 playwright install --with-deps chromium （含系统依赖）
   ```
4. 在 AstrBot 控制台进入插件配置，根据使用场景完成设置（见下文）。

## 配置说明

### 场景一：本地启动浏览器

```
connection_mode = local
browser_type    = chromium          # chromium / firefox / webkit
headless        = true              # false 则显示浏览器窗口（需桌面环境）
launch_args     = --no-sandbox      # Docker 内必须加此参数
```

若要指定自己安装的 Chrome：

```
browser_executable_path = /usr/bin/google-chrome
```

### 场景二：连接远程浏览器（WS）

适用于 [browserless](https://www.browserless.io/)、Playwright Server 等。

```
connection_mode    = remote
remote_ws_endpoint = ws://127.0.0.1:3000/
```

### 场景三：连接远程 Chrome（CDP）

适用于以 `--remote-debugging-port` 启动的 Chrome/Chromium。

```
connection_mode = remote
remote_cdp_url  = http://127.0.0.1:9222
```

### 代理

```
proxy_server   = http://127.0.0.1:7890
proxy_username =               # 有认证时填写
proxy_password =
```

### 自定义 UA 和 Cookie

- `user_agent`：填写自定义 User-Agent 字符串。
- `storage_state_path`：填写 Playwright [Storage State](https://playwright.dev/python/docs/auth) JSON 文件路径，可加载已有 Cookie/localStorage，实现免登录。

### 权限

`only_admin = true`（默认）时只有 AstrBot 管理员对话可触发此工具。如需对所有用户开放，设为 `false`。

## 管理命令

| 命令 | 说明 |
|---|---|
| `/browser_close` | 关闭当前会话的浏览器（释放资源） |
| `/browser_status` | 查看所有活跃浏览器会话（管理员） |

## 典型调用流程（LLM 视角）

```
1. browse_webpage(action="goto", url="https://example.com")
   → 返回页面标题、文本摘要、链接列表、表单元素

2. browse_webpage(action="click", selector="text=Read more")
   → 点击链接，返回新页面信息

3. browse_webpage(action="fill", selector="#search", value="Playwright")
   → 填写搜索框

4. browse_webpage(action="screenshot")
   → 返回当前页面截图（base64 JPEG）

5. browse_webpage(action="close_session")
   → 关闭浏览器，释放资源
```

## 注意事项

- **远程模式**不需要在 AstrBot 机器上安装浏览器，适合资源受限的部署环境。
- 本地模式在 **Docker** 中运行时，`launch_args` 必须包含 `--no-sandbox`。
- `session_idle_ttl`（默认 600 秒）控制空闲浏览器会话的自动回收，设为 0 则不自动回收。
- 首版不包含 Cloudflare/FlareSolverr 绕过；如有需要可结合 `evaluate` 动作做自定义处理。
