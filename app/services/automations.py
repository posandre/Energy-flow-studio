from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
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
    trigger_type: str = "measurement"  # measurement|schedule|manual|snapshot_update
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
        has_explicit_flow_payload = ("flow_blocks" in payload) or ("flow_graph" in payload)

        flow_graph_raw = payload.get("flow_graph", {})
        graph = normalize_flow_graph(flow_graph_raw if isinstance(flow_graph_raw, dict) else {})
        if not graph.get("nodes"):
            if blocks:
                graph = build_flow_graph_from_blocks(blocks)
            elif has_explicit_flow_payload:
                graph = normalize_flow_graph({})
            else:
                graph = build_flow_graph_from_legacy_fields(payload)
        if not blocks and graph.get("nodes"):
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


def _normalize_edge_branch(value: object) -> str:
    branch = str(value or "").strip().lower()
    if branch in {"true", "false"}:
        return branch
    return "any"


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
    seen_edges: set[tuple[str, str, str]] = set()
    if isinstance(edges_in, list):
        for item in edges_in:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source", "")).strip()
            target = str(item.get("target", "")).strip()
            branch = _normalize_edge_branch(item.get("branch", "any"))
            if not source or not target:
                continue
            if source not in valid_ids or target not in valid_ids:
                continue
            key = (source, target, branch)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            if branch == "any":
                edges.append({"source": source, "target": target})
            else:
                edges.append({"source": source, "target": target, "branch": branch})

    return {"version": version, "nodes": nodes, "edges": edges}


def build_flow_graph_from_blocks(blocks: list[dict[str, object]]) -> dict[str, object]:
    nodes: list[dict[str, object]] = []
    edges: list[dict[str, str]] = []
    ordered_ids: list[str] = []
    id_set: set[str] = set()
    for idx, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type", "")).strip().lower()
        if not block_type:
            continue
        raw_block_id = str(block.get("_id", "")).strip()
        node_id = raw_block_id or f"n{len(nodes) + 1}"
        if node_id in id_set:
            node_id = f"n{len(nodes) + 1}"
        id_set.add(node_id)
        ordered_ids.append(node_id)
        params = {
            str(key): value
            for key, value in block.items()
            if str(key) != "type" and not str(key).startswith("_")
        }
        nodes.append({"id": node_id, "type": block_type, "params": params})
    if nodes:
        node_types = {str(item["id"]): str(item.get("type", "")).strip().lower() for item in nodes}
        node_params = {
            str(item["id"]): dict(item.get("params", {})) if isinstance(item.get("params", {}), dict) else {}
            for item in nodes
        }
        for source_id in ordered_ids:
            source_type = node_types.get(source_id, "")
            params = node_params.get(source_id, {})
            def _targets(single_key: str, list_key: str) -> list[str]:
                result: list[str] = []
                raw_list = params.get(list_key, [])
                if isinstance(raw_list, list):
                    for value in raw_list:
                        target_id = str(value).strip()
                        if target_id and target_id not in result:
                            result.append(target_id)
                raw_single = str(params.get(single_key, "")).strip()
                if raw_single and raw_single not in result:
                    result.append(raw_single)
                return result
            if source_type in {"condition", "gate"}:
                for true_target in _targets("true_target_id", "true_target_ids"):
                    if true_target and true_target in id_set and true_target != source_id:
                        edges.append({"source": source_id, "target": true_target, "branch": "true"})
                for false_target in _targets("false_target_id", "false_target_ids"):
                    if false_target and false_target in id_set and false_target != source_id:
                        edges.append({"source": source_id, "target": false_target, "branch": "false"})
                continue
            for explicit_next in _targets("next_target_id", "next_target_ids"):
                if explicit_next and explicit_next in id_set and explicit_next != source_id:
                    edges.append({"source": source_id, "target": explicit_next})
    return normalize_flow_graph({"version": FLOW_SCHEMA_VERSION, "nodes": nodes, "edges": edges})


