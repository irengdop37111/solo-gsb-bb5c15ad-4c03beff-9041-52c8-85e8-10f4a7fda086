"""SQLite 数据访问层。

- 显式事务：写操作在 BEGIN IMMEDIATE 事务中进行，防止答卷与结算并发竞态。
- 所有时间戳统一为服务端 Unix 秒（INTEGER）。
- events 表为操作时间线：事件与业务状态在同一写事务内提交，自增 id 即提交顺序。
"""

import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("SNIFF_DB_PATH", "/data/sniff.db")

# 杯贴码字母表：去除易混字符（与盲评码同级高熵随机），码面不含任何样本信息
_STICKER_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tests (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT NOT NULL UNIQUE,          -- 负责人管理码
    token_hash         TEXT NOT NULL,                 -- 负责人令牌哈希
    sample_a           TEXT NOT NULL,
    sample_b           TEXT NOT NULL,
    deadline           INTEGER NOT NULL,
    min_valid          INTEGER NOT NULL,
    created_at         INTEGER NOT NULL,
    settled_at         INTEGER,
    ended_at           INTEGER,                          -- 负责人提前结束收集时间（秒）；NULL 表示未提前结束
    end_reason         TEXT,                             -- 提前结束原因（trim 后 1～200 字）
    valid_count        INTEGER,
    correct_count      INTEGER,
    p_value            REAL,
    distinguishable    INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,   -- 全局提交顺序（稳定排序键）
    test_id     INTEGER NOT NULL REFERENCES tests(id),
    ts          INTEGER NOT NULL,                    -- 服务端事件时间（秒，事务内采集）
    type        TEXT NOT NULL,   -- create/answer/adjust/end/settle/history_start/share_create/share_revoke/prep_issue/prep_revoke/deliver
    payload     TEXT NOT NULL DEFAULT '{}'           -- JSON，盲态字段读取时再裁剪
);

CREATE INDEX IF NOT EXISTS idx_events_test ON events(test_id, id);

CREATE TABLE IF NOT EXISTS assignments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id      INTEGER NOT NULL REFERENCES tests(id),
    panelist     TEXT NOT NULL,
    code         TEXT NOT NULL UNIQUE,                -- 评员盲评码
    odd_pos      INTEGER NOT NULL,                    -- 异样杯位置 1/2/3
    odd_sample   TEXT NOT NULL,                       -- 异样杯样本编号（A 或 B）
    answer       INTEGER,                             -- 1/2/3；弃权为 -1；未提交 NULL
    answered_at  INTEGER,
    status       TEXT NOT NULL DEFAULT 'active',      -- active 有效名单；withdrawn 已撤回
    UNIQUE(test_id, panelist)
);

CREATE INDEX IF NOT EXISTS idx_assignments_test ON assignments(test_id);

-- 已结算实验的脱敏结果分享凭证（不记名只读能力凭证）：
-- 仅已结算测试可生成；到期或撤销后读取一律拒绝。
-- (test_id, note, ttl_seconds) 唯一：同实验同备注同有效期重复请求返回原凭证。
CREATE TABLE IF NOT EXISTS shares (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id      INTEGER NOT NULL REFERENCES tests(id),
    share_code   TEXT NOT NULL UNIQUE,                -- 一次性随机分享凭证（读取凭证）
    note         TEXT NOT NULL DEFAULT '',            -- 负责人备注（归一化 trim 后存储）
    ttl_seconds  INTEGER NOT NULL,                    -- 有效期（秒），最长 7 天
    created_at   INTEGER NOT NULL,                    -- 生成时间（秒）
    expires_at   INTEGER NOT NULL,                    -- 到期时间（秒，服务端时钟判定）
    revoked_at   INTEGER,                             -- 撤销时间（秒）；NULL 未撤销
    UNIQUE(test_id, note, ttl_seconds)
);

CREATE INDEX IF NOT EXISTS idx_shares_test ON shares(test_id);

-- 杯贴码：每个有效评员的 1/2/3 杯位各一枚唯一杯贴码，码本身不含任何样本信息。
-- 评员被撤回后其杯贴码不再出现在任何制备清单中（随 assignment 状态一并失效）；
-- 补位评员是新的 assignment，取得全新杯贴码。
CREATE TABLE IF NOT EXISTS cups (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id  INTEGER NOT NULL REFERENCES assignments(id),
    pos            INTEGER NOT NULL CHECK (pos IN (1, 2, 3)),  -- 杯位 1/2/3
    sticker        TEXT NOT NULL UNIQUE,                        -- 唯一杯贴码（不含样本信息）
    UNIQUE(assignment_id, pos)
);

CREATE INDEX IF NOT EXISTS idx_cups_assignment ON cups(assignment_id);

