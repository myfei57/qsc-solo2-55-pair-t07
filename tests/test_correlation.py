"""告警合并、首因标记与投运恢复。

合并口径定死为因果链 + 固定跟随窗：有因果边且在窗内才并组，纯时间近邻不并；
首因不明按系统配置（severity / pending）二选一；停堆只认首因，复位重新投运
硬等首因明确并恢复；信号缺失是部分失败，保护通道缺失故障安全停堆。
"""

from __future__ import annotations

import pytest

from cspplant.alarm.causality import CausalEdge, CausalGraph
from cspplant.alarm.correlator import (
    FOLLOWER,
    ROOT,
    Correlator,
)
from cspplant.alarm.health import SignalHealth, is_lost_rule, lost_rule
from cspplant.alarm.rules import ALARM, WARNING, AlarmEvent
from cspplant.config import AlarmLimits
from cspplant.errors import InterlockError


SEVERITY_LIMITS = AlarmLimits(5.0, 120.0, 30.0, "severity")
PENDING_LIMITS = AlarmLimits(5.0, 120.0, 30.0, "pending")


def graph() -> CausalGraph:
    return CausalGraph(
        [
            CausalEdge("salt-flow-low", "receiver-overtemperature"),
            CausalEdge("receiver-derate", "receiver-overtemperature"),
            CausalEdge("cold-tank-low", "salt-flow-low"),
        ]
    )


def ev(rule: str, severity: str = ALARM) -> AlarmEvent:
    return AlarmEvent(rule, severity, f"sig-{rule}", 999.0, 1.0, rule)


def test_causal_chain_merges_and_marks_first_out():
    corr = Correlator(graph(), SEVERITY_LIMITS)
    first = corr.ingest(10.0, [ev("salt-flow-low")])
    assert len(first.groups) == 1
    gid = first.groups[0].group_id
    assert first.members[0].role == ROOT

    second = corr.ingest(15.0, [ev("receiver-overtemperature")])
    group = corr.group(gid)
    assert len(second.groups) == 1
    assert [item.rule for item in group.members] == [
        "salt-flow-low",
        "receiver-overtemperature",
    ]
    overt = group.member("receiver-overtemperature")
    assert overt.role == FOLLOWER
    assert overt.caused_by == "salt-flow-low"
    assert group.root_rule == "salt-flow-low"


def test_plain_time_neighbours_are_not_merged():
    """同一秒到达但没有因果边的两路告警各成一组：合并不是纯时间窗。"""

    corr = Correlator(graph(), SEVERITY_LIMITS)
    result = corr.ingest(
        10.0, [ev("hot-tank-high", WARNING), ev("steam-pressure", WARNING)]
    )
    assert len(result.groups) == 2
    assert {group.root_rule for group in result.groups} == {"hot-tank-high", "steam-pressure"}


def test_same_batch_effect_before_cause_still_identifies_first_out():
    """同一批次内后继排在前驱前面，合并器也要把先出的因果源头标成首因。"""

    corr = Correlator(graph(), SEVERITY_LIMITS)
    result = corr.ingest(
        10.0,
        [ev("receiver-overtemperature"), ev("salt-flow-low")],
    )
    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.root_rule == "salt-flow-low"
    assert group.member("receiver-overtemperature").role == FOLLOWER
    roles = {member.rule: member.role for member in group.members}
    assert roles == {"receiver-overtemperature": FOLLOWER, "salt-flow-low": ROOT}


def test_effect_outside_follow_window_starts_own_group():
    corr = Correlator(graph(), SEVERITY_LIMITS)
    corr.ingest(10.0, [ev("salt-flow-low")])
    corr.sweep(41.0)
    result = corr.ingest(41.0, [ev("receiver-overtemperature")])
    assert len(result.groups) == 1
    assert result.groups[0].root_rule == "receiver-overtemperature"
    assert len(corr.groups()) == 2


def test_late_causal_predecessor_reassigns_root_with_amendment():
    corr = Correlator(graph(), SEVERITY_LIMITS)
    corr.ingest(10.0, [ev("receiver-overtemperature")])
    gid = corr.groups()[0].group_id
    # severity 策略先按最严重认定首因（此刻它自己），不挂起。
    assert corr.group(gid).root_rule == "receiver-overtemperature"

    result = corr.ingest(20.0, [ev("salt-flow-low")])
    group = corr.group(gid)
    assert group.root_rule == "salt-flow-low"
    assert group.member("receiver-overtemperature").role == FOLLOWER
    assert result.amendments[0]["previous_root"] == "receiver-overtemperature"
    assert result.amendments[0]["root_rule"] == "salt-flow-low"


