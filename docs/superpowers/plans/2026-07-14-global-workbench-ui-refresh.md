# 全站工作台 UI 优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 SQL 预警系统的现有后台统一为清晰分区工作台视觉，并在不改变业务行为的前提下提升桌面与移动端可读性。

**Architecture:** 在 `base.html` 中通过 Jinja 的 `request.url.path` 增加当前导航状态，不改变路由上下文。`styles.css` 作为唯一的设计令牌与组件样式来源；各模板仅添加稳定的语义类和分区容器，不修改字段名、表单 action、HTTP 方法、CSRF 宏或 JavaScript 数据属性。

**Tech Stack:** FastAPI、Jinja2、原生 CSS、pytest、Ruff。

## Global Constraints

- 仅修改模板结构与 CSS；不修改路由、数据模型、接口、字段、表单提交地址或 JavaScript 业务逻辑。
- 不增加前端依赖、远程字体、图标库或图像资源。
- 保留全部现有文本操作、确认提示、CSRF 字段和 `data-sql-*` 属性。
- 使用固定字号层级、正常字距、可见键盘焦点和状态色之外的文字信息。
- 窄屏下表单网格收为单列，表格继续由 `.table-shell` 横向滚动。

---

### Task 1: 公共工作台框架与样式令牌

**Files:**
- Modify: `app/templates/base.html:12-25`
- Modify: `app/static/styles.css:1-120`, `app/static/styles.css:394-442`
- Test: `tests/test_routes.py`

**Interfaces:**
- Consumes: `_template_response()` 已传入模板上下文的 `request` 对象。
- Produces: `.nav-link`、`.is-active`、`.page-heading`、`.page-actions`、`.section-heading` 和统一的 CSS 自定义属性，供所有页面模板使用。

- [ ] **Step 1: 写入导航激活状态的失败测试**

```python
def test_navigation_marks_the_current_page(monkeypatch, session):
    client, get_settings, _ = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/rules")

        assert response.status_code == 200
        assert 'class="nav-link is-active" href="/rules"' in response.text
        assert 'class="nav-link" href="/logs"' in response.text
    finally:
        get_settings.cache_clear()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_routes.py::test_navigation_marks_the_current_page -q`

Expected: FAIL，因为当前导航链接没有 `nav-link` 和 `is-active` 类。

- [ ] **Step 3: 在 `base.html` 添加路径感知导航和语义类**

```jinja2
{% set current_path = request.url.path %}
<header class="topbar">
  <a class="brand" href="/">SQL 预警系统</a>
  <nav class="nav-links" aria-label="主导航">
    <a class="nav-link{% if current_path == '/' %} is-active{% endif %}" href="/">仪表盘</a>
    <a class="nav-link{% if current_path.startswith('/rules') %} is-active{% endif %}" href="/rules">规则</a>
    <a class="nav-link{% if current_path.startswith('/settings') %} is-active{% endif %}" href="/settings">配置</a>
    <a class="nav-link{% if current_path.startswith('/logs') %} is-active{% endif %}" href="/logs">日志</a>
  </nav>
```

保留原有退出表单、`csrf_input(csrf_token)`、链接地址和 `<main class="page">`。

- [ ] **Step 4: 重整公共 CSS，建立工作台层级与移动端规则**

将根变量替换为中性表面、蓝色主操作、绿/橙/红状态色，并加入以下完整的公共导航与标题规则：

```css
.page {
  max-width: 1280px;
  margin: 0 auto;
  padding: 32px 28px 56px;
}

.nav-link {
  border-bottom: 2px solid transparent;
  color: var(--muted);
  padding: 18px 10px 16px;
  text-decoration: none;
}

.nav-link:hover,
.nav-link.is-active {
  border-bottom-color: var(--primary);
  color: var(--text);
}

.page-heading {
  display: grid;
  gap: 4px;
}

.page-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}

.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
```

保留现有控件选择器和 SQL 预览样式；更新 `@media (max-width: 760px)` 以让 `.page-actions`、`.nav-links` 和 `.table-actions` 正常换行，且不改变 `.table-shell { overflow-x: auto; }`。

- [ ] **Step 5: 运行测试与静态检查确认通过**

Run: `.venv/bin/python -m pytest tests/test_routes.py::test_navigation_marks_the_current_page -q && .venv/bin/ruff check app tests`

Expected: 测试 PASS，Ruff 输出 `All checks passed!`。

- [ ] **Step 6: 提交公共框架修改**

