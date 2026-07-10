# Worker 动态规则同步设计

## 目标

Worker 在运行期间自动同步后台中的预警规则。管理员新增规则、修改 Cron、启用或停用规则后，无需重启 Worker，变更应在默认 10 秒内反映到定时调度器。

## 范围

本次实现包含：

- Worker 启动时立即同步一次启用规则。
- Worker 按可配置间隔轮询规则表。
- 增量新增、重排或移除 APScheduler 任务。
- 数据库读取或单个任务同步失败时保留可用任务，并在下一轮重试。
- 自动化测试、环境变量示例和运维文档更新。

本次不包含：

- Redis、消息队列或跨进程事件通知。
- APScheduler 持久化 JobStore。
- 多 Worker 协调或分布式锁。
- Web 页面上的 Worker 状态展示。

## 配置

在 `Settings` 中增加：

- `scheduler_sync_interval_seconds`: 正数，默认值为 `10`。
- 对应环境变量为 `SCHEDULER_SYNC_INTERVAL_SECONDS`。

配置无效时应用应在启动阶段明确报错，而不是静默回退。Web 和 Worker 继续共用同一份 `.env`，虽然该配置只影响 Worker。

## 架构

保留现有 `BackgroundScheduler` 和内存任务存储。新增一个职责单一的规则调度同步器，持有当前已知的 `rule_id -> cron_expression` 映射，并通过稳定任务 ID `rule-{rule_id}` 管理任务。

同步器每次接收最新规则集合后计算目标状态：

- 规则已启用、已保存且 Cron 有效：目标状态中应存在对应任务。
- 规则新增或目标任务意外丢失：添加任务。
- 规则 Cron 与已知值不同：使用新触发器替换对应任务。
- 规则停用、删除或 Cron 无效：移除已有任务。
- 规则未变化且任务仍存在：不调用 APScheduler 修改方法，避免重置下一次执行时间。

任务继续使用 `max_instances=1`、`coalesce=True` 和 `replace_existing=True`，保持现有防并发与错过执行合并行为。

同步逻辑放在 `app/scheduler.py`，数据库轮询和生命周期留在 `app/worker.py`。同步器只依赖规则集合、调度器和执行回调，不直接访问数据库，因此可以独立测试。

## 数据流

1. Worker 初始化数据库并创建空的 `BackgroundScheduler`。
2. Worker 查询当前全部规则并执行首次同步。
3. Worker 启动调度器。
4. Worker 每隔 `scheduler_sync_interval_seconds` 查询一次规则表。
5. 同步器比较最新规则与已知任务，仅执行必要的新增、替换和移除。
6. 定时任务触发后仍通过现有 `execute_rule_by_id(..., TriggerType.SCHEDULED)` 执行规则。

查询全部规则而不是只查询启用规则，便于同步器明确识别停用状态。规则数量在当前单 Worker 版本中预计较小，轮询不会读取 SQL 文本之外的关联数据，也不会连接业务 SQL Server。

## 错误处理

- 数据库查询失败：记录异常，跳过本轮同步，保留当前调度任务，下一轮继续重试。
- 单个任务新增、替换或移除失败：记录规则 ID 和异常；其他规则继续同步；失败项保持原已知状态，下一轮继续重试。
- 无效 Cron：该规则不进入目标状态；如果以前存在对应任务，则将其移除。
- Worker 收到 `KeyboardInterrupt`：正常关闭调度器并退出。
- 单条规则执行失败：继续沿用现有执行日志、重试和 `max_instances=1` 行为，不影响同步循环。

同步错误使用 Python 标准日志输出，不写入规则执行日志，因为同步尚未触发一次规则执行。

## 测试

在 `tests/test_scheduler.py` 中覆盖：

- 首次同步添加所有有效启用规则。
- 未变化规则不会重复安排。
- 新增规则会增加任务。
- Cron 修改会替换对应任务。
- 停用、删除或无效规则会移除对应任务。
- 调度任务意外丢失时会自动补回。
- 一个任务同步失败不会阻止其他任务同步，并可在下一轮重试。

在 Worker 测试中覆盖：

- 读取数据库成功时调用同步器。
- 数据库读取失败时保留现有任务状态并继续后续轮询。
- 执行回调仍使用独立 Session 和 `TriggerType.SCHEDULED`。

在 Settings 测试中覆盖默认值、环境变量覆盖和非正数拒绝。

完成后运行完整 `pytest` 与 `ruff check .`。

## 文档与验收

更新 `.env.example`、`README.md`、`docs/deployment.md`、`docs/operations.md` 和项目需求文档，说明：

- 默认每 10 秒同步规则。
- 可通过环境变量调整间隔。
- 后台规则变更不再要求重启 Worker。
- Worker 未运行时定时任务仍不会触发。

验收标准：Worker 持续运行时，管理员新增规则、修改 Cron、启用或停用规则，调度器在配置的同步间隔内反映变化，且其他未变化规则的下一次执行时间不被重置。
