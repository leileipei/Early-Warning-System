# Task 10 报告：安全响应头和 CSP

## 实现

- 为所有常规响应增加 CSP、`nosniff`、同源 Referrer、权限策略和 `DENY` 防嵌入头。
- `SESSION_COOKIE_SECURE=true` 时增加一年期 HSTS；关闭时移除该头。
- 未处理异常的 500 由应用级异常处理器复用安全头逻辑，覆盖 `BaseHTTPMiddleware` 外层错误响应。
- 删除确认改为 `data-confirm` 加事件委托；SQL 检测和预览同样使用外部脚本委托，服务器表单提交保持可用。

## 验证与自审

- 聚焦：`168 passed`。
- 全量：`439 passed`。
- Ruff：`All checks passed!`。
- 模板扫描确认不存在内联脚本或 `on*` 事件属性；真实 500、登录、后台、重定向、404 与 HSTS 开关均有回归测试。
- 后续审查加固：CSP 按分号解析为精确指令集合；临时移除 `object-src 'none'` 时对应测试失败，恢复后复验。
