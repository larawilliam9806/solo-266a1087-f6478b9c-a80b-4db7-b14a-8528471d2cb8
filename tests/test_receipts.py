"""收货磅单：登记/换交、对账视图、发货回执、签收规则与并发占用。

主场景：三批再生料同车送达，客户合开一张收货磅单 T1，且磅单早于发货单录入。

    A(1000) --P1--> B(900) + C(600)          B、C 为整批发货批
    X(500)  --P1
    T1: B 实收 880 克（-20），C 实收 600 克（0）  => 差额 -20 克
"""
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.main import app
    with TestClient(app) as c:
        yield c


def documents(client, docs, expect=201):
    resp = client.post("/api/documents", json={"documents": docs})
    assert resp.status_code == expect, resp.text
    return resp.json() if expect == 201 else resp


def incoming(batch, weight):
    return {"type": "incoming", "batch": batch, "weight": weight}


def shipment(batch, weight):
    return {"type": "shipment", "batch": batch, "weight": weight}


def process(inputs, outputs):
    return {"type": "process",
            "inputs": [{"batch": b, "weight": w} for b, w in inputs],
            "outputs": [{"batch": b, "weight": w} for b, w in outputs]}


def receipt(ticket_no, items, version=None):
    body = {"ticket_no": ticket_no,
            "items": [{"batch": b, "weight": w} for b, w in items]}
    if version is not None:
        body["version"] = version
    return body


def post_receipt(client, ticket_no, items, version=None, expect=201):
    resp = client.post("/api/receipts",
                       json=receipt(ticket_no, items, version))
    assert resp.status_code == expect, resp.text
    return resp.json() if expect == 201 else resp


def get_receipt(client, ticket_no, expect=200):
    resp = client.get(f"/api/receipts/{ticket_no}")
    assert resp.status_code == expect, resp.text
    return resp.json() if expect == 200 else resp


def sign(client, ticket_no, version, expect=200):
    resp = client.post(f"/api/receipts/{ticket_no}/sign",
                       json={"version": version})
    assert resp.status_code == expect, resp.text
    return resp.json() if expect == 200 else resp


PRODUCTION = [
    incoming("A", 1000), incoming("X", 500),
    process([("A", 1000), ("X", 500)], [("B", 900), ("C", 600)]),
]
SHIPMENTS = [shipment("B", 900), shipment("C", 600)]


def shipped(client, items=(("B", 880), ("C", 600))):
    documents(client, PRODUCTION + SHIPMENTS)
    return post_receipt(client, "T1", items)


# --- 登记与对账视图 -------------------------------------------------------

def test_receipt_before_shipments_and_pending_list(client):
    documents(client, PRODUCTION)          # 只有生产，没有发货单
    post_receipt(client, "T1", [("B", 880), ("C", 600)])

    body = get_receipt(client, "T1")
    assert body["ticket_no"] == "T1"
    assert body["version"] == 1 and body["current_version"] == 1
    assert body["signed"] is False
    assert body["pending_shipment_batches"] == ["B", "C"]
    assert body["complete"] is False
    assert body["difference_grams"] is None
    assert {i["batch"]: i["weight"] for i in body["items"]} == {"B": 880, "C": 600}


def test_difference_after_complete_and_shipment_receipt(client):
    shipped(client)                        # B 实收 880，发货 900 => -20
    body = get_receipt(client, "T1")
    assert body["complete"] is True
    assert body["pending_shipment_batches"] == []
    assert body["difference_grams"] == -20
    assert body["difference"] == -20


def test_shipment_receipt_lists_completed_ticket(client):
    documents(client, PRODUCTION)
    post_receipt(client, "T1", [("B", 880), ("C", 600)])

    result = documents(client, [shipment("B", 900)])
    assert result["receipts_completed"] == []      # C 还没发货，未齐单

    result = documents(client, [shipment("C", 600)])
    [done] = result["receipts_completed"]
    assert done["ticket_no"] == "T1"
    assert done["difference_grams"] == -20
    assert get_receipt(client, "T1")["difference_grams"] == -20