def flow_blocks_from_graph(graph: dict[str, object]) -> list[dict[str, object]]:
    normalized = normalize_flow_graph(graph)
    nodes, adjacency, indegree = _graph_maps(normalized)
    order = _topological_order(nodes, adjacency, indegree)
    blocks: list[dict[str, object]] = []
    blocks_by_id: dict[str, dict[str, object]] = {}
    for node_id in order:
        node = nodes[node_id]
        block: dict[str, object] = {"type": str(node.get("type", "")), "_id": node_id}
        params = node.get("params", {})
        if isinstance(params, dict):
            for key, value in params.items():
                block[str(key)] = value
        blocks.append(block)
        blocks_by_id[node_id] = block
    edges_in = normalized.get("edges", [])
    if isinstance(edges_in, list):
        collected_targets: dict[tuple[str, str], list[str]] = {}
        for edge in edges_in:
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("source", "")).strip()
            target = str(edge.get("target", "")).strip()
            if source not in blocks_by_id or target not in blocks_by_id:
                continue
            source_block = blocks_by_id[source]
            source_type = str(source_block.get("type", "")).strip().lower()
            if source_type not in {"condition", "gate"}:
                collected_targets.setdefault((source, "next"), [])
                if target not in collected_targets[(source, "next")]:
                    collected_targets[(source, "next")].append(target)
                continue
            branch = _normalize_edge_branch(edge.get("branch", "any"))
            if branch == "false":
                collected_targets.setdefault((source, "false"), [])
                if target not in collected_targets[(source, "false")]:
                    collected_targets[(source, "false")].append(target)
            elif branch == "true":
                collected_targets.setdefault((source, "true"), [])
                if target not in collected_targets[(source, "true")]:
                    collected_targets[(source, "true")].append(target)
            elif branch == "any":
                collected_targets.setdefault((source, "true"), [])
                if target not in collected_targets[(source, "true")]:
                    collected_targets[(source, "true")].append(target)
        for (source, branch), targets in collected_targets.items():
            source_block = blocks_by_id.get(source)
            if not isinstance(source_block, dict) or not targets:
                continue
            if branch == "next":
                source_block["next_target_ids"] = targets
                source_block["next_target_id"] = targets[0]
            elif branch == "true":
                source_block["true_target_ids"] = targets
                source_block["true_target_id"] = targets[0]
            elif branch == "false":
                source_block["false_target_ids"] = targets
                source_block["false_target_id"] = targets[0]
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
        self._last_snapshot_digest: str | None = None
        self._last_action_active: dict[str, bool] = {}

    def evaluate_ready_rules(
        self,
        *,
        rules: list[AutomationRule],
        numeric_measurements: dict[str, float],
        now_epoch: float | None = None,
    ) -> list[AutomationRule]:
        now = now_epoch if now_epoch is not None else time.time()
        snapshot_updated = self._update_snapshot_state(numeric_measurements)
        ready: list[AutomationRule] = []
        for rule in rules:
            if not rule.enabled or not rule.active:
                continue
            if self._has_graph(rule):
                if not self._is_graph_triggered(rule, numeric_measurements, now, snapshot_updated=snapshot_updated):
                    continue
            else:
                if not self._is_rule_triggered(rule, numeric_measurements, now, snapshot_updated=snapshot_updated):
                    continue
                if not self._is_conditions_passed(rule, numeric_measurements):
                    continue
            last_run = float(self._last_run_epoch.get(rule.rule_id, 0.0))
            if rule.cooldown_sec > 0 and (now - last_run) < rule.cooldown_sec:
                continue
            ready.append(rule)
        return ready

    @staticmethod
    def _snapshot_digest(measurements: dict[str, float]) -> str:
        if not measurements:
            return "empty"
        chunks: list[str] = []
        for key, value in sorted(measurements.items(), key=lambda item: str(item[0])):
            chunks.append(f"{key}={_format_number(float(value))}")
        payload = "|".join(chunks).encode("utf-8", errors="ignore")
        return hashlib.sha1(payload).hexdigest()

    def _update_snapshot_state(self, measurements: dict[str, float]) -> bool:
        current = self._snapshot_digest(measurements)
        previous = self._last_snapshot_digest
        self._last_snapshot_digest = current
        if previous is None:
            return False
        return current != previous

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
        incoming_edges: dict[str, list[tuple[str, str]]] = {}
        raw_edges = graph.get("edges", [])
        if isinstance(raw_edges, list):
            for edge in raw_edges:
                if not isinstance(edge, dict):
                    continue
                source = str(edge.get("source", "")).strip()
                target = str(edge.get("target", "")).strip()
                if not source or not target:
                    continue
                incoming_edges.setdefault(target, []).append(
                    (source, _normalize_edge_branch(edge.get("branch", "any")))
                )
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
            parent_edges = incoming_edges.get(node_id)
            if not parent_edges:
                parent_edges = [(src, "any") for src, targets in adjacency.items() if node_id in targets]
            parent_active = False if parent_edges else True
            parent_values: list[bool] = []
            if node_type == "gate":
                for parent_id, branch in parent_edges:
                    parent_result = result_map.get(parent_id)
                    if parent_result is None:
                        continue
                    normalized_branch = _normalize_edge_branch(branch)
                    if normalized_branch == "true":
                        if parent_result.status in {"success", "fail"}:
                            parent_active = True
                            parent_values.append(parent_result.status == "success")
                        continue
                    if normalized_branch == "false":
                        if parent_result.status in {"success", "fail"}:
                            parent_active = True
                            parent_values.append(parent_result.status == "fail")
                        continue
                    active, value = _edge_is_active_for_parent(
                        parent_result=parent_result,
                        branch=branch,
                    )
                    if not active:
                        continue
                    parent_active = True
                    parent_values.append(value)
            else:
                for parent_id, branch in parent_edges:
                    active, value = _edge_is_active_for_parent(
                        parent_result=result_map.get(parent_id),
                        branch=branch,
                    )
                    if not active:
                        continue
                    parent_active = True
                    parent_values.append(value)

            status = "skipped"
            reason = "upstream_blocked"

            if node_type == "trigger":
                if force_start:
                    status = "success"
                    reason = "forced_start=True"
                else:
                    ok, trigger_details = self._evaluate_trigger_node_with_details(
                        rule_id=rule.rule_id,
                        node_id=node_id,
                        params=params,
                        measurements=numeric_measurements,
                        now_epoch=now,
                        update_state=False,
                        snapshot_updated=False,
                    )
                    status = "success" if ok else "fail"
                    reason = f"{'trigger_true' if ok else 'trigger_false'}; {trigger_details}"
            elif node_type == "start":
                status = "success"
                reason = "start_ready"
            elif node_type == "end":
                status = "success"
                reason = "end_reached"
            elif node_type == "gate":
                if not parent_edges:
                    status = "fail"
                    reason = "gate_no_inputs"
                elif not parent_values:
                    status = "skipped"
                    reason = "upstream_blocked"
                else:
                    mode = str(params.get("mode", "and")).strip().lower() or "and"
                    passed = all(parent_values) if mode == "and" else any(parent_values)
                    status = "success" if passed else "fail"
                    true_count = sum(1 for value in parent_values if value)
                    false_count = len(parent_values) - true_count
                    reason = (
                        f"gate_{mode}_{'passed' if passed else 'failed'}; "
                        f"inputs={len(parent_values)} true={true_count} false={false_count}"
                    )
            elif not parent_active:
                status = "skipped"
                reason = "upstream_blocked"
                if node_type == "action":
                    self._last_action_active[f"{rule.rule_id}:{node_id}"] = False
            elif node_type == "condition":
                metric_key = str(params.get("metric_key", "")).strip()
                operator = str(params.get("operator", ">=")).strip() or ">="
                expected = _to_float(params.get("value", 0.0))
                device_id = str(params.get("device_id", "")).strip()
                value = _lookup_metric(numeric_measurements, device_id=device_id, metric_key=metric_key)
                lookup_key = _build_metric_lookup_key(device_id, metric_key)
                if value is None:
                    status = "fail"
                    reason = (
                        "metric_missing; "
                        f"device={device_id or '<global>'} metric={metric_key or '<empty>'} lookup={lookup_key} "
                        f"operator={operator} expected={_format_number(expected)} actual=<missing>"
                    )
                else:
                    passed = evaluate_numeric_condition(value, operator, expected)
                    status = "success" if passed else "fail"
                    reason = (
                        f"{'condition_true' if passed else 'condition_false'}; "
                        f"device={device_id or '<global>'} metric={metric_key or '<empty>'} lookup={lookup_key} "
                        f"actual={_format_number(value)} operator={operator} expected={_format_number(expected)}"
                    )
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
                action_state_key = f"{rule.rule_id}:{node_id}"
                previous_active = bool(self._last_action_active.get(action_state_key, False))
                current_active = True
                self._last_action_active[action_state_key] = current_active
                if force_start:
                    status = "success"
                    reason = "action_ready_forced"
                    steps.append(
                        AutomationRunStep(
                            step_type="action",
                            node_id=node_id,
                            payload={
                                "action_type": str(params.get("action_type", "power")).strip() or "power",
                                "device_id": str(params.get("device_id", "")).strip(),
                                "value": params.get("value", False),
                                "bot_token": str(params.get("bot_token", "")).strip(),
                                "chat_id": str(params.get("chat_id", "")).strip(),
                                "retries": max(0, int(_to_float(params.get("retries", 0)))),
                                "retry_delay_sec": max(0.0, _to_float(params.get("retry_delay_sec", 1.0))),
                            },
                        )
                    )
                elif previous_active:
                    status = "skipped"
                    reason = "action_state_unchanged"
                else:
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
                                "bot_token": str(params.get("bot_token", "")).strip(),
                                "chat_id": str(params.get("chat_id", "")).strip(),
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

    def _is_graph_triggered(
        self,
        rule: AutomationRule,
        measurements: dict[str, float],
        now_epoch: float,
        *,
        snapshot_updated: bool,
    ) -> bool:
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
            if self._evaluate_trigger_node(
                rule.rule_id,
                node_id,
                params,
                measurements,
                now_epoch,
                update_state=True,
                snapshot_updated=snapshot_updated,
            ):
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
        snapshot_updated: bool,
    ) -> bool:
        fired, _details = self._evaluate_trigger_node_with_details(
            rule_id=rule_id,
            node_id=node_id,
            params=params,
            measurements=measurements,
            now_epoch=now_epoch,
            update_state=update_state,
            snapshot_updated=snapshot_updated,
        )
        return fired

    def _evaluate_trigger_node_with_details(
        self,
        *,
        rule_id: str,
        node_id: str,
        params: dict[str, object],
        measurements: dict[str, float],
        now_epoch: float,
        update_state: bool,
        snapshot_updated: bool,
    ) -> tuple[bool, str]:
        trigger_type = str(params.get("trigger_type", "measurement")).strip().lower() or "measurement"
        state_key = f"{rule_id}:{node_id}"
        if trigger_type == "manual":
            return False, "trigger_type=manual (requires force_start)"
        if trigger_type == "snapshot_update":
            if update_state:
                self._last_trigger_state[state_key] = bool(snapshot_updated)
            return (
                bool(snapshot_updated),
                "trigger_type=snapshot_update "
                f"snapshot_updated={bool(snapshot_updated)} fired={bool(snapshot_updated)}",
            )
        if trigger_type == "schedule":
            interval = max(1, int(_to_float(params.get("schedule_every_minutes", 15))))
            slot = int(now_epoch // (interval * 60))
            previous_slot = self._last_schedule_slot.get(state_key)
            if previous_slot == slot:
                return (
                    False,
                    "trigger_type=schedule "
                    f"interval_min={interval} slot={slot} previous_slot={previous_slot} fired=False",
                )
            if update_state:
                self._last_schedule_slot[state_key] = slot
            return (
                True,
                "trigger_type=schedule "
                f"interval_min={interval} slot={slot} previous_slot={previous_slot} fired=True",
            )

        metric_key = str(params.get("metric_key", "")).strip()
        operator = str(params.get("operator", ">=")).strip() or ">="
        expected = _to_float(params.get("value", 0.0))
        device_id = str(params.get("device_id", "")).strip()
        value = _lookup_metric(measurements, device_id=device_id, metric_key=metric_key)
        lookup_key = _build_metric_lookup_key(device_id, metric_key)
        if value is None:
            if update_state:
                self._last_trigger_state[state_key] = False
            return (
                False,
                "trigger_type=measurement "
                f"device={device_id or '<global>'} metric={metric_key or '<empty>'} lookup={lookup_key} "
                f"actual=<missing> operator={operator} expected={_format_number(expected)} fired=False",
            )
        current_state = evaluate_numeric_condition(value, operator, expected)
        previous_state = self._last_trigger_state.get(state_key, False)
        if update_state:
            self._last_trigger_state[state_key] = current_state
        fired = current_state and not previous_state
        return (
            fired,
            "trigger_type=measurement "
            f"device={device_id or '<global>'} metric={metric_key or '<empty>'} lookup={lookup_key} "
            f"actual={_format_number(value)} operator={operator} expected={_format_number(expected)} "
            f"current={current_state} previous={previous_state} fired={fired}",
        )

    def _is_rule_triggered(
        self,
        rule: AutomationRule,
        measurements: dict[str, float],
        now_epoch: float,
        *,
        snapshot_updated: bool,
    ) -> bool:
        trigger_type = rule.trigger_type.strip().lower()
        if trigger_type == "manual":
            return False
        if trigger_type == "snapshot_update":
            return bool(snapshot_updated)
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


def _format_number(value: float) -> str:
    text = f"{float(value):.6f}"
    text = text.rstrip("0").rstrip(".")
    if text in {"-0", ""}:
        return "0"
    return text


def _edge_is_active_for_parent(
    *,
    parent_result: AutomationNodeResult | None,
    branch: str,
) -> tuple[bool, bool]:
    if parent_result is None:
        return False, False
    parent_success = parent_result.status == "success"
    normalized = _normalize_edge_branch(branch)
    if normalized == "false":
        if parent_result.status != "fail":
            return False, False
        return True, False
    if normalized == "true":
        if not parent_success:
            return False, False
        return True, True
    if parent_success:
        return True, True
    return False, False
