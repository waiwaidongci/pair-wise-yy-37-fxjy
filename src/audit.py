from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


def utc_now_dt() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_now() -> str:
    return utc_now_dt().isoformat()


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def calculate_hash(previous_hash: str, payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256((previous_hash + ":").encode("utf-8") + raw).hexdigest()


def make_entry(action: str, entity_type: str, entity_id: int, actor: str,
               detail: dict, previous_hash: str) -> dict:
    payload = {
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "actor": actor,
        "detail": detail,
        "created_at": utc_now(),
    }
    return dict(payload, previous_hash=previous_hash,
                entry_hash=calculate_hash(previous_hash, payload))
