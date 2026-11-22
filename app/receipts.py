"""收货磅单：登记/换交、对账查询与签收。

业务约束：

- 一张磅单由**单号 + 若干「批号, 实收净重」**组成，单内批号不得重复；同一批号
  可以出现在多张磅单上（重报同批）。
- 磅单可以早于发货单录入，所以磅单上的批号不要求已在账簿中。
- 未签收前，凭**所见版号**整张换交（内容整体替换，版号加 1）；签收后内容冻结。
- 对账查询把同一张单的**磅单、批号称重、发货与签收状态**放在同一个只读事务里读，
  并发交发货单或换交磅单不会让一次查询看到拼接自不同账面状态的片段。
- 一张磅单齐单（单内每个批号都已登记发货）后可算差额：
  实收净重合计 - 发货称重合计。
- 签收须同时满足：所见版号等于当前版号；单内批号全部已发货；|差额| <= 500 克；
  单内任何批号都未被签收过。整单签收在单个写事务内完成，任一条不满足则本次
  不占用任何批号。
"""
import sqlite3
from datetime import datetime, timezone
from typing import Optional

MAX_DIFFERENCE_GRAMS = 500


class ReceiptError(Exception):
    """收货磅单业务校验失败（版号、齐单、差额、重复签收等）。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _receipt_row(conn, ticket_no):
    return conn.execute(
        "SELECT ticket_no, version, signed_version, signed_at"
        " FROM receipts WHERE ticket_no = ?", (ticket_no,)).fetchone()


def _items(conn, ticket_no):
    return [{"batch": r["batch_no"], "weight": r["weight"]}
            for r in conn.execute(
                "SELECT batch_no, weight FROM receipt_items"
                " WHERE ticket_no = ? ORDER BY rowid", (ticket_no,))]


def _shipped_batches(conn, batches) -> set:
    batches = list(batches)
    if not batches:
        return set()
    marks = ",".join("?" for _ in batches)
    return {r["batch_no"] for r in conn.execute(
        f"SELECT batch_no FROM shipments WHERE batch_no IN ({marks})", batches)}


def _weights(conn, batches) -> dict:
    batches = list(batches)
    if not batches:
        return {}
    marks = ",".join("?" for _ in batches)
    return {r["batch_no"]: r["weight"] for r in conn.execute(
        f"SELECT batch_no, weight FROM batches WHERE batch_no IN ({marks})", batches)}


def _claims(conn, batches) -> dict:
    """batch_no -> {"ticket_no", "signed_version"}，仅查给定批号。"""
    batches = list(batches)
    if not batches:
        return {}
    marks = ",".join("?" for _ in batches)
    return {r["batch_no"]: {"ticket_no": r["ticket_no"],
                            "signed_version": r["signed_version"]}
            for r in conn.execute(
                f"SELECT batch_no, ticket_no, signed_version FROM receipt_claims"
                f" WHERE batch_no IN ({marks})", batches)}


def _totals(items, shipped_weights):
    received = sum(i["weight"] for i in items)
    shipped = sum(shipped_weights.values())
    return received, shipped


def _difference(conn, items, shipped: set, shipped_weights: dict) -> Optional[int]:
    """齐单时返回「实收净重合计 - 发货称重合计」，否则 None。"""
    if any(i["batch"] not in shipped for i in items):
        return None
    received, shipped_total = _totals(items, shipped_weights)
    return received - shipped_total


def reconciliation(conn, ticket_no: str) -> dict:
    """在调用方开启的读事务内，拼出一张磅单的完整对账视图。"""
    row = _receipt_row(conn, ticket_no)
    if row is None:
        raise ReceiptError(404, f"磅单 {ticket_no} 不存在")
    items = _items(conn, ticket_no)
    batches = [i["batch"] for i in items]
    shipped_set = _shipped_batches(conn, batches)
    shipped_weights = {b: w for b, w in _weights(conn, batches).items()
                       if b in shipped_set}
    claims = _claims(conn, batches)

    pending = sorted(i["batch"] for i in items if i["batch"] not in shipped_set)
    difference = _difference(conn, items, shipped_set, shipped_weights)
    received_total, shipped_total = _totals(items, shipped_weights)

    item_views = []
    for i in items:
        b = i["batch"]
        item_views.append({
            "batch": b,
            "batch_no": b,
            "weight": i["weight"],
            "received_net_grams": i["weight"],
            "shipped": b in shipped_set,
            "shipped_grams": shipped_weights.get(b),
            "signed_by": claims[b]["ticket_no"] if b in claims else None,
        })

    # 重报同批的其他磅单及签收状态
    other = {}
    if batches:
        marks = ",".join("?" for _ in batches)
        rows = list(conn.execute(
            f"SELECT r.ticket_no, r.version, r.signed_version,"
            f"       ri.batch_no, ri.weight"
            f"  FROM receipt_items ri JOIN receipts r ON r.ticket_no = ri.ticket_no"
            f" WHERE ri.batch_no IN ({marks}) AND r.ticket_no <> ?"
            f" ORDER BY r.ticket_no, ri.rowid",
            (*batches, ticket_no)))
        other_batches = sorted({r["batch_no"] for r in rows})
        other_claims = _claims(conn, other_batches)
        for r in rows:
            entry = other.setdefault(r["ticket_no"], {
                "ticket_no": r["ticket_no"],
                "version": r["version"],
                "signed": r["signed_version"] is not None,
                "signed_version": r["signed_version"],
                "batches": [],
            })
            entry["batches"].append({
                "batch": r["batch_no"],
                "weight": r["weight"],
                "signed_by": (other_claims[r["batch_no"]]["ticket_no"]
                              if r["batch_no"] in other_claims else None),
            })

    signed = row["signed_version"] is not None
    return {
        "ticket_no": row["ticket_no"],
        "version": row["version"],
        "current_version": row["version"],
        "signed": signed,
        "signed_version": row["signed_version"],
        "signed_at": row["signed_at"],
        "items": item_views,
        "received_net_grams_total": received_total,
        "shipped_grams_total": shipped_total,
        "pending_shipment_batches": pending,
        "missing_shipment_batches": pending,   # 兼容别名
        "complete": not pending,
        "difference_grams": difference,
        "difference": difference,              # 兼容别名
        "other_tickets": [other[k] for k in sorted(other)],
    }


def _apply_receipt(conn, ticket_no: str, version: Optional[int], items) -> dict:
    """在调用方事务内新建或整张换交磅单（不提交）。"""
    if not items:
        raise ReceiptError(409, f"磅单 {ticket_no} 没有收货明细")
    batches = [i["batch"] for i in items]
    if len(set(batches)) != len(batches):
        raise ReceiptError(409, f"磅单 {ticket_no} 内批号重复")

    row = _receipt_row(conn, ticket_no)
    if row is None:
        if version not in (None, 1):
            raise ReceiptError(
                409, f"磅单 {ticket_no} 不存在，不能按版号 {version} 换交")
        conn.execute("INSERT INTO receipts(ticket_no, version) VALUES (?, 1)",
                     (ticket_no,))
        new_version = 1
        created = True
    else:
        if row["signed_version"] is not None:
            raise ReceiptError(
                409, f"磅单 {ticket_no} 已签收，内容已固定，不能再换交")
        if version is None:
            raise ReceiptError(
                409, f"磅单 {ticket_no} 已存在（当前版号 {row['version']}），"
                     f"换交须提供所见版号")
        if version != row["version"]:
            raise ReceiptError(
                409, f"磅单 {ticket_no} 版号不符：所见 {version}，"
                     f"当前 {row['version']}，请按当前版号重新换交")
        conn.execute("DELETE FROM receipt_items WHERE ticket_no = ?", (ticket_no,))
        new_version = row["version"] + 1
        conn.execute("UPDATE receipts SET version = ? WHERE ticket_no = ?",
                     (new_version, ticket_no))
        created = False

    for item in items:
        conn.execute(
            "INSERT INTO receipt_items(ticket_no, batch_no, weight) VALUES (?, ?, ?)",
            (ticket_no, item["batch"], item["weight"]))
    return {"ticket_no": ticket_no, "version": new_version,
            "created": created, "replaced": not created}


def register_receipt(conn, ticket_no: str, version: Optional[int], items) -> dict:
    """新建或整张换交磅单；整次在写事务内完成，校验失败不落任何内容。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = _apply_receipt(conn, ticket_no, version, items)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