def test_shipment_with_disputed_ticket_still_posts(client):
    documents(client, PRODUCTION)
    post_receipt(client, "T1", [("B", 100), ("C", 600)])  # 差额 -800 待争议

    result = documents(client, [shipment("B", 900), shipment("C", 600)])
    assert len(result["shipment_ids"]) == 2                # 照常入账
    [done] = result["receipts_completed"]
    assert done["difference_grams"] == -800
    assert get_receipt(client, "T1")["signed"] is False
    # 超过 500 克，不能签收
    sign(client, "T1", 1, expect=409)


def test_receipt_in_documents_is_atomic_with_other_docs(client):
    # 同一次登记：发货 + 磅单一起生效，回执即列出齐单差额
    documents(client, PRODUCTION)
    result = documents(client, [
        shipment("B", 900), shipment("C", 600),
        {"type": "receipt", "ticket_no": "T2",
         "items": [{"batch": "B", "weight": 910},
                   {"batch": "C", "weight": 600}]},
    ])
    assert result["receipts"][0]["ticket_no"] == "T2"
    assert result["receipts"][0]["version"] == 1
    [done] = result["receipts_completed"]
    assert done["ticket_no"] == "T2" and done["difference_grams"] == 10
    assert get_receipt(client, "T2")["difference_grams"] == 10


def test_atomic_receipt_failure_rolls_back_shipping(client):
    documents(client, PRODUCTION)
    post_receipt(client, "T1", [("B", 900), ("C", 600)])
    # 密码单换交用了过期版号 => 整次（含新发货单）不生效
    resp = documents(client, [
        shipment("B", 900),
        {"type": "receipt", "ticket_no": "T1", "version": 7,
         "items": [{"batch": "B", "weight": 900}]},
    ], expect=409)
    assert "版号" in resp.json()["detail"]
    assert client.get("/api/shipments").json()["shipments"] == []
    assert get_receipt(client, "T1")["pending_shipment_batches"] == ["B", "C"]


# --- 结构性校验 -----------------------------------------------------------

def test_duplicate_batch_in_ticket_rejected(client):
    post_receipt(client, "T1", [("B", 900), ("B", 100)], expect=422)


def test_ticket_requires_items_and_ticket_no(client):
    assert client.post("/api/receipts", json={
        "ticket_no": "T1", "items": []}).status_code == 422
    assert client.post("/api/receipts", json={
        "ticket_no": "  ", "items": [{"batch": "B", "weight": 1}]}).status_code == 422
    assert client.post("/api/receipts", json={
        "ticket_no": "T1",
        "items": [{"batch": "B", "weight": 0}]}).status_code == 422


def test_missing_ticket_is_404(client):
    get_receipt(client, "nope", expect=404)
    sign(client, "nope", 1, expect=404)


# --- 换交 -----------------------------------------------------------------

def test_replace_requires_seen_version(client):
    post_receipt(client, "T1", [("B", 900)])
    assert post_receipt(client, "T1", [("B", 901)], expect=409)
    assert post_receipt(client, "T1", [("B", 901)], version=9, expect=409)
    body = get_receipt(client, "T1")
    assert body["version"] == 1
    assert body["items"][0]["weight"] == 900

    result = post_receipt(client, "T1", [("B", 910), ("C", 600)], version=1)
    assert result["version"] == 2 and result["replaced"] is True
    body = get_receipt(client, "T1")
    assert body["version"] == 2
    assert {i["batch"]: i["weight"] for i in body["items"]} == {"B": 910, "C": 600}

    # 旧版号再次换交失败，不改变内容
    post_receipt(client, "T1", [("B", 1)], version=1, expect=409)
    assert get_receipt(client, "T1")["version"] == 2


def test_signed_ticket_is_frozen(client):
    shipped(client)
    sign(client, "T1", 1)
    post_receipt(client, "T1", [("B", 900)], version=1, expect=409)
    post_receipt(client, "T1", [("B", 900)], version=2, expect=409)
    assert get_receipt(client, "T1")["version"] == 1


