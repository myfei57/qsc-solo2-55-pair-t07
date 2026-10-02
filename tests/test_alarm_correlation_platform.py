"""合并/首因在平台控制循环里的接线：停堆只认首因、投运硬等首因恢复、审计一致。"""

from __future__ import annotations

import dataclasses

from cspplant.alarm.rules import AlarmEvent
from cspplant.app import build_plant
from cspplant.config import default_config


def _force_rules(platform, events_by_ts):
    """把规则判定替换成按周期返回固定事件，用来精确构造告警批次。

    仍按真实规则表尊重使能标志（例如低流量联锁只在通盐后成立）。
    """

    calls = {"n": 0}

    def fake_evaluate(signals, context=None):
        wanted = events_by_ts.get(calls["n"], [])
        calls["n"] += 1
        context = context or {}
        kept = []
        for event in wanted:
            rule = platform.rules.by_name(event.rule)
            if rule and rule.enable_flag and not bool(context.get(rule.enable_flag)):
                continue
            kept.append(event)
        return kept

    platform.rules.evaluate = fake_evaluate  # type: ignore[method-assign]


def _alarm_event(rule, severity="alarm"):
    return AlarmEvent(rule, severity, f"sig-{rule}", 999.0, 1.0, rule)


def test_first_out_root_drives_trip_and_latch_carries_group(online, clock):
    platform = online
    # 低流量先出（它就是首因，具备停堆资格，当周期停堆）；超温随后被带出，
    # 并入同一组且不重复下停堆结论。
    _force_rules(
        platform,
        {
            0: [_alarm_event("salt-flow-low")],
            1: [_alarm_event("receiver-overtemperature")],
        },
    )
    platform.cycle()
    assert platform.latch.latched() is True
    trips_before = platform.latch.trips()

    platform.cycle()
    assert platform.latch.trips() == trips_before

    gid = platform.latch.group_id()
    group = platform.correlator.group(gid)
    # 停堆原因是先出的低流量首因，不是后出的超温。
    assert platform.latch.reason() == "salt-flow-low"
    assert platform.latch.root_rule() == "salt-flow-low"
    assert group.root_rule == "salt-flow-low"
    assert group.tripped is True
    assert group.member("receiver-overtemperature").role == "follower"


def test_follower_overtemperature_alone_does_not_trip(online):
    platform = online
    _force_rules(
        platform,
        {
            0: [_alarm_event("receiver-derate", "warning")],
            1: [_alarm_event("receiver-overtemperature")],
        },
    )
    platform.cycle()
    platform.cycle()
    # 超温挂在降额后面，而降额不是停堆规则：该组首因不具备停堆资格，
    # 被带出的超温不重复下停堆结论。
    assert platform.latch.latched() is False


def test_reset_blocked_until_root_recovered(online, clock):
    platform = online
    _force_rules(
        platform,
        {
            0: [_alarm_event("salt-flow-low")],
            1: [_alarm_event("receiver-overtemperature")],
            2: [_alarm_event("receiver-overtemperature")],
        },
    )
    platform.cycle()
    platform.cycle()
    assert platform.latch.latched() is True
    clock.advance(platform.config.alarm.latch_reset_delay_s + 1.0)

    blocked = platform.reset_trip("shift-lead")
    assert blocked["ok"] is False
    assert any("首因未恢复" in item for item in blocked["violations"])
    assert platform.latch.latched() is True

    # 停堆关了母管，此刻零流量是保护动作的结果，不能把低流量首因算作恢复。
    platform.cycle()
    blocked_again = platform.reset_trip("shift-lead")
    assert blocked_again["ok"] is False
    assert any("首因未恢复" in item for item in blocked_again["violations"])

    # 母管重新通盐、实测流量回到门限之上后，首因才算确实恢复。
    platform.receiver.header.admit(flow_confirmed=True)
    platform._evaluate_alarms(clock.now())
    allowed = platform.reset_trip("shift-lead")
    assert allowed["ok"] is True
    assert platform.latch.latched() is False


def test_overtemperature_root_resets_after_signal_recovers(online, clock):
    platform = online
    _force_rules(platform, {0: [_alarm_event("receiver-overtemperature")]})
    platform.cycle()
    assert platform.latch.latched() is True
    clock.advance(platform.config.alarm.latch_reset_delay_s + 1.0)
    assert platform.reset_trip("shift-lead")["ok"] is False
    _force_rules(platform, {})
    platform.cycle()
    assert platform.reset_trip("shift-lead")["ok"] is True


