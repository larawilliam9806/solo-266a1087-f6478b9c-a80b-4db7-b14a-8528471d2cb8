"""再生塑料发货追溯 API。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from .db import connect, init_db
from .models import ReceiptRequest, RegisterRequest, SignRequest
from .receipts import ReceiptError, reconciliation, register_receipt, sign_receipt
from .registry import ConflictError, register_documents
from .state import load_state
from .trace import build_trace


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(
    title="再生塑料发货追溯 API",
    version="1.1.0",
    description="登记进料批、工序投入产出批与整批发货；按不合格进料批追溯各已发批"
                "的可能含量；收货磅单对账、换交与签收。",
    lifespan=lifespan,
)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/documents", status_code=201)
def post_documents(req: RegisterRequest):
    """登记单据（进料/工序/发货/收货磅单）。同一请求内多张单据原子生效：
    批号或重量冲突、形成循环、保存失败时整次不生效。

    发货登记的回执（`receipts_completed`）列出本次因补齐发货单而齐单的磅单及差额。
    """
    try:
        return register_documents(req.documents)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ReceiptError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


def _reconciliation(ticket_no: str) -> dict:
    """在一个只读事务内读取整张磅单的对账视图。

    使用 BEGIN 显式开启读事务，SQLite 在 WAL 下为它固定一个快照：本次查询里的
    磅单、批号称重、发货与签收状态都来自同一账面状态，期间并发提交的发货单或
    换交磅单不会造成字段来自不同状态。
    """
    conn = connect()
    try:
        conn.execute("BEGIN")
        return reconciliation(conn, ticket_no)
    finally:
        conn.rollback()
        conn.close()

@app.post("/api/receipts", status_code=201)
@app.post("/api/weigh-tickets", status_code=201, include_in_schema=False)
def post_receipt(req: ReceiptRequest):
    """登记收货磅单，或凭所见版号整张换交（版号加 1）；签收后内容冻结。"""
    conn = connect()
    try:
        return register_receipt(
            conn, req.ticket_no, req.version,
            [{"batch": i.batch, "weight": i.weight} for i in req.items])
    except ReceiptError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    finally:
        conn.close()


@app.get("/api/receipts/{ticket_no}")
@app.get("/api/weigh-tickets/{ticket_no}", include_in_schema=False)
def get_receipt(ticket_no: str):
    """收货对账：当前版号、各批实收净重、待补发货批号、重报同批的其他磅单
    及签收状态；齐单后给出实收净重合计减发货称重合计的差额。"""
    try:
        return _reconciliation(ticket_no)
    except ReceiptError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@app.post("/api/receipts/{ticket_no}/sign")
@app.post("/api/weigh-tickets/{ticket_no}/sign", include_in_schema=False)
def sign_receipt_route(ticket_no: str, req: SignRequest = None):
    """签收：核对所见版号、发货单齐全、绝对差额不超过 500 克、单内各批均未
    被签收；通过后固定磅单内容。失败时本次不占用任何批号。"""
    conn = connect()
    try:
        return sign_receipt(conn, ticket_no, req.version if req else None)
    except ReceiptError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    finally:
        conn.close()


@app.get("/api/trace/{batch_no}")
def get_trace(batch_no: str):
    """质检查询：输入不合格进料批号，返回各已发批可能含该料的
    最少/最多克数（各附一份符合整条链称重记录的克数分配）；
    上游有缺单的已发批返回待补批号。"""
    conn = connect()
    try:
        state = load_state(conn)
    finally:
        conn.close()
    if batch_no not in state.weights:
        raise HTTPException(status_code=404, detail=f"批号 {batch_no} 不存在")
    if batch_no not in state.incoming:
        raise HTTPException(status_code=400, detail=f"批号 {batch_no} 不是进料批")
    return {
        "defective_batch": batch_no,
        "defective_weight": state.weights[batch_no],
        "shipments": build_trace(state, batch_no),
    }


@app.get("/api/batches")
def list_batches():
    conn = connect()
    try:
        state = load_state(conn)
    finally:
        conn.close()
    return {"batches": [{"batch_no": b, "weight": w}
                        for b, w in sorted(state.weights.items())]}


@app.get("/api/batches/{batch_no}")
def get_batch(batch_no: str):
    conn = connect()
    try:
        state = load_state(conn)
    finally:
        conn.close()
    if batch_no not in state.weights:
        raise HTTPException(status_code=404, detail=f"批号 {batch_no} 不存在")
    src_proc = state.source_process()
    dest_proc = state.dest_process()
    if batch_no in state.incoming:
        source = {"type": "incoming"}
    elif batch_no in src_proc:
        source = {"type": "process", "process_id": src_proc[batch_no]}
    else:
        source = {"type": "missing"}
    if batch_no in dest_proc:
        destination = {"type": "process", "process_id": dest_proc[batch_no]}
    elif batch_no in state.shipments:
        destination = {"type": "shipment", "shipment_id": state.shipments[batch_no]}
    else:
        destination = {"type": "none"}
    return {
        "batch_no": batch_no,
        "weight": state.weights[batch_no],
        "source": source,
        "destination": destination,
    }


@app.get("/api/processes/{process_id}")
def get_process(process_id: int):
    conn = connect()
    try:
        state = load_state(conn)
    finally:
        conn.close()
    proc = state.processes.get(process_id)
    if proc is None:
        raise HTTPException(status_code=404, detail=f"工序 {process_id} 不存在")
    return {
        "process_id": process_id,
        "inputs": [{"batch_no": b, "weight": state.weights[b]} for b in proc["inputs"]],
        "outputs": [{"batch_no": b, "weight": state.weights[b]} for b in proc["outputs"]],
    }


@app.get("/api/shipments")
def list_shipments():
    conn = connect()
    try:
        state = load_state(conn)
    finally:
        conn.close()
    return {"shipments": [{"shipment_id": sid, "batch_no": b, "weight": state.weights[b]}
                          for b, sid in sorted(state.shipments.items())]}