# --- 签收规则 -------------------------------------------------------------

def test_sign_requires_current_version(client):
    shipped(client)
    sign(client, "T1", 2, expect=409)
    assert get_receipt(client, "T1")["signed"] is False
    sign(client, "T1", 1)
    body = get_receipt(client, "T1")
    assert body["signed"] is True
    assert body["signed_version"] == 1


def test_sign_requires_all_shipments(client):
    documents(client, PRODUCTION)
    post_receipt(client, "T1", [("B", 900), ("C", 600)])
    documents(client, [shipment("B", 900)])
    resp = sign(client, "T1", 1, expect=409)
    assert "C" in resp.json()["detail"]
    assert get_receipt(client, "T1")["items"][1]["signed_by"] is None


def test_sign_difference_boundary(client):
    documents(client, PRODUCTION + [shipment("B", 900)])
    post_receipt(client, "T1", [("B", 1400)])       # +500，恰好允许
    sign(client, "T1", 1)

    post_receipt(client, "T2", [("B", 1401)])       # +501，超限
    resp = sign(client, "T2", 1, expect=409)
    assert "500" in resp.json()["detail"]

    post_receipt(client, "T3", [("B", 399)])        # -501，超限
    assert sign(client, "T3", 1, expect=409).status_code == 409


def test_failed_sign_does_not_claim_batches(client):
    documents(client, PRODUCTION + [shipment("B", 900)])
    post_receipt(client, "T1", [("B", 900), ("C", 600)])
    post_receipt(client, "T2", [("B", 900)])

    sign(client, "T1", 1, expect=409)               # C 未发货，失败不占用 B
    assert get_receipt(client, "T1")["signed"] is False

    documents(client, [shipment("C", 600)])
    # B 未被失败的签收占用，另一张只含 B 的合规磅单仍可签收
    sign(client, "T2", 1)
    assert get_receipt(client, "T2")["items"][0]["signed_by"] == "T2"


# --- 重报同批 -------------------------------------------------------------

def test_other_tickets_reporting_same_batch(client):
    documents(client, PRODUCTION + [shipment("B", 900), shipment("C", 600)])
    post_receipt(client, "T1", [("B", 900)])
    post_receipt(client, "T2", [("B", 880), ("C", 600)])

    other = get_receipt(client, "T1")["other_tickets"]
    assert [t["ticket_no"] for t in other] == ["T2"]
    assert other[0]["signed"] is False
    assert other[0]["batches"] == [{"batch": "B", "weight": 880, "signed_by": None}]

    sign(client, "T2", 1)
    other = get_receipt(client, "T1")["other_tickets"]
    assert other[0]["signed"] is True
    assert other[0]["signed_version"] == 1
    assert other[0]["batches"][0]["signed_by"] == "T2"
    # T1 的 B 已被 T2 占用，不能整单签收（失败也不占用 C）
    resp = sign(client, "T1", 1, expect=409)
    assert "B" in resp.json()["detail"]
    assert get_receipt(client, "T1")["signed"] is False


def test_same_ticket_cannot_claim_twice(client):
    documents(client, PRODUCTION + [shipment("B", 900), shipment("C", 600)])
    post_receipt(client, "T1", [("B", 900), ("C", 600)])
    sign(client, "T1", 1)
    assert sign(client, "T1", 1, expect=409).status_code == 409
    assert get_receipt(client, "T1")["signed_version"] == 1


# --- 并发 -----------------------------------------------------------------

def _seed_concurrent_tickets(db_path):
    """在同一个测试库上放好两张只含 B 的合规磅单。"""
    from app.db import connect
    from app.receipts import register_receipt

    documents = _make_client(db_path)
    documents(documents, PRODUCTION + [shipment("B", 900)])
    for ticket, weight in (("C1", 900), ("C2", 899)):
        conn = connect()
        try:
            register_receipt(conn, ticket, None,
                             [{"batch": "B", "weight": weight}])
        finally:
            conn.close()


