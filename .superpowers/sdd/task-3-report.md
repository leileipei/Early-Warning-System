# 任务 3：CSRF 路由、模板与 JavaScript 集成报告

## 实现内容

- 在 `app/auth.py` 和 `app/routes.py` 的 Router 级别挂载 `Depends(require_csrf)`，覆盖全部不安全 HTTP 方法。
- 在页面模板响应上下文中注入 `csrf_token`，并新增 `_csrf.html` 宏以统一渲染 `_csrf_token` 隐藏字段。
- 为注销、登录、规则导入与手动执行、规则新建/编辑、SQL Server 与 SMTP 的创建/测试、SQL Server 编辑等所有 POST 表单加入 Token；`logs.html` 的 GET 筛选表单未加入 Token。
- 登录成功后清空旧会话、写入管理员会话并轮换 CSRF Token。
- SQL 语法校验和 SQL 预览两处 AJAX `FormData` 均传递当前表单的 `_csrf_token`。
- 更新认证与路由测试：保留业务测试对 CSRF 依赖的显式绕过，未认证 POST 测试携带 Token 以继续验证管理员认证的 401 行为。

## RED / GREEN 证据

### RED

执行：

```bash
/Users/leo.cui/Documents/Early\ Warning\ System/.venv/bin/python -m pytest \
  tests/test_auth.py::test_login_rejects_missing_csrf_token \
  tests/test_auth.py::test_login_rotates_csrf_token \
  tests/test_routes.py::test_login_page_renders_csrf_hidden_field \
  tests/test_routes.py::test_admin_pages_render_csrf_for_every_post_form \
  tests/test_routes.py::test_sql_ajax_requests_include_csrf_token -q
```

结果：`7 failed`。失败原因符合预期：路由尚未要求 CSRF、模板没有隐藏字段、AJAX 未携带 Token，且登录后未轮换 Token。

### GREEN

在最小实现后运行同一命令，结果：`7 passed`。

## 验证结果

- 聚焦 CSRF 用例：`7 passed`。
- 修复回归后的导入规则聚焦用例：`2 passed`。
- 三组相关测试：`tests/test_web_security.py tests/test_auth.py tests/test_routes.py`，结果 `119 passed`。
- 全量测试：`/Users/leo.cui/Documents/Early Warning System/.venv/bin/python -m pytest -q`，结果 `251 passed`。
- Ruff：`/Users/leo.cui/Documents/Early Warning System/.venv/bin/ruff check app/auth.py app/routes.py tests/test_auth.py tests/test_routes.py`，结果 `All checks passed!`。
- `git diff --check`：通过，无输出。

测试输出包含现有依赖弃用警告（FastAPI TestClient、passlib crypt、项目中 `datetime.utcnow()`），未新增测试失败。

## 变更文件

- `app/auth.py`
- `app/routes.py`
- `app/static/app.js`
- `app/templates/_csrf.html`
- `app/templates/base.html`
- `app/templates/login.html`
- `app/templates/rules.html`
- `app/templates/rule_form.html`
- `app/templates/settings.html`
- `app/templates/sql_server_form.html`
- `tests/test_auth.py`
- `tests/test_routes.py`

## 自检

- 所有 10 个 `method="post"` 表单均有对应的 `csrf_input(csrf_token)`。
- `app/templates/logs.html` 保持 GET 筛选表单，无 Token。
- `app/static/app.js` 恰有两处 `payload.append("_csrf_token", ...)`。
- 子模板中 `extends` 为第一个 Jinja 标签，宏导入紧随其后。
- 未修改登录限流逻辑，也未修改任务 1/2 的文件。

## 顾虑

无阻塞顾虑。Ruff 仅检查 Python 文件；JavaScript 由静态断言覆盖其两处 CSRF 传参。
