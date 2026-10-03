"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
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
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE TABLE IF NOT EXISTS treaty_years (
                    treaty_id TEXT PRIMARY KEY,
                    year INTEGER NOT NULL,
                    aggregate_deductible REAL NOT NULL,
                    aggregate_limit REAL NOT NULL,
                    layer_attachment REAL NOT NULL,
                    layer_limit REAL NOT NULL,
                    cession_pct REAL NOT NULL,
                    reinstatement_limit INTEGER NOT NULL,
                    reserved_aggregate REAL NOT NULL DEFAULT 0,
                    confirmed_aggregate REAL NOT NULL DEFAULT 0,
                    reserved_reinstatements INTEGER NOT NULL DEFAULT 0,
                    confirmed_reinstatements INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cat_events (
                    event_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    occurrence_date TEXT NOT NULL,
                    end_date TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_number TEXT NOT NULL UNIQUE,
                    treaty_id TEXT NOT NULL REFERENCES treaty_years(treaty_id),
                    event_id TEXT NOT NULL,
                    occurrence_date TEXT,
                    occurrence_source TEXT,
                    event_version INTEGER,
                    approved_loss REAL,
                    reserved_amount REAL NOT NULL DEFAULT 0,
                    confirmed_amount REAL NOT NULL DEFAULT 0,
                    reinstatements_reserved INTEGER NOT NULL DEFAULT 0,
                    reinstatements_confirmed INTEGER NOT NULL DEFAULT 0,
                    shortfall REAL NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'submitted',
                    payment_reference TEXT,
                    reject_reason TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_claims_treaty ON claims(treaty_id, state);
                CREATE INDEX IF NOT EXISTS idx_claims_event ON claims(event_id, state);
                CREATE TABLE IF NOT EXISTS recalc_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recalc_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES recalc_jobs(id) ON DELETE CASCADE,
                    treaty_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    batch_id INTEGER,
                    error TEXT,
                    UNIQUE(job_id, treaty_id)
                );
                CREATE TABLE IF NOT EXISTS ledger_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    treaty_id TEXT NOT NULL,
                    job_id INTEGER,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ledger_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES ledger_batches(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    claim_number TEXT NOT NULL,
                    occurrence_date TEXT,
                    event_id TEXT NOT NULL,
                    event_version INTEGER,
                    gross_recovery REAL NOT NULL,
                    reserved_amount REAL NOT NULL,
                    confirmed_amount REAL NOT NULL,
                    reinstatements_used INTEGER NOT NULL,
                    shortfall REAL NOT NULL,
                    entry_status TEXT NOT NULL,
                    UNIQUE(batch_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_entries_claim ON ledger_entries(claim_id, id);
                CREATE TABLE IF NOT EXISTS ledger_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_audit_entity ON ledger_audit(entity_type, entity_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------------
    # 台账：事务助手
    # ------------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """单个写事务：BEGIN IMMEDIATE串行化写入方，提交或整体回滚。"""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # 台账：合约年度
    # ------------------------------------------------------------------
    def create_treaty_year(self, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self.transaction() as connection:
                connection.execute(
                    "INSERT INTO treaty_years(treaty_id,year,aggregate_deductible,aggregate_limit,layer_attachment,layer_limit,cession_pct,reinstatement_limit,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        data["treaty_id"], data["year"], data["aggregate_deductible"], data["aggregate_limit"],
                        data["layer_attachment"], data["layer_limit"], data["cession_pct"], data["reinstatement_limit"],
                        actor_id, now, now,
                    ),
                )
                self._audit_tx(connection, "treaty_year", data["treaty_id"], "registered", actor_id, {"year": data["year"]})
        except sqlite3.IntegrityError as exc:
            raise Conflict("合约年度已存在") from exc
        return self.get_treaty_year(data["treaty_id"])

    def get_treaty_year(self, treaty_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM treaty_years WHERE treaty_id=?", (treaty_id,)).fetchone()
        if row is None:
            raise NotFound("合约年度不存在")
        return dict(row)

    def list_treaty_years(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM treaty_years ORDER BY treaty_id").fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 台账：巨灾事件
    # ------------------------------------------------------------------
    def create_event(self, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self.transaction() as connection:
                connection.execute(
                    "INSERT INTO cat_events(event_id,name,occurrence_date,end_date,status,version,created_by,created_at,updated_at) VALUES(?,?,?,?,'active',1,?,?,?)",
                    (data["event_id"], data.get("name") or "", data["occurrence_date"], data.get("end_date"), actor_id, now, now),
                )
                self._audit_tx(connection, "cat_event", data["event_id"], "registered", actor_id, {"occurrence_date": data["occurrence_date"], "version": 1})
        except sqlite3.IntegrityError as exc:
            raise Conflict("事件号已存在") from exc
        return self.get_event(data["event_id"])

    def get_event(self, event_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM cat_events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise NotFound("巨灾事件不存在")
        return dict(row)

    def find_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM cat_events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def list_events(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if status:
                rows = connection.execute("SELECT * FROM cat_events WHERE status=? ORDER BY event_id", (status,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM cat_events ORDER BY event_id").fetchall()
        return [dict(row) for row in rows]

    def update_event_occurrence(self, event_id: str, occurrence_date: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM cat_events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise NotFound("巨灾事件不存在")
            if row["status"] != "active":
                raise Conflict("事件已撤回，不能更正")
            version = int(row["version"]) + 1
            connection.execute(
                "UPDATE cat_events SET occurrence_date=?,version=?,updated_at=? WHERE event_id=?",
                (occurrence_date, version, now, event_id),
            )
            self._audit_tx(connection, "cat_event", event_id, "corrected", actor_id, {"occurrence_date": occurrence_date, "version": version})
        return self.get_event(event_id)

    def withdraw_event(self, event_id: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM cat_events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise NotFound("巨灾事件不存在")
            if row["status"] == "withdrawn":
                raise Conflict("事件已撤回")
            version = int(row["version"]) + 1
            connection.execute(
                "UPDATE cat_events SET status='withdrawn',version=?,updated_at=? WHERE event_id=?",
                (version, now, event_id),
            )
            self._audit_tx(connection, "cat_event", event_id, "withdrawn", actor_id, {"version": version})
        return self.get_event(event_id)

    # ------------------------------------------------------------------
    # 台账：赔案
    # ------------------------------------------------------------------
    def create_claim(self, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self.transaction() as connection:
                cursor = connection.execute(
                    "INSERT INTO claims(claim_number,treaty_id,event_id,occurrence_date,occurrence_source,event_version,approved_loss,state,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'submitted',?,?,?)",
                    (
                        data["claim_number"], data["treaty_id"], data["event_id"], data.get("occurrence_date"),
                        data.get("occurrence_source"), data.get("event_version"), data.get("approved_loss"), actor_id, now, now,
                    ),
                )
                claim_id = int(cursor.lastrowid)
                self._audit_tx(connection, "claim", str(claim_id), "submitted", actor_id, {"claim_number": data["claim_number"], "treaty_id": data["treaty_id"]})
        except sqlite3.IntegrityError as exc:
            raise Conflict("赔案号已存在或合约年度不存在") from exc
        return self.get_claim(claim_id)

    def get_claim(self, claim_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if row is None:
            raise NotFound("赔案不存在")
        return dict(row)

    def list_claims(self, treaty_id: Optional[str] = None, event_id: Optional[str] = None, state: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        clauses, params = [], []
        if treaty_id:
            clauses.append("treaty_id=?")
            params.append(treaty_id)
        if event_id:
            clauses.append("event_id=?")
            params.append(event_id)
        if state:
            clauses.append("state=?")
            params.append(state)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM claims%s ORDER BY id LIMIT ?" % where, params + [limit]).fetchall()
        return [dict(row) for row in rows]

    def claims_missing_backfill(self, limit: int = 1000) -> List[Dict[str, Any]]:
        """旧数据：缺发生日或事件版本且未终结的赔案。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM claims WHERE (occurrence_date IS NULL OR event_version IS NULL) AND state NOT IN ('rejected','settled') ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 台账：重算任务（批次续作）
    # ------------------------------------------------------------------
    def create_recalc_job(self, reason: str, actor_id: str, treaty_ids: List[str]) -> Dict[str, Any]:
        now = _now()
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO recalc_jobs(reason,status,actor_id,created_at,updated_at) VALUES(?,'pending',?,?,?)",
                (reason, actor_id, now, now),
            )
            job_id = int(cursor.lastrowid)
            for treaty_id in sorted(set(treaty_ids)):
                connection.execute(
                    "INSERT INTO recalc_tasks(job_id,treaty_id,status) VALUES(?,?,'pending')",
                    (job_id, treaty_id),
                )
        return self.get_recalc_job(job_id)

    def get_recalc_job(self, job_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM recalc_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise NotFound("重算批次不存在")
            tasks = connection.execute("SELECT * FROM recalc_tasks WHERE job_id=? ORDER BY treaty_id", (job_id,)).fetchall()
        job = dict(row)
        job["tasks"] = [dict(task) for task in tasks]
        return job

    def list_recalc_jobs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM recalc_jobs ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),)).fetchall()
        return [dict(row) for row in rows]

    def pending_recalc_tasks(self, job_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM recalc_tasks WHERE job_id=? AND status='pending' ORDER BY treaty_id", (job_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def reset_failed_tasks(self, job_id: int) -> None:
        """续作前把失败任务退回待执行，已完成任务保持不动。"""
        with self.transaction() as connection:
            connection.execute(
                "UPDATE recalc_tasks SET status='pending',error=NULL WHERE job_id=? AND status='failed'", (job_id,)
            )

    def mark_job_running(self, job_id: int) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE recalc_jobs SET status='running',updated_at=? WHERE id=?", (_now(), job_id))

    def complete_recalc_task_tx(self, connection: sqlite3.Connection, job_id: int, treaty_id: str, batch_id: int) -> None:
        connection.execute(
            "UPDATE recalc_tasks SET status='completed',batch_id=? WHERE job_id=? AND treaty_id=?",
            (batch_id, job_id, treaty_id),
        )

    def fail_recalc_task(self, job_id: int, treaty_id: str, error: str) -> None:
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE recalc_tasks SET status='failed',error=? WHERE job_id=? AND treaty_id=?",
                (error[:500], job_id, treaty_id),
            )
            connection.execute("UPDATE recalc_jobs SET status='failed',updated_at=? WHERE id=?", (now, job_id))

    def complete_recalc_job(self, job_id: int) -> None:
        now = _now()
        with self.transaction() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) AS n FROM recalc_tasks WHERE job_id=? AND status='pending'", (job_id,)
            ).fetchone()["n"]
            status = "completed" if int(remaining) == 0 else "failed"
            connection.execute("UPDATE recalc_jobs SET status=?,updated_at=? WHERE id=?", (status, now, job_id))

    # ------------------------------------------------------------------
    # 台账：事务内重算落库
    # ------------------------------------------------------------------
    def treaty_year_tx(self, connection: sqlite3.Connection, treaty_id: str) -> Dict[str, Any]:
        row = connection.execute("SELECT * FROM treaty_years WHERE treaty_id=?", (treaty_id,)).fetchone()
        if row is None:
            raise NotFound("合约年度不存在")
        return dict(row)

    def event_tx(self, connection: sqlite3.Connection, event_id: str) -> Optional[Dict[str, Any]]:
        row = connection.execute("SELECT * FROM cat_events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def claim_tx(self, connection: sqlite3.Connection, claim_id: int) -> Dict[str, Any]:
        row = connection.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if row is None:
            raise NotFound("赔案不存在")
        return dict(row)

    def treaty_claims_tx(self, connection: sqlite3.Connection, treaty_id: str) -> List[Dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM claims WHERE treaty_id=? AND state IN ('approved','stale','settled','pending_review') ORDER BY id",
            (treaty_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def event_claims_tx(self, connection: sqlite3.Connection, event_id: str) -> List[Dict[str, Any]]:
        rows = connection.execute("SELECT * FROM claims WHERE event_id=? ORDER BY id", (event_id,)).fetchall()
        return [dict(row) for row in rows]

    def update_claim_tx(self, connection: sqlite3.Connection, claim_id: int, fields: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> None:
        """事务内更新赔案并写审计；fields为绝对值赋值，重复执行不产生重复累计。"""
        if not fields:
            return
        now = _now()
        assignments = ",".join("%s=?" % key for key in fields)
        params = list(fields.values()) + [now, claim_id]
        connection.execute("UPDATE claims SET %s,version=version+1,updated_at=? WHERE id=?" % assignments, params)
        self._audit_tx(connection, "claim", str(claim_id), action, actor_id, details)

    def persist_recalc_tx(self, connection: sqlite3.Connection, treaty_id: str, job_id: Optional[int], reason: str, lines: List[Dict[str, Any]], totals: Dict[str, Any], actor_id: str) -> int:
        """把一次重算结果作为完整台账批次落库：分录、赔案行、合约年度计数同事务。"""
        now = _now()
        cursor = connection.execute(
            "INSERT INTO ledger_batches(treaty_id,job_id,reason,status,created_at) VALUES(?,?,?,'completed',?)",
            (treaty_id, job_id, reason, now),
        )
        batch_id = int(cursor.lastrowid)
        for seq, line in enumerate(lines):
            connection.execute(
                "INSERT INTO ledger_entries(batch_id,seq,claim_id,claim_number,occurrence_date,event_id,event_version,gross_recovery,reserved_amount,confirmed_amount,reinstatements_used,shortfall,entry_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id, seq, line["claim_id"], line["claim_number"], line["occurrence_date"], line["event_id"],
                    line.get("event_version"), line["gross_recovery"], line["reserved_amount"], line["confirmed_amount"],
                    line["reinstatements_used"], line["shortfall"], line["entry_status"],
                ),
            )
        connection.execute(
            "UPDATE treaty_years SET reserved_aggregate=?,confirmed_aggregate=?,reserved_reinstatements=?,confirmed_reinstatements=?,version=version+1,updated_at=? WHERE treaty_id=?",
            (
                totals["reserved_aggregate"], totals["confirmed_aggregate"],
                totals["reserved_reinstatements"], totals["confirmed_reinstatements"],
                now, treaty_id,
            ),
        )
        self._audit_tx(connection, "treaty_year", treaty_id, "recalculated", actor_id, {"batch_id": batch_id, "reason": reason, "lines": len(lines)})
        return batch_id

    def latest_batch_entries(self, treaty_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            batch = connection.execute(
                "SELECT * FROM ledger_batches WHERE treaty_id=? ORDER BY id DESC LIMIT 1", (treaty_id,)
            ).fetchone()
            if batch is None:
                return []
            rows = connection.execute(
                "SELECT * FROM ledger_entries WHERE batch_id=? ORDER BY seq", (batch["id"],)
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_ledger_entries(self, claim_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ledger_entries WHERE claim_id=? ORDER BY id", (claim_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 台账：审计
    # ------------------------------------------------------------------
    def _audit_tx(self, connection: sqlite3.Connection, entity_type: str, entity_id: str, action: str, actor_id: str, details: Dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO ledger_audit(entity_type,entity_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, action, actor_id, json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
        )

    def ledger_audit(self, entity_type: str, entity_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ledger_audit WHERE entity_type=? AND entity_id=? ORDER BY id",
                (entity_type, entity_id),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result
