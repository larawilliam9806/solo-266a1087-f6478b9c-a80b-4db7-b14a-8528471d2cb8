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
```

## 目录结构

```
app/
  main.py      路由
  models.py    单据结构校验（正整数克、工序平衡等）
  registry.py  登记：一致性/唯一性/循环校验 + 单事务落库
  trace.py     追溯：影响面展开、缺单检查、上下界与分配单
  flow.py      最小费用流（SPFA 增广）
  db.py        SQLite 连接与建表
  state.py     账簿状态载入
tests/test_api.py
Dockerfile / docker-compose.yml
```
