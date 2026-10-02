"""告警因果图。

合并口径定死为"因果链 + 固定跟随窗"，不是纯时间窗：只按时间挨着就合会把两路
不相干的告警误并，漏掉真正的首因。因果图是一张静态的有向图：边 ``a -> b`` 表示
"先出 a 时，随后出现的 b 是被 a 带出来的"。边是数据而不是判定分支，工艺关系
调整时改图不改合并器。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CausalEdge:
    """一条因果边：``cause`` 先出，``effect`` 是被带出来的。"""

    cause: str
    effect: str
    note: str = ""

    def as_dict(self) -> dict:
        return {"cause": self.cause, "effect": self.effect, "note": self.note}


class CausalGraph:
    """告警规则之间的因果先后关系。"""

    def __init__(self, edges: list[CausalEdge] | None = None) -> None:
        self._edges: list[CausalEdge] = []
        self._successors: dict[str, set[str]] = {}
        self._predecessors: dict[str, set[str]] = {}
        for edge in edges or []:
            self.add(edge)

    def add(self, edge: CausalEdge) -> CausalEdge:
        """追加一条边；自环与重复边即报错。"""

        if edge.cause == edge.effect:
            raise ValueError(f"因果边不能自环: {edge.cause}")
        if edge.effect in self._successors.get(edge.cause, set()):
            raise ValueError(f"因果边重复: {edge.cause} -> {edge.effect}")
        self._edges.append(edge)
        self._successors.setdefault(edge.cause, set()).add(edge.effect)
        self._predecessors.setdefault(edge.effect, set()).add(edge.cause)
        return edge
    def predecessors(self, rule: str) -> set[str]:
        """返回可能带出该规则的全部因果前驱。"""

        return set(self._predecessors.get(rule, ()))

    def relates(self, cause: str, effect: str) -> bool:
        """``cause`` 是否是 ``effect`` 的直接因果前驱。"""

        return cause in self._predecessors.get(effect, set())

    def describe(self) -> list[dict]:
        """返回边表的可打印形式。"""

        return [edge.as_dict() for edge in self._edges]

    def __len__(self) -> int:
        return len(self._edges)
