# 任务 4：线程安全登录限流器报告

## 实现

- 在 `app/web_security.py` 增加 `client_identifier(request)`：只返回
  `request.client.host`，没有客户端信息时返回稳定值 `"unknown-client"`；不信任
  `X-Forwarded-For`。
- 增加进程内 `LoginRateLimiter`。状态按
  `(client_id, username.strip().casefold())` 分隔，由 `Lock` 保护，并支持注入
  `time.monotonic` 兼容的时钟。
- 限流器提供 `retry_after`、`record_failure` 和 `clear`；每次读写均清理过期
  失败记录和过期锁定。锁定过期时会清空旧失败，即使锁定时长短于失败窗口。

## TDD 证据

### RED

先在 `tests/test_web_security.py` 增加限流器、键隔离、客户端标识和伪时钟测试，随后运行：

```text
/Users/leo.cui/Documents/Early Warning System/.venv/bin/python -m pytest \
  tests/test_web_security.py -k "limiter or client_identifier" -q
```

结果如预期失败：测试收集阶段报 `ImportError: cannot import name
'LoginRateLimiter' from 'app.web_security'`，因为生产接口尚不存在。

### GREEN

以最小实现加入受锁保护的状态和清理逻辑后，运行同一聚焦命令：

```text
8 passed, 7 deselected, 1 warning in 0.06s
```

测试覆盖第五次失败锁定、`Retry-After` 非整数剩余时间向上取整、窗口过期、
短锁定到期后的失败记录清空、客户端和用户名键隔离、`clear`、用户名规范化及
`client_identifier` 的真实客户端/缺失客户端路径。

## 验证

```text
tests/test_web_security.py: 15 passed, 1 warning in 0.08s
完整 pytest: 260 passed, 483 warnings in 4.89s
ruff check .: All checks passed!
git diff --check: clean
```

警告均为项目已有依赖或 `datetime.utcnow()` 等弃用警告，未引入测试失败。

## 文件

- `app/web_security.py`
- `tests/test_web_security.py`
- `.superpowers/sdd/task-4-report.md`

## 自检与顾虑

- 未修改登录路由或认证流程，任务 5 仍可在该原语上接线。
- 所有限流状态仅在当前进程内，符合任务范围；多进程部署不会共享锁定状态，未来
  若需要跨进程限流应改用外部共享存储。
