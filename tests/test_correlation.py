"""告警合并、首因标记与停堆／投运依据。"""

from __future__ import annotations

import dataclasses

import pytest

from cspplant.alarm.correlation import (
    ASSUMED_FIRST_OUT,
    CONCURRENT,
    DERIVED,
    FIRST_OUT,
    PENDING,
    AlarmCorrelator,
)
from cspplant.alarm.rules import AlarmRules, default_rules
from cspplant.app import build_plant
from cspplant.config import default_config
from cspplant.console.api import Api
from cspplant.console.pages import render_page
from cspplant.errors import InterlockError

TEMP_HIGH = {"receiver_temperature_c": 730.0}
TEMP_DERATE = {"receiver_temperature_c": 625.0}
TEMP_OK = {"receiver_temperature_c": 500.0}
HEADER_HIGH = {"header_pressure_mpa": 9.9}
COLD_LOW = {"cold_tank_pct": 5.0}
HOT_HIGH = {"hot_tank_pct": 99.0}
STEAM_HIGH = {"steam_pressure_mpa": 99.0}


def _correlator(**limit_overrides):
    config = default_config()
    limits = (
        dataclasses.replace(config.alarm, **limit_overrides)
        if limit_overrides
        else config.alarm
    )
    rules = default_rules(config)
    return AlarmCorrelator(limits, rules), AlarmRules(rules)


def _merge(signals: dict, *more: dict) -> dict:
    merged = dict(signals)
    for extra in more:
        merged.update(extra)
    return merged


def test_same_signal_is_suppressed_to_one_occurrence():
    correlator, rules = _correlator()
    report = correlator.ingest(1.0, rules.evaluate(TEMP_HIGH))
    assert len(report.raised) == 1
    occurrence = report.raised[0]
    assert occurrence.rule == "receiver-overtemperature"
    assert occurrence.suppressed == ("receiver-derate",)
    assert occurrence.mark == FIRST_OUT
    assert report.opened[0]["first_out"]["rule"] == "receiver-overtemperature"
    assert [demand.basis_rule for demand in report.demands] == ["receiver-overtemperature"]


def test_active_rule_raises_once_and_clears_after_hold():
    correlator, rules = _correlator()
    correlator.ingest(1.0, rules.evaluate(TEMP_HIGH))
    for tick in (2.0, 3.0, 4.0):
        assert correlator.ingest(tick, rules.evaluate(TEMP_HIGH)).raised == ()
    # 被吸收的 derate 也在活跃跟踪里（被压制状态），不会重报。
    assert correlator.active_rules() == ["receiver-derate", "receiver-overtemperature"]
    for tick in (5.0, 6.0, 7.0):
        assert correlator.ingest(tick, rules.evaluate(TEMP_OK)).cleared == ()
    cleared = correlator.ingest(8.0, rules.evaluate(TEMP_OK)).cleared
    assert [item.rule for item in cleared] == ["receiver-derate", "receiver-overtemperature"]
    assert all(item.held_s == pytest.approx(3.0) for item in cleared)
    assert correlator.active_rules() == []


def test_jitter_stays_a_single_occurrence():
    correlator, rules = _correlator()
    raised_total = 0
    raised_total += len(correlator.ingest(1.0, rules.evaluate(TEMP_DERATE)).raised)
    correlator.ingest(2.0, rules.evaluate(TEMP_OK))
    raised_total += len(correlator.ingest(3.0, rules.evaluate(TEMP_DERATE)).raised)
    cleared = []
    for tick in (4.0, 5.0, 6.0, 7.0):
        report = correlator.ingest(tick, rules.evaluate(TEMP_OK))
        raised_total += len(report.raised)
        cleared.extend(report.cleared)
    assert raised_total == 1
    assert [item.rule for item in cleared] == ["receiver-derate"]
    assert cleared[0].cleared_at == pytest.approx(7.0)


