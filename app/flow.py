"""最小费用流（SPFA 逐次最短路增广），用于求批中某进料的最少/最多克数。

建模：把不合格进料批的全部克数视为必须从该批流出的"该料"流量，
每个批号节点容量为其称重克数，工序节点自动守恒（投入=产出），
无去向的批（未投料/已发货）为汇。容量与流量均为整数克。
"""
from collections import deque

INF = 10 ** 15


class _Edge:
    __slots__ = ("to", "cap", "cost", "rev")

    def __init__(self, to, cap, cost, rev):
        self.to = to
        self.cap = cap
        self.cost = cost
        self.rev = rev


def add_edge(g, u, v, cap, cost):
    g[u].append(_Edge(v, cap, cost, len(g[v])))
    g[v].append(_Edge(u, 0, -cost, len(g[u]) - 1))


def min_cost_flow(g, s, t, amount):
    """从 s 向 t 推送 amount 单位流量，返回最小总费用；不可行返回 None。"""
    n = len(g)
    flow = 0
    cost = 0
    while flow < amount:
        dist = [INF] * n
        prev_v = [-1] * n
        prev_e = [-1] * n
        dist[s] = 0
        in_q = [False] * n
        in_q[s] = True
        queue = deque([s])
        while queue:
            u = queue.popleft()
            in_q[u] = False
            for i, e in enumerate(g[u]):
                if e.cap > 0 and dist[e.to] > dist[u] + e.cost:
                    dist[e.to] = dist[u] + e.cost
                    prev_v[e.to] = u
                    prev_e[e.to] = i
                    if not in_q[e.to]:
                        in_q[e.to] = True
                        queue.append(e.to)
        if prev_v[t] == -1:
            return None
        d = amount - flow
        v = t
        while v != s:
            d = min(d, g[prev_v[v]][prev_e[v]].cap)
            v = prev_v[v]
        flow += d
        cost += d * dist[t]
        v = t
        while v != s:
            e = g[prev_v[v]][prev_e[v]]
            e.cap -= d
            g[v][e.rev].cap += d
            v = prev_v[v]
    return cost
