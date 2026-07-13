# Web 后台安全加固设计

## 目标

为 SQL 预警系统后台增加三项基础安全能力：

- 所有修改类表单请求执行 CSRF Token 校验。
- 管理员登录失败达到阈值后执行短时锁定。
- Session Cookie 安全属性可按 HTTP/HTTPS 部署环境配置。

加固后必须继续兼容当前 HTTP 部署；生产环境切换 HTTPS 后可通过环境变量启用 `Secure` Cookie。

## 范围

本次包含：

- Session CSRF Token 的生成、模板注入和请求校验。
- 登录失败的进程内、线程安全限流。
- Session Cookie 的显式 `HttpOnly`、`SameSite=Lax` 和可配置 `Secure` 属性。
- 环境变量、自动化测试、部署文档、运维文档和项目需求更新。

本次不包含：

- Redis 或其他外部限流存储。
- SQLite 限流表或数据库迁移。
- 多 Web 实例之间的限流状态共享。
- 验证码、短信、邮件或多因素认证。
- SSO、LDAP、AD、多角色权限或规则审批。
- 自动信任未经配置的 `X-Forwarded-For` 请求头。

## 配置

在 `Settings` 中增加：

- `session_cookie_secure: bool = False`
- `login_max_failures: int = 5`，必须大于零。
- `login_failure_window_seconds: int = 900`，必须大于零。
- `login_lockout_seconds: int = 900`，必须大于零。

对应环境变量：

```dotenv
SESSION_COOKIE_SECURE=false
LOGIN_MAX_FAILURES=5
LOGIN_FAILURE_WINDOW_SECONDS=900
LOGIN_LOCKOUT_SECONDS=900
```

当前 HTTP 环境保留 `SESSION_COOKIE_SECURE=false`。生产环境完成 HTTPS 反向代理配置后必须改为 `true`。

`SessionMiddleware` 显式使用：

- `https_only=settings.session_cookie_secure`
- `same_site="lax"`

Starlette Session Cookie 继续保持 `HttpOnly`。不更改现有 Cookie 名称和会话有效期，避免无必要的兼容性变化。

## 组件边界

新增 `app/web_security.py`，集中提供以下能力：

```python
def ensure_csrf_token(request: Request) -> str: ...
async def require_csrf(request: Request) -> None: ...
def client_identifier(request: Request) -> str: ...

class LoginRateLimiter:
    def retry_after(self, client_id: str, username: str) -> int: ...
    def record_failure(self, client_id: str, username: str) -> int: ...
    def clear(self, client_id: str, username: str) -> None: ...
```

`app/routes.py` 负责在所有 HTML 模板上下文中注入 CSRF Token。`app/auth.py` 负责在登录流程中调用限流器，但不持有限流状态。`app/main.py` 创建限流器并放入 `app.state.login_rate_limiter`，使应用实例和测试相互隔离。

限流器使用标准库锁保护内部状态，并支持注入单调时钟，便于无真实等待地测试窗口和锁定到期行为。

## CSRF 设计

### Token 生成

- Token 使用 `secrets.token_urlsafe()` 生成。
- Token 保存到签名 Session 中。
- `ensure_csrf_token` 在 Token 缺失时创建，在已存在时复用。
- 登录成功清空旧 Session 后立即创建新 Token，再写入管理员 ID，防止会话固定。

### 模板注入

`_template_response` 自动将 `csrf_token` 加入模板上下文。所有 `method="post"` 表单必须包含：

```html
<input type="hidden" name="_csrf_token" value="{{ csrf_token }}">
```

覆盖登录、退出、规则导入、新建、编辑、手动执行、SQL 检测、SQL 预览、SQL Server 配置与测试、SMTP 配置与测试等全部 POST 表单。

Token 必须由 HTML 直接输出，不能依赖 JavaScript 动态注入。

### 请求校验

`require_csrf` 对 `POST`、`PUT`、`PATCH`、`DELETE` 请求读取表单字段 `_csrf_token`，并使用 `secrets.compare_digest` 与 Session Token 比较。安全方法直接放行。

认证路由和后台页面路由均通过 Router 级依赖执行该校验。普通 URL 编码表单和 multipart 文件导入必须使用同一套校验。

以下情况返回 `403 Forbidden`，错误信息统一为“请求安全校验失败”：

- Session 中没有 Token。
- 表单缺少 Token。
- Token 错误。
- Token 来自其他 Session。
- 修改类请求的内容无法按表单解析。