def test_critical_signal_loss_trips_and_restores(online, clock):
    platform = online
    # 保护通道（温度）缺失 = 部分失败 + 故障安全停堆，绝不允许当无告警。
    report = platform.set_signal_lost("receiver_temperature_c", True)
    platform.cycle()
    assert report["critical"] is True
    assert platform.latch.latched() is True
    assert platform.latch.root_rule() == "signal-lost-receiver_temperature_c"
    status = platform.status()["alarm"]
    assert status["signals_lost"] == ["receiver_temperature_c"]

    clock.advance(platform.config.alarm.latch_reset_delay_s + 1.0)
    assert platform.reset_trip("shift-lead")["ok"] is False
    platform.set_signal_lost("receiver_temperature_c", False)
    platform.cycle()
    assert platform.reset_trip("shift-lead")["ok"] is True


def test_non_critical_signal_loss_warns_without_trip(platform):
    platform.start()
    report = platform.set_signal_lost("steam_pressure_mpa", True)
    platform.cycle()
    assert report["critical"] is False
    assert platform.latch.latched() is False
    groups = platform.correlator.groups(include_closed=False)
    assert groups[0].root_rule == "signal-lost-steam_pressure_mpa"


def test_audit_carries_same_root_as_latch_and_console(online):
    platform = online
    _force_rules(
        platform,
        {
            0: [_alarm_event("salt-flow-low")],
            1: [_alarm_event("receiver-overtemperature")],
        },
    )
    platform.cycle()
    platform.cycle()
    entries = platform.audit.entries(limit=50)
    trip_entries = [e for e in entries if e["action"] == "receiver_trip_complete"]
    assert trip_entries
    payload = trip_entries[0]["payload"]
    gid = platform.latch.group_id()
    assert payload["group_id"] == gid
    assert payload["root_rule"] == "salt-flow-low"

    all_entries = platform.audit.entries(limit=200)
    root_entries = [e for e in all_entries if e["action"] == "salt-flow-low"]
    assert any(e["payload"].get("role") == "root" and e["payload"].get("group_id") == gid for e in root_entries)
    overt = [e for e in all_entries if e["action"] == "receiver-overtemperature"]
    assert any(e["payload"].get("role") == "follower" for e in overt)

    # 控制台 / 状态快照同一口径。
    status = platform.status()
    assert status["alarm"]["group_id"] == gid
    assert any(group["group_id"] == gid and group["root_rule"] == "salt-flow-low" for group in status["alarm_groups"])


def test_pending_policy_blocks_restart_until_root_asserted(tmp_path, clock):
    cfg = dataclasses.replace(default_config().alarm, unknown_cause_policy="pending")
    config = dataclasses.replace(default_config(), alarm=cfg)
    platform = build_plant(config, clock=clock, storage_root=str(tmp_path / "p"))
    platform.start()
    _force_rules(platform, {0: [_alarm_event("receiver-overtemperature")]})
    platform.cycle()
    gid = platform.latch.group_id()
    clock.advance(platform.config.alarm.latch_reset_delay_s + 1.0)

    blocked = platform.reset_trip("shift-lead")
    assert blocked["ok"] is False
    assert any("首因未明确" in item for item in blocked["violations"])

    platform.assert_alarm_root(gid, "receiver-overtemperature", "shift-lead")
    _force_rules(platform, {})
    platform.cycle()
    assert platform.reset_trip("shift-lead")["ok"] is True


def test_trip_root_group_survives_restart(online, tmp_path, clock):
    platform = online
    _force_rules(
        platform,
        {
            0: [_alarm_event("salt-flow-low")],
            1: [_alarm_event("receiver-overtemperature")],
        },
    )
    platform.cycle()
    platform.cycle()
    gid = platform.latch.group_id()
    assert platform.latch.root_rule() == "salt-flow-low"

    rebuilt = build_plant(
        default_config(),
        clock=clock,
        storage_root=platform.store.root,
    )
    assert rebuilt.latch.latched() is True
    assert rebuilt.latch.group_id() == gid
    assert rebuilt.latch.root_rule() == "salt-flow-low"
    # 重启后合并组也从审计回放：投运条件仍按同一首因组的恢复状态判定。
    group = rebuilt.correlator.group(gid)
    assert group.root_rule == "salt-flow-low"
    assert group.tripped is True
    assert any("首因未恢复" in item for item in rebuilt._root_blockers())


def test_console_api_exposes_groups_and_readiness(online):
    from cspplant.console.api import Api

    platform = online
    api = Api(platform)
    status_code, body = api.handle("GET", "/api/alarm-groups", {}, {})
    assert status_code == 200
    assert body["groups"] == []

    _force_rules(
        platform,
        {0: [_alarm_event("salt-flow-low")], 1: [_alarm_event("receiver-overtemperature")]},
    )
    platform.cycle()
    platform.cycle()
    _, body = api.handle("GET", "/api/alarm-groups", {}, {})
    group = body["groups"][0]
    assert group["root_rule"] == "salt-flow-low"
    assert group["tripped"] is True

    _, readiness = api.handle("GET", "/api/readiness", {}, {})
    assert readiness["ready"] is False
    assert readiness["checks"]["root_cause_resolved_and_recovered"] is False
