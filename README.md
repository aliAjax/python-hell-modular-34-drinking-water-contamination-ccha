# 市政饮用水污染响应

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8334`。

- `app.py`：服务生命周期和依赖组装。
- `src/domain.py`：水源、污染物、区域和来源校验。
- `src/rules.py`：污染评分、通知去重、停水、切换水源、冲洗、消毒、复检和恢复状态机。
- `src/repository.py`：SQLite、事务、重复保护、乐观版本和审计链。
- `src/service.py`：身份、角色和用例编排。
- `src/http_api.py`：JSON 接口与静态首页。
- `src/audit.py`：审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8334
python3 -m unittest discover -s tests -v
```

接口包括 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`、审计查询和保管链接管接口。

保管链（chain of custody）把污染事件、采样瓶和实验室回执接成一本保管账：

- `POST /api/items/<id>/bottles`：登记采样瓶，记录采样时刻、封条号和经手人（当前持有人）。
- `GET /api/items/<id>/bottles`、`GET /api/bottles/<id>`：查询采样瓶及其交接、回执记录。
- `POST /api/bottles/<id>/handoffs`：提交交接。交出人必须是当前经手人（前后对上），封条号必须与瓶上一致；断档或封条不符先隔离并保留未完成交接。支持 `expected_version` 乐观锁（冲突时返回最新经手人）和 `idempotency_key`（重试不重复）。
- `POST /api/bottles/<id>/receipts`：实验室回执，仅完整保管链的结果才被认可；同一 `receipt_id` 只记一次（原回执保留）。晚到的脏结果会使已恢复区域立即失效并重算缺口。
- `POST /api/items/<id>/release`：放行，仅协调员/监管角色，需要完整保管链且所有区域达标；现场角色越权放行返回 403。
- `POST /api/bottles/<id>/clear-isolation`：协调员/监管解除隔离并可重新封条。

没有保管记录的旧事件按未采样处理（不能放行），但历史结果仍可查询。测试覆盖完整响应流程、重复事件、重复通知、复检阈值、权限、版本冲突和保管链（登记、交接、断档/封条隔离、结果认可、重复回执、晚到结果失效、越权放行、并发交接、幂等重试、旧事件查询）。内置规则不替代真实水质模型、法定通报渠道或供水控制系统的联锁。
