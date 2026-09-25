from __future__ import annotations

import json
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now, utc_now_dt
from .domain import ConflictError, NotFoundError
from .rules import (DEFAULT_VALID_YEARS, ID_PREFIX, RENEWAL_OPEN_STATES,
                    STATES, TERMINAL_STATES)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_tables()
        self._migrate()
        self._create_indexes()

    def _create_tables(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        renewal_statuses = ",".join("'" + s + "'" for s in
                                    ['submitted', 'correction', 'review_passed',
                                     'completed', 'rejected'])
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    facility TEXT NOT NULL DEFAULT '',
                    permit_type TEXT NOT NULL DEFAULT '',
                    permit_no TEXT,
                    valid_from TEXT,
                    valid_to TEXT,
                    valid_years INTEGER NOT NULL DEFAULT {DEFAULT_VALID_YEARS},
                    lifecycle TEXT NOT NULL DEFAULT 'active'
                        CHECK(lifecycle IN ('active','replaced')),
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
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
                CREATE TABLE IF NOT EXISTS renewals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    facility TEXT NOT NULL,
                    permit_type TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({renewal_statuses})),
                    note TEXT,
                    external_ref TEXT,
                    new_item_id INTEGER REFERENCES items(id),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS renewal_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    renewal_id INTEGER NOT NULL REFERENCES renewals(id) ON DELETE CASCADE,
                    round INTEGER NOT NULL,
                    conclusion TEXT NOT NULL CHECK(conclusion IN ('pass','fail')),
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

    def _create_indexes(self) -> None:
        open_renewals = ",".join("'" + s + "'" for s in RENEWAL_OPEN_STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_permit_no
                    ON items(permit_no) WHERE permit_no IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_active_permit
                    ON items(facility, permit_type)
                    WHERE status='approved' AND lifecycle='active';
                CREATE UNIQUE INDEX IF NOT EXISTS ux_renewals_external_ref
                    ON renewals(external_ref) WHERE external_ref IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS ux_renewals_open
                    ON renewals(facility, permit_type)
                    WHERE status IN ({open_renewals});
                CREATE UNIQUE INDEX IF NOT EXISTS ux_renewals_new_item
                    ON renewals(new_item_id) WHERE new_item_id IS NOT NULL;
            """)

    def _migrate(self) -> None:
        columns = {row["name"] for row in
                   self.conn.execute("PRAGMA table_info(items)").fetchall()}
        additions = {
            "facility": "ALTER TABLE items ADD COLUMN facility TEXT NOT NULL DEFAULT ''",
            "permit_type": "ALTER TABLE items ADD COLUMN permit_type TEXT NOT NULL DEFAULT ''",
            "permit_no": "ALTER TABLE items ADD COLUMN permit_no TEXT",
            "valid_from": "ALTER TABLE items ADD COLUMN valid_from TEXT",
            "valid_to": "ALTER TABLE items ADD COLUMN valid_to TEXT",
            "valid_years": f"ALTER TABLE items ADD COLUMN valid_years INTEGER NOT NULL DEFAULT {DEFAULT_VALID_YEARS}",
            "lifecycle": "ALTER TABLE items ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'active'",
        }
        with self.conn:
            for name, sql in additions.items():
                if name not in columns:
                    self.conn.execute(sql)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, facility: str,
                    permit_type: str, valid_years: int,
                    external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       facility, permit_type, valid_years, status, version, external_ref,
                       created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, facility,
                     permit_type, valid_years, STATES[0], 1, external_ref, actor, now, now),
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
        now_dt = utc_now_dt()
        now = now_dt.isoformat()
        try:
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
                if target in TERMINAL_STATES:
                    row = self.conn.execute(
                        "SELECT valid_years FROM items WHERE id=?", (item_id,)).fetchone()
                    years = int(row["valid_years"]) if row and row["valid_years"] else DEFAULT_VALID_YEARS
                    valid_to = (now_dt + timedelta(days=365 * years)).isoformat()
                    self.conn.execute(
                        """UPDATE items SET permit_no=?, valid_from=?, valid_to=?
                           WHERE id=? AND permit_no IS NULL""",
                        (f"{ID_PREFIX}-{item_id:06d}", now, valid_to, item_id),
                    )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("同一设施和许可类型只能保留一张有效证") from exc
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

    def create_renewal(self, item: Dict[str, Any], note: Optional[str],
                       external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO renewals(item_id, facility, permit_type, status, note,
                       external_ref, version, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (item["id"], item["facility"], item["permit_type"], "submitted",
                     note, external_ref, 1, actor, now, now),
                )
                renewal_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            if "ux_renewals_open" in str(exc):
                raise ConflictError("同一设施和许可类型已存在未结束的续期") from exc
            raise ConflictError("external_ref已存在") from exc
        return self.get_renewal(renewal_id)

    def get_renewal(self, renewal_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewals WHERE id=?", (renewal_id,)).fetchone()
        if row is None:
            raise NotFoundError("续期不存在")
        return dict(row)

    def list_renewals(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM renewals"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def find_open_renewal(self, facility: str, permit_type: str) -> Optional[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in RENEWAL_OPEN_STATES)
        with self._lock:
            row = self.conn.execute(
                f"""SELECT * FROM renewals WHERE facility=? AND permit_type=?
                    AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1""",
                (facility, permit_type, *RENEWAL_OPEN_STATES),
            ).fetchone()
        return dict(row) if row else None

    def list_renewal_reviews(self, renewal_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM renewal_reviews WHERE renewal_id=? ORDER BY id",
                (renewal_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_review(self, renewal_id: int, conclusion: str, detail: str,
                      target: str, expected_version: int,
                      actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE renewals SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=? AND status='submitted'""",
                (target, now, renewal_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM renewals WHERE id=?", (renewal_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("续期不存在")
                raise ConflictError("续期状态已变化，请刷新后重试")
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM renewal_reviews WHERE renewal_id=?",
                (renewal_id,),
            ).fetchone()
            round_no = int(row["n"]) + 1
            cur = self.conn.execute(
                """INSERT INTO renewal_reviews(renewal_id, round, conclusion, detail,
                   created_by, created_at) VALUES(?,?,?,?,?,?)""",
                (renewal_id, round_no, conclusion, detail, actor, now),
            )
            review_id = int(cur.lastrowid)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewal_reviews WHERE id=?", (review_id,)).fetchone()
        return dict(row)

    def transition_renewal(self, renewal_id: int, target: str, expected_version: int,
                           required_status: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE renewals SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=? AND status=?""",
                (target, now, renewal_id, expected_version, required_status),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM renewals WHERE id=?", (renewal_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("续期不存在")
                raise ConflictError("续期状态已变化，请刷新后重试")
        return self.get_renewal(renewal_id)

    def reissue_permit(self, renewal: Dict[str, Any], expected_version: int,
                       valid_years: Optional[int],
                       actor: str) -> Dict[str, Any]:
        old = self.get_item(renewal["item_id"])
        years = valid_years or int(old["valid_years"]) or DEFAULT_VALID_YEARS
        now_dt = utc_now_dt()
        now = now_dt.isoformat()
        valid_to = (now_dt + timedelta(days=365 * years)).isoformat()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """UPDATE items SET lifecycle='replaced', version=version+1, updated_at=?
                       WHERE id=? AND lifecycle='active'""",
                    (now, old["id"]),
                )
                if cur.rowcount == 0:
                    raise ConflictError("原证已失效，不能换发")
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       facility, permit_type, valid_years, status, version, external_ref,
                       lifecycle, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (old["title"], old["description"], old["severity"], old["quantity"],
                     old["threshold"], old["facility"], old["permit_type"], years,
                     STATES[-1], 1, None, "active", actor, now, now),
                )
                new_id = int(cur.lastrowid)
                self.conn.execute(
                    """UPDATE items SET permit_no=?, valid_from=?, valid_to=? WHERE id=?""",
                    (f"{ID_PREFIX}-{new_id:06d}", now, valid_to, new_id),
                )
                cur = self.conn.execute(
                    """UPDATE renewals SET status='completed', new_item_id=?,
                       version=version+1, updated_at=?
                       WHERE id=? AND version=? AND status='review_passed'""",
                    (new_id, now, renewal["id"], expected_version),
                )
                if cur.rowcount == 0:
                    raise ConflictError("续期状态已变化，请刷新后重试")
        except sqlite3.IntegrityError as exc:
            raise ConflictError("同一设施和许可类型只能保留一张有效证") from exc
        return {"new_item": self.get_item(new_id), "old_item": self.get_item(old["id"]),
                "renewal": self.get_renewal(renewal["id"])}

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
