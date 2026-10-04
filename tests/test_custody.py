import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError
from src.http_api import build_handler


class CustodyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "source_id": "SRC-1",
            "contaminant": "nitrate",
            "detected_at": "2026-10-01T00:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": ["Z-1"],
            "population": 1000,
        }, "analyst-1", "analyst")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _register(self, zone="Z-1", seal="SEAL-1", actor="field-1", role="field_operator"):
        return self.service.register_bottle(self.item["id"], {
            "seal_number": seal,
            "sampled_at": "2026-10-01T01:00:00+00:00",
            "zone_id": zone,
        }, actor, role)

    def _handoff(self, bottle, to_handler, to_role, seal, actor, role,
                 expected_version=None, idem=None):
        payload = {"to_handler": to_handler, "to_role": to_role, "seal_number": seal}
        if expected_version is not None:
            payload["expected_version"] = expected_version
        if idem is not None:
            payload["idempotency_key"] = idem
        return self.service.handoff(bottle["id"], payload, actor, role)

    def _receipt(self, bottle, receipt_id, result, zone="Z-1", actor="lab-1"):
        return self.service.receive_receipt(bottle["id"], {
            "receipt_id": receipt_id, "result": result, "zone_id": zone,
        }, actor, "lab")

    def _chain_to_lab(self, bottle, seal="SEAL-1", actor="field-1"):
        return self._handoff(
            bottle, "lab-1", "lab", seal, actor, "field_operator", bottle["version"]
        )

    def _release(self, item=None, version=None, actor="coord-1", role="coordinator"):
        item = item or self.item
        return self.service.release(item["id"], {"note": "ok"}, actor, role, version or item["version"])

    # 1. 瓶子登记采样时刻、封条号和经手人
    def test_register_bottle_records_sample_time_seal_and_handler(self):
        bottle = self._register()
        self.assertEqual(bottle["seal_number"], "SEAL-1")
        self.assertEqual(bottle["sampled_at"], "2026-10-01T01:00:00+00:00")
        self.assertEqual(bottle["current_handler"], "field-1")
        self.assertEqual(bottle["current_role"], "field_operator")
        self.assertEqual(bottle["status"], "registered")
        self.assertEqual(bottle["version"], 1)

    # 2. 交接时前后人员必须对上
    def test_handoff_continuity_updates_current_handler(self):
        bottle = self._register()
        bottle = self._chain_to_lab(bottle)
        self.assertEqual(bottle["status"], "at_lab")
        self.assertEqual(bottle["current_handler"], "lab-1")
        self.assertEqual(bottle["current_role"], "lab")
        self.assertEqual(len(bottle["handoffs"]), 1)
        self.assertEqual(bottle["handoffs"][0]["from_handler"], "field-1")
        self.assertEqual(bottle["handoffs"][0]["to_handler"], "lab-1")

    # 3. 断档先隔离
    def test_gap_isolates_bottle_and_keeps_pending_handoff(self):
        bottle = self._register()
        with self.assertRaises(DomainError) as context:
            self._handoff(
                bottle, "lab-1", "lab", "SEAL-1", "stranger", "field_operator", bottle["version"]
            )
        self.assertEqual(context.exception.code, "chain_broken")
        self.assertEqual(context.exception.details["reason"], "gap")
        bottle = self.service.get_bottle(bottle["id"])
        self.assertEqual(bottle["status"], "isolated")
        self.assertEqual(len(bottle["handoffs"]), 1)
        self.assertEqual(bottle["handoffs"][0]["outcome"], "pending")
        self.assertEqual(bottle["handoffs"][0]["reason"], "gap")

    # 3b. 封条不符先隔离
    def test_seal_mismatch_isolates_bottle(self):
        bottle = self._register(seal="SEAL-2")
        with self.assertRaises(DomainError) as context:
            self._handoff(
                bottle, "lab-1", "lab", "WRONG-SEAL", "field-1", "field_operator", bottle["version"]
            )
        self.assertEqual(context.exception.code, "chain_broken")
        self.assertEqual(context.exception.details["reason"], "seal_mismatch")
        bottle = self.service.get_bottle(bottle["id"])
        self.assertEqual(bottle["status"], "isolated")
        self.assertEqual(bottle["handoffs"][0]["reason"], "seal_mismatch")

    # 4. 结果只认完整保管链
    def test_result_held_without_complete_chain(self):
        bottle = self._register()
        result = self._receipt(bottle, "R-HELD", 2.0)
        self.assertEqual(result["receipt"]["outcome"], "held")
        self.assertFalse(result["receipt"]["valid"])
        self.assertFalse(result["receipt"]["chain_complete"])

    def test_result_valid_with_complete_chain(self):
        bottle = self._register()
        bottle = self._chain_to_lab(bottle)
        result = self._receipt(bottle, "R-1", 2.0)
        self.assertEqual(result["receipt"]["outcome"], "original")
        self.assertTrue(result["receipt"]["valid"])
        self.assertTrue(result["receipt"]["chain_complete"])
        self.assertEqual(result["bottle"]["status"], "received")

    # 5. 实验室重复回执只记一次
    def test_duplicate_receipt_recorded_once(self):
        bottle = self._register()
        bottle = self._chain_to_lab(bottle)
        first = self._receipt(bottle, "R-DUP", 2.0)
        self.assertFalse(first["duplicate"])
        second = self._receipt(bottle, "R-DUP", 2.0)
        self.assertTrue(second["duplicate"])
        bottle = self.service.get_bottle(bottle["id"])
        self.assertEqual(len(bottle["receipts"]), 1)
        self.assertEqual(bottle["receipts"][0]["receipt_id"], "R-DUP")

    # 6. 晚到结果更新后，已恢复区域立即失效并重算缺口
    def test_late_dirty_result_invalidates_release_and_recalculates_gap(self):
        bottle = self._register()
        bottle = self._chain_to_lab(bottle)
        result = self._receipt(bottle, "R-1", 2.0)
        self.assertEqual(result["item"]["payload"]["gap_zones"], [])
        released = self._release(result["item"], result["item"]["version"])
        self.assertEqual(released["status"], "released")
        # 晚到的脏结果
        late = self._receipt(bottle, "R-1-LATE", 15.0)
        self.assertEqual(late["item"]["status"], "sampled")
        self.assertEqual(late["item"]["payload"]["gap_zones"], ["Z-1"])
        self.assertNotIn("release", late["item"]["payload"])
        self.assertNotIn("restoration", late["item"]["payload"])
        # 历史结果仍可查
        got = self.service.get_bottle(bottle["id"])
        self.assertEqual([r["receipt_id"] for r in got["receipts"]], ["R-1", "R-1-LATE"])
        # 缺口未闭合，不能放行
        with self.assertRaises(DomainError) as context:
            self._release(late["item"], late["item"]["version"])
        self.assertEqual(context.exception.code, "gap_not_closed")

    # 7. 现场角色越权放行会被拒绝
    def test_field_role_release_forbidden(self):
        bottle = self._register()
        bottle = self._chain_to_lab(bottle)
        result = self._receipt(bottle, "R-1", 2.0)
        with self.assertRaises(DomainError) as context:
            self._release(result["item"], result["item"]["version"], actor="field-1", role="field_operator")
        self.assertEqual(context.exception.status, 403)
        self.assertEqual(context.exception.code, "forbidden")

    # 8. 两人同时提交同一瓶交接，后到者看到最新经手人
    def test_concurrent_handoff_later_sees_latest_handler(self):
        bottle = self._register()
        v0 = bottle["version"]
        bottle = self._handoff(
            bottle, "courier-1", "field_operator", "SEAL-1", "field-1", "field_operator", v0
        )
        self.assertEqual(bottle["version"], 2)
        with self.assertRaises(ConflictError) as context:
            self._handoff(
                bottle, "lab-1", "lab", "SEAL-1", "courier-1", "field_operator", v0
            )
        self.assertEqual(context.exception.code, "version_conflict")
        self.assertEqual(context.exception.details["current_handler"], "courier-1")
        self.assertEqual(context.exception.details["version"], 2)

    # 9. 写入失败保留未完成交接和原回执，重试不重复
    def test_retry_handoff_idempotent(self):
        bottle = self._register()
        first = self._handoff(
            bottle, "lab-1", "lab", "SEAL-1", "field-1", "field_operator",
            bottle["version"], idem="idem-1",
        )
        # 重试：同样的幂等键，即使带上过期版本也不重复写入
        second = self._handoff(
            bottle, "lab-1", "lab", "SEAL-1", "field-1", "field_operator",
            1, idem="idem-1",
        )
        self.assertEqual(len(second["handoffs"]), 1)
        self.assertEqual(second["version"], first["version"])

    def test_retry_receipt_idempotent(self):
        bottle = self._register()
        bottle = self._chain_to_lab(bottle)
        self._receipt(bottle, "R-IDEM", 2.0)
        retry = self._receipt(bottle, "R-IDEM", 2.0)
        self.assertTrue(retry["duplicate"])
        bottle = self.service.get_bottle(bottle["id"])
        self.assertEqual(len(bottle["receipts"]), 1)

    # 10. 旧事件没有保管记录按未采样处理，历史结果仍可查
    def test_legacy_event_without_custody_unsampled_but_queryable(self):
        legacy = self.service.create_item({
            "source_id": "SRC-2",
            "contaminant": "bacteria",
            "detected_at": "2026-09-30T00:00:00+00:00",
            "concentration": 5,
            "limit": 10,
            "zone_ids": ["Z-9"],
            "population": 200,
        }, "analyst-1", "analyst")
        with self.assertRaises(DomainError) as context:
            self._release(legacy, legacy["version"])
        self.assertEqual(context.exception.code, "unsampled")
        # 历史记录仍可查
        got = self.service.get_item(legacy["id"])
        self.assertEqual(got["status"], "detected")
        self.assertGreaterEqual(len(got["audit"]), 1)

    # 完整保管链放行流程
    def test_complete_custody_release_workflow(self):
        item = self.service.create_item({
            "source_id": "SRC-FULL",
            "contaminant": "nitrate",
            "detected_at": "2026-10-01T00:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": ["Z-1", "Z-2"],
            "population": 1000,
        }, "analyst-1", "analyst")
        versions = {}
        for zone in ["Z-1", "Z-2"]:
            bottle = self.service.register_bottle(item["id"], {
                "seal_number": "SEAL-F", "sampled_at": "2026-10-01T01:00:00+00:00",
                "zone_id": zone,
            }, "field-1", "field_operator")
            bottle = self._chain_to_lab(bottle, seal="SEAL-F")
            result = self._receipt(bottle, "R-%s" % zone, 2.0, zone=zone)
            versions[zone] = result["item"]["version"]
        latest = max(versions.values())
        released = self.service.release(item["id"], {"note": "放行"}, "coord-1", "coordinator", latest)
        self.assertEqual(released["status"], "released")
        for zone in ["Z-1", "Z-2"]:
            bottles = self.service.list_bottles(item["id"])
            self.assertTrue(all(b["chain_complete"] for b in bottles))


class CustodyHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(self.service, os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")))
        self.server.service = self.service
        self.port = self.server.server_address[1]
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        os.unlink(self.tmp.name)

    def _request(self, method, path, body=None, actor=None, role=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if actor:
            req.add_header("X-User-Id", actor)
        if role:
            req.add_header("X-Role", role)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_custody_routes(self):
        status, body = self._request("POST", "/api/items", {
            "source_id": "SRC-HTTP", "contaminant": "nitrate",
            "detected_at": "2026-10-01T00:00:00+00:00",
            "concentration": 20, "limit": 10, "zone_ids": ["Z-1"], "population": 1000,
        }, "analyst-1", "analyst")
        self.assertEqual(status, 201)
        item_id = body["id"]

        status, body = self._request("POST", "/api/items/%d/bottles" % item_id, {
            "seal_number": "SEAL-H", "sampled_at": "2026-10-01T01:00:00+00:00", "zone_id": "Z-1",
        }, "field-1", "field_operator")
        self.assertEqual(status, 201)
        bottle_id = body["id"]
        version = body["version"]

        status, body = self._request("POST", "/api/bottles/%d/handoffs" % bottle_id, {
            "to_handler": "lab-1", "to_role": "lab", "seal_number": "SEAL-H", "expected_version": version,
        }, "field-1", "field_operator")
        self.assertEqual(status, 201)
        self.assertEqual(body["current_handler"], "lab-1")

        status, body = self._request("POST", "/api/bottles/%d/receipts" % bottle_id, {
            "receipt_id": "R-H", "result": 2.0, "zone_id": "Z-1",
        }, "lab-1", "lab")
        self.assertEqual(status, 201)
        self.assertTrue(body["receipt"]["valid"])
        item_version = body["item"]["version"]

        status, body = self._request("POST", "/api/items/%d/release" % item_id, {
            "note": "放行", "expected_version": item_version,
        }, "field-1", "field_operator")
        self.assertEqual(status, 403)

        status, body = self._request("POST", "/api/items/%d/release" % item_id, {
            "note": "放行", "expected_version": item_version,
        }, "coord-1", "coordinator")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "released")

        status, body = self._request("GET", "/api/bottles/%d" % bottle_id)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["handoffs"]), 1)
        self.assertEqual(len(body["receipts"]), 1)


if __name__ == "__main__":
    unittest.main()