```bash
git add app/templates/base.html app/static/styles.css tests/test_routes.py
git commit -m "feat: refresh shared workbench layout"
```

### Task 2: 列表、仪表盘与审计页面的结构优化

**Files:**
- Modify: `app/templates/dashboard.html`
- Modify: `app/templates/rules.html`
- Modify: `app/templates/logs.html`
- Modify: `app/templates/rule_versions.html`
- Modify: `app/static/styles.css`
- Test: `tests/test_routes.py`

**Interfaces:**
- Consumes: Task 1 的 `.page-heading`、`.page-actions`、`.section-heading` 和既有 `.table-shell`。
- Produces: `.toolbar-panel`、`.table-panel`、`.status-text` 和 `.metric-card` 的一致标记；不改变任一链接、表单或表格列的数据来源。

- [ ] **Step 1: 写入列表页结构的失败测试**

```python
def test_rules_page_uses_workbench_list_regions(monkeypatch, session):
    data_source = _create_data_source(session)
    _create_rule(session, data_source)
    client, get_settings, _ = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/rules")

        assert response.status_code == 200
        assert 'class="page-heading"' in response.text
        assert 'class="panel table-panel"' in response.text
        assert 'action="/rules/1/run"' in response.text
    finally:
        get_settings.cache_clear()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_routes.py::test_rules_page_uses_workbench_list_regions -q`

Expected: FAIL，因为模板尚未输出 `panel table-panel` 工作台区域类。

- [ ] **Step 3: 用语义类整理四个页面的既有结构**

将每个现有标题块保留为 `page-header`，并给标题文字容器加 `page-heading`、给顶部按钮容器加 `page-actions`。在列表页面将既有 `<section class="panel">` 改为 `<section class="panel table-panel">`；筛选表单外层加 `toolbar-panel`，但不改变 `<form method="get" action="/logs">`。

规则列表的核心结构必须保持：

```jinja2
<section class="panel table-panel">
  <div class="table-shell">
    <table>
      {# 保留原有表头、rule 循环、执行/删除 POST 表单与 CSRF 宏 #}
    </table>
  </div>
</section>
```

仪表盘继续使用现有三个指标和两栏记录，只为指标标题、数值和面板标题增加 CSS 类；规则历史保留原有版本内容和返回链接。

- [ ] **Step 4: 添加列表与指标视觉规则**

```css
.toolbar-panel {
  margin-bottom: 16px;
  padding: 16px 18px;
}

.table-panel {
  padding: 0;
  overflow: hidden;
}

.table-panel .table-shell {
  padding: 0 18px;
}

th {
  background: var(--surface-muted);
  color: var(--muted);
  font-size: 12px;
  letter-spacing: 0;
}

tbody tr:hover {
  background: #f8fbff;
}

.metric-card strong {
  font-size: 32px;
  line-height: 1.15;
}
```

确保 `.table-actions` 在小屏保持换行，删除按钮继续使用 `.button-danger` 及原有确认提示。

- [ ] **Step 5: 运行页面回归测试**

Run: `.venv/bin/python -m pytest tests/test_routes.py -q`

Expected: PASS，且现有规则执行、导入导出、归档、日志筛选和配置路由断言不变。

- [ ] **Step 6: 提交列表与仪表盘修改**

```bash
git add app/templates/dashboard.html app/templates/rules.html app/templates/logs.html app/templates/rule_versions.html app/static/styles.css tests/test_routes.py
git commit -m "feat: refine dashboard and management page hierarchy"
```

### Task 3: 表单、配置页与登录页的视觉优化

**Files:**
- Modify: `app/templates/rule_form.html`
- Modify: `app/templates/settings.html`
- Modify: `app/templates/sql_server_form.html`
- Modify: `app/templates/smtp_form.html`
- Modify: `app/templates/login.html`
- Modify: `app/static/styles.css`
- Test: `tests/test_routes.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: Task 1 的公共标题、面板、按钮、表单和响应式规则，以及现有 `data-sql-input`、`data-sql-check-*` 与 `data-sql-preview-*` 属性。
- Produces: `.form-section`、`.form-actions`、`.sql-workspace` 和 `.auth-shell` 标记；所有表单字段、action 和 CSRF 字段保持原样。

- [ ] **Step 1: 写入表单和登录页标记的失败测试**

```python
def test_rule_form_keeps_sql_behaviors_inside_workbench_sections(monkeypatch, session):
    _create_data_source(session)
    client, get_settings, _ = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/rules/new")

        assert response.status_code == 200
        assert 'class="form-section sql-workspace"' in response.text
        assert 'data-sql-check-button' in response.text
        assert 'data-sql-preview-button' in response.text
    finally:
        get_settings.cache_clear()


