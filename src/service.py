from __future__ import annotations

from typing import Any, Dict, Optional

from .audit import utc_now_dt
from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_int, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DEFAULT_VALID_YEARS, ENTITY,
                    MAX_VALID_YEARS, RECORD_ROLES, RENEWAL_CONCLUSIONS,
                    RENEWAL_CREATE_ROLES, RENEWAL_DECIDE_ROLES,
                    RENEWAL_DECISIONS, RENEWAL_ENTITY, RENEWAL_RESUBMIT_ROLES,
                    RENEWAL_REVIEW_ROLES, VIEW_ROLES, completion_blockers,
                    escalation_required, permit_effective, priority_score,
                    renewal_blockers, response_deadline_hours,
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
        facility = require_text(payload.get("facility"), "facility", 200)
        permit_type = require_text(payload.get("permit_type"), "permit_type", 100)
        valid_years = require_int(payload.get("valid_years", DEFAULT_VALID_YEARS),
                                  "valid_years", 1, MAX_VALID_YEARS)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, facility, permit_type, valid_years,
                                           external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "facility": facility, "permit_type": permit_type,
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

    def create_renewal(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        item_id = require_int(payload.get("item_id"), "item_id")
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.get_item(item_id)
        blockers = renewal_blockers(item, utc_now_dt())
        if blockers:
            raise ConflictError("；".join(blockers))
        if self.repository.find_open_renewal(item["facility"], item["permit_type"]):
            raise ConflictError("同一设施和许可类型已存在未结束的续期")
        renewal = self.repository.create_renewal(item, note, external_ref, actor)
        self.repository.append_audit("renewal_create", RENEWAL_ENTITY, renewal["id"],
                                     actor, {"item_id": item["id"],
                                             "facility": item["facility"],
                                             "permit_type": item["permit_type"]})
        return self._renewal_view(renewal)

    def review_renewal(self, renewal_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        conclusion = payload.get("conclusion")
        if conclusion not in RENEWAL_CONCLUSIONS:
            raise ValidationError("conclusion必须是pass或fail")
        detail = require_text(payload.get("detail"), "detail")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        renewal = self.repository.get_renewal(renewal_id)
        if renewal["status"] != "submitted":
            raise ConflictError("当前状态不能录入复核结论")
        target = "review_passed" if conclusion == "pass" else "correction"
        review = self.repository.record_review(renewal_id, conclusion, detail, target,
                                               expected_version, actor)
        self.repository.append_audit("renewal_review", RENEWAL_ENTITY, renewal_id,
                                     actor, {"conclusion": conclusion,
                                             "round": review["round"],
                                             "target": target})
        return self._renewal_view(self.repository.get_renewal(renewal_id))

    def resubmit_renewal(self, renewal_id: int, payload: Dict[str, Any], actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_RESUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        detail = require_text(payload.get("detail"), "detail")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        renewal = self.repository.get_renewal(renewal_id)
        if renewal["status"] != "correction":
            raise ConflictError("续期不在整改状态，不能重新提交")
        updated = self.repository.transition_renewal(renewal_id, "submitted",
                                                     expected_version, "correction", actor)
        self.repository.append_audit("renewal_resubmit", RENEWAL_ENTITY, renewal_id,
                                     actor, {"detail": detail})
        return self._renewal_view(updated)

    def decide_renewal(self, renewal_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_DECIDE_ROLES)
        actor = require_text(actor, "actor", 100)
        decision = payload.get("decision")
        if decision not in RENEWAL_DECISIONS:
            raise ValidationError("decision必须是reissue或reject")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        renewal = self.repository.get_renewal(renewal_id)
        if renewal["status"] != "review_passed":
            raise ConflictError("复核未通过或续期已结束，不能换发")
        if decision == "reject":
            updated = self.repository.transition_renewal(renewal_id, "rejected",
                                                         expected_version,
                                                         "review_passed", actor)
            self.repository.append_audit("renewal_decide", RENEWAL_ENTITY, renewal_id,
                                         actor, {"decision": "reject"})
            return {"renewal": self._renewal_view(updated)}
        valid_years = payload.get("valid_years")
        if valid_years is not None:
            valid_years = require_int(valid_years, "valid_years", 1, MAX_VALID_YEARS)
        result = self.repository.reissue_permit(renewal, expected_version,
                                                valid_years, actor)
        new_item, old_item = result["new_item"], result["old_item"]
        self.repository.append_audit("renewal_reissue", RENEWAL_ENTITY, renewal_id,
                                     actor, {"old_item_id": old_item["id"],
                                             "new_item_id": new_item["id"],
                                             "permit_no": new_item["permit_no"],
                                             "valid_from": new_item["valid_from"],
                                             "valid_to": new_item["valid_to"]})
        self.repository.append_audit("create", ENTITY, new_item["id"], actor, {
            "title": new_item["title"], "facility": new_item["facility"],
            "permit_type": new_item["permit_type"], "permit_no": new_item["permit_no"],
            "renewal_id": renewal_id,
        })
        return {"renewal": self._renewal_view(result["renewal"]),
                "new_item": self.enrich(new_item), "old_item": self.enrich(old_item)}

    def get_renewal(self, renewal_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._renewal_view(self.repository.get_renewal(renewal_id))

    def list_renewals(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self._renewal_view(renewal)
                for renewal in self.repository.list_renewals(status)]

    def _renewal_view(self, renewal: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(renewal)
        result["reviews"] = self.repository.list_renewal_reviews(renewal["id"])
        return result

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["effective"] = permit_effective(item)
        return result
