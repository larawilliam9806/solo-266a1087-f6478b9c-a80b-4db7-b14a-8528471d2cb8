"""登记：校验（重量一致、来源/去向唯一、无循环）并在单个事务内落库。

任何冲突、循环或保存失败都会回滚，整次登记不生效。
"""
from collections import deque

from .db import connect
from .state import load_state


class ConflictError(Exception):
    """登记与已有账簿冲突（批号、重量、来源/去向唯一性、循环）。"""


def _ensure_acyclic(processes: dict) -> None:
    """processes: pid -> {"inputs": [...], "outputs": [...]}（含本次新增）。

    在 批号<->工序 二部图上做拓扑排序，发现环即拒绝。
    """
    indeg, adj = {}, {}

    def node(k):
        indeg.setdefault(k, 0)
        adj.setdefault(k, [])

    for pid, p in processes.items():
        pn = ("process", pid)
        node(pn)
        for b in p["inputs"]:
            bn = ("batch", b)
            node(bn)
            adj[bn].append(pn)
            indeg[pn] += 1
        for b in p["outputs"]:
            bn = ("batch", b)
            node(bn)
            adj[pn].append(bn)
            indeg[bn] += 1
    queue = deque(k for k, d in indeg.items() if d == 0)
    seen = 0
    while queue:
        u = queue.popleft()
        seen += 1
        for v in adj[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
    if seen != len(indeg):
        raise ConflictError("登记会形成循环（批号经工序首尾相接），整次登记不生效")


def register_documents(documents) -> dict:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = load_state(conn)

        weights = dict(state.weights)
        sources = {}                       # batch -> ("incoming", None) | ("process", pid)
        for b in state.incoming:
            sources[b] = ("incoming", None)
        for pid, p in state.processes.items():
            for b in p["outputs"]:
                sources[b] = ("process", pid)
        destinations = {}                  # batch -> ("process", pid) | ("shipment", sid)
        for pid, p in state.processes.items():
            for b in p["inputs"]:
                destinations[b] = ("process", pid)
        for b, sid in state.shipments.items():
            destinations[b] = ("shipment", sid)

        new_batches, new_incoming, new_shipments, new_processes = {}, [], [], []

        def note_weight(batch, weight):
            cur = weights.get(batch)
            if cur is not None:
                if cur != weight:
                    raise ConflictError(
                        f"批号 {batch} 的重量冲突：已登记 {cur} 克，本次为 {weight} 克")
            else:
                weights[batch] = weight
                new_batches[batch] = weight

        for doc in documents:
            if doc.type == "incoming":
                note_weight(doc.batch, doc.weight)
                if doc.batch in sources:
                    raise ConflictError(f"批号 {doc.batch} 已有来源，不能重复登记进料")
                sources[doc.batch] = ("incoming", None)
                new_incoming.append(doc.batch)
            elif doc.type == "shipment":
                note_weight(doc.batch, doc.weight)
                if doc.batch in destinations:
                    raise ConflictError(f"批号 {doc.batch} 已有去向，不能重复发货")
                destinations[doc.batch] = ("shipment", None)
                new_shipments.append(doc.batch)
            else:  # process
                for item in doc.inputs:
                    note_weight(item.batch, item.weight)
                for item in doc.outputs:
                    note_weight(item.batch, item.weight)
                for item in doc.inputs:
                    if item.batch in destinations:
                        raise ConflictError(f"批号 {item.batch} 已有去向，不能再次投料")
                for item in doc.outputs:
                    if item.batch in sources:
                        raise ConflictError(f"批号 {item.batch} 已有来源，不能重复产出")
                temp = ("new", len(new_processes))
                for item in doc.inputs:
                    destinations[item.batch] = ("process", temp)
                for item in doc.outputs:
                    sources[item.batch] = ("process", temp)
                new_processes.append({
                    "temp": temp,
                    "inputs": [(i.batch, i.weight) for i in doc.inputs],
                    "outputs": [(o.batch, o.weight) for o in doc.outputs],
                })

        all_processes = {("existing", pid): p for pid, p in state.processes.items()}
        for np in new_processes:
            all_processes[("existing", np["temp"])] = {
                "inputs": [b for b, _ in np["inputs"]],
                "outputs": [b for b, _ in np["outputs"]],
            }
        _ensure_acyclic(all_processes)

        for b, w in new_batches.items():
            conn.execute("INSERT INTO batches(batch_no, weight) VALUES (?, ?)", (b, w))
        for b in new_incoming:
            conn.execute("INSERT INTO incoming(batch_no) VALUES (?)", (b,))
        process_ids = []
        for np in new_processes:
            pid = conn.execute("INSERT INTO processes DEFAULT VALUES").lastrowid
            for b, _ in np["inputs"]:
                conn.execute(
                    "INSERT INTO process_inputs(process_id, batch_no) VALUES (?, ?)", (pid, b))
            for b, _ in np["outputs"]:
                conn.execute(
                    "INSERT INTO process_outputs(process_id, batch_no) VALUES (?, ?)", (pid, b))
            process_ids.append(pid)
        shipment_ids = []
        for b in new_shipments:
            sid = conn.execute("INSERT INTO shipments(batch_no) VALUES (?)", (b,)).lastrowid
            shipment_ids.append(sid)

        conn.commit()
        return {
            "applied": True,
            "batches_created": sorted(new_batches),
            "incoming_registered": sorted(new_incoming),
            "process_ids": process_ids,
            "shipment_ids": shipment_ids,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
