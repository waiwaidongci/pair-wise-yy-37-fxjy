# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改和许可续期。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8314
```

默认端口为`8314`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/renewals`，支持`?status=`过滤
- `POST /api/renewals`，申请人针对有效证发起续期
- `GET /api/renewals/{id}`，含全部复核记录
- `POST /api/renewals/{id}/review`，检查员录入复核结论（pass/fail）
- `POST /api/renewals/{id}/resubmit`，整改后重新提交
- `POST /api/renewals/{id}/decide`，合规经理决定换发（reissue）或驳回（reject）
- `GET /api/audit`

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 许可续期与换发

许可证获批（approved）时分配证号（`permit_no`）和有效期（`valid_from`/`valid_to`，默认`valid_years=5`年）。续期规则：

- 申请人须在原证失效（`valid_to`到期）前发起续期；已过期或未获批的证不能续期。
- 同一设施和许可类型只能保留一张有效证（数据库唯一索引兜底），也不能同时存在两份未结束的续期。
- 检查员在`submitted`状态录入现场复核结论：不通过转入`correction`整改，通过进入`review_passed`。
- 整改后申请人补充材料重新提交，回到`submitted`等待复核，历史复核记录全部保留。
- 合规经理在`review_passed`状态决定换发或驳回。换发在同一事务内完成：旧证`lifecycle`置为`replaced`立即失效，新证以`approved`状态、新证号和新有效期生成并出现在列表中；续期置为`completed`并记录`new_item_id`。
- 续期各写操作均需`expected_version`乐观锁；换发受状态守卫、版本检查和唯一索引三重保护，重复请求或并发重试不会生成第二张证。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
