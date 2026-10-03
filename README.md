# 再保险合约与巨灾暴露管理

纯Python标准库实现的再保险合约与巨灾暴露管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：单合约状态转换、分层摊回、赔偿限额和恢复保费和冲突检查。
- `src/ledger.py`：台账计算引擎——按发生日排序重算年度累计免赔窗口、层恢复次数与缺口（纯函数，无I/O）。
- `src/ledger_rules.py`：台账用例的角色、输入校验与赔案状态机。
- `src/ledger_service.py`：核定预占、结算确认、事件失效重算、批次续作与旧数据回填的编排。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：单合约用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8325
```

默认端口为`8325`，默认数据库位于项目目录。服务启动时自动建表。

## 合约年度台账（可重算）

合约年度、巨灾事件和赔案接成可重算台账，解决按单笔赔案看摊回时遇险日期/事件号混乱导致的年度累计免赔额错算，以及晚到赔案沿用过期层恢复的问题。

### 核心语义

- **核定预占**：赔案核定时按发生日排序预占年度累计免赔额与恢复次数；预占是暂时的，后续重算可能调整。
- **结算确认**：结算时才确认消耗，已结算金额锚定不动，后续任何重算都不改变。
- **发生日排序**：台账按（发生日、赔案号）排序计算，晚到的早发生赔案会把后续未结算赔案重新挤位。
- **并发最后一恢复**：同一层最后一次恢复被两个赔案同时核定时，写事务串行化只放行一个，另一个在台账中返回缺口（`shortfall`）。
- **事件补录/撤回**：事件更正或撤回后版本号递增，仅未结算赔案失效（`stale`）并按新顺序重算；已结算金额不动。
- **批次续作**：事件变更触发跨合约年度的重算批次（`recalc_jobs`/`recalc_tasks`），写入失败后可从完整批次续作（`POST /api/recalc-jobs/{id}/resume`），已完成任务不重复执行、不重复累计。
- **旧数据回填**：缺发生日或事件版本的赔案先按事件登记回填（继承的发生日随事件更正联动，自报的发生日不动），补不全的停在待核对（`pending_review`），不进台账。

### 赔案状态机

`submitted` → `approved`（预占）→ `settled`（确认）；另有 `rejected`、`stale`（失效待重算）、`pending_review`（待核对）。发生日不在合约年度内或事件缺失/已撤回时，核定停在待核对。

### 台账接口

- `POST /api/treaties`：注册合约年度（`underwriter`）。
- `GET /api/treaties`、`GET /api/treaties/{id}`：合约年度与累计计数。
- `GET /api/treaties/{id}/ledger`：最新台账批次分录。
- `POST /api/treaties/{id}/recalculate`：手工触发重算批次。
- `POST /api/events`：注册/补录巨灾事件（自动触发相关合约年度重算）。
- `POST /api/events/{id}/correct`：更正事件发生日（版本递增，继承日期的赔案联动重算）。
- `POST /api/events/{id}/withdraw`：撤回事件（未结算赔案失效释放预占）。
- `GET /api/events`、`GET /api/events/{id}`：事件查询。
- `POST /api/claims`：提交赔案（允许缺发生日/事件版本的旧数据）。
- `POST /api/claims/{id}/approve`：核定，请求体`{"expected_version":1,"data":{"approved_loss":...}}`，响应含预占金额与缺口。
- `POST /api/claims/{id}/settle`：结算确认（`finance`），请求体含`payment_reference`。
- `POST /api/claims/{id}/reject`：拒绝并释放预占。
- `POST /api/claims/backfill`：旧数据回填扫描，返回已补齐数量与待核对清单。
- `GET /api/claims/{id}/audit`、`GET /api/claims/{id}/ledger`：赔案审计与台账分录历史。
- `GET /api/recalc-jobs/{id}`、`POST /api/recalc-jobs/{id}/resume`：重算批次查询与续作。

角色：`underwriter`（合约/事件）、`claims_officer`（赔案/核定/回填）、`finance`（结算）、`admin`（全部）。

## 单合约接口（原有）

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突，以及台账的发生日排序重算、核定预占/结算确认、并发最后一恢复、事件撤回与更正重算、批次失败续作和旧数据回填待核对。
