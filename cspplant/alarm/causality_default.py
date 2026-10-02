"""默认告警因果图。

边的方向是"先出的首因 -> 被带出来的告警"，取自本仿真工艺链：

* 降额带先于越限：同一只温度探头先跨降额线、再跨上限；
* 低流量先于超温：熔盐流量不足导致吸热器带不走热量；
* 冷罐液位低先于低流量：供液不足拖低泵流量；
* 超压先于主蒸汽压力高：母管超压顶到蒸汽侧；
* 热罐高液位先于母管超压：回水受阻抬高母管压力。

纯信号缺失不设因果边：它永远自成一组的首因，避免被工艺告警吞掉。
"""

from __future__ import annotations

from .causality import CausalEdge, CausalGraph


def default_causal_graph() -> CausalGraph:
    """构造默认因果边表。"""

    return CausalGraph(
        [
            CausalEdge("receiver-derate", "receiver-overtemperature", "同探头先降额后越限"),
            CausalEdge("salt-flow-low", "receiver-derate", "流量不足，热量带不走"),
            CausalEdge("salt-flow-low", "receiver-overtemperature", "低流量导致出口超温"),
            CausalEdge("cold-tank-low", "salt-flow-low", "供液不足拖低泵流量"),
            CausalEdge("header-overpressure", "steam-pressure", "母管超压顶到蒸汽侧"),
            CausalEdge("hot-tank-high", "header-overpressure", "回水受阻抬高母管压力"),
        ]
    )
