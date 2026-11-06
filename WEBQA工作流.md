# WebQA 分层状态图工作流

## 独立启动

双击 `启动WebQA.cmd`，依次输入完整网址、允许进入的子页面层数、冒烟/全量模式和新建/续跑方式。启动窗口会持续显示轮次、当前层、页面、状态节点、控件队列、链接数量和最新动作。若输入层数 `0`，只测试输入的网址本身，不进入子页面。

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
13. 起始页、子页面、动态跳转页及动作后的页面状态都必须经过统一语义观察；HTTP 200 不等于内容正确。
14. 高可信内容异常在两个全新 BrowserContext 中复现后才确认为缺陷；低可信文案问题只进入人工确认。
15. PagePatrol 始终保留独立原报告；默认只有 `severity=error` 的明显缺陷进入 WebQA 总报告，正常、info 和 warning 不重复汇报。

## 测试模式与自动测试策略

- `smoke`：快速验证关键页面。默认最多 40 个页面、每页 8 个状态、每状态 120 个控件、每页 80 个动作；表单只执行合法值。
- `full`：全量执行当前深度内的安全探索，并运行合法、空值、边界和无效输入用例。
- 两种模式都会运行已配置的 API/UI 契约、只读业务流程和安全不变量检查。
- 同一运行 ID 的模式不可切换。模式会影响已经完成页面的覆盖；从 smoke 升级到 full 必须使用 `--new-run` 新建回归实例。

命令行也可直接选择：

```powershell
python .\webqa_cli.py run --profile smoke --new-run
python .\webqa_cli.py run --profile full --new-run
```

### API 契约

系统采集同页 XHR/fetch 响应，在统一脱敏后检查状态码、Content-Type、JSON Schema、必填字段和枚举值。JSON 正文默认最多采集 256 KiB。

```yaml
contracts:
  api:
    enabled: true
    automatic_http_errors: true
    capture_json_bytes: 262144
    rules:
      - key: leaderboard-api
        page_url_regex: /leaderboard
        url_regex: /api/leaderboard
        methods: [GET]
        statuses: [200]
        content_types: [application/json]
        required_fields: [data, data.*.id, data.*.status]
        enums:
          data.*.status: [active, inactive]
        json_schema:
          type: object
          required: [data]
          properties:
            data:
              type: array
        required: true
        severity: major
```

`page_url_regex` 用来限定“哪些页面必须调用此接口”，避免把某个业务页的必需接口错误套用到全站。`required: true` 默认只在匹配页面的初始观察阶段要求接口必须出现；可用 `phases` 显式限制到 `INITIAL_PAGE`、`STATE_AFTER_ACTION` 等阶段。没有业务契约时，`automatic_http_errors` 仍会记录 XHR/fetch 的 4xx/5xx。

### UI 契约

通用检查会发现空标题和无可访问名称的可见交互控件。显式规则支持标题、selector、ARIA role、文案和业务区域：

```yaml
contracts:
  ui:
    enabled: true
    automatic_title: true
    automatic_accessible_names: true
    rules:
      - key: leaderboard-ui
        path_regex: /leaderboard
        title_regex: 排行榜|Leaderboard
        required_roles:
          - {role: heading, name: 排行榜}
        required_selectors: [main]
        required_regions: ['main table, main [role="grid"]']
        forbidden_text: [Internal Server Error]
        severity: major
```

### Given/When/Then 业务流程

流程在隔离 BrowserContext 中运行，支持 `open/assert/wait_ms/click/fill/select/check/clear`。默认禁止 POST/PUT/PATCH/DELETE 副作用：

```yaml
business_flows:
  - key: guest-search
    title: 游客搜索
    profiles: [smoke, full]
    role: guest
    given:
      - open: /zh/search
      - assert: {title_regex: 搜索}
    when:
      - fill: {selector: 'input[type="search"]', value: webqa}
      - click: {role: button, name: 搜索}
    then:
      - assert: {selector: 'main [role="list"]'}
      - assert: {not_text: Internal Server Error}
    forbidden_side_effects: [POST, PUT, PATCH, DELETE]
```

