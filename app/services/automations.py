from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import time
import uuid

FLOW_SCHEMA_VERSION = 1


@dataclass(slots=True)
class AutomationCondition:
    metric_key: str
    operator: str
    value: float
    device_id: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "metric_key": self.metric_key,
            "operator": self.operator,
            "value": float(self.value),
            "device_id": self.device_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "AutomationCondition":
        return cls(
            metric_key=str(payload.get("metric_key", "")).strip(),
            operator=str(payload.get("operator", ">=")).strip() or ">=",
            value=_to_float(payload.get("value", 0.0)),
            device_id=str(payload.get("device_id", "")).strip(),
        )


@dataclass(slots=True)
class AutomationAction:
    action_type: str
    device_id: str
    value: object
    retries: int = 0
    retry_delay_sec: float = 1.0

    def to_dict(self) -> dict[str, object]:
        return {
            "action_type": self.action_type,
            "device_id": self.device_id,
            "value": self.value,
            "retries": int(max(0, self.retries)),
            "retry_delay_sec": float(max(0.0, self.retry_delay_sec)),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "AutomationAction":
        return cls(
            action_type=str(payload.get("action_type", "power")).strip() or "power",
            device_id=str(payload.get("device_id", "")).strip(),
            value=payload.get("value", False),
            retries=max(0, int(_to_float(payload.get("retries", 0)))),
            retry_delay_sec=max(0.0, _to_float(payload.get("retry_delay_sec", 1.0))),
        )


@dataclass(slots=True)
class AutomationRule:
    rule_id: str
    name: str
    enabled: bool = True
    active: bool = False
    trigger_type: str = "measurement"  # measurement|schedule|manual
    trigger_metric_key: str = ""
    trigger_operator: str = ">="
    trigger_value: float = 0.0
    trigger_device_id: str = ""
    schedule_every_minutes: int = 15
    conditions_logic: str = "all"  # all|any
    conditions: list[AutomationCondition] = field(default_factory=list)
    actions: list[AutomationAction] = field(default_factory=list)
    delay_before_actions_sec: float = 0.0
    cooldown_sec: int = 60
    flow_blocks: list[dict[str, object]] = field(default_factory=list)
    flow_graph: dict[str, object] = field(default_factory=dict)
    version: int = 1
    draft_version: int = 1
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, object]:
        graph = normalize_flow_graph(self.flow_graph)
        if not graph.get("nodes"):
            graph = build_flow_graph_from_blocks(self.flow_blocks)
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "enabled": bool(self.enabled),
            "active": bool(self.active),
            "trigger_type": self.trigger_type,
            "trigger_metric_key": self.trigger_metric_key,
            "trigger_operator": self.trigger_operator,
            "trigger_value": float(self.trigger_value),
            "trigger_device_id": self.trigger_device_id,
            "schedule_every_minutes": int(max(1, self.schedule_every_minutes)),
            "conditions_logic": self.conditions_logic,
            "conditions": [item.to_dict() for item in self.conditions],
            "actions": [item.to_dict() for item in self.actions],
            "delay_before_actions_sec": float(max(0.0, self.delay_before_actions_sec)),
            "cooldown_sec": int(max(0, self.cooldown_sec)),
            "flow_blocks": list(self.flow_blocks),
            "flow_graph": graph,
            "version": int(max(1, self.version)),
            "draft_version": int(max(1, self.draft_version)),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "AutomationRule":
        rule_id = str(payload.get("rule_id", "")).strip() or uuid.uuid4().hex
        conditions_raw = payload.get("conditions", [])
        actions_raw = payload.get("actions", [])
        conditions: list[AutomationCondition] = []
        actions: list[AutomationAction] = []
        if isinstance(conditions_raw, list):
            for item in conditions_raw:
                if isinstance(item, dict):
                    conditions.append(AutomationCondition.from_dict(item))
        if isinstance(actions_raw, list):
            for item in actions_raw:
                if isinstance(item, dict):
                    actions.append(AutomationAction.from_dict(item))

        flow_blocks = payload.get("flow_blocks", [])
        blocks = list(flow_blocks) if isinstance(flow_blocks, list) else []

        flow_graph_raw = payload.get("flow_graph", {})
        graph = normalize_flow_graph(flow_graph_raw if isinstance(flow_graph_raw, dict) else {})
        if not graph.get("nodes"):
            if blocks:
                graph = build_flow_graph_from_blocks(blocks)
            else:
                graph = build_flow_graph_from_legacy_fields(payload)
        if not blocks:
            blocks = flow_blocks_from_graph(graph)

        return cls(
            rule_id=rule_id,
            name=str(payload.get("name", "Automation")).strip() or "Automation",
            enabled=bool(payload.get("enabled", True)),
            active=bool(payload.get("active", False)),
            trigger_type=str(payload.get("trigger_type", "measurement")).strip() or "measurement",
            trigger_metric_key=str(payload.get("trigger_metric_key", "")).strip(),
            trigger_operator=str(payload.get("trigger_operator", ">=")).strip() or ">=",
            trigger_value=_to_float(payload.get("trigger_value", 0.0)),
            trigger_device_id=str(payload.get("trigger_device_id", "")).strip(),
            schedule_every_minutes=max(1, int(_to_float(payload.get("schedule_every_minutes", 15)))),
            conditions_logic=str(payload.get("conditions_logic", "all")).strip().lower() or "all",
            conditions=conditions,
            actions=actions,
            delay_before_actions_sec=max(0.0, _to_float(payload.get("delay_before_actions_sec", 0.0))),
            cooldown_sec=max(0, int(_to_float(payload.get("cooldown_sec", 60)))),
            flow_blocks=blocks,
            flow_graph=graph,
            version=max(1, int(_to_float(payload.get("version", 1)))),
            draft_version=max(1, int(_to_float(payload.get("draft_version", 1)))),
            updated_at=str(payload.get("updated_at", datetime.now().isoformat(timespec="seconds"))).strip(),
        )


