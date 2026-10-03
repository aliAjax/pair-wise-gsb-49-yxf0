# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、分层摊回、赔偿限额和恢复保费和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `src/ledger.py`：**可重算摊回台账**的常量、类型与纯函数重算引擎。
- `src/ledger_repository.py`：合约年度/分层、巨灾事件、赔案、台账分录与续作批次表，`BEGIN IMMEDIATE`写事务。
- `src/ledger_service.py`：核定预占、结算确认、事件补录/撤回、整体重算、批次续作与旧数据回填。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与台账规则测试。

## 可重算摊回台账

把**合约年度**（年度累计免赔额、分层及恢复次数）、**巨灾事件**（发生日 + 版本）和**赔案**
（核定/结算生命周期）接成一份随时可从输入整体重算的台账，解决“遇险日期/事件号错乱导致
年度累计免赔额算错、晚到赔案沿用过期层恢复”的问题。

核心规则：

1. **确定性重算**：赔案按 `(occurrence_date, claim_number)` 排序，晚到赔案按发生日插入正确
   合约年度与顺序；年度累计免赔额沿分层梯子自下而上按序消耗；每层容量 = 层宽×(1+恢复次数)，
   放不下的部分输出 `gap_amount`（缺口）而不是报错。
2. **两阶段消耗**：`reserve`（核定）只预占免赔额与恢复次数（`reserved`）；`settle`（结算）后
   转 `confirmed` 并冻结金额。重算时已结算赔案先作为冻结池从年度池扣除，金额永不变动。
3. **事件版本**：补录/修正发生日、撤回事件都让事件 `version+1`。只让未结算赔案的旧预占失效，
   按新顺序重算（撤回事件的未结算赔案转 `invalidated` 释放容量）；已结算分录金额与归属不动。
4. **同一层最后一次恢复争抢**：所有核定在一个 `BEGIN IMMEDIATE` 事务内串行执行，配合确定性
   排序，两个赔案同时提交时只有一个放行，另一个在返回体里拿到 `gap_amount`。
5. **可续作批次**：`batch_id + op_key` 幂等，每个操作独立事务落盘；写入失败后拿**完整批次**
   重试时已完成项直接跳过。台账始终由“替换式重算”产生（旧预占标记为 `superseded`），
   不追加、不重复累计。同批次号但内容不一致会被拒绝。
6. **旧数据回填**：缺发生日或事件版本（事件未补录发生日，`version=0`）的赔案停在
   `pending_review`（待核对），不进台账；核定时返回 422。回填补齐后才能核定；补不全就一直
   停在待核对。已结算赔案拒绝回填。

赔案状态：`pending_review → open → reserved → settled`，事件撤回/手动释放时未结算赔案转
`invalidated`（数据补齐后可重新核定）。

## 启动

```bash
python3 app.py --db ./data.db --port 8325
```

默认端口为`8325`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口（通用记录）

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

## 台账接口（路径前缀 `/api/ledger`）

角色：`underwriter`（合约年度）、`catastrophe_analyst`（事件）、`claims_officer`（赔案/核定/批次）、
`finance`（结算）、`admin`（全部）。

- `POST /treaty-years`：建合约年度。
  `{"year":2026,"aggregate_deductible":1000000,"layers":[{"layer_no":1,"attachment":1000000,"width":4000000,"cession_pct":0.4,"reinstatements":0,"reinstatement_pct":0}]}`
- `GET /treaty-years`、`GET /treaty-years/{年|id}/summary`：年度列表与免赔额/恢复/缺口汇总。
  `POST /treaty-years/{年|id}/recompute`：显式重算（只重建预占，已结算不动）。
- `POST /events`：登记事件，`{"event_number":"CAT-1","event_date":"2026-03-01"}`；不给日期即为
  待补录旧事件（version=0）。`GET /events`、`GET /events/{id|事件号}`。
- `POST /events/{id|事件号}`：补录/修正发生日（版本+1，未结算赔案按新顺序重算）。
- `POST /events/{id|事件号}/withdraw`：撤回（版本+1，未结算赔案失效，已结算不动）。
- `POST /claims`：登记赔案 `{"claim_number":"CLM-1","approved_loss":3000000,"occurrence_date":"...","event_number":"..."}`；
  数据不全直接落 `pending_review`。`GET /claims?status=...`、`GET /claims/{id|赔案号}`。
- `POST /claims/{id|赔案号}/backfill`：回填 `occurrence_date`/`event_number`（可清空事件）。
- `POST /claims/{id|赔案号}/reserve`：核定预占，可带 `{"approved_loss":...}`；响应含每层
  `accepted_recovery`、`gap_amount` 与总 `has_gap`。
- `POST /claims/{id|赔案号}/release`：释放预占（赔案失效并重算）。
- `POST /claims/{id|赔案号}/settle`：结算确认，`{"payment_reference":"PAY-1"}`。
- `POST /claims/batch-reserve`：可续作批次核定。
  `{"batch_id":"BATCH-1","items":[{"op_key":"0","claim":"CLM-1"}, ...]}`，失败后用同批次号+完整
  列表重试；`GET /batches/{batch_id}` 查进度。
- `GET /audit?entity=claim&entity_id=3`：台账审计时间线。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及台账的免赔额按序消耗、
核定/结算两阶段、最后一次恢复并发争抢、事件补录/撤回只动未结算、批次续作不重复累计和
旧数据待核对回填。
