# HANDOFF — `herdr-bridge` (`hb`)

## 基本信息

- **项目名称**：`herdr-bridge`（CLI 命令：`hb` / `herdr-bridge`）
- **仓库地址**：`https://github.com/awu5425/herdr-bridge`（主分支 `main`）
- **当前版本**：`0.1.0`（见 `pyproject.toml`）
- **当前状态 / 更新时间**：`v0.1.0` 核心实现、实机联调修复与开源脱敏准备已完成 · `2026-11-15`

---

## 已对齐的核心需求与架构决策

1. **核心目标**：保留本地老版 Herdr TUI 的工作区/分屏/快捷键体验，同时将新建窗格的交互 PTY 透明托管给新版 `hc`（`herdr-core`），实现桌面 Herdr 与手机端 HC 内容一致、状态同步、双向可回应。
2. **PTY 托管路线**：采用“全自动透明内胆托管”（guarded shell hook 触发 `hb pane-entry` → `hc run --bg --keep` + `hc attach`），而非抓屏/模拟输入（`hc` 无外部 PTY 接管 RPC，抓屏无法保真 TUI 与 OSC 7501）。
3. **窗格关闭策略**：`on_pane_close = "keep"`（默认以 Herdr 工作台为主，关闭窗格仅断开连接并置 `tui.attached=0`，保留后台 `hc` 终端）。
4. **`ctrl+\` 脱离行为**：按 `ctrl+\` 从 `hc attach` 脱离后退回外层本地 shell（返回码 `10`），提示 `hb attach <addr>` 可随时重连；在内层正常 `exit` 时同步关闭 Herdr 窗格（返回码 `0`）。
5. **运行时选型**：Python 3.12 + 纯标准库 `asyncio`（零第三方运行时依赖，纯事件驱动无轮询）。

---

## 安装、测试与验证入口

### 测试与静态检查入口

```bash
uv run --with pytest --with pytest-asyncio --python .venv/bin/python pytest -v
ruff check src tests
```

### 最近验证结果

- **静态检查**：`ruff check src tests` → **All checks passed! (exit 0)**
- **离线单测（Mock Herdr Socket & Mock `hc` 事件流）**：`pytest -v` → **11 passed in 0.08s (exit 0)**
  - `tests/test_cli.py`（`hc ls --json` 字符串 `status` 与 `hc get` 字典 `status` 兼容）
  - `tests/test_mapper.py`（名称清洗、CJK/长度截断、`tui.*` 标签、OSC 7501 状态映射、`antigravity`/`agy` 回退 `hc` 生命周期上报）
  - `tests/test_state.py`（原子写入、`flock`、`seq` 递增、`pane_moved` 迁移）
  - `tests/test_pane_entry.py`（递归防护、`ctrl+\` 脱离 vs `exit` 关闭行为）
  - `tests/test_daemon.py`（双向同步、`pane_moved`、`blocked` 通知、`keep`/`kill` 策略、两段式 `pane.report_agent` 认领与 `resume_not_accepted` 恢复）
- **实时协议与本地健康探针**：
  - `hc connect` 实时 `events.subscribe` 实测确认了 `{"terminal": ..., "type": "created|title|labels|foreground|finished", "data": ...}` 线格式。
  - `hb doctor` 实测确认：`herdr.sock` (`v0.9.5`)、`hc server`、`daemon` 全部连通。
  - `hb ls` 与 `herdr pane get` / `herdr agent list` 实测确认：活跃绑定与 `agent="hc"`、`agent_status="working"`、`display_agent` 同步正常。
  - `HERDR_ENV=0 bash -ic 'which hb && echo bashrc_ok'` 验证 `.bashrc` hook 加载无语法错误。
- **未在自动覆盖中的范围（需实机人工体感确认）**：
  - `blocked` 审批弹窗在 Herdr 桌面通知 + 手机端双向确认的 UI 视觉体验，以及 `ctrl+\` 脱离后 `hb attach` 重连手感。

---

## 文件清单与结构

- `pyproject.toml`：包配置、`hb` / `herdr-bridge` CLI 入口及 `pytest` / `ruff` 配置
- `LICENSE`：MIT 开源协议
- `README.md`：项目简介、安装与使用说明
- `AGENTS.md`：本项目协作与测试约定
- `HANDOFF.md`：本交接文档
- `docs/PROGRESS.md`：按时间倒序记录会话进展与决策
- `src/herdr_bridge/`
  - `config.py`：加载 `~/.config/herdr-bridge/config.toml` 与默认配置
  - `state.py`：`bindings.json` 进程安全持久化（`fcntl.flock` + 原子替换）
  - `mapper.py`：Herdr 拓扑与 `hc` 名称/标签/OSC 7501 状态映射
  - `herdr_client.py`：`herdr.sock` 同步/异步 Unix Socket 客户端
  - `hc_client.py`：`hc` CLI 封装与 `hc connect` JSON-RPC 2.0 异步客户端/事件流
  - `daemon.py`：双事件流（Herdr + `hc`）后台同步守护进程
  - `pane_entry.py`：窗格自动托管入口与防递归/脱离处理
  - `cli.py`：`hb` 命令行（`install-hook`、`doctor`、`ls`、`attach`、`run`、`daemon` 等）
- `tests/`：`test_cli.py`、`test_mapper.py`、`test_state.py`、`test_pane_entry.py`、`test_daemon.py`

---

## 待办与下一步

1. **端到端实机验收**：在 Herdr 中新建测试工作区/分屏，进一步验证 `blocked` 通知联动与 `ctrl+\` 脱离重连体验。
2. **可选优化（按实测反馈决定）**：
   - 如需在 `hc` 侧区分多机器前缀或自定义命名模板，可调整 `~/.config/herdr-bridge/config.toml`。
   - 如需开机自启 daemon，可补充 `systemd --user` service 文件（当前由 `hb pane-entry` / `hb daemon start` 按需拉起）。
