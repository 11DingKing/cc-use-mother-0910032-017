# 采购订单变更控制

本项目维护采购订单变更控制的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖采购计划员、供应商、质量工程师、仓储管理员，并明确订单基线版本、部分变更接受、并发乐观锁、差异责任追踪等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/po_change_control/`：变更控制后端（纯标准库实现）。
  - `models.py`：订单行、提案、签署、版本链、受影响承诺、成本证据。
  - `service.py`：基线下达、提案、确认、撤回、生效等核心业务规则。
  - `diff.py`：任意版本比较，逐项输出差异责任方与索赔依据。
  - `api.py`：HTTP JSON 接口（`http.server` 实现）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约完整性回归测试与后端服务、接口测试。

## 业务规则

- **订单基线**：订单下达时生成版本链第 0 号节点，此后内容只能经变更提案演进。
- **部分变更接受**：供应商逐项确认（接受/拒绝）并签署；生效时只把「已接受且签署有效」的变更项写入新版本，被拒绝或签署被撤回的项保持原值，供应商已备料部分不会被邮件式确认整体覆盖。
- **版本链**：基线、变更生效、全部拒绝、签署撤回都按序落链，每个节点持有当时快照，可比较任意两个版本。
- **并发乐观锁**：提案携带 `lock_version`，确认、撤回、生效必须带期望值，冲突返回 409；生效还要求提案基于最新内容版本，防止并发提案互相覆盖。
- **差异责任追踪**：差异责任方即提案发起方（采购计划员发起归采购方，供应商发起归供应商）；差异行上的受影响承诺与成本证据一并输出，作为索赔判断依据。

## HTTP 接口

启动：`PYTHONPATH=src python3 -m po_change_control.api --port 8080`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/orders` | 创建草拟订单 |
| POST | `/orders/{id}/release` | 下达订单，生成基线版本 |
| GET | `/orders/{id}` | 当前生效内容 |
| GET | `/orders/{id}/versions` | 版本链摘要 |
| GET | `/orders/{id}/versions/{seq}` | 指定版本快照 |
| POST | `/orders/{id}/proposals` | 提交变更提案 |
| GET | `/orders/{id}/proposals`、`/proposals/{id}` | 查询提案 |
| POST | `/proposals/{id}/confirm` | 供应商逐项确认（乐观锁） |
| POST | `/proposals/{id}/withdraw-signature` | 撤回某项签署（乐观锁） |
| POST | `/proposals/{id}/apply` | 生效，只调整被批准部分 |
| POST | `/orders/{id}/commitments` | 登记受影响承诺（如已备料） |
| POST | `/commitments/{id}/evidence` | 登记成本证据 |
| GET | `/orders/{id}/diff?from_seq=&to_seq=` | 比较任意版本，展示每项差异的责任方与索赔依据 |

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