@dataclass(slots=True)
class AutomationLogEntry:
    timestamp: str
    level: str
    rule_id: str
    rule_name: str
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "message": self.message,
        }


@dataclass(slots=True)
class AutomationNodeResult:
    node_id: str
    node_type: str
    status: str  # success|fail|skipped
    reason: str
    timestamp: str


@dataclass(slots=True)
class AutomationRunStep:
    step_type: str  # delay|action
    node_id: str
    payload: dict[str, object]


@dataclass(slots=True)
class AutomationFlowRun:
    node_results: list[AutomationNodeResult]
    steps: list[AutomationRunStep]


def normalize_flow_graph(payload: dict[str, object]) -> dict[str, object]:
    version = max(1, int(_to_float(payload.get("version", FLOW_SCHEMA_VERSION))))
    nodes_in = payload.get("nodes", [])
    edges_in = payload.get("edges", [])

    nodes: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    if isinstance(nodes_in, list):
        for idx, item in enumerate(nodes_in):
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("id", "")).strip() or f"n{idx + 1}"
            if node_id in seen_ids:
                continue
            seen_ids.add(node_id)
            node_type = str(item.get("type", "")).strip().lower()
            if not node_type:
                continue
            params_raw = item.get("params", {})
            params = dict(params_raw) if isinstance(params_raw, dict) else {}
            nodes.append({"id": node_id, "type": node_type, "params": params})

    valid_ids = {str(item["id"]) for item in nodes}
    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()
    if isinstance(edges_in, list):
        for item in edges_in:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", "")).strip()
            target = str(item.get("target", "")).strip()
            if not source or not target:
                continue
            if source not in valid_ids or target not in valid_ids:
                continue
            key = (source, target)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append({"source": source, "target": target})

    return {"version": version, "nodes": nodes, "edges": edges}


