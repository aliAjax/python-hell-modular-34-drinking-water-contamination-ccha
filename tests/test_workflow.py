import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service


def sampled_event(service, payload=None, actor="analyst-1"):
    payload = payload or {
        "source_id": "SRC-1",
        "contaminant": "nitrate",
        "detected_at": "2026-09-27T06:00:00+00:00",
        "concentration": 20,
        "limit": 10,
        "zone_ids": ["Z-1", "Z-2"],
        "population": 5000,
        "complaints": 4,
    }
    item = service.create_item(payload, actor, "analyst")
    item = service.act(item["id"], "verify", {"sample_count": 2}, actor, "analyst", item["version"])
    item = service.act(item["id"], "advise", {"notice_id": "N-1", "kind": "boil", "message": "煮沸"}, "disp-1", "dispatcher", item["version"])
    item = service.act(item["id"], "switch_source", {"alternate_source_id": "ALT-1"}, "coord-1", "coordinator", item["version"])
    for zone in payload["zone_ids"]:
        item = service.act(item["id"], "flush", {"zone_id": zone}, "field-1", "field_operator", item["version"])
    item = service.act(item["id"], "disinfect", {"zone_id": "Z-1", "completed": True}, "field-1", "field_operator", item["version"])
    return item


def deliver_bottle(service, item_id, bottle_no, zone, concentration, receipt_no,
                   field="field-1", courier="courier-1", lab="lab-1", seal=None):
    seal = seal or ("SEAL-" + bottle_no)
    service.register_bottle(item_id, {
        "bottle_no": bottle_no, "zone_id": zone,
        "sampled_at": "2026-09-27T08:00:00+00:00", "seal_no": seal,
    }, field, "field_operator")
    bottle = service.list_bottles(item_id)[0] if bottle_no == "B-1" else next(
        b for b in service.list_bottles(item_id) if b["bottle_no"] == bottle_no)
    req1 = "REQ-" + bottle_no + "-1"
    req2 = "REQ-" + bottle_no + "-2"
    service.begin_handoff(bottle["id"], {"request_id": req1, "to_holder": courier, "to_role": "courier"},
                          field, "field_operator")
    service.confirm_handoff(bottle["id"], {"request_id": req1, "to_holder": courier, "to_role": "courier",
                                           "observed_seal": seal}, courier, "courier")
    service.begin_handoff(bottle["id"], {"request_id": req2, "to_holder": lab, "to_role": "lab"},
                          courier, "courier")
    service.confirm_handoff(bottle["id"], {"request_id": req2, "to_holder": lab, "to_role": "lab",
                                           "observed_seal": seal}, lab, "lab")
    receipt = service.submit_receipt(bottle["id"], {
        "receipt_no": receipt_no, "concentration": concentration,
        "analyzed_at": "2026-09-27T09:00:00+00:00",
    }, lab, "lab")
    return bottle, receipt


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_complete_water_response_workflow(self):
        item = sampled_event(self.service)
        item_id = item["id"]
        deliver_bottle(self.service, item_id, "B-1", "Z-1", 2, "R-1")
        deliver_bottle(self.service, item_id, "B-2", "Z-2", 3, "R-2")
        item = self.service.get_item(item_id)
        self.assertEqual(item["sampling_status"], "cleared")
        self.assertEqual(item["status"], "sampled")
        item = self.service.act(item_id, "restore", {"all_zones_cleared": True},
                                "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "restored")
        self.assertEqual(item["payload"]["restoration"]["zone_ids"], ["Z-1", "Z-2"])
        self.assertGreaterEqual(len(item["audit"]), 10)


if __name__ == "__main__":
    unittest.main()
