import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src import rules as rules_module
from src.domain import ConflictError, DomainError, NotFoundError


def make_service():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    repo = Repository(tmp.name)
    repo.initialize()
    return tmp, repo, Service(repo)


def base_event(zones=None, concentration=20, limit=10):
    return {
        "source_id": "SRC-C",
        "contaminant": "nitrate",
        "detected_at": "2026-10-01T06:00:00+00:00",
        "concentration": concentration,
        "limit": limit,
        "zone_ids": zones or ["Z-1", "Z-2"],
        "population": 3000,
    }


def advance_to_disinfected(service, item):
    item = service.act(item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
    item = service.act(item["id"], "advise", {"notice_id": "N", "kind": "boil", "message": "煮沸"},
                       "disp-1", "dispatcher", item["version"])
    item = service.act(item["id"], "switch_source", {"alternate_source_id": "ALT"},
                       "coord-1", "coordinator", item["version"])
    for zone in item["payload"]["zone_ids"]:
        item = service.act(item["id"], "flush", {"zone_id": zone}, "field-1", "field_operator", item["version"])
    item = service.act(item["id"], "disinfect", {"zone_id": item["payload"]["zone_ids"][0], "completed": True},
                       "field-1", "field_operator", item["version"])
    return item


def register(service, item_id, bottle_no, zone="Z-1", seal=None, field="field-1"):
    return service.register_bottle(item_id, {
        "bottle_no": bottle_no, "zone_id": zone,
        "sampled_at": "2026-10-01T08:00:00+00:00", "seal_no": seal or ("SEAL-" + bottle_no),
    }, field, "field_operator")


def begin(service, bottle_id, request_id, actor, role, to_holder, to_role, expected_version=None):
    return service.begin_handoff(bottle_id, {
        "request_id": request_id, "to_holder": to_holder, "to_role": to_role,
    }, actor, role, expected_version)


def confirm(service, bottle_id, request_id, actor, role, seal, to_holder=None, to_role=None):
    return service.confirm_handoff(bottle_id, {
        "request_id": request_id, "to_holder": to_holder or actor, "to_role": to_role or role,
        "observed_seal": seal,
    }, actor, role)


def deliver(service, bottle_id, seal, field="field-1", courier="courier-1", lab="lab-1"):
    begin(service, bottle_id, "R1-" + str(bottle_id), field, "field_operator", courier, "courier")
    confirm(service, bottle_id, "R1-" + str(bottle_id), courier, "courier", seal)
    begin(service, bottle_id, "R2-" + str(bottle_id), courier, "courier", lab, "lab")
    confirm(service, bottle_id, "R2-" + str(bottle_id), lab, "lab", seal)


def receipt(service, bottle_id, receipt_no, concentration, lab="lab-1", analyzed_at="2026-10-01T10:00:00+00:00"):
    return service.submit_receipt(bottle_id, {
        "receipt_no": receipt_no, "concentration": concentration, "analyzed_at": analyzed_at,
    }, lab, "lab")


class CustodyTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.repo, self.service = make_service()
        self.item = self.service.create_item(base_event(), "analyst-1", "analyst")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_bottle_registers_sampling_time_seal_and_holder(self):
        bottle = register(self.service, self.item["id"], "B-1")
        self.assertEqual(bottle["sampled_at"], "2026-10-01T08:00:00+00:00")
        self.assertEqual(bottle["seal_no"], "SEAL-B-1")
        self.assertEqual(bottle["holder"], "field-1")
        self.assertEqual(bottle["status"], "active")
        self.assertFalse(bottle["delivered"])
        self.assertEqual(bottle["handoffs"], [])
        kinds = [event["event_type"] for event in bottle["audit"]]
        self.assertEqual(kinds, ["bottle_registered"])

    def test_register_rejects_zone_outside_event(self):
        with self.assertRaises(DomainError) as context:
            register(self.service, self.item["id"], "B-1", zone="Z-9")
        self.assertEqual(context.exception.code, "zone_not_in_event")

    def test_register_requires_field_or_lab_role(self):
        with self.assertRaises(DomainError) as context:
            self.service.register_bottle(self.item["id"], {
                "bottle_no": "B-1", "zone_id": "Z-1",
                "sampled_at": "2026-10-01T08:00:00+00:00", "seal_no": "S",
            }, "coord-1", "coordinator")
        self.assertEqual(context.exception.status, 403)

    def test_duplicate_bottle_number_rejected(self):
        register(self.service, self.item["id"], "B-1")
        with self.assertRaises(ConflictError):
            register(self.service, self.item["id"], "B-1", zone="Z-2")

    def test_handoff_requires_from_holder_to_be_current_holder(self):
        bottle = register(self.service, self.item["id"], "B-1")
        # courier-9 不是当前经手人，发起交接即断档 -> 隔离
        with self.assertRaises(DomainError) as context:
            begin(self.service, bottle["id"], "REQ-1", "courier-9", "courier", "lab-1", "lab")
        self.assertEqual(context.exception.code, "custody_gap")
        self.assertEqual(context.exception.status, 409)
        bottle = self.service.get_bottle(bottle["id"])
        self.assertEqual(bottle["status"], "quarantined")
        self.assertEqual(bottle["quarantine_reason"], "custody_gap")
        self.assertIsNone(bottle["pending_handoff_id"])
        self.assertEqual(bottle["handoffs"][0]["status"], "gap")

    def test_seal_mismatch_quarantines_bottle(self):
        bottle = register(self.service, self.item["id"], "B-1")
        begin(self.service, bottle["id"], "REQ-1", "field-1", "field_operator", "courier-1", "courier")
        with self.assertRaises(DomainError) as context:
            confirm(self.service, bottle["id"], "REQ-1", "courier-1", "courier", "WRONG-SEAL")
        self.assertEqual(context.exception.code, "seal_mismatch")
        bottle = self.service.get_bottle(bottle["id"])
        self.assertEqual(bottle["status"], "quarantined")
        self.assertEqual(bottle["quarantine_reason"], "seal_mismatch")
        self.assertIsNone(bottle["pending_handoff_id"])
        self.assertEqual(bottle["handoffs"][0]["status"], "seal_mismatch")
        # 隔离后任何交接都被拒绝
        with self.assertRaises(DomainError) as context2:
            begin(self.service, bottle["id"], "REQ-2", "field-1", "field_operator", "courier-2", "courier")
        self.assertEqual(context2.exception.code, "custody_quarantined")

    def test_only_named_receiver_can_confirm(self):
        bottle = register(self.service, self.item["id"], "B-1")
        begin(self.service, bottle["id"], "REQ-1", "field-1", "field_operator", "courier-1", "courier")
        with self.assertRaises(DomainError) as context:
            confirm(self.service, bottle["id"], "REQ-1", "courier-2", "courier", "SEAL-B-1",
                    to_holder="courier-2")
        self.assertEqual(context.exception.status, 403)
        self.assertEqual(context.exception.code, "wrong_receiver")

    def test_lab_rejects_bottle_without_complete_chain(self):
        bottle = register(self.service, self.item["id"], "B-1")
        # 只到 courier，未送达实验室
        begin(self.service, bottle["id"], "REQ-1", "field-1", "field_operator", "courier-1", "courier")
        confirm(self.service, bottle["id"], "REQ-1", "courier-1", "courier", "SEAL-B-1")
        with self.assertRaises(DomainError) as context:
            receipt(self.service, bottle["id"], "R-1", 2)
        self.assertEqual(context.exception.code, "custody_incomplete")
        # 隔离瓶的结果也不采信
        bottle2 = register(self.service, self.item["id"], "B-2", zone="Z-2")
        with self.assertRaises(DomainError) as context:
            begin(self.service, bottle2["id"], "REQ-X", "intruder", "courier", "lab-1", "lab")
        self.assertEqual(context.exception.code, "custody_gap")
        with self.assertRaises(DomainError) as context:
            receipt(self.service, bottle2["id"], "R-2", 2)
        self.assertEqual(context.exception.code, "custody_quarantined")

    def test_complete_chain_accepted_and_restore_gated(self):
        item = advance_to_disinfected(self.service, self.item)
        bottle = register(self.service, item["id"], "B-1", zone="Z-1")
        deliver(self.service, bottle["id"], "SEAL-B-1")
        result = receipt(self.service, bottle["id"], "R-1", 2)
        self.assertFalse(result["replayed"])
        self.assertEqual(result["item_status"], "sampled")
        states = {z["zone_id"]: z["state"] for z in result["zones"]}
        self.assertEqual(states, {"Z-1": "cleared", "Z-2": "pending"})
        item = self.service.get_item(item["id"])
        # Z-2 仍缺口 -> 不能恢复
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True},
                             "coord-1", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "quality_not_met")
        bottle2 = register(self.service, item["id"], "B-2", zone="Z-2", seal="SEAL-B-2")
        deliver(self.service, bottle2["id"], "SEAL-B-2", courier="courier-1", lab="lab-1")
        receipt(self.service, bottle2["id"], "R-2", 1)
        item = self.service.get_item(item["id"])
        self.assertEqual(item["sampling_status"], "cleared")
        item = self.service.act(item["id"], "restore", {"all_zones_cleared": True},
                                "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")

    def test_duplicate_receipt_recorded_once(self):
        bottle = register(self.service, self.item["id"], "B-1")
        deliver(self.service, bottle["id"], "SEAL-B-1")
        first = receipt(self.service, bottle["id"], "R-DUP", 2)
        second = receipt(self.service, bottle["id"], "R-DUP", 99)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        receipts = self.service.repository.list_receipts(bottle["id"])
        self.assertEqual(len(receipts), 1)
        # 重复回执不改变已记录的值
        self.assertEqual(receipts[0]["concentration"], 2)

    def test_late_bad_result_invalidates_restoration_and_recomputes_gap(self):
        self.test_complete_chain_accepted_and_restore_gated()
        item = self.service.get_item(self.item["id"])
        self.assertEqual(item["status"], "restored")
        # 晚到的坏结果：同瓶新回执取代旧回执
        bottles = {b["bottle_no"]: b for b in self.service.list_bottles(item["id"])}
        bottle = bottles["B-1"]
        late = receipt(self.service, bottle["id"], "R-LATE", 50,
                       analyzed_at="2026-10-03T09:00:00+00:00")
        self.assertEqual(late["item_status"], "sampled")
        self.assertIn("Z-1", late["invalidated_zones"])
        states = {z["zone_id"]: z["state"] for z in late["zones"]}
        self.assertEqual(states["Z-1"], "detected")
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "sampled")
        self.assertTrue(item["payload"]["restoration"]["invalidated"])
        # 旧回执仍可查，只是被标记为 superseded
        receipts = self.service.repository.list_receipts(bottle["id"])
        by_no = {r["receipt_no"]: r for r in receipts}
        self.assertTrue(by_no["R-1"]["superseded"])
        self.assertEqual(by_no["R-1"]["superseded_by"], by_no["R-LATE"]["id"])
        self.assertFalse(by_no["R-LATE"]["superseded"])
        # 缺口未闭合前再次恢复仍被拒绝
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True},
                             "coord-1", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "quality_not_met")
        # 再补一份合格的晚到结果 -> 缺口闭合，可再次恢复
        recover = receipt(self.service, bottle["id"], "R-OK", 1,
                          analyzed_at="2026-10-03T12:00:00+00:00")
        self.assertEqual(recover["item_status"], "sampled")
        self.assertEqual(recover["invalidated_zones"], [])
        item = self.service.get_item(item["id"])
        item = self.service.act(item["id"], "restore", {"all_zones_cleared": True},
                                "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")

    def test_field_operator_cannot_release(self):
        item = advance_to_disinfected(self.service, self.item)
        bottle = register(self.service, item["id"], "B-1", zone="Z-1")
        deliver(self.service, bottle["id"], "SEAL-B-1")
        receipt(self.service, bottle["id"], "R-1", 2)
        bottle2 = register(self.service, item["id"], "B-2", zone="Z-2", seal="SEAL-B-2")
        deliver(self.service, bottle2["id"], "SEAL-B-2")
        receipt(self.service, bottle2["id"], "R-2", 1)
        item = self.service.get_item(item["id"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True},
                             "field-1", "field_operator", item["version"])
        self.assertEqual(context.exception.status, 403)

    def test_concurrent_handoffs_second_sees_latest_holder(self):
        bottle = register(self.service, self.item["id"], "B-1")
        barrier = threading.Barrier(2)
        outcomes = []

        def race(request_id, to_holder):
            try:
                barrier.wait(timeout=10)
                begin(self.service, bottle["id"], request_id, "field-1", "field_operator",
                      to_holder, "courier")
                outcomes.append(("ok", request_id, to_holder))
            except ConflictError as exc:
                outcomes.append(("conflict:%s" % exc.code, request_id, str(exc)))
            except Exception as exc:  # pragma: no cover - 暴露意外问题
                outcomes.append(("error:%r" % exc, request_id, ""))

        t1 = threading.Thread(target=race, args=("REQ-C1", "courier-1"))
        t2 = threading.Thread(target=race, args=("REQ-C2", "courier-2"))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = sorted(outcome[0] for outcome in outcomes)
        self.assertEqual(statuses[0], "conflict:handoff_pending")
        self.assertEqual(statuses[1], "ok")
        winner = next(outcome for outcome in outcomes if outcome[0] == "ok")[2]
        current = self.service.get_bottle(bottle["id"])
        self.assertIsNotNone(current["pending_handoff_id"])
        self.assertEqual(current["holder"], "field-1")
        pending = self.repo.list_handoffs(bottle["id"])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["to_holder"], winner)

    def test_pending_handoff_retained_and_retry_is_idempotent(self):
        bottle = register(self.service, self.item["id"], "B-1")
        first = begin(self.service, bottle["id"], "REQ-IDEM", "field-1", "field_operator",
                      "courier-1", "courier")
        self.assertFalse(first["replayed"])
        # 同一 request_id 重试（例如调用方写响应前崩溃）：不重复创建
        retry = begin(self.service, bottle["id"], "REQ-IDEM", "field-1", "field_operator",
                      "courier-1", "courier")
        self.assertTrue(retry["replayed"])
        self.assertEqual(retry["handoff"]["id"], first["handoff"]["id"])
        handoffs = self.repo.list_handoffs(bottle["id"])
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(handoffs[0]["status"], "pending")
        # 未完成交接仍然保留，接收人可随后确认完成
        confirm(self.service, bottle["id"], "REQ-IDEM", "courier-1", "courier", "SEAL-B-1")
        handoffs = self.repo.list_handoffs(bottle["id"])
        self.assertEqual(handoffs[0]["status"], "completed")

    def test_retry_confirm_does_not_duplicate(self):
        bottle = register(self.service, self.item["id"], "B-1")
        begin(self.service, bottle["id"], "REQ-CF", "field-1", "field_operator", "courier-1", "courier")
        first = confirm(self.service, bottle["id"], "REQ-CF", "courier-1", "courier", "SEAL-B-1")
        self.assertFalse(first["replayed"])
        second = confirm(self.service, bottle["id"], "REQ-CF", "courier-1", "courier", "SEAL-B-1")
        self.assertTrue(second["replayed"])
        self.assertEqual(len(self.repo.list_handoffs(bottle["id"])), 1)

    def test_gap_replay_returns_same_gap(self):
        bottle = register(self.service, self.item["id"], "B-1")
        with self.assertRaises(DomainError):
            begin(self.service, bottle["id"], "REQ-G", "intruder", "courier", "lab-1", "lab")
        # 写入失败/网络重试后重放：返回原断档记录，不再重复创建
        retried = begin(self.service, bottle["id"], "REQ-G", "intruder", "courier", "lab-1", "lab")
        self.assertTrue(retried["replayed"])
        self.assertEqual(retried["handoff"]["status"], "gap")
        self.assertEqual(retried["handoff"]["error"], "custody_gap")
        self.assertEqual(len(self.repo.list_handoffs(bottle["id"])), 1)
        bottle = self.service.get_bottle(bottle["id"])
        self.assertEqual(bottle["status"], "quarantined")

    def test_legacy_event_treated_as_unsampled_but_history_remains_queryable(self):
        # 旧事件：走老 sample 动作，历史结果写入 sample_results，但没有任何保管瓶
        item = advance_to_disinfected(self.service, self.item)
        item = self.service.act(item["id"], "sample",
                                {"sample_id": "OLD-1", "zone_id": "Z-1", "concentration": 1},
                                "lab-1", "lab", item["version"])
        self.assertEqual(item["payload"]["sample_results"][0]["sample_id"], "OLD-1")
        self.assertEqual(rules_module.sampling_status(item["payload"]), "unsampled")
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "restore", {"all_zones_cleared": True},
                             "coord-1", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "custody_missing")
        # 历史结果仍可查
        fetched = self.service.get_item(item["id"])
        self.assertEqual(fetched["payload"]["sample_results"][0]["concentration"], 1)
        self.assertEqual(fetched["bottles"], [])

    def test_bottle_audit_chain_and_item_audit_separated(self):
        bottle = register(self.service, self.item["id"], "B-1")
        deliver(self.service, bottle["id"], "SEAL-B-1")
        receipt(self.service, bottle["id"], "R-1", 2)
        bottle_events = self.service.repository.bottle_audit_trail(bottle["id"])
        self.assertEqual([e["event_type"] for e in bottle_events][:4],
                         ["bottle_registered", "handoff_requested", "handoff_completed", "handoff_requested"])
        # 哈希链连续
        previous = "GENESIS"
        for event in bottle_events:
            self.assertEqual(event["previous_hash"], previous)
            previous = event["event_hash"]
        item = self.service.get_item(self.item["id"])
        item_kinds = {e["event_type"] for e in item["audit"] if e["bottle_id"] is None}
        self.assertIn("custody_recomputed", item_kinds)

    def test_expected_version_conflict_reports_current_holder(self):
        bottle = register(self.service, self.item["id"], "B-1")
        begin(self.service, bottle["id"], "REQ-V1", "field-1", "field_operator", "courier-1", "courier")
        confirm(self.service, bottle["id"], "REQ-V1", "courier-1", "courier", "SEAL-B-1")
        with self.assertRaises(ConflictError) as context:
            begin(self.service, bottle["id"], "REQ-V2", "field-1", "field_operator",
                  "lab-1", "lab", expected_version=1)
        self.assertEqual(context.exception.code, "version_conflict")
        self.assertIn("courier-1", str(context.exception))


if __name__ == "__main__":
    unittest.main()
