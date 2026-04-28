from __future__ import annotations

import time

from app.services.automations import (
    AutomationAction,
    AutomationCondition,
    AutomationEngine,
    AutomationRule,
    build_flow_graph_from_blocks,
    flow_blocks_from_graph,
)


def test_measurement_trigger_fires_only_on_rising_edge() -> None:
    engine = AutomationEngine()
    rule = AutomationRule(
        rule_id="r1",
        name="edge",
        active=True,
        trigger_type="measurement",
        trigger_metric_key="cur_power",
        trigger_operator=">",
        trigger_value=100.0,
        actions=[AutomationAction(action_type="power", device_id="dev", value=True)],
    )

    ready_first = engine.evaluate_ready_rules(rules=[rule], numeric_measurements={"cur_power": 120.0}, now_epoch=1000.0)
    assert len(ready_first) == 1

    ready_second = engine.evaluate_ready_rules(rules=[rule], numeric_measurements={"cur_power": 130.0}, now_epoch=1002.0)
    assert len(ready_second) == 0

    ready_reset = engine.evaluate_ready_rules(rules=[rule], numeric_measurements={"cur_power": 10.0}, now_epoch=1004.0)
    assert len(ready_reset) == 0

    ready_again = engine.evaluate_ready_rules(rules=[rule], numeric_measurements={"cur_power": 150.0}, now_epoch=1006.0)
    assert len(ready_again) == 1


def test_schedule_trigger_single_fire_per_time_slot() -> None:
    engine = AutomationEngine()
    rule = AutomationRule(
        rule_id="sched",
        name="schedule",
        active=True,
        trigger_type="schedule",
        schedule_every_minutes=5,
        actions=[AutomationAction(action_type="power", device_id="dev", value=True)],
    )

    first = engine.evaluate_ready_rules(rules=[rule], numeric_measurements={}, now_epoch=300.0)
    assert len(first) == 1

    second_same_slot = engine.evaluate_ready_rules(rules=[rule], numeric_measurements={}, now_epoch=320.0)
    assert len(second_same_slot) == 0

    third_next_slot = engine.evaluate_ready_rules(rules=[rule], numeric_measurements={}, now_epoch=620.0)
    assert len(third_next_slot) == 1


def test_conditions_logic_any_and_cooldown() -> None:
    engine = AutomationEngine()
    rule = AutomationRule(
        rule_id="logic",
        name="logic",
        active=True,
        trigger_type="measurement",
        trigger_metric_key="cur_power",
        trigger_operator=">",
        trigger_value=100.0,
        cooldown_sec=60,
        conditions_logic="any",
        conditions=[
            AutomationCondition(metric_key="cur_current", operator=">", value=10.0),
            AutomationCondition(metric_key="voltage", operator=">=", value=220.0),
        ],
        actions=[AutomationAction(action_type="power", device_id="dev", value=True)],
    )

    matched = engine.evaluate_ready_rules(
        rules=[rule],
        numeric_measurements={"cur_power": 130.0, "cur_current": 2.0, "voltage": 225.0},
        now_epoch=1000.0,
    )
    assert len(matched) == 1

    engine.mark_rule_executed("logic", now_epoch=1000.0)
    blocked_by_cooldown = engine.evaluate_ready_rules(
        rules=[rule],
        numeric_measurements={"cur_power": 150.0, "voltage": 230.0},
        now_epoch=1020.0,
    )
    assert len(blocked_by_cooldown) == 0

    after_cooldown = engine.evaluate_ready_rules(
        rules=[rule],
        numeric_measurements={"cur_power": 90.0, "voltage": 230.0},
        now_epoch=1100.0,
    )
    assert len(after_cooldown) == 0

    # Re-arm trigger edge then fire again.
    engine.evaluate_ready_rules(rules=[rule], numeric_measurements={"cur_power": 90.0, "voltage": 230.0}, now_epoch=1160.0)
    ready_again = engine.evaluate_ready_rules(
        rules=[rule],
        numeric_measurements={"cur_power": 140.0, "voltage": 230.0},
        now_epoch=1170.0,
    )
    assert len(ready_again) == 1


def test_flow_graph_roundtrip_from_blocks() -> None:
    blocks = [
        {"type": "trigger", "trigger_type": "measurement", "metric_key": "cur_power", "operator": ">", "value": 100.0},
        {"type": "condition", "metric_key": "voltage", "operator": ">=", "value": 220.0},
        {"type": "delay", "seconds": 2.0},
        {"type": "action", "action_type": "power", "device_id": "dev1", "value": True},
    ]
    graph = build_flow_graph_from_blocks(blocks)
    restored = flow_blocks_from_graph(graph)
    assert len(graph["nodes"]) == 4
    assert len(graph["edges"]) == 3
    assert [item["type"] for item in restored] == ["trigger", "condition", "delay", "action"]