def _apply_sign(conn, ticket_no: str, version: Optional[int]) -> dict:
    """在调用方事务内整单签收（不提交）。任一条不满足即抛错，不占用批号。"""
    row = _receipt_row(conn, ticket_no)
    if row is None:
        raise ReceiptError(404, f"磅单 {ticket_no} 不存在")
    if version is None:
        raise ReceiptError(409, f"签收磅单 {ticket_no} 须提供所见版号")
    if version != row["version"]:
        raise ReceiptError(
            409, f"磅单 {ticket_no} 版号不符：所见 {version}，"
                 f"当前 {row['version']}，请刷新对账结果后再签收")
    if row["signed_version"] is not None:
        raise ReceiptError(409, f"磅单 {ticket_no} 已于版号 "
                                f"{row['signed_version']} 签收，不能重复签收")

    items = _items(conn, ticket_no)
    if not items:
        raise ReceiptError(409, f"磅单 {ticket_no} 没有收货明细")
    batches = [i["batch"] for i in items]

    shipped_set = _shipped_batches(conn, batches)
    pending = sorted(i["batch"] for i in items if i["batch"] not in shipped_set)
    if pending:
        raise ReceiptError(
            409, f"磅单 {ticket_no} 发货单不齐全，待补发货批号："
                 + "、".join(pending))

    shipped_weights = {b: w for b, w in _weights(conn, batches).items()
                       if b in shipped_set}
    difference = _difference(conn, items, shipped_set, shipped_weights)
    if abs(difference) > MAX_DIFFERENCE_GRAMS:
        raise ReceiptError(
            409, f"磅单 {ticket_no} 实收净重与发货称重差额 {difference} 克，"
                 f"绝对差额超过 {MAX_DIFFERENCE_GRAMS} 克，不能签收")

    claims = _claims(conn, batches)
    if claims:
        detail = "、".join(
            f"{b}（磅单 {c['ticket_no']}）" for b, c in sorted(claims.items()))
        raise ReceiptError(
            409, f"磅单 {ticket_no} 中以下批号已被签收：{detail}，不能重复签收")

    signed_at = _now()
    conn.execute(
        "UPDATE receipts SET signed_version = version, signed_at = ?"
        " WHERE ticket_no = ?", (signed_at, ticket_no))
    for b in batches:
        conn.execute(
            "INSERT INTO receipt_claims(batch_no, ticket_no, signed_version)"
            " VALUES (?, ?, ?)", (b, ticket_no, version))
    return {
        "ticket_no": ticket_no,
        "signed": True,
        "version": version,
        "signed_version": version,
        "signed_at": signed_at,
        "batches_claimed": sorted(batches),
        "difference_grams": difference,
        "difference": difference,
    }


