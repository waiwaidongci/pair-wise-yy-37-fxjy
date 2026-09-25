from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (ID_PREFIX, OPEN_RENEWAL_STATES, PERMIT_STATES,
                    RENEWAL_STATES, STATES, generate_permit_no,
                    status_after_review)


def _state_list(values):
    return ",".join("'" + v.replace("'", "''") + "'" for v in values)


def _conflict(exc, mapping, fallback):
    text = str(exc)
    for key, message in mapping.items():
        if key in text:
            return ConflictError(message)
    return ConflictError(fallback)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = _state_list(STATES)
        permit_statuses = _state_list(PERMIT_STATES)
        renewal_statuses = _state_list(RENEWAL_STATES)
        open_renewal_statuses = _state_list(OPEN_RENEWAL_STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS permits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    permit_no TEXT NOT NULL UNIQUE,
                    facility TEXT NOT NULL,
                    permit_type TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({permit_statuses})),
                    valid_from TEXT NOT NULL,
                    valid_until TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_permits_external_ref
                    ON permits(external_ref) WHERE external_ref IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS ux_permits_one_active
                    ON permits(facility, permit_type) WHERE status='active';
                CREATE TABLE IF NOT EXISTS renewals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    permit_id INTEGER NOT NULL REFERENCES permits(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN ({renewal_statuses})),
                    note TEXT,
                    decision_reason TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_renewals_external_ref
                    ON renewals(external_ref) WHERE external_ref IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS ux_renewals_open
                    ON renewals(permit_id) WHERE status IN ({open_renewal_statuses});
                CREATE TABLE IF NOT EXISTS renewal_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    renewal_id INTEGER NOT NULL REFERENCES renewals(id) ON DELETE CASCADE,
                    conclusion TEXT NOT NULL CHECK(conclusion IN ('passed','failed')),
                    detail TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    _PERMIT_CONFLICT = {
        "permits.facility, permits.permit_type": "同一设施和许可类型只能保留一张有效证",
        "permits.permit_no": "证号已存在",
        "permits.external_ref": "external_ref已存在",
    }
    _RENEWAL_CONFLICT = {
        "renewals.permit_id": "同一许可不能同时存在两份未结束的续期",
        "renewals.external_ref": "external_ref已存在",
    }

    def create_permit(self, facility: str, permit_type: str, valid_from: str,
                      valid_until: str, permit_no: Optional[str],
                      external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO permits(permit_no, facility, permit_type, status,
                       valid_from, valid_until, version, external_ref,
                       created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (permit_no or "", facility, permit_type, "active",
                     valid_from, valid_until, 1, external_ref, actor, now, now),
                )
                permit_id = int(cur.lastrowid)
                if not permit_no:
                    self.conn.execute(
                        "UPDATE permits SET permit_no=? WHERE id=?",
                        (generate_permit_no(permit_id), permit_id),
                    )
        except sqlite3.IntegrityError as exc:
            raise _conflict(exc, self._PERMIT_CONFLICT, "许可唯一性约束冲突") from exc
        return self.get_permit(permit_id)

    def get_permit(self, permit_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM permits WHERE id=?", (permit_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("许可不存在")
        return dict(row)

    def list_permits(self, status: Optional[str] = None,
                     facility: Optional[str] = None,
                     permit_type: Optional[str] = None) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        if facility:
            clauses.append("facility=?")
            params.append(facility)
        if permit_type:
            clauses.append("permit_type=?")
            params.append(permit_type)
        sql = "SELECT * FROM permits"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def create_renewal(self, permit_id: int, note: Optional[str],
                       external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO renewals(permit_id, status, note, version,
                       external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (permit_id, "submitted", note, 1, external_ref, actor, now, now),
                )
                renewal_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise _conflict(exc, self._RENEWAL_CONFLICT, "续期唯一性约束冲突") from exc
        return self.get_renewal(renewal_id)

    def get_renewal(self, renewal_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """SELECT r.*, p.permit_no, p.facility, p.permit_type
                   FROM renewals r JOIN permits p ON p.id=r.permit_id
                   WHERE r.id=?""",
                (renewal_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("续期申请不存在")
        return dict(row)

    def list_renewals(self, permit_id: int) -> List[Dict[str, Any]]:
        self.get_permit(permit_id)
        with self._lock:
            rows = self.conn.execute(
                """SELECT r.*, p.permit_no, p.facility, p.permit_type
                   FROM renewals r JOIN permits p ON p.id=r.permit_id
                   WHERE r.permit_id=? ORDER BY r.id DESC""",
                (permit_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_review(self, renewal_id: int, conclusion: str, detail: str,
                   actor: str) -> tuple:
        now = utc_now()
        target = status_after_review(conclusion)
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE renewals SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND status='submitted'""",
                (target, now, renewal_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM renewals WHERE id=?", (renewal_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("续期申请不存在")
                raise ConflictError("当前状态不能记录复核结论")
            cur = self.conn.execute(
                """INSERT INTO renewal_reviews(renewal_id, conclusion, detail,
                   created_by, created_at) VALUES(?,?,?,?,?)""",
                (renewal_id, conclusion, detail, actor, now),
            )
            review_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewal_reviews WHERE id=?", (review_id,)
            ).fetchone()
        return dict(row), self.get_renewal(renewal_id)

    def list_reviews(self, renewal_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM renewal_reviews WHERE renewal_id=? ORDER BY id",
                (renewal_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def resubmit_renewal(self, renewal_id: int, note: Optional[str],
                         actor: str) -> Dict[str, Any]:
        del actor
        now = utc_now()
        with self._lock, self.conn:
            if note is None:
                cur = self.conn.execute(
                    """UPDATE renewals SET status='submitted', version=version+1,
                       updated_at=? WHERE id=? AND status='rectification'""",
                    (now, renewal_id),
                )
            else:
                cur = self.conn.execute(
                    """UPDATE renewals SET status='submitted', note=?,
                       version=version+1, updated_at=?
                       WHERE id=? AND status='rectification'""",
                    (note, now, renewal_id),
                )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM renewals WHERE id=?", (renewal_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("续期申请不存在")
                raise ConflictError("只有整改中的续期才能补充材料后重新提交")
        return self.get_renewal(renewal_id)

    def reject_renewal(self, renewal_id: int, reason: Optional[str]) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE renewals SET status='rejected', decision_reason=?,
                   version=version+1, updated_at=?
                   WHERE id=? AND status='reviewed'""",
                (reason, now, renewal_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM renewals WHERE id=?", (renewal_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError("续期申请不存在")
                raise ConflictError("当前状态不能作出不予换发决定")
        return self.get_renewal(renewal_id)

    def reissue_permit(self, renewal_id: int, valid_from: str, valid_until: str,
                       permit_no: Optional[str], external_ref: Optional[str],
                       actor: str) -> tuple:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                """SELECT r.id AS renewal_id, r.status AS renewal_status,
                          r.permit_id AS permit_id, p.permit_no AS permit_no,
                          p.facility AS facility, p.permit_type AS permit_type,
                          p.status AS permit_status
                   FROM renewals r JOIN permits p ON p.id=r.permit_id
                   WHERE r.id=?""",
                (renewal_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("续期申请不存在")
            if row["renewal_status"] == "reissued":
                raise ConflictError("该续期已换发，重复请求不会生成新证")
            if row["renewal_status"] != "reviewed":
                raise ConflictError("续期尚未复核通过，不能换发")
            if row["permit_status"] != "active":
                raise ConflictError("原证已失效，不能换发")
            claimed = self.conn.execute(
                """UPDATE renewals SET status='reissued', version=version+1,
                   updated_at=? WHERE id=? AND status='reviewed'""",
                (now, renewal_id),
            )
            if claimed.rowcount == 0:
                raise ConflictError("续期状态已变化，请刷新后重试")
            invalidated = self.conn.execute(
                """UPDATE permits SET status='invalidated', version=version+1,
                   updated_at=? WHERE id=? AND status='active'""",
                (now, row["permit_id"]),
            )
            if invalidated.rowcount == 0:
                raise ConflictError("原证已失效，不能换发")
            try:
                cur = self.conn.execute(
                    """INSERT INTO permits(permit_no, facility, permit_type, status,
                       valid_from, valid_until, version, external_ref,
                       created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (permit_no or "", row["facility"], row["permit_type"], "active",
                     valid_from, valid_until, 1, external_ref, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise _conflict(exc, self._PERMIT_CONFLICT, "许可唯一性约束冲突") from exc
            new_permit_id = int(cur.lastrowid)
            if not permit_no:
                self.conn.execute(
                    "UPDATE permits SET permit_no=? WHERE id=?",
                    (generate_permit_no(new_permit_id), new_permit_id),
                )
        return (self.get_permit(new_permit_id),
                self.get_permit(row["permit_id"]),
                self.get_renewal(renewal_id))

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
