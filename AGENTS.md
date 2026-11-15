# 项目协作约定 (`herdr-bridge`)

## 协作与继承规则

- 本文件记录 `herdr-bridge` 项目的开发、测试、交接与工作区洁净约定；所有技术结论必须以真实 CLI/Schema 输出、代码与实测命令为准。

---

## 最小正确改动原则

- 优先保持纯 Python 3.12 标准库实现（零第三方运行时依赖），不引入不必要的外部包、抽象或猜测性配置。
- `herdr`（TUI 0.9.5）与 `hc`（herdr-core 0.1.17）的协议字段必须对照 `herdr api schema --json` 与 `hc schema` 核实，禁止凭记忆臆造参数名或事件格式。
- `hc` 终端标签统一使用 `tui.*` 命名空间（`tui.ws` / `tui.ws_name` / `tui.tab` / `tui.tab_name` / `tui.pane` / `tui.pane_title` / `tui.attached`），避免与 `herdr-app` 内部 `herdr.space` / `herdr.tab` / `herdr.seen` 冲突。
- Shell hook 与 `pane_entry.py` 必须保留递归防死循环保护（`HERDR_ENV=1`、`HERDR_PANE_ID` 非空、`HC_TERMINAL_ID` 为空、`HERDR_BRIDGE_DISABLE != 1`）及异常回退本地 shell 兜底，任何失败不得锁死用户终端。

---

## 测试与静态检查

在项目根目录下运行：

```bash
uv run --with pytest --with pytest-asyncio --python .venv/bin/python pytest -v
ruff check src tests
```

- 汇报验证结果时须严格区分：**离线单测 / Mock 联调**、**静态检查**、**本机实测（`hb doctor` / `hc connect` / 真实 Herdr 分屏与手机端交互）**；未实测项必须如实标明。

---

## 部署与环境边界

- 在隔离 Agent Profile（修改了 `HOME`）中操作用户全局配置或 daemon 时，须确认目标用户真实 `HOME`。
- 禁止把本机路径、主机名、敏感凭据或个人隐私写入仓库；禁止改动用户已运行的旧 Herdr 窗格进程。
- 构建缓存与本地运行态（`.venv/`、`*.egg-info/`、`.pytest_cache/`、`.ruff_cache/`、`graphify-out/`、`__pycache__/`）已列入 `.gitignore`，禁止提交入库。

---

## 交接与进度沉淀

- 每次完成阶段性开发、修复或部署后，同步更新：
  - `HANDOFF.md`（当前状态、已验证与未验证边界、文件结构、下一步）
  - `docs/PROGRESS.md`（按时间倒序追加会话记录与关键决策）
