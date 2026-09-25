import tempfile, unittest
from datetime import timedelta
from pathlib import Path
from src.audit import utc_now_dt
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class RenewalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _approved_item(self, facility="一号焚烧线", permit_type="大气排污许可",
                       valid_years=5, external_ref=None):
        payload = {"title": "permit", "description": "renewal base",
                   "severity": "high", "quantity": 12, "threshold": 6,
                   "facility": facility, "permit_type": permit_type,
                   "valid_years": valid_years, "external_ref": external_ref}
        item = self.service.create_item(payload, "creator", "applicant")
        for target in STATES[1:]:
            item = self.service.transition(item["id"], target, item["version"],
                                           "reviewer", TRANSITION_ROLES[target][0])
        return self.service.get_item(item["id"], "viewer")

    def _expire_item(self, item_id):
        past = (utc_now_dt() - timedelta(days=1)).isoformat()
        with self.repo.conn:
            self.repo.conn.execute("UPDATE items SET valid_to=? WHERE id=?",
                                   (past, item_id))

    def test_full_renewal_flow_reissues_permit(self):
        item = self._approved_item(external_ref="RN-BASE")
        self.assertTrue(item["permit_no"])
        self.assertIsNotNone(item["valid_to"])
        self.assertTrue(item["effective"])

        renewal = self.service.create_renewal(
            {"item_id": item["id"], "external_ref": "RN-1"}, "applicant1", "applicant")
        self.assertEqual(renewal["status"], "submitted")
        self.assertEqual(renewal["reviews"], [])

        renewal = self.service.review_renewal(
            renewal["id"], {"conclusion": "fail", "detail": "现场排放超标",
                            "expected_version": renewal["version"]},
            "inspector1", "inspector")
        self.assertEqual(renewal["status"], "correction")
        self.assertEqual(len(renewal["reviews"]), 1)

        renewal = self.service.resubmit_renewal(
            renewal["id"], {"detail": "已补充治理方案",
                            "expected_version": renewal["version"]},
            "applicant1", "applicant")
        self.assertEqual(renewal["status"], "submitted")
        self.assertEqual(len(renewal["reviews"]), 1)
        self.assertEqual(renewal["reviews"][0]["conclusion"], "fail")

        renewal = self.service.review_renewal(
            renewal["id"], {"conclusion": "pass", "detail": "复核通过",
                            "expected_version": renewal["version"]},
            "inspector1", "inspector")
        self.assertEqual(renewal["status"], "review_passed")
        self.assertEqual(len(renewal["reviews"]), 2)

        result = self.service.decide_renewal(
            renewal["id"], {"decision": "reissue",
                            "expected_version": renewal["version"]},
            "manager1", "compliance_manager")
        new_item, old_item = result["new_item"], result["old_item"]
        self.assertEqual(result["renewal"]["status"], "completed")
        self.assertEqual(result["renewal"]["new_item_id"], new_item["id"])
        self.assertEqual(old_item["lifecycle"], "replaced")
        self.assertFalse(old_item["effective"])
        self.assertEqual(new_item["status"], "approved")
        self.assertEqual(new_item["lifecycle"], "active")
        self.assertTrue(new_item["effective"])
        self.assertNotEqual(new_item["permit_no"], old_item["permit_no"])
        self.assertIsNotNone(new_item["valid_from"])
        self.assertIsNotNone(new_item["valid_to"])

        items = self.service.list_items("viewer", status="approved")
        active = [i for i in items if i["facility"] == "一号焚烧线"
                  and i["lifecycle"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], new_item["id"])
        listed = {i["id"]: i for i in self.service.list_items("viewer")}
        self.assertEqual(listed[new_item["id"]]["permit_no"], new_item["permit_no"])
        self.assertEqual(listed[new_item["id"]]["valid_to"], new_item["valid_to"])
        self.assertEqual(listed[old_item["id"]]["lifecycle"], "replaced")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_duplicate_decide_cannot_create_second_permit(self):
        item = self._approved_item()
        renewal = self.service.create_renewal({"item_id": item["id"]},
                                              "applicant1", "applicant")
        renewal = self.service.review_renewal(
            renewal["id"], {"conclusion": "pass", "detail": "ok",
                            "expected_version": renewal["version"]},
            "inspector1", "inspector")
        result = self.service.decide_renewal(
            renewal["id"], {"decision": "reissue",
                            "expected_version": renewal["version"]},
            "manager1", "compliance_manager")
        with self.assertRaises(ConflictError):
            self.service.decide_renewal(
                renewal["id"], {"decision": "reissue",
                                "expected_version": renewal["version"]},
                "manager1", "compliance_manager")
        active = [i for i in self.service.list_items("viewer", status="approved")
                  if i["lifecycle"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], result["new_item"]["id"])
        renewals = self.service.list_renewals("viewer")
        self.assertEqual(len(renewals), 1)
        self.assertEqual(renewals[0]["status"], "completed")

    def test_open_renewal_unique_per_facility_and_type(self):
        item = self._approved_item()
        self.service.create_renewal({"item_id": item["id"], "external_ref": "RN-A"},
                                    "applicant1", "applicant")
        with self.assertRaises(ConflictError):
            self.service.create_renewal({"item_id": item["id"], "external_ref": "RN-B"},
                                        "applicant1", "applicant")
        with self.assertRaises(ConflictError):
            self.service.create_renewal({"item_id": item["id"], "external_ref": "RN-A"},
                                        "applicant1", "applicant")
        other_type = self._approved_item(permit_type="水排污许可")
        renewal = self.service.create_renewal({"item_id": other_type["id"]},
                                              "applicant1", "applicant")
        self.assertEqual(renewal["status"], "submitted")

    def test_renewal_requires_valid_permit(self):
        draft = self.service.create_item(
            {"title": "draft", "description": "not approved", "severity": "low",
             "quantity": 1, "threshold": 10, "facility": "三号车间",
             "permit_type": "大气排污许可"}, "creator", "applicant")
        with self.assertRaises(ConflictError):
            self.service.create_renewal({"item_id": draft["id"]},
                                        "applicant1", "applicant")
        expired = self._approved_item(facility="四号车间")
        self._expire_item(expired["id"])
        with self.assertRaises(ConflictError):
            self.service.create_renewal({"item_id": expired["id"]},
                                        "applicant1", "applicant")

    def test_only_one_active_permit_per_facility_and_type(self):
        first = self._approved_item()
        second = self.service.create_item(
            {"title": "second", "description": "same facility", "severity": "low",
             "quantity": 1, "threshold": 10, "facility": first["facility"],
             "permit_type": first["permit_type"]}, "creator", "applicant")
        current = second
        for target in STATES[1:-1]:
            current = self.service.transition(current["id"], target,
                                              current["version"], "reviewer",
                                              TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], STATES[-1], current["version"],
                                    "reviewer", TRANSITION_ROLES[STATES[-1]][0])

    def test_reject_ends_renewal_and_allows_new_one(self):
        item = self._approved_item()
        renewal = self.service.create_renewal({"item_id": item["id"]},
                                              "applicant1", "applicant")
        renewal = self.service.review_renewal(
            renewal["id"], {"conclusion": "pass", "detail": "ok",
                            "expected_version": renewal["version"]},
            "inspector1", "inspector")
        result = self.service.decide_renewal(
            renewal["id"], {"decision": "reject",
                            "expected_version": renewal["version"]},
            "manager1", "compliance_manager")
        self.assertEqual(result["renewal"]["status"], "rejected")
        again = self.service.create_renewal({"item_id": item["id"]},
                                            "applicant1", "applicant")
        self.assertEqual(again["status"], "submitted")
        active = [i for i in self.service.list_items("viewer", status="approved")
                  if i["lifecycle"] == "active"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], item["id"])

    def test_role_and_state_guards(self):
        item = self._approved_item()
        with self.assertRaises(PermissionDenied):
            self.service.create_renewal({"item_id": item["id"]}, "x", "inspector")
        renewal = self.service.create_renewal({"item_id": item["id"]},
                                              "applicant1", "applicant")
        with self.assertRaises(PermissionDenied):
            self.service.review_renewal(
                renewal["id"], {"conclusion": "pass", "detail": "ok",
                                "expected_version": 1}, "x", "applicant")
        with self.assertRaises(ValidationError):
            self.service.review_renewal(
                renewal["id"], {"conclusion": "maybe", "detail": "ok",
                                "expected_version": 1}, "x", "inspector")
        with self.assertRaises(ConflictError):
            self.service.resubmit_renewal(
                renewal["id"], {"detail": "补充", "expected_version": 1},
                "applicant1", "applicant")
        with self.assertRaises(ConflictError):
            self.service.decide_renewal(
                renewal["id"], {"decision": "reissue", "expected_version": 1},
                "manager1", "compliance_manager")
        with self.assertRaises(ConflictError):
            self.service.review_renewal(
                renewal["id"], {"conclusion": "pass", "detail": "ok",
                                "expected_version": 99}, "inspector1", "inspector")

    def test_review_records_retained_after_resubmit(self):
        item = self._approved_item()
        renewal = self.service.create_renewal({"item_id": item["id"]},
                                              "applicant1", "applicant")
        renewal = self.service.review_renewal(
            renewal["id"], {"conclusion": "fail", "detail": "第一次复核不通过",
                            "expected_version": renewal["version"]},
            "inspector1", "inspector")
        renewal = self.service.resubmit_renewal(
            renewal["id"], {"detail": "补充材料", "expected_version": renewal["version"]},
            "applicant1", "applicant")
        renewal = self.service.review_renewal(
            renewal["id"], {"conclusion": "fail", "detail": "第二次仍不通过",
                            "expected_version": renewal["version"]},
            "inspector1", "inspector")
        detail = self.service.get_renewal(renewal["id"], "viewer")
        self.assertEqual([r["round"] for r in detail["reviews"]], [1, 2])
        self.assertEqual(detail["reviews"][0]["detail"], "第一次复核不通过")
        self.assertEqual(detail["reviews"][1]["detail"], "第二次仍不通过")


if __name__ == "__main__":
    unittest.main()
