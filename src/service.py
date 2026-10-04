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
        item["bottles"] = self.repository.list_bottles(item_id)
        item["sampling_status"] = rules.sampling_status(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()

    # ------------------------------------------------------------------
    # 采样瓶保管链
    # ------------------------------------------------------------------
    def _identity(self, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)

    def register_bottle(self, item_id, payload, actor, role):
        self._identity(actor, role)
        if role not in rules.REGISTER_BOTTLE_ROLES:
            raise DomainError("forbidden", "当前角色不能登记采样瓶", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_bottle(payload)
        if normalized["zone_id"] not in item["payload"].get("zone_ids", []):
            raise DomainError("zone_not_in_event", "采样区域 %s 不属于该污染事件" % normalized["zone_id"])
        bottle = self.repository.create_bottle(
            item_id, normalized["bottle_no"], normalized["zone_id"], normalized["sampled_at"],
            normalized["seal_no"], actor, role,
        )
        return self.get_bottle(bottle["id"])

    def begin_handoff(self, bottle_id, payload, actor, role, expected_version=None):
        self._identity(actor, role)
        if role not in rules.BEGIN_HANDOFF_ROLES:
            raise DomainError("forbidden", "当前角色不能发起采样瓶交接", 403)
        normalized = domain.normalize_handoff(payload)
        if normalized["to_role"] not in rules.CONFIRM_HANDOFF_ROLES:
            raise DomainError("invalid_receiver_role", "接收角色不能参与采样瓶保管")
        bottle, handoff, replayed = self.repository.begin_handoff(
            bottle_id, normalized["request_id"], normalized["to_holder"], normalized["to_role"],
            actor, role, expected_version,
        )
        result = self.get_bottle(bottle_id)
        result["handoff"] = handoff
        result["replayed"] = replayed
        return result

    def confirm_handoff(self, bottle_id, payload, actor, role):
        self._identity(actor, role)
        if role not in rules.CONFIRM_HANDOFF_ROLES:
            raise DomainError("forbidden", "当前角色不能确认采样瓶交接", 403)
        normalized = domain.normalize_handoff(payload, require_seal=True)
        bottle, handoff, replayed = self.repository.confirm_handoff(
            bottle_id, normalized["request_id"], actor, role, normalized["observed_seal"]
        )
        result = self.get_bottle(bottle_id)
        result["handoff"] = handoff
        result["replayed"] = replayed
        return result

    def submit_receipt(self, bottle_id, payload, actor, role):
        self._identity(actor, role)
        if role not in rules.LAB_RECEIPT_ROLES:
            raise DomainError("forbidden", "只有实验室角色可以登记回执", 403)
        normalized = domain.normalize_receipt(payload)
        receipt, replayed = self.repository.submit_receipt(
            bottle_id, normalized["receipt_no"], normalized["concentration"],
            normalized["analyzed_at"], actor, role,
        )
        receipt["replayed"] = replayed
        return receipt

    def get_bottle(self, bottle_id):
        bottle = self.repository.get_bottle(bottle_id)
        bottle["handoffs"] = self.repository.list_handoffs(bottle_id)
        bottle["receipts"] = self.repository.list_receipts(bottle_id)
        bottle["audit"] = self.repository.bottle_audit_trail(bottle_id)
        bottle["custody_chain_complete"] = (
            bottle["status"] == "active"
            and bottle["delivered"]
            and all(h["status"] == "completed" for h in bottle["handoffs"])
        )
        return bottle

    def list_bottles(self, item_id=None):
        return self.repository.list_bottles(item_id)
