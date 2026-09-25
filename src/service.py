from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import utc_now
from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, parse_datetime, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DECIDE_ROLES, ENTITY,
                    PERMIT_CREATE_ROLES, PERMIT_ENTITY, PERMIT_STATES,
                    RENEWAL_CREATE_ROLES, RENEWAL_ENTITY,
                    REVIEW_RECORD_ROLES, RESUBMIT_ROLES, RECORD_ROLES,
                    TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, normalize_conclusion, permit_expired,
                    priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def _validity_window(self, payload):
        valid_from = parse_datetime(payload.get("valid_from"), "valid_from") or utc_now()
        valid_until = parse_datetime(payload.get("valid_until"), "valid_until", required=True)
        if valid_until <= valid_from:
            raise ValidationError("valid_until必须晚于valid_from")
        return valid_from, valid_until

    @staticmethod
    def _optional_text(payload, field, max_length=2000):
        value = payload.get(field)
        if value is not None:
            value = require_text(value, field, max_length)
        return value

    def create_permit(self, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, PERMIT_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        facility = require_text(payload.get("facility"), "facility", 200)
        permit_type = require_text(payload.get("permit_type"), "permit_type", 100)
        valid_from, valid_until = self._validity_window(payload)
        permit_no = self._optional_text(payload, "permit_no", 50)
        external_ref = self._optional_text(payload, "external_ref", 100)
        permit = self.repository.create_permit(
            facility, permit_type, valid_from, valid_until,
            permit_no, external_ref, actor)
        self.repository.append_audit("permit_create", PERMIT_ENTITY, permit["id"], actor, {
            "permit_no": permit["permit_no"], "facility": facility,
            "permit_type": permit_type, "valid_from": valid_from,
            "valid_until": valid_until, "source": "issue",
        })
        return self.enrich_permit(permit)

    def list_permits(self, role: str, status: Optional[str] = None,
                     facility: Optional[str] = None,
                     permit_type: Optional[str] = None) -> list:
        self._view(role)
        if status is not None and status not in PERMIT_STATES:
            raise ValidationError("未知许可状态")
        return [self.enrich_permit(item)
                for item in self.repository.list_permits(status, facility, permit_type)]

    def get_permit(self, permit_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_permit(self.repository.get_permit(permit_id))

    def initiate_renewal(self, permit_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        permit = self.repository.get_permit(permit_id)
        if permit["status"] != "active":
            raise ConflictError("原证已失效，不能发起续期")
        if permit_expired(permit["valid_until"]):
            raise ConflictError("原证已过有效期，必须在原证失效前发起续期")
        note = self._optional_text(payload, "note")
        external_ref = self._optional_text(payload, "external_ref", 100)
        renewal = self.repository.create_renewal(permit_id, note, external_ref, actor)
        self.repository.append_audit("renewal_initiate", RENEWAL_ENTITY, renewal["id"], actor, {
            "permit_id": permit_id, "permit_no": permit["permit_no"],
        })
        return renewal

    def list_renewals(self, permit_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_renewals(permit_id)

    def get_renewal(self, renewal_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        renewal = self.repository.get_renewal(renewal_id)
        renewal["reviews"] = self.repository.list_reviews(renewal_id)
        return renewal

    def record_review(self, renewal_id: int, payload: Dict[str, Any],
                      actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, REVIEW_RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        conclusion = normalize_conclusion(payload.get("conclusion"))
        detail = require_text(payload.get("detail"), "detail")
        review, renewal = self.repository.add_review(renewal_id, conclusion, detail, actor)
        self.repository.append_audit("renewal_review", RENEWAL_ENTITY, renewal_id, actor, {
            "review_id": review["id"], "conclusion": conclusion,
            "permit_id": renewal["permit_id"], "renewal_status": renewal["status"],
        })
        return review

    def resubmit_renewal(self, renewal_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RESUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        note = self._optional_text(payload, "note")
        renewal = self.repository.resubmit_renewal(renewal_id, note, actor)
        self.repository.append_audit("renewal_resubmit", RENEWAL_ENTITY, renewal_id, actor, {
            "permit_id": renewal["permit_id"], "retained_reviews": True,
        })
        return renewal

    def reissue(self, renewal_id: int, payload: Dict[str, Any],
                actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DECIDE_ROLES)
        actor = require_text(actor, "actor", 100)
        valid_from, valid_until = self._validity_window(payload)
        permit_no = self._optional_text(payload, "permit_no", 50)
        external_ref = self._optional_text(payload, "external_ref", 100)
        new_permit, old_permit, renewal = self.repository.reissue_permit(
            renewal_id, valid_from, valid_until, permit_no, external_ref, actor)
        self.repository.append_audit("renewal_reissue", RENEWAL_ENTITY, renewal_id, actor, {
            "permit_id": old_permit["id"], "old_permit_no": old_permit["permit_no"],
            "new_permit_id": new_permit["id"], "new_permit_no": new_permit["permit_no"],
        })
        self.repository.append_audit("permit_create", PERMIT_ENTITY, new_permit["id"], actor, {
            "permit_no": new_permit["permit_no"], "facility": new_permit["facility"],
            "permit_type": new_permit["permit_type"], "valid_from": valid_from,
            "valid_until": valid_until, "source": "reissue",
            "renewal_id": renewal_id, "previous_permit_id": old_permit["id"],
        })
        result = self.enrich_permit(new_permit)
        result["renewal_id"] = renewal_id
        result["replaced_permit_id"] = old_permit["id"]
        return result

    def reject_renewal(self, renewal_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DECIDE_ROLES)
        actor = require_text(actor, "actor", 100)
        reason = self._optional_text(payload, "reason")
        renewal = self.repository.reject_renewal(renewal_id, reason)
        self.repository.append_audit("renewal_reject", RENEWAL_ENTITY, renewal_id, actor, {
            "permit_id": renewal["permit_id"], "reason": reason,
        })
        return renewal

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result

    @staticmethod
    def enrich_permit(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["expired"] = (item["status"] == "active"
                             and permit_expired(item["valid_until"]))
        return result
