# 再生塑料发货追溯 API

再生塑料混炼后分装、部分分装料回炉的生产场景下，用 **FastAPI + SQLite** 实现的
发货追溯服务：登记进料批、工序投入/产出批与整批发货；质检时输入不合格进料批号，
查出各已发批可能含该进料的**最少/最多克数**，两个极值各附一份符合整条链称重记录的
克数分配，供逐道核对。

## 启动

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，交互文档见 http://localhost:8000/docs
```

SQLite 数据文件落在命名卷 `trace-data`（容器内 `/data/app.db`，可用 `DB_PATH` 改）。
本地开发：`pip install -r requirements-dev.txt && uvicorn app.main:app --reload`，
测试：`python -m pytest tests/`。

## 概念与规则

- **批号**是追溯的基本单位，重量一律为**正整数克**。
- 每个批号**至多一个来源**（进料登记，或某道工序的产出）和**至多一个去向**
  （投入某道工序，或整批发货）。投料按整批记账。
- **来源可以晚交**：工序单、发货单可以先登记，批号的来源单（进料或上游工序）后补。
- 三类单据一次可提交多张，**整次原子生效**：
  - 同一批号在各单上的重量必须一致；
  - 每道工序投入总重必须等于产出总重；
  - 批号/重量冲突、来源或去向重复、形成循环、保存失败——任一发生，整次登记不生效。
- **收货磅单**按「单号 + 各整批批号与实收净重」记录到货，可**早于发货单**录入；
  单内批号不得重复，同一批号可出现在多张磅单上（重报同批）。
  - **换交**：未签收前凭**所见版号**整张替换（版号加 1）。
  - **对账**：显示当前版号、待补发货的批号、重报同批的其他磅单及签收状态；
    单内批号全部已发货（**齐单**）后给出 **实收净重合计 − 发货称重合计** 的差额。
  - **发货回执**：`POST /api/documents` 的回执列出本次因补上发货单而齐单的磅单及
    差额；磅单有差额或争议时，发货单照常入账。
  - **签收**：须核对所见版号、单内发货单齐全、**绝对差额不超过 500 克**、且
    整单各批均未被签收；通过后磅单内容固定。同批被两张合规磅单并发签收时
    只能签成一张；签收失败不占用任何批号。
  - 对账查询的磅单、称重与签收状态取自**同一账面快照**，查询期间并发交发货单
    或换交磅单不会读到拼接自不同状态的字段。

## 接口

### `POST /api/documents` — 登记（原子）

```json
{
  "documents": [
    {"type": "incoming", "batch": "A", "weight": 1000},
    {"type": "incoming", "batch": "X", "weight": 500},
    {"type": "process",
     "inputs":  [{"batch": "A", "weight": 1000}, {"batch": "X", "weight": 500}],
     "outputs": [{"batch": "B", "weight": 900},  {"batch": "C", "weight": 600}]},
    {"type": "process",
     "inputs":  [{"batch": "C", "weight": 600}],
     "outputs": [{"batch": "S1", "weight": 400}, {"batch": "R", "weight": 200}]},
    {"type": "shipment", "batch": "B", "weight": 900},
    {"type": "shipment", "batch": "S1", "weight": 400}
  ]
}
```

- `201`：全部落库，返回新建的工序/发货 id 等。
- `409`：批号、重量冲突，来源/去向重复，或形成循环（整次不生效）。
- `422`：单据结构非法（重量非正整数、工序投入≠产出、批号重复等，整次不生效）。

收货磅单也可以混在同一次登记里（`"type": "receipt"`，见下），与发货单一起原子生效。
回执里的 `receipts_completed` 列出本次因补上发货单而齐单的磅单及差额：

```json
{
  "applied": true, "shipment_ids": [7], "...": "...",
  "receipts_completed": [
    {"ticket_no": "T1", "version": 1,
     "received_net_grams_total": 1480, "shipped_grams_total": 1500,
     "difference_grams": -20}
  ]
}
```

### `POST /api/receipts` — 登记 / 换交收货磅单

```json
{
  "ticket_no": "T1",
  "version": 1,
  "items": [{"batch": "B", "weight": 880}, {"batch": "C", "weight": 600}]
}
```

- 新建时省略 `version`（或给 `1`）；对已有磅单换交时 `version` 为**所见版号**，
  内容整张替换、版号加 1。版号不符返回 `409`，不会改动磅单。
- 磅单可早于发货单录入，批号不必已在账簿中。
- 单内批号重复或重量非正整数返回 `422`；已签收后再换交返回 `409`。
- `201`：`{"ticket_no": "T1", "version": 2, "created": false, "replaced": true}`。

### `GET /api/receipts/{ticket_no}` — 收货对账

```json
{
  "ticket_no": "T1", "version": 2, "current_version": 2,
  "signed": false, "signed_version": null, "signed_at": null,
  "items": [
    {"batch": "B", "weight": 880, "shipped": true,
     "shipped_grams": 900, "signed_by": null},
    {"batch": "C", "weight": 600, "shipped": false,
     "shipped_grams": null, "signed_by": null}
  ],
  "received_net_grams_total": 1480, "shipped_grams_total": 900,
  "pending_shipment_batches": ["C"], "complete": false,
  "difference_grams": null,
  "other_tickets": [
    {"ticket_no": "T2", "version": 1, "signed": true, "signed_version": 1,
     "batches": [{"batch": "B", "weight": 880, "signed_by": "T2"}]}
  ]
}
```

- `pending_shipment_batches`：单内尚未登记发货的批号。
- `difference_grams`：**齐单后**为实收净重合计减发货称重合计，未齐单为 `null`。
- `other_tickets`：重报同批的其他磅单及其签收状态。
- 整个响应在一个只读事务里读取，字段来自同一账面快照。`404`：磅单不存在。

### `POST /api/receipts/{ticket_no}/sign` — 签收

```json
{"version": 2}
```

同时满足以下条件才签收，否则 `409` 且**不占用任何批号**：

- 所见版号 = 当前版号；
- 单内批号全部已登记发货（齐单）；
- `|difference_grams| <= 500`；
- 单内任何批号都未被其他磅单签收（并发签收同一批也只能签成一张）。

成功后 `200` 返回 `signed_version`、`signed_at`、`batches_claimed` 与差额，磅单内容
固定（不能再换交）。`404`：磅单不存在。

### `GET /api/trace/{batch_no}` — 质检查询

输入**不合格进料批号**，返回**每个已发批**可能含该进料的克数下界与上界：

```json
{
  "defective_batch": "A",
  "defective_weight": 1000,
  "shipments": [
    {"batch": "B", "status": "ok", "min_grams": 400, "max_grams": 900,
     "min_witness": {"batch_grams": {"A": 1000, "B": 400, "C": 600, "S1": 400, "R": 200},
                     "processes": [{"process_id": 1,
                                    "input_grams":  {"A": 1000, "X": 0},
                                    "output_grams": {"B": 400, "C": 600}},
                                   {"process_id": 2,
                                    "input_grams":  {"C": 600},
                                    "output_grams": {"S1": 400, "R": 200}}]},
     "max_witness": {...}},
    {"batch": "S2", "status": "pending", "missing_batches": ["Y"]}
  ]
}
```

- **上下界**：在符合全部称重记录（批重上限、工序投入=产出守恒、该进料批全部克数
  进入链路）的前提下，该已发批含该进料的最少/最多克数。用最小费用流在
  "批号容量 = 称重克数"的守恒网络上求得，结果为整数克。
- **克数分配（witness）**：每个极值附一份达到该极值的分配——`batch_grams` 给出
  链路内每个批号含该料的克数，`processes` 按工序列出投入/产出克数，可逐道核对：
  每道工序投入克数之和 = 产出克数之和，且每个批号不超过其称重。
- **待补批号（pending）**：已发批的上游有缺单（批号既非进料也非任何工序产出）时，
  该批不给出上下界，返回待补批号；其他已发批照常给出上下界。补齐来源单后再次
  查询即可得到结果。
- `404`：批号不存在；`400`：批号不是进料批。

### 辅助查询

- `GET /api/batches` / `GET /api/batches/{batch_no}`：批号列表 / 单个批号的重量、
  来源与去向（`missing` 表示来源缺单）。
- `GET /api/processes/{id}`：工序的投入/产出批及称重。
- `GET /api/shipments`：已发批列表。
- `GET /api/health`：健康检查。

（`/api/weigh-tickets` 是 `/api/receipts` 的等价别名；`/api/documents` 也接受
`"type": "weigh_ticket"` 的磅单。）

## 典型流程

```bash
# 1. 登记进料、混炼分装、回炉、整批发货（可一次提交，也可分次；来源可晚交）
curl -X POST localhost:8000/api/documents -H 'Content-Type: application/json' \
  -d '{"documents":[...]}'

