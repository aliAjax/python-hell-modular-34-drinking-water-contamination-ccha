# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域、采样瓶、交接和回执的请求校验。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检、恢复状态机，以及保管链区域缺口重算。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本、审计哈希链、采样瓶/交接/回执。
- `src/service.py`：身份、角色和用例编排。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和审计查询，以及采样瓶保管账：

- `POST /api/items/<id>/bottles`：登记采样瓶（瓶号、区域、采样时刻、封条号），登记人即当前经手人。
- `POST /api/bottles/<id>/handoffs`（`phase` 默认 `begin`；`confirm` 需 `observed_seal`）：两段式交接，发起方必须是当前经手人，接收人确认时核对封条号；`request_id` 幂等，同一瓶只允许一个未完成交接，`expected_version` 提供乐观并发。
- `POST /api/bottles/<id>/receipts`：实验室回执（回执号唯一，重复提交只记一次）。
- 查询：`GET /api/bottles`、`GET /api/bottles/<id>`（含交接、回执、保管链审计和 `custody_chain_complete`）、`GET /api/bottles/<id>/audit`、`/handoffs`、`/receipts`。

保管账规则：

1. 瓶子登记采样时刻、封条号和经手人；交接前后经手人必须连续，断档（发起人不是当前经手人）立即隔离，封条号不符立即隔离。
2. 隔离瓶（`quarantined`）禁止继续交接，实验室结果一律不予采信；未送达实验室的瓶子回执被拒（保管链不完整）。
3. 恢复供水只认保管链完整且回执达标的区域；无保管记录的旧事件按未采样处理，历史 `sample_results` 仍可查。
4. 晚到结果以新回执取代旧回执（旧回执标记 `superseded` 仍可查），并立即重算区域缺口；若已恢复区域因此不再达标，恢复立即失效、事件退回 `sampled`。
5. 现场角色（`field_operator` 等）越权恢复/放行返回 403，仅 `coordinator`、`regulator` 可恢复。
6. 两人同时提交同一瓶交接时串行化：后到者收到 `handoff_pending`/版本冲突，错误信息带最新经手人。
7. 未完成交接（`pending`）持久保留，调用方用同一 `request_id` 重试不产生第二条记录；回执重放同理。

内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。
