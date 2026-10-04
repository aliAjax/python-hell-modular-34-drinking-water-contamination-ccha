import json
import sqlite3
from datetime import datetime, timezone

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
                CREATE TABLE IF NOT EXISTS custody_bottles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bottle_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL,
                    zone_id TEXT NOT NULL,
                    sampled_at TEXT NOT NULL,
                    seal_no TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    delivered INTEGER NOT NULL DEFAULT 0,
                    holder TEXT NOT NULL,
                    holder_role TEXT NOT NULL,
                    pending_handoff_id INTEGER,
                    quarantine_reason TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS custody_handoffs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bottle_id INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    from_holder TEXT,
                    from_role TEXT,
                    to_holder TEXT NOT NULL,
                    to_role TEXT NOT NULL,
                    seal_no TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    expected_version INTEGER,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(bottle_id, seq),
                    FOREIGN KEY(bottle_id) REFERENCES custody_bottles(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS custody_handoff_one_pending
                    ON custody_handoffs(bottle_id) WHERE status='pending';
                CREATE TABLE IF NOT EXISTS lab_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_no TEXT NOT NULL UNIQUE,
                    bottle_id INTEGER NOT NULL,
                    concentration REAL NOT NULL,
                    analyzed_at TEXT NOT NULL,
                    superseded_by INTEGER,
                    actor TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(bottle_id) REFERENCES custody_bottles(id)
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
                    bottle_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
            if "bottle_id" not in existing:
                conn.execute("ALTER TABLE audit_events ADD COLUMN bottle_id INTEGER")
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id=None, bottle_id=None):
        if bottle_id is not None:
            row = conn.execute(
                "SELECT event_hash FROM audit_events WHERE bottle_id IS ? ORDER BY id DESC LIMIT 1",
                (bottle_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT event_hash FROM audit_events WHERE item_id IS ? AND bottle_id IS NULL ORDER BY id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload, bottle_id=None):
        previous = self._last_hash(conn, item_id, bottle_id)
        event = {
            "item_id": item_id,
            "bottle_id": bottle_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,bottle_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (item_id, bottle_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
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
            bottle_counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM custody_bottles GROUP BY status").fetchall():
                bottle_counts[row["status"]] = row["total"]
            return {"counts": counts, "bottle_counts": bottle_counts, "items": self.list_items()}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 采样瓶保管链
    # ------------------------------------------------------------------
    def _row_to_bottle(self, row):
        if row is None:
            return None
        bottle = dict(row)
        bottle["delivered"] = bool(bottle["delivered"])
        return bottle

    def _row_to_handoff(self, row):
        if row is None:
            return None
        return dict(row)

    def _row_to_receipt(self, row):
        if row is None:
            return None
        receipt = dict(row)
        receipt["superseded"] = receipt["superseded_by"] is not None
        return receipt

    def get_bottle_row(self, conn, bottle_id):
        row = conn.execute("SELECT * FROM custody_bottles WHERE id=?", (bottle_id,)).fetchone()
        if row is None:
            raise NotFoundError("bottle_not_found", "采样瓶不存在")
        return row

    def _quarantine(self, conn, bottle_id, reason):
        conn.execute(
            "UPDATE custody_bottles SET status='quarantined',pending_handoff_id=NULL,"
            "quarantine_reason=?,version=version+1,updated_at=? WHERE id=?",
            (reason, now_iso(), bottle_id),
        )

    def _recompute_item_custody(self, conn, item, actor, role, reason):
        from .rules import recompute_custody_zones

        zone_ids = list(item["payload"].get("zone_ids", []))
        rows = conn.execute(
            "SELECT r.receipt_no,r.concentration,r.analyzed_at,r.created_at,b.id AS bottle_id,"
            "b.bottle_no,b.zone_id FROM lab_receipts r JOIN custody_bottles b ON b.id=r.bottle_id "
            "WHERE b.item_id=? AND b.status='active' AND b.delivered=1 AND r.superseded_by IS NULL",
            (item["id"],),
        ).fetchall()
        zones = recompute_custody_zones(zone_ids, [dict(row) for row in rows], item["payload"].get("limit", 0))
        payload = dict(item["payload"])
        payload["custody"] = {"zones": zones, "updated_at": now_iso(), "reason": reason}
        status = item["status"]
        invalidated = []
        if status == "disinfected":
            status = "sampled"
        elif status == "restored":
            restoration = dict(payload.get("restoration") or {})
            restored_zones = restoration.get("zone_ids") or zone_ids
            state_by_zone = {zone["zone_id"]: zone["state"] for zone in zones}
            if any(state_by_zone.get(zone_id) != "cleared" for zone_id in restored_zones):
                status = "sampled"
                invalidated = restored_zones
                restoration["invalidated"] = {"at": now_iso(), "reason": "late_result_gap", "zone_ids": invalidated}
                payload["restoration"] = restoration
        conn.execute(
            "UPDATE items SET status=?,version=version+1,payload=?,updated_at=? WHERE id=?",
            (status, canonical_json(payload), now_iso(), item["id"]),
        )
        self.append_audit(conn, item["id"], "custody_recomputed", actor, role,
                          {"reason": reason, "zones": zones, "status": status})
        if invalidated:
            self.append_audit(conn, item["id"], "restoration_invalidated", actor, role,
                              {"reason": "late_result_gap", "zone_ids": invalidated})
        return status, zones, invalidated

    def create_bottle(self, item_id, bottle_no, zone_id, sampled_at, seal_no, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone() is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO custody_bottles(bottle_no,item_id,zone_id,sampled_at,seal_no,"
                    "status,delivered,holder,holder_role,version,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'active',0,?,?,1,?,?)",
                    (bottle_no, item_id, zone_id, sampled_at, seal_no, actor, role, now_iso(), now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_bottle", "采样瓶编号已经登记")
            bottle_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "bottle_registered", actor, role,
                              {"bottle_id": bottle_id, "bottle_no": bottle_no, "zone_id": zone_id,
                               "sampled_at": sampled_at, "seal_no": seal_no, "holder": actor},
                              bottle_id=bottle_id)
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
            return self._row_to_bottle(self.get_bottle_row(conn, bottle_id))
        finally:
            conn.close()

    def list_bottles(self, item_id=None):
        conn = self.connect()
        try:
            if item_id is None:
                rows = conn.execute("SELECT * FROM custody_bottles ORDER BY id DESC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM custody_bottles WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            return [self._row_to_bottle(row) for row in rows]
        finally:
            conn.close()

    def list_handoffs(self, bottle_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM custody_handoffs WHERE bottle_id=? ORDER BY id", (bottle_id,)).fetchall()
            return [self._row_to_handoff(row) for row in rows]
        finally:
            conn.close()

    def list_receipts(self, bottle_id=None):
        conn = self.connect()
        try:
            if bottle_id is None:
                rows = conn.execute("SELECT * FROM lab_receipts ORDER BY id DESC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM lab_receipts WHERE bottle_id=? ORDER BY id DESC", (bottle_id,)).fetchall()
            return [self._row_to_receipt(row) for row in rows]
        finally:
            conn.close()

    def bottle_audit_trail(self, bottle_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE bottle_id=? ORDER BY id", (bottle_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def begin_handoff(self, bottle_id, request_id, to_holder, to_role, actor, role, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            bottle = self._row_to_bottle(self.get_bottle_row(conn, bottle_id))
            replay = conn.execute("SELECT * FROM custody_handoffs WHERE request_id=?", (request_id,)).fetchone()
            if replay is not None:
                if replay["bottle_id"] != bottle_id:
                    raise ConflictError("request_id_used", "交接请求编号已用于其他采样瓶")
                replay_row = self._row_to_handoff(replay)
                conn.execute("COMMIT")
                # 断档/封条不符的请求重放：返回原记录，不重复判定
                if replay_row["status"] == "gap":
                    replay_row["error"] = "custody_gap"
                elif replay_row["status"] == "seal_mismatch":
                    replay_row["error"] = "seal_mismatch"
                return self.get_bottle(bottle_id), replay_row, True
            if bottle["status"] == "quarantined":
                raise DomainError("custody_quarantined", "采样瓶已隔离，禁止继续交接", 409)
            if bottle["pending_handoff_id"] is not None:
                raise ConflictError("handoff_pending",
                                    "该采样瓶已有未完成交接，当前经手人：%s" % bottle["holder"])
            if expected_version is not None and int(expected_version) != int(bottle["version"]):
                raise ConflictError("version_conflict",
                                    "采样瓶记录已更新，当前经手人：%s" % bottle["holder"])
            seq = conn.execute("SELECT COUNT(*) AS total FROM custody_handoffs WHERE bottle_id=?", (bottle_id,)).fetchone()["total"] + 1
            if actor != bottle["holder"]:
                conn.execute(
                    "INSERT INTO custody_handoffs(bottle_id,seq,request_id,from_holder,from_role,"
                    "to_holder,to_role,seal_no,status,reason,expected_version,created_at,completed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,'gap',?,?,?,?)",
                    (bottle_id, seq, request_id, bottle["holder"], bottle["holder_role"],
                     to_holder, to_role, bottle["seal_no"], "from_holder_mismatch",
                     expected_version, now_iso(), None),
                )
                handoff_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                self._quarantine(conn, bottle_id, "custody_gap")
                event = {"request_id": request_id, "handoff_id": handoff_id,
                         "expected_from": bottle["holder"], "actual_from": actor, "to": to_holder}
                self.append_audit(conn, bottle["item_id"], "custody_gap", actor, role,
                                  {"bottle_id": bottle_id, "bottle_no": bottle["bottle_no"], **event},
                                  bottle_id=bottle_id)
                item_row = conn.execute("SELECT * FROM items WHERE id=?", (bottle["item_id"],)).fetchone()
                item = self._row_to_item(item_row)
                self._recompute_item_custody(conn, item, actor, role, "custody_gap")
                conn.execute("COMMIT")
                raise DomainError("custody_gap",
                                  "交接人 %s 与当前经手人 %s 不一致，采样瓶已隔离" % (actor, bottle["holder"]), 409)
            conn.execute(
                "INSERT INTO custody_handoffs(bottle_id,seq,request_id,from_holder,from_role,"
                "to_holder,to_role,seal_no,status,expected_version,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,'pending',?,?)",
                (bottle_id, seq, request_id, actor, role, to_holder, to_role,
                 bottle["seal_no"], expected_version, now_iso()),
            )
            handoff_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute(
                "UPDATE custody_bottles SET pending_handoff_id=?,version=version+1,updated_at=? WHERE id=?",
                (handoff_id, now_iso(), bottle_id),
            )
            self.append_audit(conn, bottle["item_id"], "handoff_requested", actor, role,
                              {"request_id": request_id, "handoff_id": handoff_id,
                               "from": actor, "to": to_holder, "to_role": to_role},
                              bottle_id=bottle_id)
            handoff = conn.execute("SELECT * FROM custody_handoffs WHERE id=?", (handoff_id,)).fetchone()
            conn.execute("COMMIT")
            return self.get_bottle(bottle_id), self._row_to_handoff(handoff), False
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def confirm_handoff(self, bottle_id, request_id, actor, role, observed_seal):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            bottle = self._row_to_bottle(self.get_bottle_row(conn, bottle_id))
            handoff_row = conn.execute("SELECT * FROM custody_handoffs WHERE request_id=?", (request_id,)).fetchone()
            if handoff_row is None or handoff_row["bottle_id"] != bottle_id:
                raise NotFoundError("handoff_not_found", "未找到该采样瓶的待确认交接")
            handoff = self._row_to_handoff(handoff_row)
            if handoff["status"] == "completed":
                conn.execute("COMMIT")
                return self.get_bottle(bottle_id), handoff, True
            if handoff["status"] in ("gap", "seal_mismatch"):
                conn.execute("COMMIT")
                handoff["error"] = handoff["status"]
                return self.get_bottle(bottle_id), handoff, True
            if actor != handoff["to_holder"]:
                raise DomainError("wrong_receiver", "只有指定接收人 %s 才能确认交接" % handoff["to_holder"], 403)
            if bottle["status"] == "quarantined":
                raise DomainError("custody_quarantined", "采样瓶已隔离，禁止继续交接", 409)
            if not isinstance(observed_seal, str) or not observed_seal.strip():
                raise DomainError("seal_required", "确认交接时必须核对封条号")
            observed_seal = observed_seal.strip()
            if observed_seal != bottle["seal_no"]:
                conn.execute(
                    "UPDATE custody_handoffs SET status='seal_mismatch',reason=?,completed_at=? WHERE id=?",
                    ("observed:%s" % observed_seal, now_iso(), handoff["id"]),
                )
                self._quarantine(conn, bottle_id, "seal_mismatch")
                event = {"request_id": request_id, "handoff_id": handoff["id"],
                         "bottle_id": bottle_id, "bottle_no": bottle["bottle_no"],
                         "expected_seal": bottle["seal_no"], "observed_seal": observed_seal,
                         "receiver": actor}
                self.append_audit(conn, bottle["item_id"], "seal_mismatch", actor, role, event, bottle_id=bottle_id)
                item_row = conn.execute("SELECT * FROM items WHERE id=?", (bottle["item_id"],)).fetchone()
                self._recompute_item_custody(conn, self._row_to_item(item_row), actor, role, "seal_mismatch")
                conn.execute("COMMIT")
                raise DomainError("seal_mismatch",
                                  "封条号不符（期望 %s，实际 %s），采样瓶已隔离" % (bottle["seal_no"], observed_seal), 409)
            delivered = role == "lab"
            conn.execute(
                "UPDATE custody_handoffs SET status='completed',completed_at=? WHERE id=?",
                (now_iso(), handoff["id"]),
            )
            conn.execute(
                "UPDATE custody_bottles SET holder=?,holder_role=?,pending_handoff_id=NULL,"
                "delivered=?,version=version+1,updated_at=? WHERE id=?",
                (actor, role, 1 if delivered else 0, now_iso(), bottle_id),
            )
            event = {"request_id": request_id, "handoff_id": handoff["id"],
                     "from": handoff["from_holder"], "to": actor, "seal_no": observed_seal,
                     "delivered_to_lab": delivered}
            self.append_audit(conn, bottle["item_id"], "handoff_completed", actor, role, event, bottle_id=bottle_id)
            if delivered:
                self.append_audit(conn, bottle["item_id"], "delivered_to_lab", actor, role,
                                  {"bottle_id": bottle_id, "bottle_no": bottle["bottle_no"], "holder": actor})
            conn.execute("COMMIT")
            return self.get_bottle(bottle_id), self._row_to_handoff(
                conn.execute("SELECT * FROM custody_handoffs WHERE id=?", (handoff["id"],)).fetchone()), False
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def submit_receipt(self, bottle_id, receipt_no, concentration, analyzed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            bottle = self._row_to_bottle(self.get_bottle_row(conn, bottle_id))
            existing = conn.execute("SELECT * FROM lab_receipts WHERE receipt_no=?", (receipt_no,)).fetchone()
            if existing is not None:
                if existing["bottle_id"] != bottle_id:
                    raise ConflictError("duplicate_receipt_no", "实验室回执编号已用于其他采样瓶")
                conn.execute("COMMIT")
                return self._row_to_receipt(existing), True
            if bottle["status"] == "quarantined":
                raise DomainError("custody_quarantined", "采样瓶已隔离，实验室结果不予采信", 409)
            if not bottle["delivered"]:
                raise DomainError("custody_incomplete", "保管链不完整：采样瓶尚未送达实验室", 409)
            previous = conn.execute(
                "SELECT id FROM lab_receipts WHERE bottle_id=? AND superseded_by IS NULL ORDER BY id",
                (bottle_id,),
            ).fetchall()
            conn.execute(
                "INSERT INTO lab_receipts(receipt_no,bottle_id,concentration,analyzed_at,actor,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (receipt_no, bottle_id, concentration, analyzed_at, actor, now_iso()),
            )
            receipt_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            superseded_ids = [row["id"] for row in previous]
            if superseded_ids:
                conn.execute("UPDATE lab_receipts SET superseded_by=? WHERE id IN (%s)"
                             % ",".join("?" * len(superseded_ids)), [receipt_id] + superseded_ids)
            event = {"receipt_no": receipt_no, "receipt_id": receipt_id,
                     "concentration": concentration, "analyzed_at": analyzed_at,
                     "superseded_receipt_ids": superseded_ids}
            self.append_audit(conn, bottle["item_id"], "lab_receipt_recorded", actor, role, event, bottle_id=bottle_id)
            item_row = conn.execute("SELECT * FROM items WHERE id=?", (bottle["item_id"],)).fetchone()
            item = self._row_to_item(item_row)
            status, zones, invalidated = self._recompute_item_custody(conn, item, actor, role, "lab_receipt")
            conn.execute("COMMIT")
            receipt = {
                "id": receipt_id,
                "receipt_no": receipt_no,
                "bottle_id": bottle_id,
                "concentration": concentration,
                "analyzed_at": analyzed_at,
                "superseded_by": None,
                "superseded": False,
                "actor": actor,
                "item_status": status,
                "zones": zones,
                "invalidated_zones": invalidated,
            }
            return receipt, False
        except sqlite3.IntegrityError:
            raise ConflictError("duplicate_receipt_no", "实验室回执编号已经存在")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
