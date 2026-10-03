# 依赖漏洞修复与加密兼容性验证报告

日期：2026-10-03

> 发布进展：三批修复已推送 PR #11，补充本地 Python 3.11 及远端 3.11/3.12/3.13 CI 验证。见 [发布验证记录](release-validation-2026-10-03.md)。下文保留第三批交付时的本地检查快照。

基线：`c16e493` 及前两批本地修复。当前修改未提交、未推送，未重启服务。

## 1. 本轮结论

本地已关闭本次生产和开发锁文件扫描发现的已知依赖漏洞，完整自动化回归通过。
这不是整个项目的生产验收结论，也不证明不存在未公开漏洞或业务逻辑缺陷。

前两批修复与历史证据见 [调度和 SMTP 修复记录](audit-reliability-fixes-2026-10-03.md)
及 [SQL 修复记录](audit-sql-fixes-2026-10-03.md)。

## 2. 依赖修改

| 包 | 原锁定版本 | 新锁定版本 | 范围 |
| --- | --- | --- | --- |
| cryptography | 49.0.0 | 50.0.2 | 生产及开发 |
| httpx2 | 2.7.0 | 2.12.0 | 仅开发 |
| httpcore2 | 2.7.0 | 2.12.0 | 仅开发，HTTPX2 要求同版本 |
| urllib3 | 2.7.0 | 2.8.0 | 仅开发 |

- pyproject.toml 提高 cryptography、httpx2 的版本下限，并增加 urllib3 开发依赖安全下限。
- 两份锁文件通过 pip-compile 定向更新，没有全量升级无关依赖。
- 与 Git 基线比较，除了上述版本变化和第二批新增的 sqlglot==30.21.0，其余锁定版本未变，没有删除依赖。
- CI 保留生产依赖扫描，并增加开发锁文件扫描；Python 3.11/3.12/3.13 矩阵不变。

cryptography 公告涉及 PKCS#7 EnvelopedData 解密接口，修复起始版本为 50.0.0。
50.0.2 是本轮核对的已发布稳定补丁版本；51.0.0 尚未发布，未使用开发版本。
依据：[官方安全公告](https://github.com/pyca/cryptography/security/advisories/GHSA-g6cj-pr64-35w5)、
[官方版本记录](https://cryptography.io/en/latest/changelog/)。

项目 app 中使用 Fernet，没有发现上述 PKCS#7 接口调用。
因此本次发现是依赖扫描风险，不是已经证明本项目存在可利用的 PKCS#7 攻击路径或密码泄漏。

开发扫描额外发现 3 个包的 9 条记录：
httpcore2 的 PYSEC-2026-3844，httpx2 的 PYSEC-2026-3845 至 3849，
urllib3 的 PYSEC-2026-4175 至 4177。这些包不在生产锁文件中。
版本依据：[HTTPX2 变更记录](https://github.com/pydantic/httpx2/blob/main/src/httpx2/CHANGELOG.md)、
[HTTPCore2 发布记录](https://pypi.org/project/httpcore2/)、
[urllib3 2.8.0 发布说明](https://github.com/urllib3/urllib3/releases/tag/2.8.0)。

## 3. 加密兼容性

新增 tests/test_crypto.py，使用升级前 cryptography 49.0.0 生成并固定的合成 Fernet 密文。
密钥和明文均为专用测试值，不读取 .env，不解密实际数据库密码或 SMTP 密码。

- 升级后使用相同测试密钥能解密旧密文，保留中文、分号和花括号内容。
- 新版本加密及解密往返通过。
- 错误密钥及被篡改的旧密文均被拒绝，抛出 InvalidToken。
- 没有修改 app/crypto.py、实际 SECRET_KEY 或存量配置数据，不进行重新加密迁移。

这验证了测试样本的跨版本兼容性，不替代生产环境的配置恢复检查。
发布时必须保留原 SECRET_KEY；更换密钥会导致既有密文不可解密。

## 4. 验证结果

环境：macOS、本地 Python 3.12.14。已安装完整 requirements-dev.lock，
逐项核对两份锁文件中的包与本地安装版本，没有版本偏差。
此前虚拟环境有 8 个包与既有锁文件不一致，本轮已补齐，而非修改这些包的锁定版本。

| 检查 | 结果 |
| --- | --- |
| 升级前依赖约束回归 | cryptography 回归失败，复现仍允许 49.0.0 |
| 开发约束和 CI 回归 | 2 failed，复现旧 HTTPX2 版本可安装及缺少开发扫描 |
| 最终完整 pytest | 579 passed，29.16 秒；本轮新增 6 个测试用例 |
| app 覆盖率 | 94.17%，达到 93% 门槛 |
| Ruff | All checks passed |
| pip check | No broken requirements found；本机缓存不可写提示不影响结果 |
| 生产锁文件扫描 | No known vulnerabilities found，退出 0 |
| 开发锁文件扫描 | No known vulnerabilities found，退出 0 |
| Git 差异检查 | 通过 |

本轮最终漏洞扫描使用默认依赖解析，与 CI 的 strict 检查方式一致，
没有使用忽略漏洞编号或关闭依赖解析的方式让扫描通过。
扫描结果是当次漏洞库快照，不包含 ODBC 驱动、操作系统或服务管理软件的安全评估。

```bash
COVERAGE_FILE=/tmp/ews-dependency-fix.coverage .venv/bin/python -m pytest --cov=app --cov-report=term-missing --cov-fail-under=93
.venv/bin/ruff check .
.venv/bin/python -m pip check
.venv/bin/pip-audit -r requirements.lock --strict --cache-dir /tmp/ews-dependency-audit-cache
.venv/bin/pip-audit -r requirements-dev.lock --strict --cache-dir /tmp/ews-dependency-audit-cache
git diff --check
```

代码差异已本地复核；未执行独立审查或远端 GitHub Actions。
Python 3.11、3.13 和 Linux/Windows 环境本轮未运行，不宣称跨平台 CI 已通过。

## 5. 发布与剩余事项

1. 发布前提交并推送经复核的三批修改，运行 GitHub CI 全部矩阵，并配置主分支保护。
2. 升级前按部署手册停止 Web 和 Worker，备份同一时点数据库与 .env，保留原 SECRET_KEY。
3. 生产安装更新后的 requirements.lock；开发和 CI 安装 requirements-dev.lock。不要混用新旧代码或依赖。
4. 逐条复核已有 SQL 规则与第二批安全筛查兼容性，在真实 SQL Server 上验证语法、只读权限、连接故障恢复。
5. 在受控 SMTP 环境验证全部接受、部分拒收、完全拒收；SMTP 接受不等于收件箱投递成功。
6. 按单 Web、单 Worker、单机 SQLite 边界检查存活、就绪、备份恢复及故障演练。

SMTP 逐收件人明细及仅对失败收件人重发仍未实现，当前提供至少一次发送语义。
本轮不访问业务库、不发送真实邮件、不修改真实密码、不启动或重启运行服务。
