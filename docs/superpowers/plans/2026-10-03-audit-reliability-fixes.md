# 审计可靠性修复实施计划

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复 Cron 时区偏移和 SMTP 部分拒收被误记成功的问题。

**Architecture:** 保持 FastAPI、单 Worker 和单机 SQLite 架构。所有 CronTrigger 显式使用 Asia/Shanghai；SMTP 返回值携带部分成功标记，由执行器传播至日志持久化，不扩大为自动补发或数据库结构重构。

**Tech Stack:** Python、APScheduler、smtplib、SQLModel、pytest。

**Spec:** docs/project-requirements.md，第 4.4、4.6、4.8 节及本次审计复现。

## Global Constraints

- 保留旧 MailSendResult 调用的兼容性和已有日志。
- SMTP 外部错误不直接回显，继续使用固定安全摘要。
- 部分失败不整批重试，不写入成功抑制状态。
- 不改动现有配置、真实业务库和未提交的历史计划。

## Review Focus

- UTC 及非 UTC 宿主机上的首次任务和动态新增任务均按北京时间触发。
- Cron 修改后仍保留明确的业务时区。
- 主送或抄送部分拒收均不能记录为全部成功。
- 完全拒收仍是失败，正常成功仍是成功。
- 旧 SQLite 日志与新部分失败日志均能查询、筛选和导出。

## Task 1: Cron 时区

**Files:** app/scheduler.py、tests/test_scheduler.py。

**Interfaces:** build_scheduler 和 RuleScheduleSynchronizer 保持原签名；内部统一 Asia/Shanghai。

- [x] 添加首次、动态新增、动态修改的下一执行时间测试，模拟 UTC 和 America/New_York 宿主机。
- [x] 运行定点测试，确认旧实现产生错误时间（6 个用例失败）。
- [x] 显式传入业务时区，运行整个调度测试文件（35 passed）。

## Task 2: SMTP 部分失败

**Files:** app/mailer.py、app/executor.py、app/execution_service.py、app/models.py、app/dashboard.py、app/templates/dashboard.html，以及对应 tests/ 测试文件。

**Interfaces:** MailSendResult 增加默认 False 的 partial_success；MailStatus 增加 partial_failed。现有列结构不变。

- [x] 添加主送、抄送和完全拒收的返回值测试。
- [x] 添加真实执行器到 SQLite 日志的部分失败测试，验证不重试、不记录成功抑制。
- [x] 确认旧实现失败（5 个用例失败），再实现部分失败标记的传播和固定错误摘要。
- [x] 验证新状态筛选、CSV 导出及旧状态兼容（4 passed）。
- [x] 复查新状态的仪表盘计数；先复现漏计失败，再将部分失败纳入统计，保持 24 小时边界。

## Final Verification

- [x] 运行 Ruff、完整 pytest 覆盖率检查、pip check、git diff --check（535 passed，覆盖率 94.16%）。
- [x] 自审修改，写中文修复记录，明确 SQL 校验、SQLSTATE 重试和依赖升级仍待完成。
- [x] 本次不直接推送或重启生产服务，交付本地修复和验证结果。
