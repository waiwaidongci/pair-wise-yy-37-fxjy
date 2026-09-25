import tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src.domain import (ConflictError, NotFoundError, PermissionDenied,
                        ValidationError)
from src.repository import Repository
from src.service import Service


def iso(days=0):
    return ((datetime.now(timezone.utc) + timedelta(days=days))
            .replace(microsecond=0).isoformat())


class RenewalTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def make_permit(self, facility="一号焚烧炉", permit_type="大气排放",
                    valid_until=None, **extra):
        payload = {"facility": facility, "permit_type": permit_type,
                   "valid_from": iso(-30), "valid_until": valid_until or iso(30)}
        payload.update(extra)
        return self.service.create_permit(payload, "manager", "compliance_manager")

    def renew(self, permit_id, actor="applicant-1", role="applicant", **extra):
        return self.service.initiate_renewal(permit_id, extra, actor, role)

    def review(self, renewal_id, conclusion="passed", detail="复核完成",
               actor="inspector-1", role="inspector"):
        return self.service.record_review(
            renewal_id, {"conclusion": conclusion, "detail": detail}, actor, role)


class RenewalFlowTest(RenewalTestBase):
    def test_full_flow_rectification_then_reissue(self):
        permit = self.make_permit(external_ref="P-1")
        self.assertEqual(permit["status"], "active")
        self.assertTrue(permit["permit_no"].startswith("AQ-"))
        self.assertFalse(permit["expired"])

        renewal = self.renew(permit["id"], note="证件即将到期", external_ref="R-1")
        self.assertEqual(renewal["status"], "submitted")
        self.assertEqual(renewal["permit_no"], permit["permit_no"])

        # 检查员记录复核结论：不通过 -> 整改
        failed = self.review(renewal["id"], "failed", "台账不全")
        self.assertEqual(failed["conclusion"], "failed")
        renewal = self.service.get_renewal(renewal["id"], "viewer")
        self.assertEqual(renewal["status"], "rectification")
        self.assertEqual(len(renewal["reviews"]), 1)

        # 申请人补充材料后重新提交，原复核记录仍保留
        renewal = self.service.resubmit_renewal(
            renewal["id"], {"note": "已补充台账"}, "applicant-1", "applicant")
        self.assertEqual(renewal["status"], "submitted")
        detail = self.service.get_renewal(renewal["id"], "viewer")
        self.assertEqual(len(detail["reviews"]), 1)
        self.assertEqual(detail["reviews"][0]["conclusion"], "failed")

        # 复核通过后合规经理决定换发
        self.review(renewal["id"], "passed", "现场复核通过")
        new_permit = self.service.reissue(
            renewal["id"], {"valid_until": iso(365)}, "manager",
            "compliance_manager")
        self.assertEqual(new_permit["status"], "active")
        self.assertNotEqual(new_permit["permit_no"], permit["permit_no"])
        self.assertEqual(new_permit["valid_until"], iso(365))
        self.assertEqual(new_permit["renewal_id"], renewal["id"])

        # 旧证立即失效，新证号/有效期/状态在列表可见
        old = self.service.get_permit(permit["id"], "viewer")
        self.assertEqual(old["status"], "invalidated")
        permits = self.service.list_permits("viewer")
        active = [p for p in permits if p["status"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["permit_no"], new_permit["permit_no"])
        self.assertEqual(active[0]["valid_until"], iso(365))

        final = self.service.get_renewal(renewal["id"], "viewer")
        self.assertEqual(final["status"], "reissued")
        self.assertEqual(len(final["reviews"]), 2)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_duplicate_renewal_blocked_while_open(self):
        permit = self.make_permit()
        self.renew(permit["id"])
        with self.assertRaises(ConflictError):
            self.renew(permit["id"], external_ref="R-2")

    def test_renewal_must_start_before_expiry(self):
        expired = self.make_permit(valid_until=iso(-1))
        self.assertTrue(expired["expired"])
        with self.assertRaises(ConflictError):
            self.renew(expired["id"])

    def test_duplicate_reissue_does_not_create_second_permit(self):
        permit = self.make_permit()
        renewal = self.renew(permit["id"])
        self.review(renewal["id"], "passed", "ok")
        first = self.service.reissue(
            renewal["id"], {"valid_until": iso(365)}, "manager",
            "compliance_manager")
        with self.assertRaises(ConflictError):
            self.service.reissue(
                renewal["id"], {"valid_until": iso(365)}, "manager",
                "compliance_manager")
        permits = self.service.list_permits("viewer")
        self.assertEqual(len(permits), 2)
        self.assertEqual(len([p for p in permits if p["status"] == "active"]), 1)
        self.assertEqual(len([p for p in permits if p["permit_no"] == first["permit_no"]]), 1)
        # 旧证上不能再发起续期
        with self.assertRaises(ConflictError):
            self.renew(permit["id"])

    def test_one_active_permit_per_facility_and_type(self):
        self.make_permit()
        with self.assertRaises(ConflictError):
            self.make_permit(external_ref="P-2")
        other_type = self.make_permit(permit_type="废水排放", external_ref="P-3")
        self.assertEqual(other_type["status"], "active")
        other_facility = self.make_permit(
            facility="二号焚烧炉", external_ref="P-4")
        self.assertEqual(other_facility["status"], "active")

    def test_reject_then_new_renewal_allowed(self):
        permit = self.make_permit()
        renewal = self.renew(permit["id"])
        self.review(renewal["id"], "passed", "ok")
        rejected = self.service.reject_renewal(
            renewal["id"], {"reason": "产能调整"}, "manager",
            "compliance_manager")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["decision_reason"], "产能调整")
        again = self.renew(permit["id"], external_ref="R-AGAIN")
        self.assertEqual(again["status"], "submitted")
        self.assertTrue(self.repo.verify_audit_chain())