def test_time_window_merges_then_splits_groups():
    correlator, rules = _correlator()
    correlator.ingest(1.0, rules.evaluate(HEADER_HIGH))
    second = correlator.ingest(2.0, rules.evaluate(_merge(HEADER_HIGH, COLD_LOW)))
    assert [item.rule for item in second.raised] == ["cold-tank-low"]
    assert second.raised[0].mark == CONCURRENT
    assert second.raised[0].group == "g0001"
    for tick in (3.0, 4.0, 5.0, 6.0):
        correlator.ingest(tick, [])
    closed = correlator.ingest(40.0, []).closed
    assert [item["gid"] for item in closed] == ["g0001"]
    assert closed[0]["first_out"]["rule"] == "header-overpressure"
    opened = correlator.ingest(41.0, rules.evaluate(HOT_HIGH)).opened
    assert [item["gid"] for item in opened] == ["g0002"]
    assert correlator.group_count() == 2


def test_causal_chain_marks_derived_alarms():
    correlator, rules = _correlator()
    correlator.ingest(1.0, rules.evaluate(TEMP_DERATE))
    correlator.ingest(2.0, rules.evaluate(TEMP_HIGH))
    correlator.ingest(3.0, rules.evaluate(_merge(TEMP_HIGH, STEAM_HIGH)))
    group = correlator.groups(1)[0]
    marks = {item["rule"]: item["mark"] for item in group["occurrences"]}
    assert marks == {
        "receiver-derate": FIRST_OUT,
        "receiver-overtemperature": DERIVED,
        "steam-pressure": DERIVED,
    }
    assert group["first_out"]["rule"] == "receiver-derate"


def test_ambiguous_first_out_is_reported_as_worst_for_receiver():
    correlator, rules = _correlator()
    report = correlator.ingest(1.0, rules.evaluate(_merge(TEMP_HIGH, HEADER_HIGH)))
    group = report.opened[0]
    assert group["pending"] is False
    assert group["first_out"]["rule"] == "receiver-overtemperature"
    assert group["first_out"]["mark"] == ASSUMED_FIRST_OUT
    marks = {item.rule: item.mark for item in report.raised}
    assert marks["header-overpressure"] == CONCURRENT
    assert [demand.basis_rule for demand in report.demands] == ["receiver-overtemperature"]


def test_ambiguous_first_out_pends_for_salt_by_default():
    correlator, rules = _correlator()
    report = correlator.ingest(1.0, rules.evaluate(_merge(HEADER_HIGH, COLD_LOW)))
    group = report.opened[0]
    assert group["pending"] is True
    assert group["first_out"] is None
    assert {item.mark for item in report.raised} == {PENDING}
    assert report.demands == ()
    with pytest.raises(InterlockError):
        correlator.note_trip("g0001", 2.0)


def test_pend_policy_holds_trip_demand():
    correlator, rules = _correlator(ambiguity_policy=(("default", "pend"),))
    report = correlator.ingest(1.0, rules.evaluate(_merge(TEMP_HIGH, HEADER_HIGH)))
    assert report.opened[0]["pending"] is True
    assert report.demands == ()
    assert report.held == ("g0001",)


def test_trip_demand_follows_first_out_not_trigger():
    correlator, rules = _correlator()
    correlator.ingest(1.0, rules.evaluate(TEMP_DERATE))
    report = correlator.ingest(2.0, rules.evaluate(TEMP_HIGH))
    assert len(report.demands) == 1
    demand = report.demands[0]
    assert demand.basis_rule == "receiver-derate"
    assert demand.trigger_rule == "receiver-overtemperature"
    basis = correlator.note_trip(demand.group, 2.0)
    assert basis["rule"] == "receiver-derate"
    assert basis["assumed"] is False
    assert correlator.trip_basis()["group"] == demand.group


def test_basis_recovery_requires_clear_and_hold():
    correlator, rules = _correlator()
    correlator.ingest(1.0, rules.evaluate(TEMP_DERATE))
    correlator.ingest(2.0, rules.evaluate(TEMP_HIGH))
    correlator.note_trip("g0001", 2.0)
    assert correlator.basis_recovered() is False
    correlator.ingest(3.0, rules.evaluate(TEMP_OK))
    assert correlator.basis_recovered() is False
    for tick in (4.0, 5.0, 6.0):
        correlator.ingest(tick, rules.evaluate(TEMP_OK))
    assert correlator.basis_recovered() is True
    correlator.clear_trip_basis()
    assert correlator.trip_basis() is None
    assert correlator.basis_recovered() is True