def build_flow_graph_from_blocks(blocks: list[dict[str, object]]) -> dict[str, object]:
    nodes: list[dict[str, object]] = []
    edges: list[dict[str, str]] = []
    prev_id = ""
    for idx, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type", "")).strip().lower()
        if not block_type:
            continue
        node_id = f"n{len(nodes) + 1}"
        params = {str(key): value for key, value in block.items() if str(key) != "type"}
        nodes.append({"id": node_id, "type": block_type, "params": params})
        if prev_id:
            edges.append({"source": prev_id, "target": node_id})
        prev_id = node_id
    return normalize_flow_graph({"version": FLOW_SCHEMA_VERSION, "nodes": nodes, "edges": edges})


def flow_blocks_from_graph(graph: dict[str, object]) -> list[dict[str, object]]:
    normalized = normalize_flow_graph(graph)
    nodes, adjacency, indegree = _graph_maps(normalized)
    order = _topological_order(nodes, adjacency, indegree)
    blocks: list[dict[str, object]] = []
    for node_id in order:
        node = nodes[node_id]
        block: dict[str, object] = {"type": str(node.get("type", ""))}
        params = node.get("params", {})
        if isinstance(params, dict):
            for key, value in params.items():
                block[str(key)] = value
        blocks.append(block)
    return blocks


def build_flow_graph_from_legacy_fields(payload: dict[str, object]) -> dict[str, object]:
    blocks: list[dict[str, object]] = []
    trigger_type = str(payload.get("trigger_type", "measurement")).strip() or "measurement"
    blocks.append(
        {
            "type": "trigger",
            "trigger_type": trigger_type,
            "metric_key": str(payload.get("trigger_metric_key", "")).strip(),
            "operator": str(payload.get("trigger_operator", ">=")).strip() or ">=",
            "value": _to_float(payload.get("trigger_value", 0.0)),
            "device_id": str(payload.get("trigger_device_id", "")).strip(),
            "schedule_every_minutes": max(1, int(_to_float(payload.get("schedule_every_minutes", 15)))),
        }
    )

    conditions_logic = str(payload.get("conditions_logic", "all")).strip().lower() or "all"
    conditions_raw = payload.get("conditions", [])
    condition_ids: list[str] = []
    if isinstance(conditions_raw, list):
        for item in conditions_raw:
            if not isinstance(item, dict):
                continue
            condition_node = {
                "id": f"n{len(condition_ids) + 2}",
                "type": "condition",
                "params": {
                    "metric_key": str(item.get("metric_key", "")).strip(),
                    "operator": str(item.get("operator", ">=")).strip() or ">=",
                    "value": _to_float(item.get("value", 0.0)),
                    "device_id": str(item.get("device_id", "")).strip(),
                },
            }
            condition_ids.append(str(condition_node["id"]))
            blocks.append({"type": "condition", **dict(condition_node["params"])})

    delay_sec = max(0.0, _to_float(payload.get("delay_before_actions_sec", 0.0)))
    if delay_sec > 0:
        blocks.append({"type": "delay", "seconds": delay_sec})

    actions_raw = payload.get("actions", [])
    if isinstance(actions_raw, list):
        for action_raw in actions_raw:
            if not isinstance(action_raw, dict):
                continue
            blocks.append(
                {
                    "type": "action",
                    "action_type": str(action_raw.get("action_type", "power")).strip() or "power",
                    "device_id": str(action_raw.get("device_id", "")).strip(),
                    "value": action_raw.get("value", False),
                    "retries": max(0, int(_to_float(action_raw.get("retries", 0)))),
                    "retry_delay_sec": max(0.0, _to_float(action_raw.get("retry_delay_sec", 1.0))),
                }
            )

    graph = build_flow_graph_from_blocks(blocks)
    if not condition_ids:
        return graph

    # Insert a gate when multiple conditions exist so the new graph has explicit logic.
    normalized = normalize_flow_graph(graph)
    nodes = list(normalized.get("nodes", []))
    edges = list(normalized.get("edges", []))
    if len(condition_ids) > 1:
        gate_id = f"n{len(nodes) + 1}"
        gate_mode = "and" if conditions_logic == "all" else "or"
        nodes.append({"id": gate_id, "type": "gate", "params": {"mode": gate_mode}})

        trigger_id = ""
        for node in nodes:
            if str(node.get("type", "")) == "trigger":
                trigger_id = str(node.get("id", ""))
                break

        outgoing_from_conditions: list[str] = []
        retained_edges: list[dict[str, str]] = []
        condition_id_set = set(condition_ids)
        for edge in edges:
            source = str(edge.get("source", ""))
            target = str(edge.get("target", ""))
            if source in condition_id_set:
                outgoing_from_conditions.append(target)
                continue
            if trigger_id and source == trigger_id and target in condition_id_set:
                continue
            retained_edges.append({"source": source, "target": target})

        for cond_id in condition_ids:
            retained_edges.append({"source": trigger_id, "target": cond_id})
            retained_edges.append({"source": cond_id, "target": gate_id})

        unique_targets = []
        for target in outgoing_from_conditions:
            if target and target not in unique_targets and target != gate_id:
                unique_targets.append(target)
        if not unique_targets:
            # keep chain continuity: connect gate to the node after the last condition when possible
            order = _topological_order(*_graph_maps(normalized))
            for idx, node_id in enumerate(order):
                if node_id in condition_id_set and idx + 1 < len(order):
                    next_id = order[idx + 1]
                    if next_id not in condition_id_set:
                        unique_targets.append(next_id)
                        break

        for target in unique_targets:
            retained_edges.append({"source": gate_id, "target": target})

        return normalize_flow_graph({"version": FLOW_SCHEMA_VERSION, "nodes": nodes, "edges": retained_edges})

    return normalized


