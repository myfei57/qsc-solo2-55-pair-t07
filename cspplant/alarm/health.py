"""信号健康监视。

某路告警输入缺失属于部分失败，绝不能当成"这一路无告警"：规则判定对缺失信号
是跳过的（见 :meth:`AlarmRules.evaluate`），所以缺失必须在这里显式补成一条
``signal-lost`` 告警。保护通道（被任一停堆规则引用的信号）缺失按故障安全处理，
合成告警直接具备停堆资格；非保护通道缺失只报警不停堆。恢复时补一条恢复事件，
合并器据此放行重新投运。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .rules import ALARM, WARNING, AlarmEvent

LOST_RULE_PREFIX = "signal-lost-"


@dataclass(frozen=True)
class SignalStatus:
    """一路信号的健康状态。"""

    signal: str
    available: bool
    critical: bool
    since: float

    def as_dict(self) -> dict:
        return {
            "signal": self.signal,
            "available": self.available,
            "critical": self.critical,
            "since": round(self.since, 3),
        }


def lost_rule(signal: str) -> str:
    """信号缺失合成告警的规则名。"""

    return f"{LOST_RULE_PREFIX}{signal}"


def is_lost_rule(rule: str) -> bool:
    return rule.startswith(LOST_RULE_PREFIX)


def lost_signal(rule: str) -> str:
    return rule[len(LOST_RULE_PREFIX):]


class SignalHealth:
    """按信号名跟踪可用/缺失，缺失边沿产出合成告警。"""

    def __init__(self, signals: list[str], critical: set[str]) -> None:
        self._critical = set(critical)
        self._available: dict[str, bool] = {name: True for name in signals}
        self._since: dict[str, float] = {name: 0.0 for name in signals}
        self._recovered_edge: list[str] = []
        self._lock = threading.RLock()

    def set_available(self, signal: str, now: float) -> SignalStatus:
        with self._lock:
            if not self._available.get(signal, True):
                self._recovered_edge.append(signal)
            self._available[signal] = True
            self._since[signal] = float(now)
            return self.status_of(signal)

    def set_lost(self, signal: str, now: float) -> SignalStatus:
        with self._lock:
            if self._available.get(signal, True):
                self._available[signal] = False
                self._since[signal] = float(now)
            return self.status_of(signal)

    def is_available(self, signal: str) -> bool:
        with self._lock:
            return self._available.get(signal, True)

    def status_of(self, signal: str) -> SignalStatus:
        return SignalStatus(signal, self._available[signal], signal in self._critical, self._since[signal])

    def statuses(self) -> list[SignalStatus]:
        with self._lock:
            return [
                SignalStatus(name, self._available[name], name in self._critical, self._since[name])
                for name in self._available
            ]

    def lost(self) -> list[str]:
        with self._lock:
            return [name for name, ok in self._available.items() if not ok]

    def lost_events(self) -> list[AlarmEvent]:
        """为当前所有缺失信号生成合成告警（缺失边沿事件由 :meth:`poll` 产出）。"""

        with self._lock:
            return [self._event(name) for name, ok in self._available.items() if not ok]

    def poll(self, present: dict, now: float) -> tuple[list[AlarmEvent], list[str]]:
        """对照本轮到齐的信号，返回缺失边沿告警与恢复信号列表。

        只处理两种边沿：上一轮可用、本轮缺失 → 补缺失告警；上一轮缺失、本轮
        到齐 → 恢复。持续缺失的信号保持缺失，绝不允许在某一轮被重置成可用。
        """

        raised: list[AlarmEvent] = []
        recovered: list[str] = []
        with self._lock:
            recovered.extend(self._recovered_edge)
            self._recovered_edge.clear()
            for signal in self._available:
                was_available = self._available[signal]
                if signal in present:
                    if not was_available:
                        recovered.append(signal)
                    self._available[signal] = True
                    self._since[signal] = float(now)
                elif was_available:
                    self._available[signal] = False
                    self._since[signal] = float(now)
                    raised.append(self._event(signal))
        return raised, recovered

    def _event(self, signal: str) -> AlarmEvent:
        critical = signal in self._critical
        return AlarmEvent(
            rule=lost_rule(signal),
            severity=ALARM if critical else WARNING,
            signal=signal,
            value=float("nan"),
            threshold=float("nan"),
            message=("保护信号缺失（故障安全）" if critical else "信号缺失（部分失败）")
            + f"：{signal}",
        )

    def trips_plant(self, rule: str) -> bool:
        """缺失合成告警是否具备停堆资格（保护通道缺失）。"""

        return is_lost_rule(rule) and lost_signal(rule) in self._critical