def _make_client(db_path):
    from app.main import app
    client = TestClient(app)
    client.__enter__()
    return client


def test_concurrent_sign_of_same_batch_only_one_wins(tmp_path, monkeypatch):
    """两个线程各用独立连接同时签收含同一批号的两张磅单：只能签成一张。"""
    db_path = str(tmp_path / "concurrent.db")
    monkeypatch.setenv("DB_PATH", db_path)
    from app.db import connect, init_db
    from app.receipts import ReceiptError, register_receipt, sign_receipt

    init_db()
    conn = connect()
    try:
        conn.execute("INSERT INTO batches(batch_no, weight) VALUES ('B', 900)")
        conn.execute("INSERT INTO shipments(batch_no) VALUES ('B')")
        conn.commit()
        for ticket, weight in (("C1", 900), ("C2", 899)):
            register_receipt(conn, ticket, None, [{"batch": "B", "weight": weight}])
    finally:
        conn.close()

    barrier = threading.Barrier(2)
    results = []

    def worker(ticket):
        conn = connect()
        try:
            barrier.wait(timeout=5)
            results.append(("ok", ticket, sign_receipt(conn, ticket, 1)))
        except ReceiptError as exc:
            results.append(("err", ticket, exc.detail))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("C1", "C2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(results) == 2, results
    ok = [r for r in results if r[0] == "ok"]
    assert len(ok) == 1, results

    conn = connect()
    try:
        claims = conn.execute(
            "SELECT batch_no, ticket_no FROM receipt_claims").fetchall()
        assert len(claims) == 1 and claims[0]["batch_no"] == "B"
        assert claims[0]["ticket_no"] == ok[0][1]
        signed = conn.execute(
            "SELECT ticket_no FROM receipts WHERE signed_version IS NOT NULL"
        ).fetchall()
        assert [r["ticket_no"] for r in signed] == [ok[0][1]]
    finally:
        conn.close()


def test_concurrent_read_sees_single_snapshot(tmp_path, monkeypatch):
    """对账查询期间并发换交磅单：查询结果不得出现跨账面状态拼接。

    读到版号 1 就必须读到版号 1 的明细；读到版号 2 就必须读到版号 2 的明细。
    """
    db_path = str(tmp_path / "snapshot.db")
    monkeypatch.setenv("DB_PATH", db_path)
    from app.db import connect, init_db
    from app.receipts import reconciliation, register_receipt

    init_db()
    conn = connect()
    try:
        conn.execute("INSERT INTO batches(batch_no, weight) VALUES ('B', 900)")
        conn.execute("INSERT INTO shipments(batch_no) VALUES ('B')")
        conn.commit()
        register_receipt(conn, "T1", None,
                         [{"batch": "B", "weight": 900}, {"batch": "C", "weight": 1}])
    finally:
        conn.close()

    seen = []
    stop = threading.Event()

    def reader():
        read_conn = connect()
        try:
            while not stop.is_set():
                read_conn.execute("BEGIN")
                body = reconciliation(read_conn, "T1")
                read_conn.rollback()
                seen.append((body["version"], {i["batch"]: i["weight"]
                                               for i in body["items"]}))
        finally:
            read_conn.close()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        write_conn = connect()
        try:
            for version in range(1, 120):
                register_receipt(write_conn, "T1", version,
                                 [{"batch": "B", "weight": 900 + version},
                                  {"batch": "C", "weight": 1}])
        finally:
            write_conn.close()
    finally:
        stop.set()
        thread.join(timeout=15)

    assert seen, "读取线程没有采到样本"
    for version, items in seen:
        # 版号必须与同一次查询看到的明细属于同一账面状态
        if version == 1:
            assert items == {"B": 900, "C": 1}, (version, items)
        else:
            assert items == {"B": 900 + version - 1, "C": 1}, (version, items)
