# WebQA 分层状态图工作流

## 独立启动

双击 `启动WebQA.cmd`，依次输入完整网址和允许进入的子页面层数即可。启动窗口会持续显示轮次、当前层、页面、状态节点、控件队列、链接数量和最新动作。若输入层数 `0`，只测试输入的网址本身，不进入子页面。

运行期间也可以双击 `查看WebQA状态.cmd`，在另一个窗口查看最近实例或指定运行 ID。每次运行目录中的 `live-status.json` 和 `live-status.md` 会持续更新。

按 Ctrl+C 会把运行标为 `PAUSED` 并保留页面、控件和链接断点；下次启动时选择 `R` 即可续跑相同网址。已完成实例也可以用相同网址、更大的层数和 `R` 继续扩展。

## 核心规则

1. 主页是第 0 层；`max_page_depth` 是当前实例已解锁的最大测试深度，可以在后续轮次递增。
2. 当前页面发现的同域子页面链接先按规范化 URL 去重并写入链接总账。目标深度未解锁时不创建页面任务。
3. 当前页面会完整探索可见控件及同 URL 状态。菜单、弹窗、Tab、筛选等动作出现新状态后，新控件立即插入当前页面队列前面。
4. 当前页面发现的静态同域子页面只记录链接、来源控件和完整点击历程；当前控件步骤不点击进入。达到解锁深度后，它才会在独立页面任务中作为标准页面完整测试。
5. 同一运行内，已经记录过的规范化目标 URL 只记录重复边，不再次建档；新开运行会重新建立本次链接总账。
6. 从深度 0 提高到 1 并续跑时，系统复用原运行 ID 和目录，只把历史链接提升为第 1 层 `PENDING` 页面；第 0 层 `DONE` 页面不会重跑。
7. 外域链接和尚未解锁的下一层链接只记录路径，不点击。
8. 每个页面、状态、控件、动作、链接、运行轮次和证据均写入 SQLite。`PENDING/RUNNING/RETRY` 可在进程中断后恢复。
9. 自动化超时、定位失败或无可观察变化只能判为“未确认”；不能直接写成产品缺陷。
10. 动作按 `READ_ONLY/UNKNOWN/STATEFUL/DESTRUCTIVE` 分类；持久化和高影响动作默认不执行。
11. 只有可以证明安全重试的只读动作才会自动重试；动作已触发但结果不确定时标记为 `UNKNOWN_COMMIT`。
12. 截图默认遮盖表单字段；URL、日志、报告和 PagePatrol 文本结果默认脱敏；Playwright trace 默认关闭。

## 安全配置

`webqa.yaml` 的 `safety` 段控制执行和证据边界：

- `allow_stateful_actions: false`：默认禁止保存、提交、发送、登录等动作。
- `allow_destructive_actions: false`：默认禁止删除、支付、发布等高影响动作；`prohibited` 仍具有更高优先级。
- `allow_unknown_actions: true`：允许探索未知按钮，但不能证明安全时不会自动重试。
- `mask_form_fields_in_screenshots: true`：截图遮盖输入框、文本域和可编辑区域。
- `capture_traces: false`：trace 可能包含完整 DOM，默认不生成。
- `allow_sensitive_pagepatrol: false`：带敏感参数的 URL 默认不交给外部视觉审计工具。
- `allow_sensitive_target_storage: false`：目标 URL 含 token、session 等参数时默认拒绝运行，优先使用 `storage_state`。

默认子页面策略保持 `record_only`；如需在发现链接时只验证加载，可显式改成 `load_only`。

## 深度递增续跑

第一次以深度 `0` 新建实例：

```powershell
python .\webqa_cli.py run --target https://staging.evomap.work/zh --max-depth 0 --new-run
```

主页完成后，报告列出所有第 1 层候选 URL。下一次以同一网址提高到深度 `1`，且不使用 `--new-run`：

```powershell
python .\webqa_cli.py run --target https://staging.evomap.work/zh --max-depth 1
```

系统选择该网址最近实例，创建 `EXPAND` 轮次，将历史候选链接按最浅来源路径去重后提升为标准页面。同一实例不能降低深度；需要重新测试已完成页面时使用 `--new-run`。

## 路径语义

- `arrival_path_json`：从主页到当前页面的完整点击历程。
- `state_nodes.action_path_json`：当前页面内从初始状态到菜单、弹窗或 Tab 状态的动作组合。
- `actions.full_path_json`：到达页面、进入状态并点击当前控件的完整历程。
- 报告中的“业务流程线索”来自这些路径，只作为后续语义 Planner 的输入，不作为业务真值。

## 常用命令

```powershell
# 只执行当前层；便于逐轮审核
python .\webqa_cli.py run --one-layer

# 从 SQLite 断点继续，或提高深度后接续历史链接
python .\webqa_cli.py run

# 临时改为两层、同层四路并行
python .\webqa_cli.py run --max-depth 2 --parallel-pages 4

# 开启一次独立回归测试；不复用上一次运行的链接点击历史
python .\webqa_cli.py run --new-run

# 查看进度或重新生成报告
python .\webqa_cli.py status
python .\webqa_cli.py report
```

入口优先使用项目内 `.venv`，其次使用 PagePatrol 虚拟环境，最后使用系统 Python。数据库和所有证据只写入当前项目的 `webqa-output` 目录；旧的 `测试进度.json` 不会被覆盖或自动导入。

## 固定目录与时间归档

每个独立实例使用精确到秒的本地开始时间建目录：

```text
webqa-output/
├── webqa.sqlite3
├── 运行归档索引.md
├── 运行归档索引.html
└── runs/
    └── 20261105-173012-a1b2c3/
        ├── run-manifest.json
        ├── live-status.json
        ├── live-status.md
        ├── WebQA测试报告.md
        ├── WebQA测试报告.html
        ├── screenshots/
        ├── traces/
        ├── reproductions/
        └── pagepatrol/
```

普通 `run`、中断恢复或提高深度续跑都会沿用原实例目录；`run --new-run` 才创建新的时间归档。报告和 manifest 中的 `run_rounds` 会记录每次新建、恢复、深度扩展及提升页面数。

## 覆盖审计

最终报告会分别对账：

- 页面节点和页面状态；
- 可见控件出现次数和去重后的控件队列；
- 首次记录链接、重复链接、外域链接和深度边界；
- 技术通过、风险跳过、表单规划、业务规则未确认、自动化未确认和阻塞项；
- PagePatrol 报告、截图以及完整点击历程。