def test_pending_policy_holds_until_root_known():
    corr = Correlator(graph(), PENDING_LIMITS)
    corr.ingest(10.0, [ev("receiver-overtemperature")])
    gid = corr.groups()[0].group_id
    group = corr.group(gid)
    assert group.root_resolved is False
    assert [item.group_id for item in corr.pending_groups()] == [gid]

    corr.ingest(20.0, [ev("salt-flow-low")])
    assert corr.group(gid).root_rule == "salt-flow-low"
    assert corr.group(gid).root_resolved is True
    assert corr.pending_groups() == []


def test_pending_policy_operator_can_assert_root():
    corr = Correlator(graph(), PENDING_LIMITS)
    corr.ingest(10.0, [ev("receiver-overtemperature")])
    gid = corr.groups()[0].group_id
    corr.sweep(100.0)  # 跟随窗封口后仍未明确，值班员可继续认定
    amendment = corr.assert_root(gid, "receiver-overtemperature", "shift-lead", 110.0)
    assert amendment["root_rule"] == "receiver-overtemperature"
    assert corr.pending_groups() == []
    with pytest.raises(InterlockError):
        corr.assert_root(gid, "receiver-overtemperature", "", 111.0)
    with pytest.raises(InterlockError):
        corr.assert_root(gid, "salt-flow-low", "shift-lead", 111.0)


def test_members_keep_time_order_and_clock_rewind_rejected():
    corr = Correlator(graph(), SEVERITY_LIMITS)
    corr.ingest(10.0, [ev("receiver-overtemperature")])
    corr.ingest(12.0, [ev("salt-flow-low")])
    gid = corr.groups()[0].group_id
    assert [item.ts for item in corr.group(gid).members] == [10.0, 12.0]
    with pytest.raises(InterlockError):
        corr.ingest(9.0, [ev("cold-tank-low", WARNING)])


def test_group_closes_after_quiet_follow_window():
    corr = Correlator(graph(), SEVERITY_LIMITS)
    corr.ingest(10.0, [ev("salt-flow-low")])
    closed = corr.sweep(41.0)
    assert len(closed) == 1
    assert closed[0].state == "closed"
    # 封口后再来的后继不能回挂，只能另开组。
    result = corr.ingest(42.0, [ev("receiver-overtemperature")])
    assert result.groups[0].group_id != closed[0].group_id


def test_root_recovery_gates_group_state():
    corr = Correlator(graph(), SEVERITY_LIMITS)
    corr.ingest(10.0, [ev("salt-flow-low")])
    corr.ingest(11.0, [ev("receiver-overtemperature")])
    gid = corr.groups()[0].group_id
    # 只恢复带出的那条，首因没恢复 → 仍不能投运。
    corr.mark_cleared(30.0, ["receiver-overtemperature"])
    group = corr.group(gid)
    assert group.root_recovered() is False
    assert group.all_recovered() is False
    corr.mark_cleared(31.0, ["salt-flow-low"])
    assert corr.group(gid).root_recovered() is True
    assert corr.group(gid).all_recovered() is True


def test_critical_signal_loss_is_alarm_non_critical_is_warning():
    health = SignalHealth(["a", "b"], {"a"})
    raised, recovered = health.poll({"b": 1.0}, 5.0)
    assert recovered == []
    assert len(raised) == 1
    lost = raised[0]
    assert lost.rule == lost_rule("a")
    assert is_lost_rule(lost.rule)
    assert lost.severity == ALARM
    assert health.trips_plant(lost.rule) is True
    # 信号回来产生恢复边沿，不是新告警。
    raised2, recovered2 = health.poll({"a": 1.0, "b": 1.0}, 8.0)
    assert raised2 == []
    assert recovered2 == ["a"]
    # 非保护通道缺失只报警，不具备停堆资格。
    raised3, _ = health.poll({}, 9.0)
    lost_b = next(item for item in raised3 if item.rule == lost_rule("b"))
    assert lost_b.severity == WARNING
    assert health.trips_plant(lost_b.rule) is False