def test_login_page_uses_auth_shell(auth_app):
    client = TestClient(auth_app)

    response = client.get("/login")

    assert response.status_code == 200
    assert 'class="auth-shell"' in response.text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_routes.py::test_rule_form_keeps_sql_behaviors_inside_workbench_sections tests/test_auth.py::test_login_page_uses_auth_shell -q`

Expected: FAIL，因为新结构类尚不存在。

- [ ] **Step 3: 为表单页面添加不改变行为的分区标记**

规则表单中保留现有 SQL 文本框及两个检测按钮，仅用以下结构包裹原有内容：

```jinja2
<section class="form-section sql-workspace">
  <div class="section-heading">
    <div>
      <h2>SQL 查询</h2>
      <p class="section-description">仅允许只读 SELECT 或 WITH 查询。</p>
    </div>
  </div>
  {# 保留现有 textarea、data-sql-* 按钮、反馈 span 和预览容器 #}
</section>
```

在规则、SQL Server 与 SMTP 表单中使用 `form-section` 划分既有字段组，在末尾 `.button-row` 上追加 `form-actions`。配置列表继续保留测试、编辑、删除表单和对应 action。登录页增加 `<section class="auth-shell">`，原有登录表单、字段和 POST action 保持不变；不得嵌套新的 `<main>`，因为 `base.html` 已提供页面主区域。

- [ ] **Step 4: 添加表单与认证视觉规则**

```css
.form-section {
  display: grid;
  gap: 14px;
  padding-top: 18px;
  border-top: 1px solid var(--border);
}

.form-section:first-child {
  padding-top: 0;
  border-top: 0;
}

.form-actions {
  margin-top: 8px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
}

.sql-workspace textarea {
  min-height: 220px;
  background: #fbfcfe;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}

.auth-shell {
  display: grid;
  max-width: 420px;
  margin: 48px auto 0;
}
```

保留现有 `.preview-panel` 显示规则和 `.field-feedback.is-success` / `.is-error` 状态。

- [ ] **Step 5: 运行表单、认证与完整验证**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check app tests`

Expected: 所有测试 PASS，Ruff 输出 `All checks passed!`。

- [ ] **Step 6: 提交表单与登录页修改**

```bash
git add app/templates/rule_form.html app/templates/settings.html app/templates/sql_server_form.html app/templates/smtp_form.html app/templates/login.html app/static/styles.css tests/test_routes.py tests/test_auth.py
git commit -m "feat: polish configuration and form workflows"
```

### Task 4: 浏览器视觉回归检查

**Files:**
- Modify: none
- Test: 浏览器中的 `/login`、`/`、`/rules`、`/rules/new`、`/settings`、`/logs`

**Interfaces:**
- Consumes: Tasks 1-3 完成后的本地 Web 服务与现有管理员测试账号。
- Produces: 对桌面和窄屏布局的人工验证结论，不改变应用状态。

- [ ] **Step 1: 启动隔离端口的 Web 服务**

Run: `.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8003`

Expected: 日志包含 `Uvicorn running on http://127.0.0.1:8003`。

- [ ] **Step 2: 在桌面宽度检查公共页面**

打开登录页并使用现有管理员登录；依次检查仪表盘、规则、规则新建、配置和日志页面。确认导航激活项、标题操作区、表格列、表单分组和 SQL 预览区域没有重叠或截断。

- [ ] **Step 3: 在窄屏宽度检查响应式布局**

将浏览器视口切换到 390px 宽度，确认导航可滚动或换行、页面操作按钮不遮挡、表单单列、表格可横向滚动，且没有文字超出容器。

- [ ] **Step 4: 停止临时服务并检查工作区**

Run: `git diff --check && git status --short`

Expected: 无空白错误；只显示本任务中已提交的文件和用户已有的未跟踪计划文件。

## Plan Self-Review

- 设计覆盖：Task 1 覆盖全站视觉系统、导航和响应式基础；Task 2 覆盖仪表盘、规则、日志和版本历史；Task 3 覆盖配置、表单、SQL 工作区和登录；Task 4 覆盖桌面与窄屏浏览器验证。
- 完整性检查：未发现未完成标记或延期实现的表述。
- 一致性检查：所有新增 CSS 类在产生任务中定义，并由后续任务明确消费；既有模板字段、CSRF 宏、`data-sql-*` 属性和提交地址均被列为不可变约束。