def test_missing_channel_is_partial_failure_not_no_alarm():
    correlator, rules = _correlator()
    report = correlator.ingest(1.0, [], missing={"steam_pressure_mpa"})
    assert report.health == "partial"
    assert report.newly_missing == ("steam_pressure_mpa",)
    assert correlator.missing_channels() == ["steam_pressure_mpa"]
    restored = correlator.ingest(2.0, [])
    assert restored.health == "ok"
    assert restored.newly_restored == ("steam_pressure_mpa",)


def test_missing_channel_holds_clear_and_recovery():
    correlator, rules = _correlator()
    correlator.ingest(1.0, rules.evaluate(TEMP_DERATE))
    correlator.note_trip("g0001", 1.0)
    for tick in (2.0, 3.0, 4.0):
        report = correlator.ingest(tick, rules.evaluate(TEMP_OK), missing={"receiver_temperature_c"})
        assert report.cleared == ()
    assert correlator.active_rules() == ["receiver-derate"]
    assert correlator.basis_recovered() is False
    correlator.ingest(5.0, rules.evaluate(TEMP_OK))
    for tick in (6.0, 7.0):
        assert correlator.ingest(tick, rules.evaluate(TEMP_OK)).cleared == ()
    cleared = correlator.ingest(8.0, rules.evaluate(TEMP_OK)).cleared
    assert [item.rule for item in cleared] == ["receiver-derate"]
    assert correlator.basis_recovered() is True


def test_shadowed_rule_is_promoted_when_absorber_exits():
    correlator, rules = _correlator()
    correlator.ingest(1.0, rules.evaluate(TEMP_HIGH))
    report = correlator.ingest(2.0, rules.evaluate(TEMP_DERATE))
    assert [item.rule for item in report.raised] == ["receiver-derate"]
    assert report.raised[0].mark == CONCURRENT
    assert report.raised[0].group == "g0001"
    for tick in (3.0, 4.0):
        assert correlator.ingest(tick, rules.evaluate(TEMP_OK)).cleared == ()
    first = correlator.ingest(5.0, rules.evaluate(TEMP_OK)).cleared
    assert [item.rule for item in first] == ["receiver-overtemperature"]
    second = correlator.ingest(6.0, rules.evaluate(TEMP_OK)).cleared
    assert [item.rule for item in second] == ["receiver-derate"]
    assert correlator.groups(1)[0]["active"] == 0


def test_timestamps_are_clamped_monotonic():
    correlator, rules = _correlator()
    correlator.ingest(10.0, rules.evaluate(TEMP_DERATE))
    report = correlator.ingest(5.0, rules.evaluate(TEMP_HIGH))
    assert report.ts == pytest.approx(10.0)
    assert correlator.clamped() == 1
    stamps = [item["ts"] for item in correlator.groups(1)[0]["occurrences"]]
    assert stamps == sorted(stamps)


def test_restore_basis_requires_full_payload():
    correlator, _rules = _correlator()
    with pytest.raises(InterlockError):
        correlator.restore_basis({})
    with pytest.raises(InterlockError):
        correlator.restore_basis({"rule": "receiver-derate"})
    correlator.restore_basis({"rule": "receiver-derate", "ts": 1.0, "group": "g0001"})
    assert correlator.trip_basis()["rule"] == "receiver-derate"
    assert correlator.basis_recovered() is False


def test_platform_auto_trip_uses_first_out_basis(platform, clock):
    platform.start()
    real = platform._signals
    platform._signals = lambda: {**real(), "receiver_temperature_c": 730.0}
    clock.advance(1.0)
    platform.cycle()
    assert platform.latch.latched() is True
    assert platform.latch.reason() == "receiver-overtemperature"
    basis = platform.correlator.trip_basis()
    assert basis["rule"] == "receiver-overtemperature"
    assert platform.store.read("alarm_trip_basis")["group"] == basis["group"]
    alarm = platform.status()["alarm"]
    assert alarm["trip_basis"]["rule"] == "receiver-overtemperature"
    assert alarm["basis_recovered"] is False