def deserialize_rules(payload: object) -> list[AutomationRule]:
    if not isinstance(payload, list):
        return []
    items: list[AutomationRule] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        items.append(AutomationRule.from_dict(item))
    return items


def serialize_rules(rules: list[AutomationRule]) -> list[dict[str, object]]:
    return [rule.to_dict() for rule in rules]


def deserialize_logs(payload: object) -> list[AutomationLogEntry]:
    if not isinstance(payload, list):
        return []
    items: list[AutomationLogEntry] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        items.append(
            AutomationLogEntry(
                timestamp=str(item.get("timestamp", "")).strip(),
                level=str(item.get("level", "info")).strip() or "info",
                rule_id=str(item.get("rule_id", "")).strip(),
                rule_name=str(item.get("rule_name", "")).strip(),
                message=str(item.get("message", "")).strip(),
            )
        )
    return items


def serialize_logs(logs: list[AutomationLogEntry]) -> list[dict[str, object]]:
    return [item.to_dict() for item in logs]


def evaluate_numeric_condition(value: float, operator: str, expected: float) -> bool:
    op = operator.strip()
    if op == ">":
        return value > expected
    if op == ">=":
        return value >= expected
    if op == "<":
        return value < expected
    if op == "<=":
        return value <= expected
    if op == "==":
        return abs(value - expected) < 1e-9
    if op == "!=":
        return abs(value - expected) >= 1e-9
    return False