校验失败时不得进入登录、数据库写入、SQL 执行、邮件发送或退出逻辑。

## 登录限流设计

### 限流 Key

Key 由以下两部分组成：

- `request.client.host`，不存在时使用稳定占位值。
- 去除首尾空白并执行 `casefold()` 的用户名。

系统不直接读取 `X-Forwarded-For`。部署在反向代理后时，应通过 Uvicorn 的可信代理配置，使 `request.client.host` 只接受可信代理传递的客户端地址。

### 状态和时间

每个 Key 保存：

- 当前窗口中的失败时间戳。
- `locked_until`。

使用单调时钟计算时间，避免系统时间调整影响锁定窗口。每次访问时清理过期失败记录和已过期锁定；无活动状态的 Key 从字典移除，防止内存持续增长。

### 登录流程

1. CSRF 校验通过。
2. 计算客户端 ID 和规范化用户名。
3. 调用 `retry_after`；已锁定时不查询用户和不校验密码，直接返回 `429`。
4. 查询管理员并校验密码。
5. 用户不存在或密码错误时调用 `record_failure`。
6. 第 5 次有效失败立即建立 15 分钟锁定，本次响应返回 `429`。
7. 未达到阈值时继续返回现有 `400` 和“用户名或密码错误”。
8. 登录成功后调用 `clear`，清空该 Key 的失败状态。
9. 清空旧 Session、生成新 CSRF Token、写入管理员 ID，并返回现有 `303` 跳转。

锁定响应包含：

- 状态码 `429 Too Many Requests`。
- `Retry-After`，值为向上取整的剩余锁定秒数。
- 统一信息“登录尝试过多，请稍后重试”。

未知用户名和错误密码执行相同计数与响应，避免用户名枚举。

## 运行边界

限流状态只存在于当前 Web 进程：

- Web 进程重启后锁定状态清空。
- 多 Web Worker 之间不共享失败次数。
- 当前项目推荐的单 Web 进程部署可以使用此方案。
- 后续扩展多实例时，将 `LoginRateLimiter` 的状态接口迁移到 Redis，不改变登录路由语义。

CSRF Token 存放于签名 Session；`SESSION_SECRET` 泄露会破坏会话和 CSRF 防护，因此继续作为生产必备秘密管理。

## 测试

### CSRF

- 登录页生成 Session Token，并在表单中输出隐藏字段。
- 后台所有 POST 表单均包含隐藏字段。
- 正确 Token 可提交登录、普通表单和 multipart 导入。
- 缺失、错误和其他 Session Token 返回 `403`。
- CSRF 失败时目标路由逻辑不执行。
- 登录成功后 Token 发生轮换，旧 Token 不再可用。

### 登录限流

- 同一 IP 和用户名在 15 分钟内累计失败。
- 前 4 次返回 `400`，第 5 次返回 `429`。
- 锁定响应包含正确 `Retry-After`。
- 锁定期间即使密码正确也不执行密码校验。
- 15 分钟后自动解除。
- 登录成功清除失败状态。
- 不同 IP 或不同用户名的状态互不影响。
- 未知用户名与错误密码使用相同限流行为。
- 过期状态被清理。

### Cookie 和配置

- 默认 HTTP Cookie 不包含 `Secure`。
- `SESSION_COOKIE_SECURE=true` 时 Cookie 包含 `Secure`。
- Cookie 包含 `HttpOnly` 和 `SameSite=Lax`。
- 限流整数配置拒绝零和负数。

完成后运行完整 `pytest`、`ruff check .` 和 `git diff --check`。

## 文档和验收

更新 `.env.example`、`README.md`、`docs/deployment.md`、`docs/operations.md` 和 `docs/project-requirements.md`，说明：

- HTTP 环境与 HTTPS 环境的 Cookie 配置差异。
- HTTPS 上线后必须启用 `SESSION_COOKIE_SECURE=true`。
- 登录失败锁定阈值、窗口和默认锁定时间。
- 单 Web 进程限流状态的边界。
- 反向代理下客户端 IP 的可信代理配置要求。
- CSRF `403` 和登录限流 `429` 的排障方法。

验收标准：原有后台工作流在携带有效 CSRF Token 时保持可用；无有效 Token 的修改请求被拒绝；登录暴力尝试在第 5 次失败时锁定；HTTP 开发环境仍可登录；HTTPS 环境可启用 `Secure` Cookie；完整测试与静态检查通过。