非 guest 角色从 `accounts.roles.<role>.storage_state` 载入已录制会话。保存、提交、发送等持久化流程必须同时设置流程 `authorized_stateful: true` 和全局 `safety.allow_stateful_actions: true`；删除、支付和发布仍受破坏性策略与 `prohibited` 限制。

### 表单、幂等、可逆与蜕变关系

普通输入框会实际执行 `fill/select/check/clear` 级别的非提交用例，覆盖合法值、空值、min/max、minlength/maxlength、格式和 pattern；每个用例结束后恢复原值，不触发表单提交。

系统会保守识别或按规则声明以下关系：

- `idempotent`：同一安全动作执行两次，第二次状态与第一次一致；
- `reversible`：主题、菜单等动作执行两次恢复原状态；
- `non_decreasing_count`：加载更多后集合数量不减少；
- `non_increasing_count`：筛选后集合数量不增加；
- `same_collection_members`：排序前后成员集合不变，只允许顺序变化；
- `stable_structure`：语言切换可改变文案，但关键页面结构计数保持稳定。

```yaml
testing:
  invariants:
    enabled: true
    auto_detect: true
    rules:
      - label_regex: ^按时间排序$
        type: same_collection_members
        source: requirement:list-sort
        confidence: high
```

自动推断只用于明显的 ARIA 状态和标签；不确定的业务关系应使用显式规则或 Given/When/Then 断言，避免把猜测当成缺陷。

### 大列表与分页采样

当 `table/tbody`、list、ARIA row/listitem、article 或 card 容器出现至少 6 个重复项目时，系统把它识别为列表，而不是把所有行内控件都加入执行队列：

- 冒烟模式：默认真实点击并验证 1 个代表项目；
- 全量模式：有分页时取首页首项和末页末项，共 2 个；无分页时取当前可见列表的首尾；
- 每个被选项目只执行一个主入口，优先选择详情链接，避免同一行内多个重复按钮全部执行；
- 分页页属于同一个业务层级，不占用子页面深度；项目详情仍属于下一层；
- 分页链接会真实点击并进行加载、网络、语义和截图检查，默认选择可识别的首页、中间代表页和末页；
- 如果分页只显示 `1、2、…、10`，系统仅在已验证 URL 明确使用 `page=数字` 或 `/page/数字` 时推导并直接检查第 5 页；不会用不可靠的字符串替换猜 URL；
- 其余重复控件仍写入控件总账，但标记为采样跳过，不进入执行队列；最终报告显示可见项目数、执行样本和跳过数量。

```yaml
testing:
  profiles:
    smoke:
      list_item_samples: 1
      pagination_samples: 3
    full:
      list_item_samples: 2
      pagination_samples: 3
  list_sampling:
    enabled: true
    min_repeated_items: 6
    item_detail_samples: 2
    pagination_samples: 3
    open_sampled_items: true
```

若站点只提供“下一页”，没有总页数、末页入口或可推导页码，系统不会猜测不存在的 URL；此时只验证页面明确暴露的安全分页入口，并在报告中保留实际覆盖范围。

### 无人值守边界

遍历、契约检查、非提交表单用例、只读业务流程、不变量验证、PagePatrol 缺陷导入和总报告生成可全程无人值守。业务 Oracle、角色会话和持久化动作授权仍需预先配置；系统不会自动索取密码，也不会自动放开保存、删除、支付或发布操作。语句通顺度等主观语义需要配置本地语义模型适配器；未配置时会明确标记为确定性规则覆盖，而不会伪装成模型已经判断。

## 页面理解与语义 Oracle

`semantic.required: true` 使语义判断成为每次页面观察后的必经阶段。系统先根据路由、HTML/ARIA 和页面结构推断列表、搜索、仪表盘、详情、表单、登录、错误页或空状态，再执行确定性 Oracle：

