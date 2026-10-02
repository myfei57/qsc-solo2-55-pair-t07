"""故障闩锁与告警。

告警是"现在越限了"，闩锁是"越限之后必须有人处理过才准复位"。规则负责判定，
合并器（:mod:`cspplant.alarm.correlator`）按因果链加固定跟随窗把时间上挨着、
因果上相连的告警合成一组并标出首因与带出，信号健康（:mod:`cspplant.alarm.health`）
把输入缺失补成显式告警。任何情况下都不允许"温度回落就自动复位"。
"""

from __future__ import annotations

from .causality import CausalEdge, CausalGraph
from .causality_default import default_causal_graph
from .correlator import (
    FOLLOWER,
    ROOT,
    AlarmGroup,
    Correlator,
    GroupMember,
    IngestResult,
)
from .escalation import Escalation, EscalationPolicy
from .health import SignalHealth, is_lost_rule, lost_rule, lost_signal
from .history import AlarmEntry, AlarmHistory
from .latch import FaultLatch, LatchState
from .rules import ALARM, WARNING, AlarmEvent, AlarmRule, AlarmRules, default_rules

__all__ = [
    "CausalEdge",
    "CausalGraph",
    "default_causal_graph",
    "Correlator",
    "AlarmGroup",
    "GroupMember",
    "IngestResult",
    "ROOT",
    "FOLLOWER",
    "SignalHealth",
    "lost_rule",
    "lost_signal",
    "is_lost_rule",
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
    "ALARM",
    "WARNING",
]
