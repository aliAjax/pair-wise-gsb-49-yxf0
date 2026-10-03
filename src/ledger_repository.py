"""台账持久化：合约年度/分层、巨灾事件、赔案、台账分录与续作批次。

与既有 Repository 共用同一个 SQLite 文件，建表均为 IF NOT EXISTS，互不影响。
所有写操作走 begin() 提供的 BEGIN IMMEDIATE 事务：SQLite 在库级把写事务串行化，
配合重算引擎的确定性排序，保证两个赔案同时争抢同一层最后一次恢复时只有一个放行。
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from .domain import Conflict, NotFound


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LedgerRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        # 测试钩子：在第 N 次事务提交后注入写入失败（1 起算），用于验证批次续作。
        self.fail_commit_ordinals: set = set()
        self._commits = 0
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS treaty_years (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    year INTEGER NOT NULL UNIQUE,
                    aggregate_deductible REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS layers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    treaty_year_id INTEGER NOT NULL REFERENCES treaty_years(id) ON DELETE CASCADE,
                    layer_no INTEGER NOT NULL,
                    attachment REAL NOT NULL,
                    width REAL NOT NULL,
                    cession_pct REAL NOT NULL,
                    reinstatements INTEGER NOT NULL,
                    reinstatement_pct REAL NOT NULL,
                    UNIQUE(treaty_year_id, layer_no)
                );
                CREATE TABLE IF NOT EXISTS cat_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_number TEXT NOT NULL UNIQUE,
                    event_date TEXT,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_number TEXT NOT NULL UNIQUE,
                    treaty_year_id INTEGER REFERENCES treaty_years(id),
                    event_id INTEGER REFERENCES cat_events(id),
                    occurrence_date TEXT,
                    approved_loss REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    review_reason TEXT,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ledger_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    treaty_year_id INTEGER NOT NULL REFERENCES treaty_years(id),
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    layer_no INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    event_version INTEGER,
                    gross_layer_loss REAL NOT NULL,
                    deductible_applied REAL NOT NULL,
                    eligible_gross REAL NOT NULL,
                    accepted_gross REAL NOT NULL,
                    gap_gross REAL NOT NULL,
                    accepted_recovery REAL NOT NULL,
                    gap_amount REAL NOT NULL,
                    reinstatement_premium REAL NOT NULL,
                    reinstatements_used INTEGER NOT NULL,
                    superseded_batch_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_current
                    ON ledger_entries(treaty_year_id, claim_id, layer_no) WHERE status != 'superseded';
                CREATE INDEX IF NOT EXISTS idx_ledger_claim ON ledger_entries(claim_id, id);
                CREATE INDEX IF NOT EXISTS idx_ledger_status ON ledger_entries(status);
                CREATE TABLE IF NOT EXISTS ledger_batches (
                    batch_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ledger_batch_ops (
                    batch_id TEXT NOT NULL,
                    op_key TEXT NOT NULL,
                    claim_id INTEGER,
                    result TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY (batch_id, op_key)
                );
                CREATE TABLE IF NOT EXISTS ledger_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity TEXT NOT NULL,
                    entity_id INTEGER,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def arm_fail_after_commits(self, count: int) -> None:
        """测试钩子：从现在起再成功提交 count 次后，于提交完成瞬间抛出写入失败。"""
        self.fail_commit_ordinals = {self._commits + int(count)}

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
            self._commits += 1
            if self._commits in self.fail_commit_ordinals:
                raise RuntimeError("注入的写入失败：提交后中断")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # ---- 通用 ----
    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def audit(self, conn: sqlite3.Connection, entity: str, entity_id: Optional[int], action: str, actor_id: str, details: Dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO ledger_audit(entity,entity_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (entity, entity_id, action, actor_id, self._json(details), now()),
        )

    def audit_timeline(self, entity: Optional[str] = None, entity_id: Optional[int] = None, limit: int = 200) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM ledger_audit"
        clauses = []
        params: List[Any] = []
        if entity:
            clauses.append("entity=?")
            params.append(entity)
        if entity_id is not None:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    # ---- 合约年度 / 分层 ----
    def create_treaty_year(self, conn: sqlite3.Connection, year: int, aggregate_deductible: float, actor_id: str) -> Dict[str, Any]:
        ts = now()
        try:
            cursor = conn.execute(
                "INSERT INTO treaty_years(year,aggregate_deductible,created_by,created_at) VALUES(?,?,?,?)",
                (year, aggregate_deductible, actor_id, ts),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("合约年度%s已存在" % year) from exc
        year_id = int(cursor.lastrowid)
        self.audit(conn, "treaty_year", year_id, "created", actor_id, {"year": year, "aggregate_deductible": aggregate_deductible})
        return self.get_year(conn, year_id)

    def add_layer(self, conn, year_id, layer_no, attachment, width, cession_pct, reinstatements, reinstatement_pct, actor_id) -> None:
        try:
            conn.execute(
                "INSERT INTO layers(treaty_year_id,layer_no,attachment,width,cession_pct,reinstatements,reinstatement_pct) VALUES(?,?,?,?,?,?,?)",
                (year_id, layer_no, attachment, width, cession_pct, reinstatements, reinstatement_pct),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("第%s层已存在" % layer_no) from exc
        self.audit(conn, "treaty_year", year_id, "layer_added", actor_id, {"layer_no": layer_no})

    def get_year(self, conn: sqlite3.Connection, year_id: int) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM treaty_years WHERE id=?", (year_id,)).fetchone()
        if row is None:
            raise NotFound("合约年度不存在")
        item = dict(row)
        item["layers"] = self.list_layers(conn, year_id)
        return item

    def get_year_by_calendar(self, conn: sqlite3.Connection, year: int) -> Optional[Dict[str, Any]]:
        row = conn.execute("SELECT * FROM treaty_years WHERE year=?", (year,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["layers"] = self.list_layers(conn, item["id"])
        return item

    def list_layers(self, conn: sqlite3.Connection, year_id: int) -> List[Dict[str, Any]]:
        rows = conn.execute("SELECT * FROM layers WHERE treaty_year_id=? ORDER BY layer_no", (year_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_years(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        rows = conn.execute("SELECT * FROM treaty_years ORDER BY year").fetchall()
        return [self.get_year(conn, int(row["id"])) for row in rows]

    # ---- 巨灾事件 ----
    def create_event(self, conn, event_number: str, event_date: Optional[str], actor_id: str) -> Dict[str, Any]:
        ts = now()
        status = "active" if event_date else "active"
        version = 1 if event_date else 0
        try:
            cursor = conn.execute(
                "INSERT INTO cat_events(event_number,event_date,status,version,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (event_number, event_date, status, version, actor_id, actor_id, ts, ts),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("事件号%s已存在" % event_number) from exc
        event_id = int(cursor.lastrowid)
        self.audit(conn, "cat_event", event_id, "registered", actor_id, {"event_number": event_number, "event_date": event_date, "version": version})
        return self.get_event(conn, event_id)

    def get_event(self, conn: sqlite3.Connection, event_id: int) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM cat_events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise NotFound("巨灾事件不存在")
        return dict(row)

    def get_event_by_number(self, conn: sqlite3.Connection, event_number: str) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM cat_events WHERE event_number=?", (event_number,)).fetchone()
        if row is None:
            raise NotFound("事件号不存在：%s" % event_number)
        return dict(row)

    def list_events(self, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
        return [dict(row) for row in conn.execute("SELECT * FROM cat_events ORDER BY id").fetchall()]

    def update_event(self, conn, event_id: int, event_date: Optional[str], status: str, actor_id: str, *, bump_version: bool = True) -> Dict[str, Any]:
        current = self.get_event(conn, event_id)
        version = int(current["version"]) + (1 if bump_version else 0)
        conn.execute(
            "UPDATE cat_events SET event_date=?,status=?,version=?,updated_by=?,updated_at=? WHERE id=?",
            (event_date, status, version, actor_id, now(), event_id),
        )
        return self.get_event(conn, event_id)

    # ---- 赔案 ----
    def create_claim(self, conn, claim_number, year_id, event_id, occurrence_date, approved_loss, status, review_reason, actor_id) -> Dict[str, Any]:
        ts = now()
        try:
            cursor = conn.execute(
                "INSERT INTO claims(claim_number,treaty_year_id,event_id,occurrence_date,approved_loss,status,review_reason,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (claim_number, year_id, event_id, occurrence_date, approved_loss, status, review_reason, actor_id, actor_id, ts, ts),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("赔案号%s已存在" % claim_number) from exc
        claim_id = int(cursor.lastrowid)
        self.audit(conn, "claim", claim_id, "registered", actor_id, {"claim_number": claim_number, "status": status, "review_reason": review_reason})
        return self.get_claim(conn, claim_id)

    def get_claim(self, conn: sqlite3.Connection, claim_id: int) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if row is None:
            raise NotFound("赔案不存在")
        return dict(row)

    def get_claim_by_number(self, conn: sqlite3.Connection, claim_number: str) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM claims WHERE claim_number=?", (claim_number,)).fetchone()
        if row is None:
            raise NotFound("赔案不存在：%s" % claim_number)
        return dict(row)

    def resolve_claim(self, conn: sqlite3.Connection, key: Any) -> Dict[str, Any]:
        if isinstance(key, bool):
            raise NotFound("赔案标识无效")
        if isinstance(key, int):
            return self.get_claim(conn, key)
        if isinstance(key, str) and key.strip().isdigit():
            return self.get_claim(conn, int(key.strip()))
        if isinstance(key, str) and key.strip():
            return self.get_claim_by_number(conn, key.strip())
        raise NotFound("赔案标识无效")

    def list_claims(self, conn: sqlite3.Connection, status: Optional[str] = None, year_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM claims"
        clauses = []
        params: List[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if year_id is not None:
            clauses.append("treaty_year_id=?")
            params.append(year_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def update_claim(self, conn, claim_id, actor_id, **fields) -> Dict[str, Any]:
        fields["updated_by"] = actor_id
        fields["updated_at"] = now()
        sets = ", ".join("%s=?" % name for name in fields)
        params = list(fields.values()) + [claim_id]
        conn.execute("UPDATE claims SET %s WHERE id=?" % sets, params)
        return self.get_claim(conn, claim_id)

    # ---- 台账分录 ----
    def current_entries(self, conn: sqlite3.Connection, year_id: Optional[int] = None, claim_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM ledger_entries WHERE status != 'superseded'"
        clauses = []
        params: List[Any] = []
        if year_id is not None:
            clauses.append("treaty_year_id=?")
            params.append(year_id)
        if claim_id is not None:
            clauses.append("claim_id=?")
            params.append(claim_id)
        if clauses:
            sql += " AND " + " AND ".join(clauses)
        sql += " ORDER BY claim_id, layer_no"
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def supersede_reserved(self, conn: sqlite3.Connection, year_id: int, batch_id: str) -> int:
        """作废一个年度内全部“预占”分录；已确认（结算）分录永不动。返回作废行数。"""
        cursor = conn.execute(
            "UPDATE ledger_entries SET status='superseded', superseded_batch_id=? WHERE treaty_year_id=? AND status='reserved'",
            (batch_id, year_id),
        )
        return cursor.rowcount

    def insert_entry(self, conn, *, year_id, claim_id, layer_no, event_version, allocation, batch_id, status) -> None:
        conn.execute(
            """INSERT INTO ledger_entries(treaty_year_id,claim_id,layer_no,status,event_version,
               gross_layer_loss,deductible_applied,eligible_gross,accepted_gross,gap_gross,
               accepted_recovery,gap_amount,reinstatement_premium,reinstatements_used,superseded_batch_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                year_id, claim_id, layer_no, status, event_version,
                allocation.gross_layer_loss, allocation.deductible_applied, allocation.eligible_gross,
                allocation.accepted_gross, allocation.gap_gross, allocation.accepted_recovery,
                allocation.gap_amount, allocation.reinstatement_premium, allocation.reinstatements_used,
                batch_id if status == "superseded" else None, now(),
            ),
        )

    def confirm_claim(self, conn: sqlite3.Connection, claim_id: int) -> List[Dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM ledger_entries WHERE claim_id=? AND status='reserved' ORDER BY layer_no",
            (claim_id,),
        ).fetchall()
        conn.execute("UPDATE ledger_entries SET status='confirmed' WHERE claim_id=? AND status='reserved'", (claim_id,))
        return [dict(row) for row in rows]

    # ---- 续作批次 ----
    def get_batch(self, conn: sqlite3.Connection, batch_id: str) -> Optional[Dict[str, Any]]:
        row = conn.execute("SELECT * FROM ledger_batches WHERE batch_id=?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def ensure_batch(self, conn: sqlite3.Connection, batch_id: str, kind: str, total: int) -> Dict[str, Any]:
        ts = now()
        existing = self.get_batch(conn, batch_id)
        if existing is None:
            conn.execute(
                "INSERT INTO ledger_batches(batch_id,kind,total,created_at,updated_at) VALUES(?,?,?,?,?)",
                (batch_id, kind, total, ts, ts),
            )
        elif existing["kind"] != kind or int(existing["total"]) != int(total):
            raise Conflict("批次%s已存在但内容不一致，请核对后使用新批次号" % batch_id)
        return self.get_batch(conn, batch_id)

    def touch_batch(self, conn: sqlite3.Connection, batch_id: str) -> None:
        conn.execute("UPDATE ledger_batches SET updated_at=? WHERE batch_id=?", (now(), batch_id))

    def completed_ops(self, conn: sqlite3.Connection, batch_id: str) -> Dict[str, Dict[str, Any]]:
        rows = conn.execute("SELECT * FROM ledger_batch_ops WHERE batch_id=? ORDER BY rowid", (batch_id,)).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item["result"])
            result[item["op_key"]] = item
        return result

    def record_op(self, conn: sqlite3.Connection, batch_id: str, op_key: str, claim_id: int, result: Dict[str, Any]) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO ledger_batch_ops(batch_id,op_key,claim_id,result,completed_at) VALUES(?,?,?,?,?)",
            (batch_id, op_key, claim_id, self._json(result), now()),
        )
        self.touch_batch(conn, batch_id)