def test_platform_reset_waits_for_first_out_recovery(platform, clock):
    platform.start()
    real = platform._signals
    platform._signals = lambda: {**real(), "receiver_temperature_c": 730.0}
    clock.advance(1.0)
    platform.cycle()
    assert platform.latch.latched() is True
    early = platform.reset_trip("shift-lead")
    assert early["ok"] is False
    assert "首因" in early["violations"][0]
    platform._signals = lambda: {**real(), "receiver_temperature_c": 500.0}
    clock.advance(1.0)
    platform.cycle()
    pending = platform.reset_trip("shift-lead")
    assert pending["ok"] is False
    for _ in range(4):
        clock.advance(1.0)
        platform.cycle()
    done = platform.reset_trip("shift-lead")
    assert done["ok"] is True
    assert platform.correlator.trip_basis() is None
    assert platform.store.read("alarm_trip_basis") is None


def test_trip_basis_survives_restart(platform, clock):
    platform.start()
    real = platform._signals
    platform._signals = lambda: {**real(), "receiver_temperature_c": 730.0}
    clock.advance(1.0)
    platform.cycle()
    assert platform.latch.latched() is True

    restarted = build_plant(default_config(), clock=clock, storage_root=platform.store.root)
    assert restarted.correlator.trip_basis()["rule"] == "receiver-overtemperature"
    refused = restarted.reset_trip("shift-lead")
    assert refused["ok"] is False
    real_restarted = restarted._signals
    restarted._signals = lambda: {**real_restarted(), "receiver_temperature_c": 500.0}
    for _ in range(6):
        clock.advance(1.0)
        restarted.cycle()
    done = restarted.reset_trip("shift-lead")
    assert done["ok"] is True
    assert restarted.store.read("alarm_trip_basis") is None


def test_successful_reset_is_not_relatched_after_restart(platform, clock):
    platform.start()
    platform.trip("overtemperature")
    clock.advance(platform.config.alarm.latch_reset_delay_s + 1.0)
    assert platform.reset_trip("shift-lead")["ok"] is True

    restarted = build_plant(default_config(), clock=clock, storage_root=platform.store.root)
    assert restarted.latch.latched() is False
    assert restarted.receiver.state() != "tripped"


def test_missing_channel_degrades_readiness(platform, clock):
    platform.start()
    clock.advance(1.0)
    platform.cycle()
    real = platform._signals
    platform._signals = lambda: {
        key: value for key, value in real().items() if key != "steam_pressure_mpa"
    }
    clock.advance(1.0)
    platform.cycle()
    alarm = platform.status()["alarm"]
    assert alarm["health"] == "partial"
    assert alarm["missing_channels"] == ["steam_pressure_mpa"]
    api = Api(platform)
    _, ready = api.handle("GET", "/api/readiness", {}, {})
    assert ready["checks"]["alarm_channels_complete"] is False
    assert ready["ready"] is False
    actions = [event.action for event in platform.bus.history() if event.component == "alarm"]
    assert "alarm_channel_missing" in actions


def test_console_alarms_expose_groups_and_basis(platform, clock):
    platform.start()
    real = platform._signals
    platform._signals = lambda: {**real(), "receiver_temperature_c": 730.0}
    clock.advance(1.0)
    platform.cycle()
    api = Api(platform)
    status, payload = api.handle("GET", "/api/alarms", {}, {})
    assert status == 200
    assert payload["health"] == "ok"
    assert payload["trip_basis"]["rule"] == "receiver-overtemperature"
    assert payload["basis_recovered"] is False
    assert payload["groups"][0]["first_out"]["rule"] == "receiver-overtemperature"
    assert payload["groups"][0]["occurrences"][0]["mark"] == "first_out"
    _, ready = api.handle("GET", "/api/readiness", {}, {})
    assert ready["checks"]["first_out_recovered"] is False


def test_alarm_page_lists_merged_groups(platform, clock):
    platform.start()
    real = platform._signals
    platform._signals = lambda: {**real(), "receiver_temperature_c": 625.0}
    clock.advance(1.0)
    platform.cycle()
    html = render_page("/alarms", platform)
    assert "合并组" in html
    assert "g0001" in html
    assert "receiver-derate" in html
