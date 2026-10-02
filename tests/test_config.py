"""配置解析与校验。"""

from __future__ import annotations

import json

import pytest

from cspplant.config import (
    ConfigError,
    config_to_mapping,
    default_config,
    load_config,
)


def test_default_config_is_consistent():
    config = default_config()
    assert config.receiver.temp_target_c < config.receiver.temp_derate_c < config.receiver.temp_max_c
    assert config.salt.header_pressure_target_mpa < config.salt.header_pressure_max_mpa
    assert config.irradiance.in_range(800.0) is True
    assert config.irradiance.in_range(10.0) is False


def test_config_reloads_from_mapping():
    original = default_config()
    restored = type(original).from_mapping(config_to_mapping(original))
    assert restored == original


def test_load_config_reads_json_file(tmp_path):
    path = tmp_path / "csp.json"
    path.write_text(json.dumps(config_to_mapping(default_config())), encoding="utf-8")
    loaded = load_config(str(path))
    assert loaded.heliostat.mirror_area_m2 == default_config().heliostat.mirror_area_m2


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.json"))


def test_broken_temperature_band_is_rejected():
    mapping = config_to_mapping(default_config())
    mapping["limits"]["receiver"]["temp_derate_c"] = 100.0
    with pytest.raises(ConfigError):
        type(default_config()).from_mapping(mapping)


def test_broken_reflectivity_is_rejected():
    mapping = config_to_mapping(default_config())
    mapping["limits"]["heliostat"]["reflectivity"] = 1.4
    with pytest.raises(ConfigError):
        type(default_config()).from_mapping(mapping)


def test_receiver_and_salt_helpers():
    config = default_config()
    assert config.receiver.flow_sufficient(config.receiver.flow_min_kg_s) is True
    assert config.receiver.overtemperature(config.receiver.temp_max_c + 1.0) is True
    threshold = config.salt.low_flow_threshold(60.0)
    assert threshold == pytest.approx(60.0 - config.salt.flow_tolerance_kg_s)
    assert config.steam.settled(config.steam.pressure_target_mpa) is True


def test_alarm_limits_require_correlation_fields():
    mapping = config_to_mapping(default_config())
    del mapping["limits"]["alarm"]["correlation_window_s"]
    with pytest.raises(ConfigError):
        type(default_config()).from_mapping(mapping)
    mapping = config_to_mapping(default_config())
    del mapping["limits"]["alarm"]["ambiguity_policy"]
    with pytest.raises(ConfigError):
        type(default_config()).from_mapping(mapping)


def test_correlation_window_must_not_exceed_escalation_window():
    mapping = config_to_mapping(default_config())
    mapping["limits"]["alarm"]["correlation_window_s"] = 999.0
    with pytest.raises(ConfigError):
        type(default_config()).from_mapping(mapping)


def test_ambiguity_policy_values_are_checked():
    mapping = config_to_mapping(default_config())
    mapping["limits"]["alarm"]["ambiguity_policy"] = {"default": "guess"}
    with pytest.raises(ConfigError):
        type(default_config()).from_mapping(mapping)
    mapping["limits"]["alarm"]["ambiguity_policy"] = {"salt": "pend"}
    with pytest.raises(ConfigError):
        type(default_config()).from_mapping(mapping)


def test_ambiguity_policy_lookup_falls_back_to_default():
    config = default_config()
    assert config.alarm.policy_for("salt") == "pend"
    assert config.alarm.policy_for("steam") == "pend"
    assert config.alarm.policy_for("receiver") == "report_worst"
    assert config.alarm.policy_for("no-such-system") == "report_worst"
