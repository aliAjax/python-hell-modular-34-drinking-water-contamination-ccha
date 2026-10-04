from datetime import datetime


class DomainError(Exception):
    def __init__(self, code, message, status=400, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


class ConflictError(DomainError):
    def __init__(self, code, message, details=None):
        super().__init__(code, message, 409, details)


class NotFoundError(DomainError):
    def __init__(self, code, message, details=None):
        super().__init__(code, message, 404, details)


def require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def number(payload, name, minimum=None):
    value = payload.get(name)
    if isinstance(value, bool):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    return value


def parse_timestamp(payload, name):
    value = require_text(payload, name)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_timestamp", "%s 必须是 ISO 时间" % name)
    return value


def normalize_create(payload):
    source_id = require_text(payload, "source_id")
    contaminant = require_text(payload, "contaminant")
    detected_at = parse_timestamp(payload, "detected_at")
    concentration = number(payload, "concentration", 0)
    limit = number(payload, "limit", 0.000001)
    zones = payload.get("zone_ids", [])
    if not isinstance(zones, list) or not zones:
        raise DomainError("zones_required", "至少需要一个受影响区域")
    if any(not isinstance(zone, str) or not zone.strip() for zone in zones):
        raise DomainError("invalid_zones", "区域编号必须是字符串列表")
    population = int(payload.get("population", 0) or 0)
    if population < 0:
        raise DomainError("invalid_population", "受影响人数不能为负数")
    stable_key = "%s|%s|%s" % (source_id, contaminant, detected_at)
    return {
        "source_id": source_id,
        "contaminant": contaminant,
        "detected_at": detected_at,
        "concentration": concentration,
        "limit": limit,
        "zone_ids": [zone.strip() for zone in zones],
        "population": population,
        "complaints": int(payload.get("complaints", 0) or 0),
        "notifications": [],
        "response_actions": [],
        "sample_results": [],
        "_stable_key": stable_key,
    }


def normalize_source(payload):
    source_type = require_text(payload, "source_type")
    external_id = require_text(payload, "external_id")
    observed_at = parse_timestamp(payload, "observed_at")
    result = {
        "source_type": source_type,
        "external_id": external_id,
        "observed_at": observed_at,
        "concentration": number(payload, "concentration", 0) if "concentration" in payload else None,
        "zone_id": payload.get("zone_id"),
        "note": payload.get("note", ""),
    }
    return result


KNOWN_ROLES = ("analyst", "dispatcher", "coordinator", "field_operator", "lab", "regulator")


def _zone_id(payload):
    zone_id = payload.get("zone_id")
    if zone_id is not None and (not isinstance(zone_id, str) or not zone_id.strip()):
        raise DomainError("invalid_zone", "区域编号必须是字符串")
    return zone_id.strip() if zone_id else None


def normalize_bottle(payload):
    seal_number = require_text(payload, "seal_number")
    sampled_at = parse_timestamp(payload, "sampled_at")
    zone_id = _zone_id(payload)
    bottle_code = payload.get("bottle_code")
    if bottle_code is not None and (not isinstance(bottle_code, str) or not bottle_code.strip()):
        raise DomainError("invalid_bottle_code", "瓶号必须是字符串")
    return {
        "bottle_code": bottle_code.strip() if bottle_code else None,
        "seal_number": seal_number,
        "sampled_at": sampled_at,
        "zone_id": zone_id,
        "note": payload.get("note", ""),
    }


def normalize_handoff(payload):
    to_handler = require_text(payload, "to_handler")
    to_role = require_text(payload, "to_role")
    if to_role not in KNOWN_ROLES:
        raise DomainError("invalid_role", "接手角色必须是已知角色")
    seal_number = require_text(payload, "seal_number")
    idem = payload.get("idempotency_key")
    if idem is not None and (not isinstance(idem, str) or not idem.strip()):
        raise DomainError("invalid_idempotency_key", "幂等键必须是字符串")
    return {
        "to_handler": to_handler,
        "to_role": to_role,
        "seal_number": seal_number,
        "idempotency_key": idem.strip() if idem else None,
    }


def normalize_receipt(payload):
    receipt_id = require_text(payload, "receipt_id")
    result = number(payload, "result", 0)
    zone_id = _zone_id(payload)
    return {
        "receipt_id": receipt_id,
        "result": result,
        "zone_id": zone_id,
        "note": payload.get("note", ""),
    }
