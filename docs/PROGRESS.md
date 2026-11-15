# 项目进展日志 (`herdr-bridge`)

> 按时间倒序记录每次会话的工作内容、关键决策、验证状态与遗留事项。

## 2026-11-15（接管、实机联调修复与开源准备）：修复 `hb ls` 崩溃、Herdr 状态同步与脱敏发布

### 问题排查（基于真实 `herdr` + `hc` 联调）
在真实多端运行环境联调时，发现 3 个在纯 Mock 测未暴露的协议差异：
1. **`hb ls` 遇到活跃 Agent 时崩溃**：`hc ls --json` 返回的 `TerminalSummary.status` 为字符串（如 `"working"`），而 `hc get --json` / `hc connect` 返回字典；`cli.py` 直接调用 `status.get("state")` 导致 `AttributeError`。
2. **`pane.report_agent` 首次上报携带 `resume_argv` 被拒**：`herdr 0.9.5` 要求汇报方先不带 `resume_argv` 调用 `pane.report_agent` 持有窗格，否则返回 `resume_not_accepted: resume_argv requires the reporter to hold the pane; report its state with pane.report_agent first`，导致后续 `report_metadata` 与 `digest` 更新全部被跳过。
3. **`herdr 0.9.5` 静默忽略 `agy`/`antigravity`/`antigravity-cli` 的 `pane.report_agent`**：实测 `herdr` 对这 3 个别名返回 `{"type":"ok"}` 但保持 `agent_status="unknown"`（因其内置期望直接对本地终端做屏幕识别，而 `hc attach` 隔离了进程树与识别）。实测传入 `agent="hc"` 则可正常持有窗格并同步 `working`/`blocked`/`idle` 及 `resume_argv`。

### 完成事项
- **`src/herdr_bridge/cli.py`**：新增 `_extract_hc_state()` 兼容 `str` 与 `dict` 两种 `status` 形态，修复 `hb ls`。
- **`src/herdr_bridge/daemon.py`**：新增 `_push_agent_report()`，首次认领或切换 agent 时先不带 `resume_argv` 认领窗格，再附加 `resume_argv`，并在遇到 `resume_not_accepted` 时自动降级重认领；同时在 `clear_state_labels=True` 时避免再传空 `state_labels={}`。
- **`src/herdr_bridge/mapper.py`**：新增 `_resolve_report_agent()`，将 `agy`/`antigravity`/`antigravity-cli` 及非法 agent 名在 `pane.report_agent` 中映射为 `"hc"`，同时在 `metadata_report.display_agent` 中保留原始名称（如 `"antigravity"`）。
- **回归测试**：新增 `tests/test_cli.py`，并在 `tests/test_daemon.py` 与 `tests/test_mapper.py` 补充对应测试用例。
- **开源整理**：新增 `LICENSE`（MIT）、完善 `README.md`、将 `graphify-out/` 加入 `.gitignore`、清理文档中本机路径/主机名/进程号与邮箱痕迹。

### 验证状态
- `ruff check src tests`：通过（0 errors）。
- `pytest -v`：11/11 通过。
- 本机实测：`hb doctor`、`hb ls`、`herdr pane get` 与 `herdr agent list` 确认同步正常且服务端无报错。

---

## 2026-11-15（首次会话）：需求对齐、架构实现与测试验证

### 背景与目标
将老版 `herdr`（`0.9.5`）的本地 TUI 工作区/Tab/分屏体验，与新版 `hc` / `herdr-core`（`0.1.17`）的后台持久化 PTY、OSC 7501 状态感知及手机端远程流打通：本地 Herdr 界面保持原样操作，每个工作区窗格的内容与状态同步到 `hc`，支持本地与手机端双向查看与回应。

### 关键架构决策
1. **透明内胆托管（`hc` inside Herdr pane）替代抓屏桥接**：核查 `hc schema` 确认 `hc` 无“接管外部已有 PTY”的 RPC；通过 shell hook 在 Herdr 窗格内自动执行 `hc run --bg --keep` + `hc attach`，使本地与手机端共享同一 PTY 字节流。
2. **独立标签命名空间 `tui.*`**：实测发现 `hc` 中已存在 `herdr.space` / `herdr.tab` / `herdr.seen` 等 `herdr-app` 内部标签，为避免冲突，桥接同步标签采用 `tui.ws` / `tui.ws_name` / `tui.tab` / `tui.tab_name` / `tui.pane` / `tui.pane_title` / `tui.attached`。
3. **协议实测对齐**：
   - `herdr.sock` 对普通 RPC 为“单连接单请求”模式，仅 `events.subscribe` 保持长连接推流；`herdr_client.py` 据此实现短连接 RPC + 长连接事件订阅。
   - 实测 `hc connect` 的 `events.subscribe` 实时事件包为 `{"terminal": "<addr>", "type": "created|title|labels|foreground|finished|...", "data": {...}}`，已同步修正 `daemon.py` 事件合并逻辑。
4. **生命周期与逃生舱策略**：
   - `on_pane_close = "keep"`：在 Herdr 中直接关闭窗格时，默认保留后台 `hc` 终端仅置 `tui.attached=0`。
   - `ctrl+\` Detach 返回退出码 `10`，退回外层本地 shell 并提示重连命令；内层 shell 正常 `exit` 返回 `0`，外层同步退出以关闭窗格。
   - **纯标准库 Python 3.12 (`asyncio`)**：零第三方运行时依赖的事件驱动架构。

### 完成事项
- 完成 `herdr-bridge`（`hb`）全套源码（`config.py`、`state.py`、`mapper.py`、`herdr_client.py`、`hc_client.py`、`daemon.py`、`pane_entry.py`、`cli.py`）与 `README.md`。
- 完成 4 个测试模块共 8 项单测与异步集成测试（mock Herdr socket 与 `hc` 事件流）。
- 补齐项目协作与交接文档（`AGENTS.md`、`HANDOFF.md`、`docs/PROGRESS.md`、`.gitignore`）并初始化本地 Git 仓库。

### 验证状态
- `ruff check src tests`：通过（0 errors）。
- `pytest -v`：8/8 通过。
- `hb doctor`：`herdr.sock`、`hc server`、daemon 双连接均正常；`bash -ic` 验证 `.bashrc` 加载正常。

### 遗留 / 待验收
- [ ] 在真实多端场景中持续验证 `blocked` 通知与 `ctrl+\` 脱离重连手感。
- [ ] 按需配置 `~/.config/herdr-bridge/config.toml`。
