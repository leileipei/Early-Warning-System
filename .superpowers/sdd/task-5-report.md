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
