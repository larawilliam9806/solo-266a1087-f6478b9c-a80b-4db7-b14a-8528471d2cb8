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
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", "./data/app.db")


def connect() -> sqlite3.Connection:
    path = db_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
