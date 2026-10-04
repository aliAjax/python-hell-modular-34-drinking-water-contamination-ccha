import json
import sqlite3
import uuid
from datetime import datetime, timezone

from . import rules
from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bottles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    bottle_code TEXT NOT NULL,
                    seal_number TEXT NOT NULL,
                    sampled_at TEXT NOT NULL,
                    zone_id TEXT,
                    current_handler TEXT NOT NULL,
                    current_role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    chain_complete INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, bottle_code),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS custody_handoffs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bottle_id INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    from_handler TEXT NOT NULL,
                    from_role TEXT NOT NULL,
                    to_handler TEXT NOT NULL,
                    to_role TEXT NOT NULL,
                    seal_number TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    reason TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(bottle_id, idempotency_key),
                    FOREIGN KEY(bottle_id) REFERENCES bottles(id)
                );
                CREATE TABLE IF NOT EXISTS lab_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bottle_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    receipt_id TEXT NOT NULL,
                    zone_id TEXT,
                    result REAL NOT NULL,
                    valid INTEGER NOT NULL DEFAULT 0,
                    outcome TEXT NOT NULL,
                    chain_complete INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(receipt_id),
                    FOREIGN KEY(bottle_id) REFERENCES bottles(id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                """
            )
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()

    # ---------------- chain of custody ----------------

    def _row_to_bottle(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["chain_complete"] = bool(result["chain_complete"])
        return result

    def _list_handoffs(self, conn, bottle_id):
        rows = conn.execute(
            "SELECT * FROM custody_handoffs WHERE bottle_id=? ORDER BY id", (bottle_id,)
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value["payload"])
            result.append(value)
        return result

    def _list_receipts(self, conn, bottle_id):
        rows = conn.execute(
            "SELECT * FROM lab_receipts WHERE bottle_id=? ORDER BY id", (bottle_id,)
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value["payload"])
            value["valid"] = bool(value["valid"])
            value["chain_complete"] = bool(value["chain_complete"])
            result.append(value)
        return result

    def _insert_handoff(self, conn, bottle_id, idem_key, from_handler, from_role, to_handler, to_role, seal_number, outcome, reason, extra):
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS n FROM custody_handoffs WHERE bottle_id=?", (bottle_id,)
        ).fetchone()["n"]
        payload = {
            "from_role": from_role,
            "to_role": to_role,
            "seal_number": seal_number,
            "outcome": outcome,
            "reason": reason,
        }
        payload.update(extra or {})
        conn.execute(
            "INSERT INTO custody_handoffs(bottle_id,seq,idempotency_key,from_handler,from_role,to_handler,to_role,seal_number,outcome,reason,payload,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                bottle_id,
                seq,
                idem_key,
                from_handler,
                from_role,
                to_handler,
                to_role,
                seal_number,
                outcome,
                reason,
                canonical_json(payload),
                now_iso(),
            ),
        )

    def register_bottle(self, item_id, bottle_code, seal_number, sampled_at, zone_id, note, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            code = bottle_code or ("B-%d-%s" % (item_id, uuid.uuid4().hex[:8]))
            try:
                conn.execute(
                    "INSERT INTO bottles(item_id,bottle_code,seal_number,sampled_at,zone_id,current_handler,current_role,status,chain_complete,version,payload,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item_id,
                        code,
                        seal_number,
                        sampled_at,
                        zone_id,
                        actor,
                        role,
                        "registered",
                        0,
                        1,
                        canonical_json({"note": note, "zone_id": zone_id}),
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_bottle", "同一瓶号已经登记")
            bottle_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "bottle_registered",
                actor,
                role,
                {"bottle_id": bottle_id, "bottle_code": code, "seal_number": seal_number, "sampled_at": sampled_at},
            )
            conn.execute("COMMIT")
            return self.get_bottle(bottle_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_bottle(self, bottle_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM bottles WHERE id=?", (bottle_id,)).fetchone()
            if row is None:
                raise NotFoundError("bottle_not_found", "采样瓶不存在")
            bottle = self._row_to_bottle(row)
            bottle["handoffs"] = self._list_handoffs(conn, bottle_id)
            bottle["receipts"] = self._list_receipts(conn, bottle_id)
            return bottle
        finally:
            conn.close()

    def list_bottles(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM bottles WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            return [self._row_to_bottle(row) for row in rows]
        finally:
            conn.close()

    def submit_handoff(self, bottle_id, to_handler, to_role, seal_number, idem_key, actor, role, expected_version):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            bottle = conn.execute("SELECT * FROM bottles WHERE id=?", (bottle_id,)).fetchone()
            if bottle is None:
                raise NotFoundError("bottle_not_found", "采样瓶不存在")
            item_id = bottle["item_id"]
            idem_key = idem_key or canonical_json(["handoff", bottle_id, actor, to_handler, seal_number])
            existing = conn.execute(
                "SELECT * FROM custody_handoffs WHERE bottle_id=? AND idempotency_key=?",
                (bottle_id, idem_key),
            ).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                return self.get_bottle(bottle_id)
            if expected_version is not None and int(expected_version) != int(bottle["version"]):
                raise ConflictError(
                    "version_conflict",
                    "记录已被其他交接更新，请重新读取",
                    {
                        "current_handler": bottle["current_handler"],
                        "current_role": bottle["current_role"],
                        "version": bottle["version"],
                    },
                )
            if bottle["status"] == "isolated":
                self._insert_handoff(
                    conn, bottle_id, idem_key, actor, role, to_handler, to_role, seal_number,
                    "pending", "already_isolated", {"note": "bottle isolated"},
                )
                conn.execute("COMMIT")
                raise DomainError(
                    "bottle_isolated", "采样瓶已隔离，交接暂停", 409,
                    {"current_handler": bottle["current_handler"], "version": bottle["version"]},
                )
            gap = actor != bottle["current_handler"]
            seal_bad = seal_number != bottle["seal_number"]
            if gap or seal_bad:
                reason = "gap" if gap else "seal_mismatch"
                self._insert_handoff(
                    conn, bottle_id, idem_key, actor, role, to_handler, to_role, seal_number,
                    "pending", reason, {"gap": gap, "seal_mismatch": seal_bad},
                )
                conn.execute(
                    "UPDATE bottles SET status='isolated', version=version+1, updated_at=? WHERE id=?",
                    (now_iso(), bottle_id),
                )
                self.append_audit(
                    conn, item_id, "bottle_isolated", actor, role,
                    {"bottle_id": bottle_id, "reason": reason, "from_handler": actor, "to_handler": to_handler},
                )
                conn.execute("COMMIT")
                raise DomainError(
                    "chain_broken", "保管链断档或封条不符，已隔离", 409,
                    {"reason": reason, "current_handler": bottle["current_handler"], "version": bottle["version"] + 1},
                )
            self._insert_handoff(
                conn, bottle_id, idem_key, actor, role, to_handler, to_role, seal_number,
                "completed", None, {},
            )
            new_status = "at_lab" if to_role == "lab" else "in_transit"
            conn.execute(
                "UPDATE bottles SET current_handler=?, current_role=?, status=?, version=version+1, updated_at=? WHERE id=?",
                (to_handler, to_role, new_status, now_iso(), bottle_id),
            )
            self.append_audit(
                conn, item_id, "handoff_completed", actor, role,
                {"bottle_id": bottle_id, "from": actor, "to": to_handler, "seal_number": seal_number},
            )
            conn.execute("COMMIT")
            return self.get_bottle(bottle_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def receive_receipt(self, bottle_id, receipt_id, result, zone_id, note, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            bottle = conn.execute("SELECT * FROM bottles WHERE id=?", (bottle_id,)).fetchone()
            if bottle is None:
                raise NotFoundError("bottle_not_found", "采样瓶不存在")
            item_id = bottle["item_id"]
            duplicate = conn.execute(
                "SELECT * FROM lab_receipts WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            if duplicate is not None:
                self.append_audit(
                    conn, item_id, "duplicate_receipt", actor, role,
                    {"receipt_id": receipt_id, "bottle_id": bottle_id},
                )
                conn.execute("COMMIT")
                receipt = dict(duplicate)
                receipt["payload"] = json.loads(receipt["payload"])
                receipt["valid"] = bool(receipt["valid"])
                receipt["chain_complete"] = bool(receipt["chain_complete"])
                return {"receipt": receipt, "duplicate": True, "bottle": self.get_bottle(bottle_id)}
            handoffs = self._list_handoffs(conn, bottle_id)
            chain_ok = rules.chain_complete(self._row_to_bottle(bottle), handoffs)
            valid = 1 if (chain_ok and bottle["status"] != "isolated") else 0
            outcome = "original" if valid else "held"
            resolved_zone = zone_id or bottle["zone_id"]
            conn.execute(
                "INSERT INTO lab_receipts(bottle_id,item_id,receipt_id,zone_id,result,valid,outcome,chain_complete,payload,received_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bottle_id,
                    item_id,
                    receipt_id,
                    resolved_zone,
                    result,
                    valid,
                    outcome,
                    1 if chain_ok else 0,
                    canonical_json({"note": note, "zone_id": resolved_zone}),
                    now_iso(),
                    now_iso(),
                ),
            )
            receipt_row = conn.execute(
                "SELECT * FROM lab_receipts WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            if valid:
                conn.execute(
                    "UPDATE bottles SET status='received', chain_complete=1, version=version+1, updated_at=? WHERE id=?",
                    (now_iso(), bottle_id),
                )
                item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
                item_payload = json.loads(item["payload"])
                item_payload.setdefault("sample_results", []).append(
                    {"sample_id": receipt_id, "zone_id": resolved_zone, "concentration": result, "valid": True}
                )
                limit = float(item_payload.get("limit", 0))
                clearance = rules.zone_clearance(item_payload.get("sample_results", []), limit)
                gap = rules.gap_zones(item_payload.get("zone_ids", []), item_payload.get("sample_results", []), limit)
                item_payload["zone_clearance"] = clearance
                item_payload["gap_zones"] = gap
                new_status = item["status"]
                if item["status"] in ("restored", "released"):
                    item_payload.pop("restoration", None)
                    item_payload.pop("release", None)
                    new_status = "sampled"
                    self.append_audit(
                        conn, item_id, "restoration_invalidated", actor, role,
                        {"receipt_id": receipt_id, "zone_id": resolved_zone, "result": result, "gap": gap},
                    )
                elif item["status"] != "cancelled":
                    new_status = "sampled"
                conn.execute(
                    "UPDATE items SET status=?, payload=?, version=version+1, updated_at=? WHERE id=?",
                    (new_status, canonical_json(item_payload), now_iso(), item_id),
                )
                self.append_audit(
                    conn, item_id, "result_recorded", actor, role,
                    {"receipt_id": receipt_id, "zone_id": resolved_zone, "result": result, "valid": True, "gap": gap},
                )
            else:
                self.append_audit(
                    conn, item_id, "result_held", actor, role,
                    {"receipt_id": receipt_id, "reason": "chain_incomplete", "zone_id": resolved_zone, "result": result},
                )
            conn.execute("COMMIT")
            receipt = dict(receipt_row)
            receipt["payload"] = json.loads(receipt["payload"])
            receipt["valid"] = bool(receipt["valid"])
            receipt["chain_complete"] = bool(receipt["chain_complete"])
            return {
                "receipt": receipt,
                "duplicate": False,
                "bottle": self.get_bottle(bottle_id),
                "item": self.get_item(item_id),
            }
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def release_item(self, item_id, note, actor, role, expected_version):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(item["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            if item["status"] == "cancelled":
                raise DomainError("invalid_state", "事件已取消，不能放行")
            bottles = conn.execute("SELECT * FROM bottles WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            if not bottles:
                raise DomainError("unsampled", "事件没有保管记录，按未采样处理，不能放行", 409)
            for row in bottles:
                handoffs = self._list_handoffs(conn, row["id"])
                if not rules.chain_complete(self._row_to_bottle(row), handoffs):
                    raise DomainError(
                        "chain_incomplete", "保管链不完整或已隔离，不能放行", 409,
                        {"bottle_id": row["id"], "bottle_code": row["bottle_code"]},
                    )
            payload = json.loads(item["payload"])
            gap = rules.gap_zones(
                payload.get("zone_ids", []), payload.get("sample_results", []), float(payload.get("limit", 0))
            )
            if gap:
                raise DomainError("gap_not_closed", "仍有区域未达到放行标准", 409, {"gap_zones": gap})
            payload["release"] = {"actor": actor, "note": note}
            conn.execute(
                "UPDATE items SET status='released', payload=?, version=version+1, updated_at=? WHERE id=?",
                (canonical_json(payload), now_iso(), item_id),
            )
            conn.execute(
                "UPDATE bottles SET status='released', updated_at=? WHERE item_id=?",
                (now_iso(), item_id),
            )
            self.append_audit(conn, item_id, "released", actor, role, {"note": note})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def clear_isolation(self, bottle_id, seal_number, note, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            bottle = conn.execute("SELECT * FROM bottles WHERE id=?", (bottle_id,)).fetchone()
            if bottle is None:
                raise NotFoundError("bottle_not_found", "采样瓶不存在")
            if bottle["status"] != "isolated":
                raise DomainError("not_isolated", "采样瓶未处于隔离状态")
            new_seal = seal_number or bottle["seal_number"]
            conn.execute(
                "UPDATE bottles SET status='in_transit', seal_number=?, version=version+1, updated_at=? WHERE id=?",
                (new_seal, now_iso(), bottle_id),
            )
            self.append_audit(
                conn, bottle["item_id"], "isolation_cleared", actor, role,
                {"bottle_id": bottle_id, "new_seal_number": new_seal, "note": note},
            )
            conn.execute("COMMIT")
            return self.get_bottle(bottle_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