def test_rule_deserialize_migrates_legacy_fields_to_graph() -> None:
    payload = {
        "rule_id": "legacy1",
        "name": "legacy",
        "enabled": True,
        "active": True,
        "trigger_type": "measurement",
        "trigger_metric_key": "cur_power",
        "trigger_operator": ">",
        "trigger_value": 100.0,
        "conditions_logic": "all",
        "conditions": [
            {"metric_key": "voltage", "operator": ">=", "value": 220.0},
            {"metric_key": "cur_current", "operator": "<", "value": 10.0},
        ],
        "actions": [{"action_type": "power", "device_id": "dev", "value": True}],
    }
    rule = AutomationRule.from_dict(payload)
    assert rule.flow_graph.get("version") == 1
    assert isinstance(rule.flow_graph.get("nodes"), list)
    assert len(rule.flow_graph["nodes"]) >= 4
    assert any(str(node.get("type")) == "gate" for node in rule.flow_graph["nodes"])


def test_graph_runtime_respects_gate_and_branch_block() -> None:
    engine = AutomationEngine()
    rule = AutomationRule(
        rule_id="g1",
        name="graph",
        active=True,
        enabled=True,
        flow_graph={
            "version": 1,
            "nodes": [
                {"id": "t1", "type": "trigger", "params": {"trigger_type": "measurement", "metric_key": "cur_power", "operator": ">", "value": 100.0}},
                {"id": "c1", "type": "condition", "params": {"metric_key": "voltage", "operator": ">=", "value": 220.0}},
                {"id": "c2", "type": "condition", "params": {"metric_key": "cur_current", "operator": "<", "value": 10.0}},
                {"id": "g2", "type": "gate", "params": {"mode": "and"}},
                {"id": "a1", "type": "action", "params": {"action_type": "power", "device_id": "dev1", "value": True}},
            ],
            "edges": [
                {"source": "t1", "target": "c1"},
                {"source": "t1", "target": "c2"},
                {"source": "c1", "target": "g2"},
                {"source": "c2", "target": "g2"},
                {"source": "g2", "target": "a1"},
            ],
        },
    )

    ready = engine.evaluate_ready_rules(
        rules=[rule],
        numeric_measurements={"cur_power": 130.0, "voltage": 225.0, "cur_current": 12.0},
        now_epoch=1000.0,
    )
    assert len(ready) == 1

    failed_run = engine.run_flow(
        rule=rule,
        numeric_measurements={"cur_power": 130.0, "voltage": 225.0, "cur_current": 12.0},
        force_start=True,
    )
    assert any(item.node_id == "g2" and item.status == "fail" for item in failed_run.node_results)
    assert not [step for step in failed_run.steps if step.step_type == "action"]

    passed_run = engine.run_flow(
        rule=rule,
        numeric_measurements={"cur_power": 130.0, "voltage": 225.0, "cur_current": 8.0},
        force_start=True,
    )
    assert any(item.node_id == "g2" and item.status == "success" for item in passed_run.node_results)
    assert [step for step in passed_run.steps if step.step_type == "action"]


def test_send_message_action_payload_is_preserved() -> None:
    engine = AutomationEngine()
    rule = AutomationRule(
        rule_id="msg1",
        name="message",
        active=True,
        enabled=True,
        flow_graph={
            "version": 1,
            "nodes": [
                {"id": "t1", "type": "trigger", "params": {"trigger_type": "manual"}},
                {
                    "id": "a1",
                    "type": "action",
                    "params": {
                        "action_type": "send_message",
                        "bot_token": "token123",
                        "chat_id": "777",
                        "retries": 1,
                        "retry_delay_sec": 2.0,
                    },
                },
            ],
            "edges": [{"source": "t1", "target": "a1"}],
        },
    )

    run = engine.run_flow(
        rule=rule,
        numeric_measurements={},
        force_start=True,
    )
    action_steps = [step for step in run.steps if step.step_type == "action"]
    assert len(action_steps) == 1
    step_payload = action_steps[0].payload
    assert step_payload.get("action_type") == "send_message"
    assert step_payload.get("bot_token") == "token123"
    assert step_payload.get("chat_id") == "777"
