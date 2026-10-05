# 采购订单变更控制

本项目维护采购订单变更控制的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖采购计划员、供应商、质量工程师、仓储管理员，并明确订单基线版本、部分变更接受、并发乐观锁、差异责任追踪等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/change_control/`：变更控制后端（基线版本链、提案、确认、承诺、成本证据、责任判定、HTTP API）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约完整性与后端行为回归测试。

## 后端服务

`change_control` 包实现：

- 订单基线与版本链：基线为 v1，每次变更生效追加版本并记录父版本与来源提案，拒绝、部分接受、连续变更、撤回签署均留痕；
- 变更提案与供应商确认：逐项 ACCEPT/REJECT，部分接受时仅已接受项生效，不会覆盖供应商已备料部分；
- 受影响承诺与成本证据：供应商登记已备料/排产数量与成本单据，作为责任判定依据；
- 责任判定：采购方调减数量低于受保护备货量时由采购方承担索赔，供应商发起的变更责任归供应商；
- 乐观锁：提案修订号（revision）与订单版本号双重校验，冲突返回 409；
- 版本比较：`compare_versions` 输出每个变更项的责任方与索赔金额及净差异汇总。

启动 HTTP API：`PYTHONPATH=src python3 -m change_control.api --port 8080`

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
