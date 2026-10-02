"""告警合并与首因标记。

合并准则（定死）：**只按时间窗合并**。一条告警落组后 ``correlation_window_s``
内再出告警就并入同组，超出则闭组开新组；组内还有未清除的活跃告警时窗口不闭。
因果链不参与合并边界——合并结果必须能脱离因果表的完整性被审计按同一份配置
复算；因果链只用于组内标记：能沿因果边找到更早来源的告警标"被带出"，找不到
来源的最早那条标"首因"。

首因不明（同刻多个互无因果关系的根源）按系统策略处置，策略来自配置、按系统
定：``report_worst`` 按最严重那条报并记为假定首因；``pend`` 挂起等首因明确，
挂起的组不出停堆依据，闭组时按"首因未明"落审计。

时序不能乱：每条告警进组即取全序序号，时间戳只许单调前进（回退即钳制并计
数），组只增不改，首因一经标定不翻案。

通道缺失是部分失败：缺失期间不报"无告警"，相关告警的清除与首因恢复判定挂
起，直到通道恢复。停堆依据落盘后跨重启有效，投运复位只认"首因已恢复"。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from ..config import AlarmLimits
from ..errors import InterlockError
from .rules import ALARM, WARNING, AlarmEvent, AlarmRule

# 告警在组内的角色标记。
FIRST_OUT = "first_out"
ASSUMED_FIRST_OUT = "assumed_first_out"
DERIVED = "derived"
CONCURRENT = "concurrent"
PENDING = "pending"

# 首因不明时的处置策略（按系统配置）。
REPORT_WORST = "report_worst"
PEND = "pend"

OPEN = "open"
CLOSED = "closed"

HEALTH_OK = "ok"
HEALTH_PARTIAL = "partial"

# 因果边（cause → effect）：effect 能被 cause 带出来。只画有直接物理依据的边，
# 画多了会把独立故障错标成派生；画少了最坏情况是并发标记，不影响合并边界。
CAUSAL_EDGES: tuple[tuple[str, str], ...] = (
    ("receiver-derate", "receiver-overtemperature"),
    ("receiver-overtemperature", "steam-pressure"),
)

_SEVERITY_RANK = {WARNING: 1, ALARM: 2}


@dataclass(frozen=True)
class Occurrence:
    """一次告警发生（同一规则持续越限只产生一条）。"""

    seq: int
    ts: float
    rule: str
    system: str
    severity: str
    signal: str
    value: float
    threshold: float
    message: str
    mark: str
    group: str
    suppressed: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": round(self.ts, 3),
            "rule": self.rule,
            "system": self.system,
            "severity": self.severity,
            "signal": self.signal,
            "value": round(self.value, 3),
            "threshold": round(self.threshold, 3),
            "message": self.message,
            "mark": self.mark,
            "group": self.group,
            "suppressed": list(self.suppressed),
        }


@dataclass(frozen=True)
class ClearEvent:
    """一条告警的清除：越限结束后又稳定观察了恢复保持时间。"""

    rule: str
    group: str
    cleared_at: float
    held_s: float

    def as_dict(self) -> dict:
        return {
            "rule": self.rule,
            "group": self.group,
            "cleared_at": round(self.cleared_at, 3),
            "held_s": round(self.held_s, 3),
        }


@dataclass(frozen=True)
class TripDemand:
    """一条停堆依据：跳闸级规则所在组的首因。"""

    group: str
    basis_rule: str
    basis_ts: float
    trigger_rule: str

    def as_dict(self) -> dict:
        return {
            "group": self.group,
            "basis_rule": self.basis_rule,
            "basis_ts": round(self.basis_ts, 3),
            "trigger_rule": self.trigger_rule,
        }


@dataclass
class _Active:
    """一条仍在越限（或清除观察期）的规则。

    ``shadowed`` 表示它被同信号更严重的告警压着：不开自己的发生额，但要
    占住活跃状态，免得下一轮被当成新告警；压制它的规则退出后它才补发生额。
    """

    rule: str
    group: str
    since: float
    clear_pending_since: float | None = None
    shadowed: bool = False


@dataclass
class _Group:
    """一个告警合并组（内部可变，对外只给快照）。"""

    gid: str
    opened_ts: float
    last_ts: float
    occurrences: list[Occurrence] = field(default_factory=list)
    first_out_rule: str | None = None
    pending: bool = False
    degraded: bool = False
    active: int = 0
    status: str = OPEN

    def first_out(self) -> Occurrence | None:
        for occurrence in self.occurrences:
            if occurrence.rule == self.first_out_rule:
                return occurrence
        return None

    def as_dict(self) -> dict:
        first = self.first_out()
        return {
            "gid": self.gid,
            "status": self.status,
            "opened_ts": round(self.opened_ts, 3),
            "last_ts": round(self.last_ts, 3),
            "first_out": None
            if first is None
            else {"rule": first.rule, "mark": first.mark, "ts": round(first.ts, 3)},
            "pending": self.pending,
            "degraded": self.degraded,
            "active": self.active,
            "occurrences": [occurrence.as_dict() for occurrence in self.occurrences],
        }


@dataclass(frozen=True)
class IngestReport:
    """一轮合并的结果，供平台落历史、发事件、做停堆判定。"""

    ts: float
    raised: tuple[Occurrence, ...]
    cleared: tuple[ClearEvent, ...]
    opened: tuple[dict, ...]
    closed: tuple[dict, ...]
    demands: tuple[TripDemand, ...]
    held: tuple[str, ...]
    health: str
    missing: tuple[str, ...]
    newly_missing: tuple[str, ...]
    newly_restored: tuple[str, ...]


class AlarmCorrelator:
    """按时间窗合并告警、按因果链标记首因的合并器。"""

    def __init__(
        self,
        limits: AlarmLimits,
        rules: list[AlarmRule],
        edges: tuple[tuple[str, str], ...] = CAUSAL_EDGES,
    ) -> None:
        self._limits = limits
        self._rules = {rule.name: rule for rule in rules}
        self._rule_order = {rule.name: index for index, rule in enumerate(rules)}
        self._edges = tuple(edges)
        self._seq = 0
        self._group_seq = 0
        self._groups: list[_Group] = []
        self._active: dict[str, _Active] = {}
        self._missing: set[str] = set()
        self._last_ts: float | None = None
        self._started_at: float | None = None
        self._last_breach: dict[str, float] = {}
        self._clear_ok: dict[str, float] = {}
        self._trip_basis: dict | None = None
        self._clamped = 0
        self._ingests = 0
        self._group_limit = 512

    # ---- 时序 ---------------------------------------------------------

    def _clamp(self, now: float) -> float:
        """时间戳只许单调前进；回退即钳到上一时刻并计数。"""

        ts = float(now)
        if self._last_ts is not None and ts < self._last_ts:
            self._clamped += 1
            return self._last_ts
        return ts

    def clamped(self) -> int:
        """返回被钳制的时间戳个数（时序异常的审计线索）。"""

        return self._clamped

    # ---- 通道健康 -----------------------------------------------------

    def health(self) -> str:
        """通道健康：有缺失即部分失败。"""

        return HEALTH_PARTIAL if self._missing else HEALTH_OK

    def missing_channels(self) -> list[str]:
        """返回当前缺失的信号通道。"""

        return sorted(self._missing)

    # ---- 合并 ---------------------------------------------------------

    def ingest(
        self,
        now: float,
        events: list[AlarmEvent],
        missing: tuple[str, ...] | list[str] | set[str] = (),
    ) -> IngestReport:
        """吃进一轮越限判定，返回合并与首因标记结果。

        每个控制周期都要调用（包括没有越限的周期）：清除判定、组关闭与
        通道健康都依赖连续观察。
        """

        ts = self._clamp(now)
        if self._started_at is None:
            self._started_at = ts
        self._last_ts = ts
        self._ingests += 1
        previous_missing = set(self._missing)
        self._missing = set(missing)
        newly_missing = tuple(sorted(self._missing - previous_missing))
        newly_restored = tuple(sorted(previous_missing - self._missing))

        breached = {event.rule for event in events}
        for event in events:
            self._last_breach[event.rule] = ts

        # 仍在越限的规则：取消清除观察期。
        for rule in breached & set(self._active):
            self._active[rule].clear_pending_since = None

        # 本轮不再越限的活跃规则：进入或推进清除观察期；通道缺失则挂起，
        # 缺失期间既不清除也不计"已恢复"。
        cleared: list[ClearEvent] = []
        for rule in sorted(set(self._active) - breached):
            active = self._active[rule]
            if self._signal_of(rule) in self._missing:
                continue
            if active.clear_pending_since is None:
                active.clear_pending_since = ts
                continue
            if ts - active.clear_pending_since >= self._limits.recovery_hold_s:
                held = ts - active.clear_pending_since
                cleared.append(ClearEvent(rule, active.group, ts, held))
                self._clear_ok[rule] = ts
                del self._active[rule]
                group = self._group_by_id(active.group)
                if group is not None:
                    group.active = max(0, group.active - 1)

        # 新发生的规则（本轮越限且此前不活跃）：同信号只留最严重的一条。
        fresh = [event for event in events if event.rule not in self._active]
        fresh.sort(key=lambda event: self._order_of(event.rule))
        pairs, absorbed = self._suppress(fresh)

        # 被压制的规则在本轮重新成为该信号最重的一条时补发生额（压制它的
        # 规则已退出越限）；仍被压着时只刷新活跃状态，不重报。
        best = self._best_by_signal(events)
        promoted = [
            event
            for event in events
            if (active := self._active.get(event.rule)) is not None
            and active.shadowed
            and best.get(event.signal) is event
        ]

        # 没有活跃成员的组，窗口一过即关闭；关闭只随时间前进发生一次。
        closed: list[dict] = []
        current = self._current_group()
        if current is not None and not self._attachable(current, ts):
            current.status = CLOSED
            closed.append(current.as_dict())

        opened: list[dict] = []
        raised: list[Occurrence] = []
        group = self._current_group()
        if pairs:
            if group is None:
                group = self._open_group(ts)
                for event, absorbed_names in pairs:
                    self._occurrence(group, event, ts, "", absorbed_names)
                self._resolve_first_batch(group)
                raised.extend(group.occurrences)
                opened.append(group.as_dict())
            else:
                for event, absorbed_names in pairs:
                    raised.append(self._attach(group, event, ts, absorbed_names))
        if group is not None:
            for event in absorbed:
                group.active += 1
                self._active[event.rule] = _Active(event.rule, group.gid, ts, shadowed=True)
            for event in promoted:
                raised.append(self._attach(group, event, ts, (), count_active=False))
            if pairs or promoted:
                group.last_ts = ts

        if self._missing:
            for group in self._groups:
                if group.status == OPEN:
                    group.degraded = True

        self._trim_groups()
        demands, held = self._demands()
        return IngestReport(
            ts=ts,
            raised=tuple(raised),
            cleared=tuple(cleared),
            opened=tuple(opened),
            closed=tuple(closed),
            demands=demands,
            held=held,
            health=self.health(),
            missing=tuple(self.missing_channels()),
            newly_missing=newly_missing,
            newly_restored=newly_restored,
        )

    def _suppress(
        self, events: list[AlarmEvent]
    ) -> tuple[list[tuple[AlarmEvent, tuple[str, ...]]], list[AlarmEvent]]:
        """同一信号同一轮只留最严重的一条，返回（保留对, 被吸收事件）。

        被吸收规则的名字随保留方的发生额落审计；被吸收事件本身也要返回，
        由调用方登记成被压制的活跃，防止下一轮被当成新告警。
        """

        by_signal: dict[str, list[AlarmEvent]] = {}
        for event in events:
            by_signal.setdefault(event.signal, []).append(event)
        pairs: list[tuple[AlarmEvent, tuple[str, ...]]] = []
        absorbed: list[AlarmEvent] = []
        for signal_events in by_signal.values():
            best = max(
                signal_events,
                key=lambda event: (_SEVERITY_RANK.get(event.severity, 0), -self._order_of(event.rule)),
            )
            pairs.append(
                (best, tuple(sorted(event.rule for event in signal_events if event.rule != best.rule)))
            )
            absorbed.extend(event for event in signal_events if event.rule != best.rule)
        return pairs, absorbed

    def _best_by_signal(self, events: list[AlarmEvent]) -> dict[str, AlarmEvent]:
        """本轮每个信号上最严重的一条事件（与吸收判定同一把尺子）。"""

        best: dict[str, AlarmEvent] = {}
        for event in events:
            current = best.get(event.signal)
            if current is None or self._better(event, current):
                best[event.signal] = event
        return best

    def _better(self, candidate: AlarmEvent, current: AlarmEvent) -> bool:
        """严重度优先，并列时按规则表顺序。"""

        rank_candidate = _SEVERITY_RANK.get(candidate.severity, 0)
        rank_current = _SEVERITY_RANK.get(current.severity, 0)
        if rank_candidate != rank_current:
            return rank_candidate > rank_current
        return self._order_of(candidate.rule) < self._order_of(current.rule)

    def _open_group(self, ts: float) -> "_Group":
        self._group_seq += 1
        group = _Group(
            gid=f"g{self._group_seq:04d}",
            opened_ts=ts,
            last_ts=ts,
            degraded=bool(self._missing),
        )
        self._groups.append(group)
        return group

    def _trim_groups(self) -> None:
        """合并组只留最近若干条：已关闭的从最旧的开始淘汰，开着的组不动。

        关闭的组已经落审计流水，内存里淘汰不影响审计一致性；上限固定，
        长时间运行不会把内存撑爆。
        """

        overflow = len(self._groups) - self._group_limit
        if overflow <= 0:
            return
        kept: list[_Group] = []
        for group in self._groups:
            if overflow > 0 and group.status == CLOSED:
                overflow -= 1
                continue
            kept.append(group)
        self._groups = kept

    def _resolve_first_batch(self, group: "_Group") -> None:
        """标定新组的首因：首批告警定案，之后不再翻案。"""

        rules = {occurrence.rule for occurrence in group.occurrences}
        effects = {effect for cause, effect in self._edges if cause in rules and effect in rules}
        roots = [occurrence for occurrence in group.occurrences if occurrence.rule not in effects]
        marks: dict[str, str] = {}
        if len(roots) == 1:
            group.first_out_rule = roots[0].rule
            for occurrence in group.occurrences:
                if occurrence.rule == group.first_out_rule:
                    marks[occurrence.rule] = FIRST_OUT
                else:
                    marks[occurrence.rule] = DERIVED if occurrence.rule in effects else CONCURRENT
        else:
            worst = self._worst_of(roots)
            if self._limits.policy_for(worst.system) == REPORT_WORST:
                group.first_out_rule = worst.rule
                for occurrence in group.occurrences:
                    if occurrence.rule == worst.rule:
                        marks[occurrence.rule] = ASSUMED_FIRST_OUT
                    else:
                        marks[occurrence.rule] = (
                            DERIVED if occurrence.rule in effects else CONCURRENT
                        )
            else:
                group.pending = True
                for occurrence in group.occurrences:
                    marks[occurrence.rule] = (
                        DERIVED if occurrence.rule in effects else PENDING
                    )
        group.occurrences = [
            dataclasses.replace(occurrence, mark=marks[occurrence.rule])
            for occurrence in group.occurrences
        ]

    def _attach(
        self,
        group: "_Group",
        event: AlarmEvent,
        ts: float,
        absorbed: tuple[str, ...],
        count_active: bool = True,
    ) -> Occurrence:
        """把新告警并入已开的组：有组内因果来源标被带出，否则标并发。"""

        known = {occurrence.rule for occurrence in group.occurrences}
        caused = {effect for cause, effect in self._edges if cause in known}
        mark = DERIVED if event.rule in caused else CONCURRENT
        return self._occurrence(group, event, ts, mark, absorbed, count_active)

    def _occurrence(
        self,
        group: "_Group",
        event: AlarmEvent,
        ts: float,
        mark: str,
        absorbed: tuple[str, ...],
        count_active: bool = True,
    ) -> Occurrence:
        self._seq += 1
        rule = self._rules.get(event.rule)
        occurrence = Occurrence(
            seq=self._seq,
            ts=ts,
            rule=event.rule,
            system=rule.system if rule else "unknown",
            severity=event.severity,
            signal=event.signal,
            value=event.value,
            threshold=event.threshold,
            message=event.message,
            mark=mark,
            group=group.gid,
            suppressed=absorbed,
        )
        group.occurrences.append(occurrence)
        if count_active:
            group.active += 1
        self._active[event.rule] = _Active(event.rule, group.gid, ts)
        return occurrence

    def _worst_of(self, occurrences: list[Occurrence]) -> Occurrence:
        """最严重的一条：严重度优先，并列时按规则表顺序（确定性）。"""

        return max(
            occurrences,
            key=lambda occurrence: (
                _SEVERITY_RANK.get(occurrence.severity, 0),
                -self._order_of(occurrence.rule),
            ),
        )

    def _order_of(self, rule: str) -> int:
        return self._rule_order.get(rule, len(self._rule_order))

    def _signal_of(self, rule: str) -> str:
        found = self._rules.get(rule)
        return found.signal if found else ""

    def _group_by_id(self, gid: str) -> "_Group | None":
        for group in self._groups:
            if group.gid == gid:
                return group
        return None

    def _current_group(self) -> "_Group | None":
        if self._groups and self._groups[-1].status == OPEN:
            return self._groups[-1]
        return None

    def _attachable(self, group: "_Group", ts: float) -> bool:
        """组内还有活跃告警，或距上一条告警仍在窗口内，才可并入。"""

        return group.active > 0 or (ts - group.last_ts) <= self._limits.correlation_window_s

    # ---- 停堆依据 -----------------------------------------------------

    def _demands(self) -> tuple[tuple[TripDemand, ...], tuple[str, ...]]:
        """汇总当前的停堆依据与因首因挂起而被压住的组。"""

        demands: list[TripDemand] = []
        held: list[str] = []
        for group in self._groups:
            if group.status != OPEN:
                continue
            triggers = [
                occurrence
                for occurrence in group.occurrences
                if occurrence.rule in self._active and self._is_trip(occurrence.rule)
            ]
            if not triggers:
                continue
            first = group.first_out()
            if group.pending or first is None:
                held.append(group.gid)
            else:
                demands.append(TripDemand(group.gid, first.rule, first.ts, triggers[0].rule))
        return tuple(demands), tuple(held)

    def _is_trip(self, rule: str) -> bool:
        found = self._rules.get(rule)
        return bool(found and found.trip)

    def note_trip(self, gid: str, now: float) -> dict:
        """把某组的首因登记为停堆依据；首因未定不允许登记。"""

        group = self._group_by_id(gid)
        if group is None:
            raise InterlockError(f"停堆依据指向不存在的告警组: {gid}", component="alarm")
        first = group.first_out()
        if group.pending or first is None:
            raise InterlockError(f"告警组 {gid} 首因未定，不能作为停堆依据", component="alarm")
        self._trip_basis = {
            "group": gid,
            "rule": first.rule,
            "system": first.system,
            "ts": round(first.ts, 3),
            "message": first.message,
            "assumed": first.mark == ASSUMED_FIRST_OUT,
            "recorded_at": round(self._clamp(now), 3),
        }
        return dict(self._trip_basis)

    def trip_basis(self) -> dict | None:
        """返回当前生效的停堆依据（首因），没有则为 ``None``。"""

        return None if self._trip_basis is None else dict(self._trip_basis)

    def clear_trip_basis(self) -> None:
        """复位投运成功后清除停堆依据。"""

        self._trip_basis = None

    def restore_basis(self, payload: dict) -> None:
        """跨重启恢复停堆依据，保证停堆、投运与审计口径一致。"""

        if not isinstance(payload, dict) or not payload.get("rule") or "ts" not in payload:
            raise InterlockError("停堆依据落盘内容不完整", component="alarm")
        self._trip_basis = dict(payload)

    def basis_recovered(self) -> bool:
        """首因是否确实恢复：已清除、通道未缺失，且恢复被稳定观察到。

        重启后还没评估过一轮时一律算未恢复——"看到它确实恢复了"必须以
        本轮观察为准，不能拿重启前的空白当恢复。
        """

        basis = self._trip_basis
        if basis is None:
            return True
        if self._ingests == 0 or self._last_ts is None:
            return False
        rule = str(basis["rule"])
        if rule in self._active:
            return False
        found = self._rules.get(rule)
        if found is None:
            return True
        if found.signal in self._missing:
            return False
        reference = max(
            float(basis["ts"]),
            self._last_breach.get(rule, float(basis["ts"])),
            self._started_at if self._started_at is not None else float(basis["ts"]),
        )
        return (self._last_ts - reference) >= self._limits.recovery_hold_s

    # ---- 查询 ---------------------------------------------------------

    def groups(self, limit: int = 20) -> list[dict]:
        """返回最近的合并组快照（最新的在前），供控制台展示。"""

        return [group.as_dict() for group in reversed(self._groups[-max(1, int(limit)):])]

    def open_groups(self) -> list[dict]:
        """返回仍未关闭的合并组。"""

        return [group.as_dict() for group in self._groups if group.status == OPEN]

    def group_count(self) -> int:
        """返回累计开组合并组数（含已淘汰的）。"""

        return self._group_seq

    def active_rules(self) -> list[str]:
        """返回仍在越限或清除观察期的规则。"""

        return sorted(self._active)