class RenewalGuardTest(RenewalTestBase):
    def test_roles_and_state_guards(self):
        permit = self.make_permit()
        with self.assertRaises(PermissionDenied):
            self.service.create_permit(
                {"facility": "x", "permit_type": "y"}, "x", "applicant")
        with self.assertRaises(PermissionDenied):
            self.service.initiate_renewal(permit["id"], {}, "x", "inspector")

        renewal = self.renew(permit["id"])
        with self.assertRaises(PermissionDenied):
            self.review(renewal["id"], role="applicant")
        with self.assertRaises(PermissionDenied):
            self.service.reissue(
                renewal["id"], {"valid_until": iso(1)}, "x", "inspector")
        with self.assertRaises(PermissionDenied):
            self.service.resubmit_renewal(renewal["id"], {}, "x", "inspector")

        # 未复核通过不能换发；未进入整改不能重新提交
        with self.assertRaises(ConflictError):
            self.service.reissue(
                renewal["id"], {"valid_until": iso(1)}, "m",
                "compliance_manager")
        with self.assertRaises(ConflictError):
            self.service.resubmit_renewal(renewal["id"], {}, "a", "applicant")

        # 一轮只能记录一次复核结论
        self.review(renewal["id"], "failed", "bad")
        with self.assertRaises(ConflictError):
            self.review(renewal["id"], "passed", "ok")

        # 整改重新提交后，新证在换发前仍只能有一张有效证
        self.service.resubmit_renewal(
            renewal["id"], {"note": "补充"}, "a", "applicant")
        self.review(renewal["id"], "passed", "ok")
        self.service.reissue(
            renewal["id"], {"valid_until": iso(365)}, "m",
            "compliance_manager")
        with self.assertRaises(ConflictError):
            self.service.reject_renewal(renewal["id"], {}, "m",
                                        "compliance_manager")

    def test_validation_and_not_found(self):
        with self.assertRaises(ValidationError):
            self.make_permit(valid_until="not-a-date")
        with self.assertRaises(ValidationError):
            self.make_permit(valid_from=iso(10), valid_until=iso(1))
        with self.assertRaises(NotFoundError):
            self.service.get_permit(999, "viewer")
        with self.assertRaises(NotFoundError):
            self.renew(999)
        with self.assertRaises(NotFoundError):
            self.review(999)
        permit = self.make_permit(facility="X炉")
        renewal = self.renew(permit["id"])
        with self.assertRaises(ValidationError):
            self.review(renewal["id"], "maybe", "x")

    def test_custom_permit_no_duplicate(self):
        first = self.make_permit(permit_no="CUSTOM-1")
        self.assertEqual(first["permit_no"], "CUSTOM-1")
        with self.assertRaises(ConflictError):
            self.make_permit(facility="二号炉", permit_no="CUSTOM-1",
                             external_ref="P-9")


if __name__ == "__main__":
    unittest.main()
