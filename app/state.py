"""从 SQLite 载入完整账簿状态，供校验与追溯计算使用。"""
from dataclasses import dataclass, field


@dataclass
class State:
    weights: dict = field(default_factory=dict)      # batch_no -> 克
    incoming: set = field(default_factory=set)       # 已登记进料的批号
    processes: dict = field(default_factory=dict)    # pid -> {"inputs": [...], "outputs": [...]}
    shipments: dict = field(default_factory=dict)    # batch_no -> shipment_id

    def source_process(self) -> dict:
        """batch_no -> 产出它的工序 id"""
        return {b: pid for pid, p in self.processes.items() for b in p["outputs"]}

    def dest_process(self) -> dict:
        """batch_no -> 消耗它的工序 id"""
        return {b: pid for pid, p in self.processes.items() for b in p["inputs"]}


def load_state(conn) -> State:
    weights = {r["batch_no"]: r["weight"]
               for r in conn.execute("SELECT batch_no, weight FROM batches")}
    incoming = {r["batch_no"] for r in conn.execute("SELECT batch_no FROM incoming")}
    processes = {r["id"]: {"inputs": [], "outputs": []}
                 for r in conn.execute("SELECT id FROM processes")}
    for r in conn.execute("SELECT process_id, batch_no FROM process_inputs ORDER BY rowid"):
        processes[r["process_id"]]["inputs"].append(r["batch_no"])
    for r in conn.execute("SELECT process_id, batch_no FROM process_outputs ORDER BY rowid"):
        processes[r["process_id"]]["outputs"].append(r["batch_no"])
    shipments = {r["batch_no"]: r["id"]
                 for r in conn.execute("SELECT id, batch_no FROM shipments")}
    return State(weights=weights, incoming=incoming,
                 processes=processes, shipments=shipments)
