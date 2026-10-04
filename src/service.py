from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()

    # ---------------- chain of custody ----------------

    def register_bottle(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.BOTTLE_REGISTER_ROLES:
            raise DomainError("forbidden", "当前角色不能登记采样瓶", 403)
        item = self.repository.get_item(item_id)
        if item["status"] == "cancelled":
            raise DomainError("invalid_state", "事件已取消，不能登记采样瓶")
        normalized = domain.normalize_bottle(payload)
        return self.repository.register_bottle(
            item_id,
            normalized["bottle_code"],
            normalized["seal_number"],
            normalized["sampled_at"],
            normalized["zone_id"],
            normalized["note"],
            actor,
            role,
        )

    def list_bottles(self, item_id):
        self.repository.get_item(item_id)
        return self.repository.list_bottles(item_id)

    def get_bottle(self, bottle_id):
        return self.repository.get_bottle(bottle_id)

    def handoff(self, bottle_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        normalized = domain.normalize_handoff(payload)
        return self.repository.submit_handoff(
            bottle_id,
            normalized["to_handler"],
            normalized["to_role"],
            normalized["seal_number"],
            normalized["idempotency_key"],
            actor,
            role,
            payload.get("expected_version"),
        )

    def receive_receipt(self, bottle_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.RECEIPT_ROLES:
            raise DomainError("forbidden", "只有实验室可以提交回执", 403)
        normalized = domain.normalize_receipt(payload)
        return self.repository.receive_receipt(
            bottle_id,
            normalized["receipt_id"],
            normalized["result"],
            normalized["zone_id"],
            normalized["note"],
            actor,
            role,
        )

    def release(self, item_id, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.RELEASE_ROLES:
            raise DomainError("forbidden", "当前角色越权放行", 403)
        if expected_version is None:
            raise DomainError("expected_version_required", "放行需要 expected_version", 400)
        note = payload.get("note", "")
        return self.repository.release_item(item_id, note, actor, role, expected_version)

    def clear_isolation(self, bottle_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.ISOLATION_ROLES:
            raise DomainError("forbidden", "当前角色不能解除隔离", 403)
        seal_number = payload.get("seal_number")
        note = payload.get("note", "")
        return self.repository.clear_isolation(bottle_id, seal_number, note, actor, role)
