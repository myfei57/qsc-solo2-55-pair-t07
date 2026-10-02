"""故障闩锁与告警。

告警是"现在越限了"，闩锁是"越限之后必须有人处理过才准复位"。两者分开：规则
负责判定，闩锁负责留住状态，升级策略按重复次数与严重度决定要不要叫人。任何
情况下都不允许"温度回落就自动复位"。

合并器在规则之上：按时间窗把同时段的告警并成组，按因果链在组内标出首因与
被带出的告警；停堆只认首因，投运复位只认首因已恢复，通道缺失一律按部分失
败处理。
"""

from __future__ import annotations

from .correlation import (
    ASSUMED_FIRST_OUT,
    CAUSAL_EDGES,
    CONCURRENT,
    DERIVED,
    FIRST_OUT,
    HEALTH_OK,
    HEALTH_PARTIAL,
    OPEN,
    PEND,
    PENDING,
    REPORT_WORST,
    AlarmCorrelator,
    ClearEvent,
    IngestReport,
    Occurrence,
    TripDemand,
)
from .escalation import Escalation, EscalationPolicy
from .history import AlarmEntry, AlarmHistory
from .latch import FaultLatch, LatchState
from .rules import ALARM, WARNING, AlarmEvent, AlarmRule, AlarmRules, default_rules

__all__ = [
    "Escalation",
    "EscalationPolicy",
    "AlarmEntry",
    "AlarmHistory",
    "FaultLatch",
    "LatchState",
    "AlarmEvent",
    "AlarmRule",
    "AlarmRules",
    "default_rules",
    "AlarmCorrelator",
    "Occurrence",
    "ClearEvent",
    "TripDemand",
    "IngestReport",
    "CAUSAL_EDGES",
    "FIRST_OUT",
    "ASSUMED_FIRST_OUT",
    "DERIVED",
    "CONCURRENT",
    "PENDING",
    "REPORT_WORST",
    "PEND",
    "OPEN",
    "HEALTH_OK",
    "HEALTH_PARTIAL",
    "ALARM",
    "WARNING",
]
