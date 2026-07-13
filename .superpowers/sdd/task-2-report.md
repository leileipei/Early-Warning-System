# Task 2: CSRF Security Primitives

## 实现内容

- 新增 `app/web_security.py`。
- 提供 `CSRF_SESSION_KEY`、`CSRF_FORM_FIELD`、`ensure_csrf_token`、`rotate_csrf_token` 和 `require_csrf`。
- 使用 Session CSRF token；缺失时以 `secrets.token_urlsafe(32)` 生成，轮换时覆盖旧 token。
- 对 `POST`、`PUT`、`PATCH`、`DELETE` 读取解析后的表单数据，使用常量时间比较校验 token；缺失、错误或表单解析异常统一返回 403 和 `请求安全校验失败`。
- 新增 `tests/test_web_security.py`，覆盖匹配 token、缺失/错误 token、跨会话 token、multipart 上传和 token 轮换。

## RED/GREEN 证据

### RED

命令：

```text
/Users/leo.cui/Documents/Early\ Warning\ System/.venv/bin/python -m pytest tests/test_web_security.py -q
```

结果：收集失败，`ModuleNotFoundError: No module named 'app.web_security'`。

### GREEN

同一命令在实现后结果：`6 passed, 1 warning`。

## 验证结果

- 完整测试：`244 passed, 479 warnings in 4.33s`
- Ruff：`/Users/leo.cui/Documents/Early\ Warning\ System/.venv/bin/ruff check app/web_security.py tests/test_web_security.py`，结果 `All checks passed!`
- `git diff --check`：通过。

## 变更文件

- `app/web_security.py`
- `tests/test_web_security.py`
- `.superpowers/sdd/task-2-report.md`

## 自检与顾虑

- 实现范围仅限 CSRF 原语和隔离测试，未添加路由或模板集成。
- 精确遵循简报中的接口、字段名、方法集合及错误文本。
- 测试输出中的警告来自现有依赖及既有模块（Starlette/httpx、passlib、datetime.utcnow 等），本任务未改动相关代码。