# 2. 质检发现进料批 A 不合格，查各已发批的可能含量
curl localhost:8000/api/trace/A

# 3. 若某已发批返回 pending，按 missing_batches 补登来源单后重新查询
curl -X POST localhost:8000/api/documents -H 'Content-Type: application/json' \
  -d '{"documents":[{"type":"incoming","batch":"Y","weight":50}]}'
curl localhost:8000/api/trace/A

# 4. 同车送达的磅单可以先于发货单录入
curl -X POST localhost:8000/api/receipts -H 'Content-Type: application/json' \
  -d '{"ticket_no":"T1","items":[{"batch":"B","weight":880},{"batch":"C","weight":600}]}'

# 5. 补登发货单；回执列出本次齐单的磅单与差额
curl -X POST localhost:8000/api/documents -H 'Content-Type: application/json' \
  -d '{"documents":[{"type":"shipment","batch":"B","weight":900},
                    {"type":"shipment","batch":"C","weight":600}]}'

# 6. 对账：待补发货批号（应为空）、差额 -20 克
curl localhost:8000/api/receipts/T1

# 7. 齐单、|差额| <= 500 克且各批未被签收时，按所见版号签收
curl -X POST localhost:8000/api/receipts/T1/sign \
  -H 'Content-Type: application/json' -d '{"version":1}'
```

## 目录结构

```
app/
  main.py      路由
  models.py    单据结构校验（正整数克、工序平衡、磅单内批号不重复等）
  registry.py  登记：一致性/唯一性/循环校验 + 单事务落库（含磅单与发货回执）
  receipts.py  收货磅单：登记/换交、对账视图、签收与批号占用
  trace.py     追溯：影响面展开、缺单检查、上下界与分配单
  flow.py      最小费用流（SPFA 增广）
  db.py        SQLite 连接与建表
  state.py     账簿状态载入
tests/test_api.py       原有追溯测试
tests/test_receipts.py  收货对账、换交、签收与并发测试
Dockerfile / docker-compose.yml
```