class AutomationEngine:
    """Evaluates and executes automation rules with cooldown/retry protections."""

    def __init__(self) -> None:
        self._last_run_epoch: dict[str, float] = {}
        self._last_schedule_slot: dict[str, int] = {}
        self._last_trigger_state: dict[str, bool] = {}

    def evaluate_ready_rules(
        self,
        *,
        rules: list[AutomationRule],
        numeric_measurements: dict[str, float],
        now_epoch: float | None = None,
    ) -> list[AutomationRule]:
        now = now_epoch if now_epoch is not None else time.time()
        ready: list[AutomationRule] = []
        for rule in rules:
            if not rule.enabled or not rule.active:
                continue
            if self._has_graph(rule):
                if not self._is_graph_triggered(rule, numeric_measurements, now):
                    continue
            else:
                if not self._is_rule_triggered(rule, numeric_measurements, now):
                    continue
                if not self._is_conditions_passed(rule, numeric_measurements):
                    continue
            last_run = float(self._last_run_epoch.get(rule.rule_id, 0.0))
            if rule.cooldown_sec > 0 and (now - last_run) < rule.cooldown_sec:
                continue
            ready.append(rule)
        return ready

    def mark_rule_executed(self, rule_id: str, *, now_epoch: float | None = None) -> None:
        self._last_run_epoch[rule_id] = now_epoch if now_epoch is not None else time.time()

    def run_flow(
        self,
        *,
        rule: AutomationRule,
        numeric_measurements: dict[str, float],
        now_epoch: float | None = None,
        force_start: bool = False,
    ) -> AutomationFlowRun:
        now = now_epoch if now_epoch is not None else time.time()
        timestamp = datetime.fromtimestamp(now).isoformat(timespec="seconds")

        graph = self._rule_graph(rule)
        nodes, adjacency, indegree = _graph_maps(graph)
        order = _topological_order(nodes, adjacency, indegree)
        order_set = set(order)

        node_results: list[AutomationNodeResult] = []
        result_map: dict[str, AutomationNodeResult] = {}
        steps: list[AutomationRunStep] = []

        for node_id, node in nodes.items():
            if node_id not in order_set:
                result = AutomationNodeResult(
                    node_id=node_id,
                    node_type=str(node.get("type", "")),
                    status="fail",
                    reason="cycle_detected",
                    timestamp=timestamp,
                )
                node_results.append(result)
                result_map[node_id] = result

        for node_id in order:
            node = nodes[node_id]
            node_type = str(node.get("type", "")).strip().lower()
            params = node.get("params", {})
            params = dict(params) if isinstance(params, dict) else {}
            parents = [src for src, targets in adjacency.items() if node_id in targets]
            parent_states = [result_map[parent].status == "success" for parent in parents if parent in result_map]
            parent_active = any(parent_states) if parents else True

            status = "skipped"
            reason = "upstream_blocked"

            if node_type == "trigger":
                if force_start:
                    status = "success"
                    reason = "forced_start"
                else:
                    ok = self._evaluate_trigger_node(rule.rule_id, node_id, params, numeric_measurements, now, update_state=False)
                    status = "success" if ok else "fail"
                    reason = "trigger_true" if ok else "trigger_false"
            elif node_type == "gate":
                if not parents:
                    status = "fail"
                    reason = "gate_no_inputs"
                else:
                    mode = str(params.get("mode", "and")).strip().lower() or "and"
                    passed = all(parent_states) if mode == "and" else any(parent_states)
                    status = "success" if passed else "fail"
                    reason = f"gate_{mode}_{'passed' if passed else 'failed'}"
            elif not parent_active:
                status = "skipped"
                reason = "upstream_blocked"
            elif node_type == "condition":
                metric_key = str(params.get("metric_key", "")).strip()
                operator = str(params.get("operator", ">=")).strip() or ">="
                expected = _to_float(params.get("value", 0.0))
                device_id = str(params.get("device_id", "")).strip()
                value = _lookup_metric(numeric_measurements, device_id=device_id, metric_key=metric_key)
                if value is None:
                    status = "fail"
                    reason = "metric_missing"
                else:
                    passed = evaluate_numeric_condition(value, operator, expected)
                    status = "success" if passed else "fail"
                    reason = "condition_true" if passed else "condition_false"
            elif node_type == "delay":
                seconds = max(0.0, _to_float(params.get("seconds", 0.0)))
                status = "success"
                reason = "delay_ready"
                steps.append(
                    AutomationRunStep(
                        step_type="delay",
                        node_id=node_id,
                        payload={"seconds": seconds},
                    )
                )
            elif node_type == "action":
                status = "success"
                reason = "action_ready"
                steps.append(
                    AutomationRunStep(
                        step_type="action",
                        node_id=node_id,
                        payload={
                            "action_type": str(params.get("action_type", "power")).strip() or "power",
                            "device_id": str(params.get("device_id", "")).strip(),
                            "value": params.get("value", False),
                            "retries": max(0, int(_to_float(params.get("retries", 0)))),
                            "retry_delay_sec": max(0.0, _to_float(params.get("retry_delay_sec", 1.0))),
                        },
                    )
                )
            else:
                status = "fail"
                reason = "unsupported_node"

            result = AutomationNodeResult(
                node_id=node_id,
                node_type=node_type,
                status=status,
                reason=reason,
                timestamp=timestamp,
            )
            node_results.append(result)
            result_map[node_id] = result

        return AutomationFlowRun(node_results=node_results, steps=steps)

    def _has_graph(self, rule: AutomationRule) -> bool:
        graph = normalize_flow_graph(rule.flow_graph)
        return bool(graph.get("nodes"))

    def _rule_graph(self, rule: AutomationRule) -> dict[str, object]:
        graph = normalize_flow_graph(rule.flow_graph)
        if graph.get("nodes"):
            return graph
        if rule.flow_blocks:
            return build_flow_graph_from_blocks(rule.flow_blocks)
        return build_flow_graph_from_legacy_fields(rule.to_dict())

    def _is_graph_triggered(self, rule: AutomationRule, measurements: dict[str, float], now_epoch: float) -> bool:
        graph = self._rule_graph(rule)
        nodes = graph.get("nodes", [])
        if not isinstance(nodes, list):
            return False
        triggered = False
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if str(node.get("type", "")).strip().lower() != "trigger":
                continue
            node_id = str(node.get("id", "")).strip()
            params_raw = node.get("params", {})
            params = dict(params_raw) if isinstance(params_raw, dict) else {}
            if self._evaluate_trigger_node(rule.rule_id, node_id, params, measurements, now_epoch, update_state=True):
                triggered = True
        return triggered

    def _evaluate_trigger_node(
        self,
        rule_id: str,
        node_id: str,
        params: dict[str, object],
        measurements: dict[str, float],
        now_epoch: float,
        *,
        update_state: bool,
    ) -> bool:
        trigger_type = str(params.get("trigger_type", "measurement")).strip().lower() or "measurement"
        state_key = f"{rule_id}:{node_id}"
        if trigger_type == "manual":
            return False
        if trigger_type == "schedule":
            interval = max(1, int(_to_float(params.get("schedule_every_minutes", 15))))
            slot = int(now_epoch // (interval * 60))
            previous_slot = self._last_schedule_slot.get(state_key)
            if previous_slot == slot:
                return False
            if update_state:
                self._last_schedule_slot[state_key] = slot
            return True

        metric_key = str(params.get("metric_key", "")).strip()
        operator = str(params.get("operator", ">=")).strip() or ">="
        expected = _to_float(params.get("value", 0.0))
        device_id = str(params.get("device_id", "")).strip()
        value = _lookup_metric(measurements, device_id=device_id, metric_key=metric_key)
        if value is None:
            if update_state:
                self._last_trigger_state[state_key] = False
            return False
        current_state = evaluate_numeric_condition(value, operator, expected)
        previous_state = self._last_trigger_state.get(state_key, False)
        if update_state:
            self._last_trigger_state[state_key] = current_state
        return current_state and not previous_state

    def _is_rule_triggered(self, rule: AutomationRule, measurements: dict[str, float], now_epoch: float) -> bool:
        trigger_type = rule.trigger_type.strip().lower()
        if trigger_type == "manual":
            return False
        if trigger_type == "schedule":
            interval = max(1, int(rule.schedule_every_minutes))
            slot = int(now_epoch // (interval * 60))
            previous_slot = self._last_schedule_slot.get(rule.rule_id)
            if previous_slot == slot:
                return False
            self._last_schedule_slot[rule.rule_id] = slot
            return True

        value = _lookup_metric(measurements, device_id=rule.trigger_device_id, metric_key=rule.trigger_metric_key)
        if value is None:
            return False
        current_state = evaluate_numeric_condition(value, rule.trigger_operator, rule.trigger_value)
        previous_state = self._last_trigger_state.get(rule.rule_id, False)
        self._last_trigger_state[rule.rule_id] = current_state
        return current_state and not previous_state

    def _is_conditions_passed(self, rule: AutomationRule, measurements: dict[str, float]) -> bool:
        if not rule.conditions:
            return True
        outcomes: list[bool] = []
        for condition in rule.conditions:
            value = _lookup_metric(measurements, device_id=condition.device_id, metric_key=condition.metric_key)
            if value is None:
                outcomes.append(False)
                continue
            outcomes.append(evaluate_numeric_condition(value, condition.operator, condition.value))
        if rule.conditions_logic == "any":
            return any(outcomes)
        return all(outcomes)


def build_rule_analytics(logs: list[AutomationLogEntry]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for entry in logs:
        bucket = summary.setdefault(entry.rule_id, {"ok": 0, "error": 0, "info": 0})
        level = (entry.level or "info").strip().lower()
        if level not in bucket:
            level = "info"
        bucket[level] += 1
    return summary


def _graph_maps(
    graph: dict[str, object],
) -> tuple[dict[str, dict[str, object]], dict[str, list[str]], dict[str, int]]:
    nodes_in = graph.get("nodes", [])
    edges_in = graph.get("edges", [])

    nodes: dict[str, dict[str, object]] = {}
    adjacency: dict[str, list[str]] = {}
    indegree: dict[str, int] = {}

    if isinstance(nodes_in, list):
        for item in nodes_in:
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("id", "")).strip()
            if not node_id:
                continue
            nodes[node_id] = {
                "id": node_id,
                "type": str(item.get("type", "")).strip().lower(),
                "params": dict(item.get("params", {})) if isinstance(item.get("params", {}), dict) else {},
            }
            adjacency.setdefault(node_id, [])
            indegree.setdefault(node_id, 0)

    if isinstance(edges_in, list):
        for edge in edges_in:
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("source", "")).strip()
            target = str(edge.get("target", "")).strip()
            if source not in nodes or target not in nodes:
                continue
            adjacency.setdefault(source, []).append(target)
            indegree[target] = indegree.get(target, 0) + 1

    return nodes, adjacency, indegree


def _topological_order(
    nodes: dict[str, dict[str, object]],
    adjacency: dict[str, list[str]],
    indegree: dict[str, int],
) -> list[str]:
    queue = deque(sorted([node_id for node_id, degree in indegree.items() if degree == 0]))
    degree_map = dict(indegree)
    order: list[str] = []
    while queue:
        node_id = queue.popleft()
        order.append(node_id)
        for target in adjacency.get(node_id, []):
            degree_map[target] = degree_map.get(target, 0) - 1
            if degree_map[target] == 0:
                queue.append(target)
    return order


def _lookup_metric(measurements: dict[str, float], *, device_id: str, metric_key: str) -> float | None:
    key = _build_metric_lookup_key(device_id, metric_key)
    value = measurements.get(key)
    if value is None and metric_key.strip():
        value = measurements.get(metric_key.strip().lower())
    return value


def _build_metric_lookup_key(device_id: str, metric_key: str) -> str:
    clean_metric = metric_key.strip().lower()
    clean_device = device_id.strip()
    if clean_device:
        return f"{clean_device}:{clean_metric}"
    return clean_metric


def _to_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