- `CONTENT_UNAVAILABLE`：页面明确表示内容、接口或服务不可用；
- `DATA_MISSING`：数据型页面的数据项为 0，并有失败证据；
- `STUCK_LOADING`：等待窗口结束后仍存在可见 loading/skeleton；
- `COPY_QUALITY`：乱码、异常裁切，或语义模型报告的不通顺文案；
- `BUSINESS_MISMATCH`：违反显式结构化业务预期；
- `ORACLE_UNCERTAIN`：存在异常迹象，但当前预期不足，不能直接报 Bug；
- `SEMANTIC_CHECK_UNAVAILABLE`：已请求的模型不可用，不能静默算作语义通过。

`expected_behavior` 支持来源、引用、条件、预期结果、禁止副作用和可信度。例如：

```yaml
expected_behavior:
  - source: requirement:catalog-1
    reference: 商品目录需求
    conditions:
      path_regex: /catalog
      page_type: LIST
    expected_result:
      must_contain: 商品目录
      min_items: 1
      loading_must_finish: true
    forbidden_side_effects: [不得创建或修改数据]
    confidence: high
```

这些规则会同步到 `business_rules`，每次实际使用的规则、页面理解、观察证据与发现分别写入 `semantic_evaluations` 和 `semantic_findings`。主观的语句通顺度需要模型；项目通过 `semantic.model.provider: python` 和 `package.module:factory` 形式的本地适配器接入，不绑定特定供应商。传给适配器的正文、URL 和网络证据会先经过统一脱敏。若启用但适配器不可用，运行结果会进入 `NEEDS_REVIEW`。模型凭据不得写入 YAML，应由适配器从环境变量或系统凭据存储读取。

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

## 跨页面共享控件

`deduplication.shared_controls` 用于页眉、页脚、全局导航以及 `fixed/sticky` 悬浮控件：

1. 控件第一次出现时仍执行完整测试；只有 `TECH_PASS`、`STATE_DISCOVERED` 或 `LINK_RECORDED` 会成为可复用来源。
2. 后续页面不会直接跳过。系统会打开页面、重放必要状态，确认控件仍可见且可用，并重新采集实时共享指纹。
3. 共享指纹由站点、全局区域、稳定 `id/data-testid/name`、角色、标签和链接目标构成；坐标、DOM 序号和完整 selector 不参与身份判断。
4. 完全匹配后记录 `SHARED_CONTROL_REUSED`，报告保留来源页面、控件和动作 ID，但不再次点击。
5. 失败、未确认、禁用、属性变化、链接目标变化以及页面业务区/表单提交控件不会复用。
6. 复用只在同一个运行 ID 内生效；`--new-run` 会重新测试，保证回归覆盖。

自动识别证据不足的 AI 助手或反馈入口，可以加入 `include_selectors`；确认不应跨页复用的控件可以加入 `exclude_selectors`。默认 `min_confidence: high`，不建议为了提高命中率直接改成 `medium`。

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
- 报告中的“业务流程线索”来自这些路径，只作为语义判断的证据，不单独作为业务真值。

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

# 选择冒烟或全量模式
python .\webqa_cli.py run --profile smoke --new-run
python .\webqa_cli.py run --profile full --new-run

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
        ├── semantic-reproductions/
        └── pagepatrol/
```

普通 `run`、中断恢复或提高深度续跑都会沿用原实例目录；`run --new-run` 才创建新的时间归档。报告和 manifest 中的 `run_rounds` 会记录每次新建、恢复、深度扩展及提升页面数。

## 覆盖审计

最终报告会分别对账：

- 页面节点和页面状态；
- 可见控件出现次数和去重后的控件队列；
- 首次记录链接、重复链接、外域链接和深度边界；
- 技术通过、风险跳过、表单规划、业务规则未确认、自动化未确认和阻塞项；
- 页面类型、语义覆盖状态、Oracle 来源、预期/实际、置信度和双上下文复现结果；
- PagePatrol 报告、截图以及完整点击历程。
- API/UI 契约异常、PagePatrol 明显缺陷、表单边界异常、幂等/可逆/蜕变关系和 Given/When/Then 业务流程结果。
- 大列表的可见项数量、首尾项目样本、分页头/中/尾样本及被采样跳过的重复控件数量。
