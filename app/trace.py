"""质检查询：给定不合格进料批号，求各已发批可能含该料的最少/最多克数。

- 沿已登记单据从该进料批向下游展开，得到其可达的批号与工序（"影响面"）。
- 该料的全部克数在影响面内流动：批号容量=称重克数，工序守恒，无去向批为汇。
- 对某已发批 S：
  最少克数 = 推送全部料时，被迫进入 S 的最小流量（S 的汇边费用 1，其余 0）；
  最多克数 = 总料重 - 其余汇至少吸收的量（其余汇边费用 1，S 的 0）。
  费用流最优解为整数，每个极值对应的流量分配即一份符合整条链称重记录的
  克数分配（逐批克数 + 逐工序投入/产出克数），供逐道核对。
- 若 S 的上游有缺单（批号既非进料也非任何工序产出），返回待补批号，不给上下界。
"""
from .flow import INF, add_edge, min_cost_flow


def _solve_extreme(state, defective, cone, cone_procs, dest_proc, target, objective):
    """返回 (极值克数, {批号: 该料克数})。"""
    weights = state.weights
    node_id = {}

    def nid(key):
        if key not in node_id:
            node_id[key] = len(node_id) + 2  # 0=源, 1=汇
        return node_id[key]

    for b in cone:
        nid(("in", b))
        nid(("out", b))
    for p in cone_procs:
        nid(("proc", p))
    g = [[] for _ in range(len(node_id) + 2)]
    SRC, SNK = 0, 1

    add_edge(g, SRC, nid(("in", defective)), weights[defective], 0)
    handles = {}
    for b in cone:  # 批号节点容量 = 称重克数
        u, v = nid(("in", b)), nid(("out", b))
        handles[b] = (u, len(g[u]), weights[b])
        add_edge(g, u, v, weights[b], 0)
    for p in cone_procs:  # 工序节点守恒
        pn = nid(("proc", p))
        for b in state.processes[p]["inputs"]:
            if b in cone:
                add_edge(g, nid(("out", b)), pn, INF, 0)
        for b in state.processes[p]["outputs"]:
            add_edge(g, pn, nid(("in", b)), INF, 0)
    for b in cone:  # 无去向（未投料或已发货）的批为汇
        if b not in dest_proc:
            if objective == "min":
                c = 1 if b == target else 0
            else:
                c = 0 if b == target else 1
            add_edge(g, nid(("out", b)), SNK, INF, c)

    cost = min_cost_flow(g, SRC, SNK, weights[defective])
    if cost is None:  # 理论上恒可行（按称重比例分配即一可行流）
        raise RuntimeError("内部错误：流量分配不可行")
    flow = {b: cap - g[u][i].cap for b, (u, i, cap) in handles.items()}
    value = cost if objective == "min" else weights[defective] - cost
    return value, flow


def _make_witness(state, cone, cone_procs, flow):
    """把流量解整理成可逐道核对的克数分配。"""
    return {
        "batch_grams": {b: flow.get(b, 0) for b in sorted(cone)},
        "processes": [
            {
                "process_id": pid,
                "input_grams": {b: flow.get(b, 0) for b in state.processes[pid]["inputs"]},
                "output_grams": {b: flow.get(b, 0) for b in state.processes[pid]["outputs"]},
            }
            for pid in sorted(cone_procs)
        ],
    }


def build_trace(state, defective):
    """返回各已发批的追溯结果列表（ok: 上下界+分配 / pending: 待补批号）。"""
    src_proc = state.source_process()
    dest_proc = state.dest_process()

    # 影响面：从该进料批沿去向可达的所有批号与工序
    cone, cone_procs, stack = set(), set(), [defective]
    while stack:
        b = stack.pop()
        if b in cone:
            continue
        cone.add(b)
        pid = dest_proc.get(b)
        if pid is not None:
            cone_procs.add(pid)
            stack.extend(state.processes[pid]["outputs"])

    results = []
    for sb in sorted(state.shipments):
        # 上游缺单检查：沿来源向上，找出既非进料也非工序产出的批号
        missing, seen, stack = [], set(), [sb]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            if x in state.incoming:
                continue
            pid = src_proc.get(x)
            if pid is None:
                missing.append(x)
            else:
                stack.extend(state.processes[pid]["inputs"])
        if missing:
            results.append({
                "batch": sb,
                "status": "pending",
                "missing_batches": sorted(missing),
            })
            continue
        mn, min_flow = _solve_extreme(state, defective, cone, cone_procs,
                                      dest_proc, sb, "min")
        mx, max_flow = _solve_extreme(state, defective, cone, cone_procs,
                                      dest_proc, sb, "max")
        results.append({
            "batch": sb,
            "status": "ok",
            "min_grams": mn,
            "max_grams": mx,
            "min_witness": _make_witness(state, cone, cone_procs, min_flow),
            "max_witness": _make_witness(state, cone, cone_procs, max_flow),
        })
    return results
