from .domain import DomainError

ENTITY_TYPE = "water_contamination"
INITIAL_STATUS = "detected"
CREATE_ROLES = {"analyst", "dispatcher"}
SOURCE_ROLES = {"analyst", "dispatcher", "field_operator", "lab"}
ACTION_ROLES = {
    "verify": {"analyst", "dispatcher"},
    "advise": {"coordinator", "dispatcher"},
    "switch_source": {"coordinator"},
    "flush": {"field_operator"},
    "disinfect": {"field_operator"},
    "sample": {"lab", "field_operator"},
    "restore": {"coordinator", "regulator"},
    "cancel": {"coordinator"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"advise", "switch_source", "flush", "disinfect", "sample", "restore", "cancel"}

REGISTER_BOTTLE_ROLES = {"field_operator", "lab"}
BEGIN_HANDOFF_ROLES = {"field_operator", "courier", "lab"}
CONFIRM_HANDOFF_ROLES = {"field_operator", "courier", "lab"}
LAB_RECEIPT_ROLES = {"lab"}


def custody_zone_states(item_payload):
    custody = item_payload.get("custody")
    if not custody or not custody.get("zones"):
        return {}
    return {zone["zone_id"]: zone["state"] for zone in custody["zones"]}


def sampling_status(item_payload):
    """无保管记录的（旧）事件一律按未采样处理。"""
    zones = custody_zone_states(item_payload)
    if not zones:
        return "unsampled"
    if all(state == "cleared" for state in zones.values()):
        return "cleared"
    if any(state == "detected" for state in zones.values()):
        return "sampled"
    return "sampling"


def recompute_custody_zones(zone_ids, effective_results, limit):
    """effective_results：每个瓶子仅包含当前有效（未被取代）的回执，瓶子必须保管链完整。

    区域状态：
    - pending：尚无保管链完整的回执
    - detected：任一有效回执超过限值
    - cleared：存在有效回执且全部在限值以内
    """
    by_zone = {zone_id: [] for zone_id in zone_ids}
    for result in effective_results:
        if result.get("zone_id") in by_zone:
            by_zone[result["zone_id"]].append(result)
    zones = []
    for zone_id in zone_ids:
        results = sorted(by_zone.get(zone_id, []),
                         key=lambda item: (item.get("analyzed_at") or "", item.get("created_at") or ""))
        if not results:
            state = "pending"
            latest = None
        else:
            latest = results[-1]
            state = "detected" if any(float(r["concentration"]) > float(limit) for r in results) else "cleared"
        zones.append({"zone_id": zone_id, "state": state,
                      "latest": {"bottle_id": latest["bottle_id"], "bottle_no": latest.get("bottle_no"),
                                 "receipt_no": latest["receipt_no"], "concentration": latest["concentration"],
                                 "analyzed_at": latest["analyzed_at"]} if latest else None})
    return zones


def assess(payload):
    concentration = float(payload.get("concentration", 0))
    limit = max(float(payload.get("limit", 0.000001)), 0.000001)
    ratio = concentration / limit
    population = int(payload.get("population", 0))
    score = min(100.0, ratio * 35.0 + min(population / 1000.0, 40.0))
    if score >= 80:
        level = "critical"
    elif score >= 50:
        level = "high"
    elif score >= 20:
        level = "medium"
    else:
        level = "low"
    return {"score": round(score, 2), "level": level, "ratio": round(ratio, 3)}


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "verify":
        _need_status(item, {"detected", "verified"})
        sample_count = int(payload.get("sample_count", 0) or 0)
        if sample_count < 1:
            raise DomainError("sample_required", "需要至少一份复检样本", 409)
        current["assessment"] = assess(current)
        current["verification"] = {"sample_count": sample_count, "note": payload.get("note", "")}
        return "verified", current, {"assessment": current["assessment"], "verification": current["verification"]}

    if action == "advise":
        _need_status(item, {"verified", "advisory"})
        notice_id = _text(payload, "notice_id")
        notice = {
            "notice_id": notice_id,
            "kind": _text(payload, "kind"),
            "message": _text(payload, "message"),
        }
        notices = current.setdefault("notifications", [])
        if any(existing.get("notice_id") == notice_id for existing in notices):
            raise DomainError("duplicate_notification", "同一通知编号不能重复发送", 409)
        notices.append(notice)
        return "advisory", current, {"notice": notice}

    if action == "switch_source":
        _need_status(item, {"verified", "advisory", "flushing", "disinfected", "sampled", "switched"})
        alternate = _text(payload, "alternate_source_id")
        current["alternate_source_id"] = alternate
        return "switched", current, {"alternate_source_id": alternate}

    if action == "flush":
        _need_status(item, {"advisory", "flushing", "switched"})
        zone_id = _text(payload, "zone_id")
        current.setdefault("response_actions", []).append({"type": "flush", "zone_id": zone_id})
        return "flushing", current, {"zone_id": zone_id, "type": "flush"}

    if action == "disinfect":
        _need_status(item, {"flushing", "disinfected"})
        if not payload.get("completed"):
            raise DomainError("disinfection_incomplete", "消毒尚未完成", 409)
        zone_id = _text(payload, "zone_id")
        current.setdefault("response_actions", []).append({"type": "disinfect", "zone_id": zone_id})
        return "disinfected", current, {"zone_id": zone_id, "type": "disinfect"}

    if action == "sample":
        _need_status(item, {"disinfected", "sampled"})
        result = {
            "sample_id": _text(payload, "sample_id"),
            "zone_id": _text(payload, "zone_id"),
            "concentration": float(payload.get("concentration", 0)),
        }
        if result["concentration"] < 0:
            raise DomainError("invalid_concentration", "浓度不能为负数")
        current.setdefault("sample_results", []).append(result)
        return "sampled", current, {"sample_result": result}

    if action == "restore":
        _need_status(item, {"sampled"})
        if not payload.get("all_zones_cleared"):
            raise DomainError("zones_not_cleared", "仍有区域未完成水质恢复", 409)
        custody = current.get("custody")
        zones = custody.get("zones") if custody else None
        if not zones:
            # 旧事件没有保管记录：历史结果仍可查，但按未采样处理，结果不予采信
            raise DomainError("custody_missing", "无完整保管链记录，按未采样处理，不能恢复", 409)
        failed = [zone["zone_id"] for zone in zones if zone["state"] != "cleared"]
        if failed:
            raise DomainError("quality_not_met", "以下区域缺口未闭合或复检未达标：%s" % ",".join(failed), 409)
        current["restoration"] = {
            "actor": actor,
            "note": payload.get("note", ""),
            "zone_ids": [zone["zone_id"] for zone in zones],
        }
        return "restored", current, {"restoration": current["restoration"]}

    if action == "cancel":
        _need_status(item, {"detected", "verified"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
