# 任务 5 报告：真实数据库仪表盘

## RED/GREEN 周期 1：查询边界

- RED：新增 `tests/test_dashboard.py` 后运行
  `.venv/bin/python -m pytest tests/test_dashboard.py -q`，因
  `ModuleNotFoundError: No module named 'app.dashboard'` 失败。
- GREEN：新增 `app/dashboard.py`。`build_dashboard_context()` 以 Asia/Shanghai
  自然日换算为 naive UTC `[start, end)` 区间，并返回真实的启用规则数、当日执行数、
  近 24 小时失败/部分失败数、最近 5 条执行和近 24 小时邮件成功/失败数。
- 验证：`.venv/bin/python -m pytest tests/test_dashboard.py -q`，`4 passed`。

## RED/GREEN 周期 2：路由和模板

- RED：新增 `test_dashboard_uses_real_metrics` 后运行
  `.venv/bin/python -m pytest tests/test_routes.py -k 'dashboard_uses_real' -q`；
  页面仍缺少真实的 `enabled-rule-count`，断言失败。
- GREEN：仪表盘路由注入 `Session` 并调用 `build_dashboard_context()`；模板呈现实时指标、
  最近执行表和邮件汇总，只在对应真实列表为空时显示空态。
- 验证：`.venv/bin/python -m pytest tests/test_dashboard.py tests/test_routes.py -k
  'dashboard or navigation' -q`，`7 passed, 138 deselected`。

## 完整验证

- `.venv/bin/python -m pytest -q`：`340 passed`（仅既有第三方弃用警告）。
- `.venv/bin/ruff check .`：通过。
- `git diff --check`：通过，无空白错误。

## 自审

- 查询错误未被吞掉或转换为零值，因此会正常形成服务错误。
- 时间戳继续使用既有 schema 的 naive UTC；仅 Shanghai 日界换算使用时区对象。
- 范围只包含任务分配的路由、模板、查询模块、测试与本报告，未回退任务 1-4 改动，未修改进度账本。

## 视觉顾虑

- 保留现有 `panel`、`metric-card`、`table-panel`、`table-shell` 和 `status` 类，无新增装饰背景或嵌套卡片。
- 最近执行表在窄屏沿用既有 `table-shell` 横向滚动；长规则名仍受表格容器约束，不会挤破双栏布局。

## 提交

- `feat: show live operational dashboard metrics`

## 审查修复：近 24 小时上界

- RED：新增 `test_dashboard_recent_window_includes_boundaries_and_excludes_future_records`，
  运行该测试后失败；未来一秒的失败记录被计入，实际为 `3`、期望为 `2`。
- GREEN：失败执行与邮件成功/失败查询均追加 `<= current`，明确窗口为
  `[current - 24h, current]`；精确下界和精确当前时间包含，未来记录排除。
- 验证：该测试单独运行，`1 passed`。

## 审查修复：最近执行稳定排序

- RED：新增 `test_dashboard_recent_executions_breaks_timestamp_ties_by_latest_id`，
  同时间戳结果实际为 `[1, 2, 3, 4, 5]`，期望为 `[6, 5, 4, 3, 2]`。
- GREEN：最近执行查询追加 `ExecutionLog.id DESC` 次级排序，`limit 5` 边界稳定。
- 验证：该测试单独运行，`1 passed`。

## 审查修复验证

- `.venv/bin/python -m pytest tests/test_dashboard.py -q`：`6 passed`。
- `.venv/bin/python -m pytest tests/test_dashboard.py tests/test_routes.py -k
  "dashboard or navigation" -q`：`9 passed, 138 deselected`。
- `.venv/bin/python -m pytest -q`：`342 passed`（仅既有第三方弃用警告）。
- `.venv/bin/ruff check .`：通过。
- `git diff --check`：通过，无空白错误。
- 修复提交：`fix: bound and stabilize dashboard metrics`。

## 审查修复自审与顾虑

- 生产代码仅增加查询上界与确定性排序；未触碰路由、模板、进度账本或其他任务文件。
- 无视觉变更。剩余输出仅为既有 `crypt` 与 Starlette/httpx 弃用警告，不影响本次行为。