-- 单场制备凭证：负责人凭管理令牌签发；同一时刻同一实验仅一份有效。
-- 部分唯一索引在库级保证「仅一份有效」；撤销（revoked_at 置位）后可重签，旧凭证始终失效。
CREATE TABLE IF NOT EXISTS prep_credentials (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id     INTEGER NOT NULL REFERENCES tests(id),
    prep_code   TEXT NOT NULL UNIQUE,                 -- 制备凭证（不记名能力码，发给制备员）
    created_at  INTEGER NOT NULL,                     -- 签发时间（秒）
    revoked_at  INTEGER                               -- 撤销时间（秒）；NULL 表示当前有效
);

CREATE INDEX IF NOT EXISTS idx_prep_test ON prep_credentials(test_id, id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_prep_active
ON prep_credentials(test_id) WHERE revoked_at IS NULL;

-- 整组三杯发放记录：同一评员（assignment）至多一条；
-- 重复确认命中同一行（幂等），并发确认由唯一约束 + 写锁保证只形成一条记录。
CREATE TABLE IF NOT EXISTS deliveries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id  INTEGER NOT NULL UNIQUE REFERENCES assignments(id),
    credential_id  INTEGER NOT NULL REFERENCES prep_credentials(id),  -- 以哪份凭证确认发放
    delivered_at   INTEGER NOT NULL                    -- 发放时间（秒，服务端时钟）
);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        # 兼容旧库：为已存在的 assignments 表补 status 列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(assignments)")}
        if "status" not in cols:
            conn.execute(
                "ALTER TABLE assignments ADD COLUMN status TEXT NOT NULL "
                "DEFAULT 'active'"
            )
        # 兼容旧库：为已存在的 tests 表补提前结束相关列
        tcols = {r["name"] for r in conn.execute("PRAGMA table_info(tests)")}
        if "ended_at" not in tcols:
            conn.execute("ALTER TABLE tests ADD COLUMN ended_at INTEGER")
        if "end_reason" not in tcols:
            conn.execute("ALTER TABLE tests ADD COLUMN end_reason TEXT")
        conn.commit()
    finally:
        conn.close()
    # 兼容旧库：events 表上线前创建的测试没有任何操作记录。
    # 为每个这样的测试补一条明确的“记录起点”标记，不重建、不虚构既往操作。
    backfill_history_markers()
    # 兼容旧库：本轮新增杯贴体系，为已存在但缺少杯贴的评员补发杯贴码。
    backfill_cups()


def new_sticker(conn) -> str:
    """生成一枚全局唯一的杯贴码（10 位）；调用方须在写事务内。"""
    while True:
        code = "".join(secrets.choice(_STICKER_ALPHABET) for _ in range(10))
        if conn.execute(
            "SELECT 1 FROM cups WHERE sticker = ?", (code,)
        ).fetchone() is None:
            return code


def add_cups(conn, assignment_id: int):
    """为一名评员的三杯（杯位 1/2/3）各插入一枚唯一杯贴码。"""
    for pos in (1, 2, 3):
        conn.execute(
            "INSERT INTO cups (assignment_id, pos, sticker) VALUES (?, ?, ?)",
            (assignment_id, pos, new_sticker(conn)),
        )


def backfill_cups():
    """为缺少杯贴记录的旧评员（含已撤回者）一次性补发，幂等可重复执行。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT a.id FROM assignments a WHERE NOT EXISTS ("
            "SELECT 1 FROM cups c WHERE c.assignment_id = a.id)"
        ).fetchall()
        for r in rows:
            add_cups(conn, r["id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def backfill_history_markers():
    """为缺少任何事件的旧测试补 history_start 起点标记（幂等）。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, created_at FROM tests t WHERE NOT EXISTS ("
            "SELECT 1 FROM events e WHERE e.test_id = t.id)"
        ).fetchall()
        marker_ts = int(time.time())
        for r in rows:
            conn.execute(
                "INSERT INTO events (test_id, ts, type, payload) "
                "VALUES (?, ?, 'history_start', ?)",
                (
                    r["id"],
                    marker_ts,
                    json.dumps({
                        "note": "操作时间线自本次升级起开始记录；该测试创建于旧版本，"
                                "既往创建、名单调整、答卷与结算操作无可追溯记录，"
                                "不做历史重建。",
                        "legacy_created_at": r["created_at"],
                    }, ensure_ascii=False),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def log_event(conn, test_id: int, ts: int, event_type: str, payload: dict):
    """在当前写事务内追加一条时间线事件；随业务状态一起提交或回滚。"""
    conn.execute(
        "INSERT INTO events (test_id, ts, type, payload) VALUES (?, ?, ?, ?)",
        (test_id, ts, event_type,
         json.dumps(payload, ensure_ascii=False)),
    )


@contextmanager
def write_tx():
    """立即拿写锁的事务；异常回滚，正常提交。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_conn():
    return connect()
