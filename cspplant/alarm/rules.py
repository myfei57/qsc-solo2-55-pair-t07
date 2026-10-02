"""告警规则。

规则按"信号名 + 比较方向 + 阈值"定义，判定时一次性吃进全部信号，返回本轮越限
的条目。规则表是数据而不是代码分支：加一条保护不需要改判定逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import PlatformConfig

WARNING = "warning"
ALARM = "alarm"


@dataclass(frozen=True)
class AlarmEvent:
    """一条告警。"""

    rule: str
    severity: str
    signal: str
    value: float
    threshold: float
    message: str

    def as_dict(self) -> dict:
        def clean(number: float) -> float:
            return None if number != number else round(number, 3)

        return {
            "rule": self.rule,
            "severity": self.severity,
            "signal": self.signal,
            "value": clean(self.value),
            "threshold": clean(self.threshold),
            "message": self.message,
        }


@dataclass(frozen=True)
class AlarmRule:
    """一条告警规则。

    ``trips_plant`` 标记该规则越限是否构成停堆保护；停堆只认合并组里的首因规则
    有这个资格，被带出来的后继即使本身是停堆规则也不重复下停堆结论。
    """

    name: str
    signal: str
    threshold: float
    severity: str = ALARM
    above: bool = True
    message: str = ""
    trips_plant: bool = False
    enable_flag: str = ""

    def breached(self, value: float) -> bool:
        """给定信号值是否越限。"""

        return float(value) >= self.threshold if self.above else float(value) <= self.threshold

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "signal": self.signal,
            "threshold": round(self.threshold, 3),
            "severity": self.severity,
            "above": self.above,
            "message": self.message,
            "trips_plant": self.trips_plant,
            "enable_flag": self.enable_flag,
        }


def default_rules(config: PlatformConfig) -> list[AlarmRule]:
    """按配置生成默认规则表。"""

    return [
        AlarmRule(
            "receiver-overtemperature",
            "receiver_temperature_c",
            config.receiver.temp_max_c,
            ALARM,
            True,
            "吸热器出口温度越限",
            trips_plant=True,
        ),
        AlarmRule(
            "receiver-derate",
            "receiver_temperature_c",
            config.receiver.temp_derate_c,
            WARNING,
            True,
            "吸热器进入降额带",
        ),
        AlarmRule(
            "header-overpressure",
            "header_pressure_mpa",
            config.salt.header_pressure_max_mpa,
            ALARM,
            True,
            "熔盐母管超压",
            trips_plant=True,
        ),
        AlarmRule(
            "salt-flow-low",
            "salt_flow_kg_s",
            config.salt.flow_baseline_kg_s - config.salt.flow_tolerance_kg_s,
            ALARM,
            False,
            "熔盐流量低于联锁门限",
            trips_plant=True,
            enable_flag="salt_admitted",
        ),
        AlarmRule(
            "cold-tank-low",
            "cold_tank_pct",
            config.salt.cold_level_min_pct,
            WARNING,
            False,
            "冷盐罐液位偏低",
        ),
        AlarmRule(
            "hot-tank-high",
            "hot_tank_pct",
            config.salt.hot_level_max_pct,
            WARNING,
            True,
            "热盐罐液位偏高",
        ),
        AlarmRule(
            "steam-pressure",
            "steam_pressure_mpa",
            config.steam.pressure_target_mpa + 2.0 * config.steam.pressure_band_mpa,
            WARNING,
            True,
            "主蒸汽压力偏高",
        ),
    ]


@dataclass
class AlarmRules:
    """规则表与判定入口。"""

    rules: list[AlarmRule] = field(default_factory=list)

    def add(self, rule: AlarmRule) -> AlarmRule:
        """追加一条规则，重名即报错。"""

        if any(existing.name == rule.name for existing in self.rules):
            raise ValueError(f"告警规则重名: {rule.name}")
        self.rules.append(rule)
        return rule

    def evaluate(self, signals: dict, context: dict | None = None) -> list[AlarmEvent]:
        """判定本轮越限，返回触发的告警。

        带 ``enable_flag`` 的规则只在上下文中对应标志为真时才判（例如低流量
        联锁只在母管已通盐后成立，冷态零流量是正常工况不是故障）。
        """

        context = context or {}
        raised: list[AlarmEvent] = []
        for rule in self.rules:
            if rule.signal not in signals:
                continue
            if rule.enable_flag and not bool(context.get(rule.enable_flag)):
                continue
            value = float(signals[rule.signal])
            if rule.breached(value):
                raised.append(
                    AlarmEvent(
                        rule=rule.name,
                        severity=rule.severity,
                        signal=rule.signal,
                        value=value,
                        threshold=rule.threshold,
                        message=rule.message,
                    )
                )
        return raised

    def names(self) -> list[str]:
        """返回全部规则名。"""

        return [rule.name for rule in self.rules]

    def by_name(self, name: str) -> AlarmRule | None:
        """按规则名查找。"""

        for rule in self.rules:
            if rule.name == name:
                return rule
        return None

    def trips_plant(self, name: str) -> bool:
        """该规则越限是否构成停堆保护。"""

        rule = self.by_name(name)
        return bool(rule and rule.trips_plant)

    def describe(self) -> list[dict]:
        """返回规则表的可打印形式。"""

        return [rule.as_dict() for rule in self.rules]
