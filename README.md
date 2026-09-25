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
- `GET /api/audit`
- `GET /api/permits`、`POST /api/permits`（compliance_manager发证，可用`status`、`facility`、`permit_type`过滤）
- `GET /api/permits/{id}`
- `GET /api/permits/{id}/renewals`、`POST /api/permits/{id}/renewals`
- `GET /api/renewals/{id}`（含历次复核记录）
- `POST /api/renewals/{id}/reviews`（检查员记录现场复核结论）
- `POST /api/renewals/{id}/resubmit`（整改后补充材料重新提交）
- `POST /api/renewals/{id}/reissue`（合规经理换发，请求体需含`valid_until`）
- `POST /api/renewals/{id}/reject`（合规经理决定不予换发）

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 许可续期规则

- 续期只能在原证`active`且未过`valid_until`时由applicant发起；每张证同一时间只能有一份未结束（`submitted`/`rectification`/`reviewed`）的续期。
- 状态机：`submitted` →（检查员复核）→ `reviewed`（通过）或`rectification`（不通过）；`rectification` →（申请人补充材料）→ `submitted`；`reviewed` →（合规经理）→ `reissued`或`rejected`。
- 复核记录只追加，重新提交不删除历史结论。
- 同一设施+许可类型只能保留一张`active`证（数据库部分唯一索引保证）。换发在单个事务内完成：旧证立即置为`invalidated`，生成新证号（`AQ-`前缀）、新有效期和`active`状态，列表可见。
- 换发以续期状态为乐观守卫，重复请求返回409且不会生成第二张证。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
