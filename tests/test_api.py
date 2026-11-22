"""API 端到端测试：登记、冲突回滚、循环拒绝、缺单待补、上下界与分配单核对。"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.main import app
    with TestClient(app) as c:
        yield c


def register(client, docs, expect=201):
    resp = client.post("/api/documents", json={"documents": docs})
    assert resp.status_code == expect, resp.text
    return resp.json() if expect == 201 else resp


def incoming(batch, weight):
    return {"type": "incoming", "batch": batch, "weight": weight}


def process(inputs, outputs):
    return {"type": "process",
            "inputs": [{"batch": b, "weight": w} for b, w in inputs],
            "outputs": [{"batch": b, "weight": w} for b, w in outputs]}


def shipment(batch, weight):
    return {"type": "shipment", "batch": batch, "weight": weight}


def trace(client, batch, expect=200):
    resp = client.get(f"/api/trace/{batch}")
    assert resp.status_code == expect, resp.text
    return resp.json() if expect == 200 else resp


def by_batch(shipments):
    return {s["batch"]: s for s in shipments}


def check_witness(witness, weights, defective, defective_weight, target, extreme):
    """分配单必须：总量出自不合格批、不超各批称重、逐工序守恒、目标批等于极值。"""
    bg = witness["batch_grams"]
    assert bg[defective] == defective_weight
    for b, g in bg.items():
        assert 0 <= g <= weights[b], (b, g)
    assert bg.get(target, 0) == extreme
    for p in witness["processes"]:
        assert sum(p["input_grams"].values()) == sum(p["output_grams"].values())
        for b, g in list(p["input_grams"].items()) + list(p["output_grams"].items()):
            assert 0 <= g <= weights[b], (b, g)


# 混炼分装 + 回炉场景：
#   A(1000,不合格) + X(500) --P1--> B(900) + C(600)
#   C(600) --P2--> S1(400) + R(200)
#   R(200) + Y(300) --P3--> S2(500)     （R 回炉）
#   发货: B, S1, S2
MAIN_DOCS = [
    incoming("A", 1000), incoming("X", 500), incoming("Y", 300),
    process([("A", 1000), ("X", 500)], [("B", 900), ("C", 600)]),
    process([("C", 600)], [("S1", 400), ("R", 200)]),
    process([("R", 200), ("Y", 300)], [("S2", 500)]),
    shipment("B", 900), shipment("S1", 400), shipment("S2", 500),
]
MAIN_WEIGHTS = {"A": 1000, "X": 500, "Y": 300, "B": 900, "C": 600,
                "S1": 400, "R": 200, "S2": 500}


def test_trace_bounds_and_witnesses(client):
    register(client, MAIN_DOCS)
    body = trace(client, "A")
    assert body["defective_weight"] == 1000
    got = by_batch(body["shipments"])
    assert set(got) == {"B", "S1", "S2"}
    assert (got["B"]["min_grams"], got["B"]["max_grams"]) == (400, 900)
    assert (got["S1"]["min_grams"], got["S1"]["max_grams"]) == (0, 400)
    assert (got["S2"]["min_grams"], got["S2"]["max_grams"]) == (0, 200)
    for batch, entry in got.items():
        assert entry["status"] == "ok"
        check_witness(entry["min_witness"], MAIN_WEIGHTS, "A", 1000,
                      batch, entry["min_grams"])
        check_witness(entry["max_witness"], MAIN_WEIGHTS, "A", 1000,
                      batch, entry["max_grams"])


def test_trace_other_incoming(client):
    register(client, MAIN_DOCS)
    got = by_batch(trace(client, "X")["shipments"])
    assert (got["B"]["min_grams"], got["B"]["max_grams"]) == (0, 500)
    assert (got["S1"]["min_grams"], got["S1"]["max_grams"]) == (0, 400)
    assert (got["S2"]["min_grams"], got["S2"]["max_grams"]) == (0, 200)
    # Y 只经 P3 流向 S2，且 S2 是其唯一出口：300 克必然全部落在 S2
    got_y = by_batch(trace(client, "Y")["shipments"])
    assert (got_y["B"]["min_grams"], got_y["B"]["max_grams"]) == (0, 0)
    assert (got_y["S1"]["min_grams"], got_y["S1"]["max_grams"]) == (0, 0)
    assert (got_y["S2"]["min_grams"], got_y["S2"]["max_grams"]) == (300, 300)


def test_late_source_pending_then_resolved(client):
    register(client, [
        incoming("D", 100), incoming("E", 70),
        process([("D", 100)], [("M", 100)]),
        process([("M", 100), ("Y", 50)], [("S", 150)]),  # Y 来源缺单
        shipment("S", 150), shipment("E", 70),
    ])
    got = by_batch(trace(client, "D")["shipments"])
    assert got["S"] == {"batch": "S", "status": "pending", "missing_batches": ["Y"]}
    # 其他已发批照常给出上下界
    assert got["E"]["status"] == "ok"
    assert (got["E"]["min_grams"], got["E"]["max_grams"]) == (0, 0)
    # 补齐来源后即可查到结果
    register(client, [incoming("Y", 50)])
    got = by_batch(trace(client, "D")["shipments"])
    assert got["S"]["status"] == "ok"
    assert (got["S"]["min_grams"], got["S"]["max_grams"]) == (100, 100)
    check_witness(got["S"]["min_witness"],
                  {"D": 100, "M": 100, "Y": 50, "S": 150, "E": 70},
                  "D", 100, "S", 100)


def test_pending_self_when_shipped_without_source(client):
    register(client, [incoming("D", 10), shipment("Z", 5)])  # Z 无任何来源单
    got = by_batch(trace(client, "D")["shipments"])
    assert got["Z"] == {"batch": "Z", "status": "pending", "missing_batches": ["Z"]}


def test_weight_conflict_rolls_back(client):
    register(client, [incoming("A", 100)])
    resp = register(client, [process([("A", 150)], [("B", 150)])], expect=409)
    assert "重量冲突" in resp.json()["detail"]
    batch = client.get("/api/batches/A").json()
    assert batch["weight"] == 100
    assert batch["destination"] == {"type": "none"}
    assert client.get("/api/batches/B").status_code == 404


def test_cycle_rejected(client):
    resp = register(client, [
        process([("A", 100)], [("B", 100)]),
        process([("B", 100)], [("C", 100)]),
        process([("C", 100)], [("A", 100)]),  # A<-P3, 形成 A->P1->B->P2->C->P3->A
    ], expect=409)
    assert "循环" in resp.json()["detail"]
    assert client.get("/api/batches/B").status_code == 404


def test_unbalanced_process_rejected(client):
    resp = register(client, [process([("A", 100)], [("B", 90)])], expect=422)
    assert "不等于" in str(resp.json())


def test_process_structure_rejected(client):
    assert register(client, [process([("A", 50), ("A", 50)], [("B", 100)])],
                    expect=422).status_code == 422  # 投入重复
    assert register(client, [process([("A", 100)], [("A", 100)])],
                    expect=422).status_code == 422  # 投入产出重叠
    assert register(client, [{"type": "incoming", "batch": "A", "weight": 0}],
                    expect=422).status_code == 422  # 重量非正整数
    assert register(client, [{"type": "incoming", "batch": "A", "weight": 1.5}],
                    expect=422).status_code == 422


def test_duplicate_source_and_destination(client):
    register(client, [incoming("A", 100), process([("A", 100)], [("B", 100)]),
                      shipment("B", 100)])
    assert register(client, [incoming("A", 100)], expect=409).status_code == 409
    assert register(client, [process([("X", 1)], [("B", 1)])],
                    expect=409).status_code == 409   # B 已有来源
    assert register(client, [process([("B", 100)], [("C", 100)])],
                    expect=409).status_code == 409   # B 已有去向
    assert register(client, [shipment("B", 100)], expect=409).status_code == 409


def test_atomicity_across_documents(client):
    register(client, [shipment("K", 5)])
    resp = register(client, [incoming("N", 10), shipment("K", 5)], expect=409)
    assert "已有去向" in resp.json()["detail"]
    assert client.get("/api/batches/N").status_code == 404  # 整次不生效


def test_same_request_late_source_and_shipment(client):
    # 同一请求内：工序先用料、进料后补、产出即发货
    result = register(client, [
        process([("A", 100)], [("B", 60), ("C", 40)]),
        incoming("A", 100),
        shipment("B", 60),
    ])
    assert result["batches_created"] == ["A", "B", "C"]
    got = by_batch(trace(client, "A")["shipments"])
    assert (got["B"]["min_grams"], got["B"]["max_grams"]) == (60, 60)


def test_trace_query_errors(client):
    register(client, [process([("A", 100)], [("B", 100)])])
    assert trace(client, "ZZ", expect=404).status_code == 404
    assert trace(client, "B", expect=400).status_code == 400  # B 不是进料批


def test_direct_shipment_of_incoming(client):
    register(client, [incoming("A", 100), shipment("A", 100)])
    got = by_batch(trace(client, "A")["shipments"])
    assert (got["A"]["min_grams"], got["A"]["max_grams"]) == (100, 100)


def test_batch_and_process_endpoints(client):
    ids = register(client, MAIN_DOCS)
    b = client.get("/api/batches/C").json()
    assert b["weight"] == 600
    assert b["source"] == {"type": "process", "process_id": ids["process_ids"][0]}
    assert b["destination"] == {"type": "process", "process_id": ids["process_ids"][1]}
    p = client.get(f"/api/processes/{ids['process_ids'][0]}").json()
    assert {x["batch_no"] for x in p["inputs"]} == {"A", "X"}
    assert {x["batch_no"] for x in p["outputs"]} == {"B", "C"}
    assert sum(x["weight"] for x in p["inputs"]) == sum(x["weight"] for x in p["outputs"])
    ships = client.get("/api/shipments").json()["shipments"]
    assert {s["batch_no"] for s in ships} == {"B", "S1", "S2"}
