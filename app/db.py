"""SQLite 连接与建表。"""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_no TEXT PRIMARY KEY,
    weight  INTEGER NOT NULL CHECK (weight > 0)
);
CREATE TABLE IF NOT EXISTS incoming (
    batch_no TEXT PRIMARY KEY REFERENCES batches(batch_no)
);
CREATE TABLE IF NOT EXISTS processes (
    id INTEGER PRIMARY KEY AUTOINCREMENT
);
CREATE TABLE IF NOT EXISTS process_inputs (
    process_id INTEGER NOT NULL REFERENCES processes(id),
    batch_no   TEXT NOT NULL REFERENCES batches(batch_no),
    PRIMARY KEY (process_id, batch_no)
);
-- 每个批号至多投入一道工序（至多一个去向）
CREATE UNIQUE INDEX IF NOT EXISTS ux_process_inputs_batch ON process_inputs(batch_no);
CREATE TABLE IF NOT EXISTS process_outputs (
    process_id INTEGER NOT NULL REFERENCES processes(id),
    batch_no   TEXT NOT NULL REFERENCES batches(batch_no),
    PRIMARY KEY (process_id, batch_no)
);
-- 每个批号至多由一道工序产出（与进料登记互斥，由代码保证）
CREATE UNIQUE INDEX IF NOT EXISTS ux_process_outputs_batch ON process_outputs(batch_no);
CREATE TABLE IF NOT EXISTS shipments (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no TEXT NOT NULL UNIQUE REFERENCES batches(batch_no)
);

-- 收货磅单：可先于发货单录入；未签收前凭版号整张换交。
CREATE TABLE IF NOT EXISTS receipts (
    ticket_no      TEXT PRIMARY KEY,
    version        INTEGER NOT NULL CHECK (version > 0),
    signed_version INTEGER,
    signed_at      TEXT
);
-- 磅单内批号不得重复；同一批号可出现在多张磅单上（重报同批）。
-- 磅单可先于该批号进入账簿，故此处不外键引用 batches。
CREATE TABLE IF NOT EXISTS receipt_items (
    ticket_no TEXT NOT NULL REFERENCES receipts(ticket_no) ON DELETE CASCADE,
    batch_no  TEXT NOT NULL,
    weight    INTEGER NOT NULL CHECK (weight > 0),
    PRIMARY KEY (ticket_no, batch_no)
);
CREATE INDEX IF NOT EXISTS ix_receipt_items_batch ON receipt_items(batch_no);
-- 签收占用：每个批号至多被一张磅单签收；主键同时是并发签收的最终防线。
CREATE TABLE IF NOT EXISTS receipt_claims (
    batch_no       TEXT PRIMARY KEY,
    ticket_no      TEXT NOT NULL REFERENCES receipts(ticket_no),
    signed_version INTEGER NOT NULL
);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", "./data/app.db")


def connect() -> sqlite3.Connection:
    path = db_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        # WAL 让对账查询的读快照与并发写入互不阻塞。
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()