def sign_receipt(conn, ticket_no: str, version: Optional[int]) -> dict:
    """整单签收。失败时回滚，本次不占用任何批号。

    `BEGIN IMMEDIATE` 让并发签收串行化；`receipt_claims` 的主键是最终防线，
    即使检查与写入之间被插入，也只有一张磅单能签成。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = _apply_sign(conn, ticket_no, version)
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            raise ReceiptError(
                409, f"磅单 {ticket_no} 的批号已被其他磅单签收，不能重复签收")
        except Exception:
            conn.rollback()
            raise
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise ReceiptError(409, "签收与另一笔写入冲突，请重试")
        raise
    return result


def completed_by_shipments(conn, newly_shipped) -> list:
    """本次发货登记补齐的磅单（此前缺发货单，现在齐单），附差额。

    调用方需已处于同一写事务内；读取到的是本次插入发货单之后的状态。
    """
    newly_shipped = sorted(set(newly_shipped))
    if not newly_shipped:
        return []
    marks = ",".join("?" for _ in newly_shipped)
    ticket_nos = [r["ticket_no"] for r in conn.execute(
        f"SELECT DISTINCT ticket_no FROM receipt_items"
        f" WHERE batch_no IN ({marks}) ORDER BY ticket_no", newly_shipped)]
    completed = []
    for ticket_no in ticket_nos:
        row = _receipt_row(conn, ticket_no)
        items = _items(conn, ticket_no)
        batches = [i["batch"] for i in items]
        shipped_set = _shipped_batches(conn, batches)
        difference = _difference(
            conn, items, shipped_set,
            {b: w for b, w in _weights(conn, batches).items() if b in shipped_set})
        if difference is None:      # 还有其他批号待补发货，本次未齐单
            continue
        received, shipped = _totals(
            items, {b: w for b, w in _weights(conn, batches).items()
                    if b in shipped_set})
        completed.append({
            "ticket_no": ticket_no,
            "version": row["version"],
            "received_net_grams_total": received,
            "shipped_grams_total": shipped,
            "difference_grams": difference,
            "difference": difference,
        })
    return completed
