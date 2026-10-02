"""告警合并器（因果链 + 固定跟随窗）。

合并口径只有一条：两条告警必须能在因果图上连上、且报告时刻相差不超过跟随窗，
才允许并到同一组；单纯时间挨着但没有因果关系的告警各成一组。每条入组告警带
全局单调序号，组内按时序排列；时间戳回退一律拒绝，合并与标记的时序不许乱。

组内标记两类角色：首因（``root``，先出的那条因果源头）与带出（``follower``，
挂在某条因果前驱后面）。首因不明时按系统配置二选一，不允许现场临时拍：

* ``severity``：不等，按最严重那条认定首因，严重度并列取时序最早；之后若在窗
  内补到真正的因果前驱，重新裁定并留一条"首因改判"审计痕迹；
* ``pending``：挂起，首因暂挂最严重的那条但标记未明确，直到窗内补到因果前驱
  或值班员在控制台认定；挂起期间保护照常动作，但重新投运硬等首因明确并恢复。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from ..config import AlarmLimits
from ..errors import InterlockError
from .rules import ALARM, AlarmEvent

ROOT = "root"
FOLLOWER = "follower"

PENDING = "pending"
OPEN = "open"
CLOSED = "closed"

POLICY_SEVERITY = "severity"
POLICY_PENDING = "pending"

_SEVERITY_RANK = {ALARM: 2}


@dataclass
class GroupMember:
    """组内一条告警及其角色标记。"""

    seq: int
    ts: float
    rule: str
    severity: str
    role: str
    caused_by: str
    recovered: bool = False
    recovered_ts: float = 0.0

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": round(self.ts, 3),
            "rule": self.rule,
            "severity": self.severity,
            "role": self.role,
            "caused_by": self.caused_by,
            "recovered": self.recovered,
            "recovered_ts": round(self.recovered_ts, 3) if self.recovered else None,
        }


@dataclass
class AlarmGroup:
    """一组时间上挨着、因果上相连的告警。"""

    group_id: str
    created_ts: float
    policy: str
    members: list[GroupMember] = field(default_factory=list)
    root_rule: str = ""
    state: str = OPEN
    root_resolved: bool = True
    tripped: bool = False
    last_join_ts: float = 0.0
    closed_ts: float = 0.0

    def member(self, rule: str) -> GroupMember | None:
        for item in self.members:
            if item.rule == rule:
                return item
        return None

    @property
    def root(self) -> GroupMember:
        return next(item for item in self.members if item.rule == self.root_rule)

    def rules(self) -> list[str]:
        return [item.rule for item in self.members]

    def root_recovered(self) -> bool:
        root = self.member(self.root_rule)
        return bool(root and root.recovered)

    def all_recovered(self) -> bool:
        return bool(self.members) and all(item.recovered for item in self.members)

    def as_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "state": self.state,
            "policy": self.policy,
            "tripped": self.tripped,
            "created_ts": round(self.created_ts, 3),
            "last_join_ts": round(self.last_join_ts, 3),
            "closed_ts": round(self.closed_ts, 3) if self.closed_ts else None,
            "root_rule": self.root_rule,
            "root_resolved": self.root_resolved,
            "root_severity": self.root.severity,
            "root_ts": round(self.root.ts, 3),
            "root_recovered": self.root_recovered(),
            "all_recovered": self.all_recovered(),
            "member_count": len(self.members),
            "members": [item.as_dict() for item in self.members],
        }


@dataclass(frozen=True)
class IngestResult:
    """一轮合并结果：新入组的成员、涉及的组与首因改判记录。"""

    members: list[GroupMember]
    groups: list[AlarmGroup]
    amendments: list[dict]

    def raised(self) -> list[GroupMember]:
        """本轮新出现的成员（按入组顺序）。"""

        return list(self.members)


class Correlator:
    """因果链 + 跟随窗的告警合并器。"""

    def __init__(
        self,
        graph,
        limits: AlarmLimits,
        *,
        id_prefix: str = "grp",
        active_limit: int = 512,
    ) -> None:
        self._graph = graph
        self._limits = limits
        self._prefix = id_prefix
        self._active_limit = max(1, int(active_limit))
        self._groups: list[AlarmGroup] = []
        self._active_rules: set[str] = set()
        self._seq = 0
        self._last_ts = 0.0
        self._lock = threading.RLock()

    @property
    def window_s(self) -> float:
        return self._limits.follow_window_s

    @property
    def policy(self) -> str:
        return self._limits.unknown_cause_policy

    # ---- 入组 ---------------------------------------------------------

    def ingest(self, now: float, events: list[AlarmEvent]) -> IngestResult:
        """把本轮越限告警按因果链合并，拒绝时间戳回退。"""

        moment = float(now)
        if moment < self._last_ts:
            raise InterlockError(
                f"告警时序回退：{moment} < {self._last_ts}", component="alarm"
            )
        self.sweep(moment)
        members: list[GroupMember] = []
        touched: dict[str, AlarmGroup] = {}
        amendments: list[dict] = []
        with self._lock:
            for event in events:
                if event.rule in self._active_rules:
                    continue
                self._seq += 1
                member, group, amendment = self._place(moment, event, self._seq)
                self._active_rules.add(event.rule)
                members.append(member)
                touched[group.group_id] = group
                if amendment:
                    amendments.append(amendment)
            self._last_ts = moment
        return IngestResult(members, list(touched.values()), amendments)

    def _place(
        self, moment: float, event: AlarmEvent, seq: int
    ) -> tuple[GroupMember, AlarmGroup, dict | None]:
        """把一条告警放进因果组：先补迟到的首因，再挂带出。"""

        # 1) 它是某个挂起/开放组内成员的因果前驱，且在跟随窗内 → 迟到的首因。
        root_group = self._find_open_with_successor(event.rule, moment)
        if root_group is not None:
            group = root_group
            member = GroupMember(seq, moment, event.rule, event.severity, ROOT, "")
            group.members.append(member)
            group.last_join_ts = moment
            amendment = self._reassign_root(group, moment, reason="causal_predecessor")
            self._sort_members(group)
            return member, group, amendment

        # 2) 组内已有它的因果前驱，且在跟随窗内 → 被带出来的告警。
        predecessor_match = self._find_open_with_predecessor(event.rule, moment)
        if predecessor_match is not None:
            predecessor_group, predecessor = predecessor_match
            group = predecessor_group
            member = GroupMember(
                seq, moment, event.rule, event.severity, FOLLOWER, predecessor
            )
            group.members.append(member)
            group.last_join_ts = moment
            self._sort_members(group)
            return member, group, None

        # 3) 无前因可挂：自成一组，按系统策略决定首因状态。
        group = self._new_group(moment)
        member = GroupMember(seq, moment, event.rule, event.severity, ROOT, "")
        group.members.append(member)
        group.root_rule = event.rule
        if self.policy == POLICY_PENDING and self._graph.predecessors(event.rule):
            group.state = PENDING
            group.root_resolved = False
        return member, group, None

    def _find_open_with_successor(self, rule: str, moment: float) -> AlarmGroup | None:
        """找一个开放组：组内有该规则的因果后继，且该规则是在跟随窗内补到的。"""

        candidates = [
            group
            for group in self._groups
            if group.state in (OPEN, PENDING)
            and any(self._graph.relates(rule, item.rule) for item in group.members)
            and moment >= min(item.ts for item in group.members)
            and moment - min(item.ts for item in group.members) <= self.window_s
        ]
        return candidates[-1] if candidates else None

    def _find_open_with_predecessor(
        self, rule: str, moment: float
    ) -> tuple[AlarmGroup, str] | None:
        """找一个开放组：组内有该规则的因果前驱且时间差落在跟随窗内。

        一个规则可能挂在多条边上（如超温的前驱既有降额也有低流量），取组内
        时序最近的直接前驱，保证长链上 ``caused_by`` 指向上一跳而不是链头。
        """

        best: tuple[float, int, AlarmGroup, str] | None = None
        for group in self._groups:
            if group.state not in (OPEN, PENDING):
                continue
            for item in group.members:
                if self._graph.relates(item.rule, rule) and 0.0 <= moment - item.ts <= self.window_s:
                    key = (item.ts, item.seq, group, item.rule)
                    if best is None or key[:2] > best[:2]:
                        best = (item.ts, item.seq, group, item.rule)
        if best is None:
            return None
        return best[2], best[3]

    def _new_group(self, moment: float) -> AlarmGroup:
        group = AlarmGroup(
            group_id=f"{self._prefix}-{len(self._groups) + 1:04d}",
            created_ts=moment,
            last_join_ts=moment,
            policy=self.policy,
        )
        self._groups.append(group)
        if len(self._groups) > self._active_limit:
            del self._groups[: len(self._groups) - self._active_limit]
        return group

    def _reassign_root(self, group: AlarmGroup, now: float, *, reason: str) -> dict | None:
        """按策略重新裁定首因，返回改判记录；首因未变且非解除挂起则不记。"""

        previous = group.root_rule
        was_pending = not group.root_resolved
        if self.policy == POLICY_SEVERITY:
            # 补到了真正的因果前驱：前驱先于严重度；其余按最严重、并列取时序最早。
            chosen = max(
                group.members,
                key=lambda item: (
                    0 if self._graph.predecessors(item.rule) & set(group.rules()) else 1,
                    _SEVERITY_RANK.get(item.severity, 1),
                    -item.ts,
                    -item.seq,
                ),
            )
        else:
            # pending：因果前驱到齐即解除挂起，首因取组内没有前驱的因果源头；
            # 多个源头（无因果关系的成员）并列时取时序最早。
            chosen = min(
                group.members,
                key=lambda item: (
                    1 if self._graph.predecessors(item.rule) & set(group.rules()) else 0,
                    item.ts,
                    item.seq,
                ),
            )
        for item in group.members:
            item.role = ROOT if item.rule == chosen.rule else FOLLOWER
            if item.rule != chosen.rule and not item.caused_by:
                item.caused_by = self._parent_rule(group, item.rule)
        group.root_rule = chosen.rule
        group.state = OPEN
        group.root_resolved = True
        if chosen.rule == previous and not was_pending:
            return None
        return {
            "ts": round(now, 3),
            "group_id": group.group_id,
            "previous_root": previous,
            "root_rule": chosen.rule,
            "reason": reason,
            "had_been_pending": was_pending,
        }

    def _parent_rule(self, group: AlarmGroup, rule: str) -> str:
        """在组内为一条带出告警找最近的因果前驱。"""

        parents = [
            item.rule
            for item in group.members
            if item.rule != rule and self._graph.relates(item.rule, rule)
        ]
        return parents[0] if parents else group.root_rule

    def _sort_members(self, group: AlarmGroup) -> None:
        """组内按 (ts, seq) 排死，时序不能乱。"""

        group.members.sort(key=lambda item: (item.ts, item.seq))

    # ---- 恢复与关组 ---------------------------------------------------

    def restore_member(
        self,
        ts: float,
        rule: str,
        severity: str,
        group_id: str,
        role: str,
        caused_by: str = "",
        root_resolved: bool = True,
    ) -> AlarmGroup:
        """按审计流水幂等重建一个组成员（跨重启恢复用）。

        与 :meth:`ingest` 的区别：不去重"当前活跃规则"，恢复后又重新报出的告警
        是新一轮事件，必须再次进组；组沿用审计里的原组号，角色与首因也以审计
        记录为准，重放结果不得依赖重放顺序重新推断。
        """

        moment = float(ts)
        with self._lock:
            group = self._group_or_restore(group_id, moment)
            existing = group.member(rule)
            if existing is not None and existing.ts == moment:
                return group
            self._seq += 1
            group.members.append(
                GroupMember(self._seq, moment, rule, severity, role, caused_by)
            )
            group.last_join_ts = max(group.last_join_ts, moment)
            if role == ROOT:
                group.root_rule = rule
            group.root_resolved = group.root_resolved and root_resolved
            self._sort_members(group)
            return group

    def _group_or_restore(self, group_id: str, moment: float) -> AlarmGroup:
        for group in self._groups:
            if group.group_id == group_id:
                return group
        group = AlarmGroup(
            group_id=group_id,
            created_ts=moment,
            last_join_ts=moment,
            policy=self.policy,
        )
        self._groups.append(group)
        self._groups.sort(key=lambda item: item.created_ts)
        return group

    def mark_cleared(self, now: float, rules: list[str]) -> list[dict]:
        """把已不越限的规则标记恢复，返回本轮恢复的成员。"""

        moment = float(now)
        if moment < self._last_ts:
            raise InterlockError(
                f"告警时序回退：{moment} < {self._last_ts}", component="alarm"
            )
        cleared: list[dict] = []
        with self._lock:
            for rule in rules:
                self._active_rules.discard(rule)
                # 封口不影响恢复：首因可能在组封口后才真正恢复，仍要标记。
                for group in self._groups:
                    member = group.member(rule)
                    if member and not member.recovered:
                        member.recovered = True
                        member.recovered_ts = moment
                        cleared.append(
                            {"ts": round(moment, 3), "group_id": group.group_id, "rule": rule}
                        )
            self._last_ts = moment
        return cleared

    def sweep(self, now: float) -> list[AlarmGroup]:
        """关组：最后一条入组后过了整个跟随窗再无新成员，组封口。"""

        moment = float(now)
        closed: list[AlarmGroup] = []
        with self._lock:
            for group in self._groups:
                if group.state in (OPEN, PENDING) and moment - group.last_join_ts > self.window_s:
                    group.state = CLOSED
                    group.closed_ts = moment
                    closed.append(group)
        return closed

    def mark_tripped(self, group_id: str) -> AlarmGroup:
        """登记某个组已经作为停堆首因组。"""

        with self._lock:
            group = self.group(group_id)
            group.tripped = True
            return group

    def assert_root(self, group_id: str, rule: str, operator: str, now: float) -> dict:
        """值班员认定挂起组的首因；只对挂起组、组内成员生效。"""

        if not operator:
            raise InterlockError("认定首因必须记录操作人", component="alarm")
        with self._lock:
            group = self.group(group_id)
            if group.root_resolved:
                raise InterlockError(f"组 {group_id} 首因已明确，不能重复认定", component="alarm")
            if group.member(rule) is None:
                raise InterlockError(
                    f"规则 {rule} 不在组 {group_id} 内，不能认定为首因", component="alarm"
                )
            amendment = self._reassign_root(group, float(now), reason="operator_asserted")
            amendment["operator"] = operator
            return amendment

    # ---- 查询 ---------------------------------------------------------

    def group(self, group_id: str) -> AlarmGroup:
        for group in self._groups:
            if group.group_id == group_id:
                return group
        raise InterlockError(f"告警组不存在: {group_id}", component="alarm")

    def groups(self, *, include_closed: bool = True) -> list[AlarmGroup]:
        with self._lock:
            selected = list(self._groups)
        if not include_closed:
            selected = [group for group in selected if group.state in (OPEN, PENDING)]
        return selected

    def pending_groups(self) -> list[AlarmGroup]:
        """首因尚未明确的组（含跟随窗到期封口后仍未明确的）。"""

        with self._lock:
            return [group for group in self._groups if not group.root_resolved]

    def group_of(self, rule: str) -> AlarmGroup | None:
        """返回某规则当前所在的最近一个组。"""

        for group in reversed(self._groups):
            if group.member(rule) is not None:
                return group
        return None

    def describe(self) -> list[dict]:
        return [group.as_dict() for group in self.groups()]
