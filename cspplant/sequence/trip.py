"""停堆保护编排：过温 → 闩锁 → 镜场避让 → 汽机旁路 → 排盐。

保护动作的顺序决定了停堆之后还能不能安全重启：先闩锁住故障，再让镜场退出
聚焦，然后汽机走旁路卸载，最后关母管。缺少闩锁这一步，温度回落时系统会自己
恢复聚焦，超温会反复发生。
"""

from __future__ import annotations

from ..alarm.history import AlarmHistory
from ..alarm.latch import FaultLatch
from ..alarm.rules import ALARM, AlarmEvent
from ..errors import PlantError
from ..field.field import FieldController
from ..receiver.receiver import Receiver
from ..steam.system import SteamSystem

MAX_STEPS = 1800


def emergency_trip(
    receiver: Receiver,
    field: FieldController,
    steam: SteamSystem,
    latch: FaultLatch,
    history: AlarmHistory,
    now: float,
    reason: str = "overtemperature",
    source: str = "receiver",
) -> dict:
    """执行一次停堆保护。"""

    steps: list[dict] = []
    violations: list[str] = []

    receiver.trip(reason)
    steps.append(
        {
            "component": "receiver",
            "action": "trip",
            "detail": {"reason": reason, "temperature_c": round(receiver.output_temperature_c(), 2)},
        }
    )

    latch_state = latch.trip(source, reason, now)
    history.record(
        now,
        _event(f"trip-{source}", reason, receiver.output_temperature_c(), receiver.limits.temp_max_c),
    )
    steps.append({"component": "alarm", "action": "latch", "detail": latch_state.as_dict()})

    stowed = field.stow_all()
    steps.append(
        {
            "component": "field",
            "action": "stow_all",
            "detail": {"mirrors": stowed, "posture": "stowed"},
        }
    )
    if field.focusing_count() > 0:
        violations.append("mirrors_still_focusing")

    report = steam.trip(now)
    steps.extend(report.steps)
    violations.extend(report.violations)

    try:
        receiver.header.close()
    except PlantError as error:  # pragma: no cover - 关阀失败属于硬件故障
        violations.append(error.message)
    steps.append(
        {
            "component": "receiver",
            "action": "close_header",
            "detail": {"state": receiver.header.state(), "pressure_mpa": round(receiver.header.pressure_mpa(), 3)},
        }
    )

    for _ in range(MAX_STEPS):
        pressure = receiver.header.depressurise(1.0)
        if pressure <= 0.0:
            break
    steps.append(
        {
            "component": "receiver",
            "action": "depressurise",
            "detail": {"pressure_mpa": round(receiver.header.pressure_mpa(), 3)},
        }
    )

    return {
        "ok": not violations,
        "reason": reason,
        "steps": steps,
        "violations": violations,
        "latched": latch.latched(),
    }


def reset_after_trip(
    receiver: Receiver,
    latch: FaultLatch,
    now: float,
    operator: str,
    trip_basis: dict | None = None,
    basis_recovered: bool = True,
) -> dict:
    """复位一次停堆：先确认首因已恢复，再清闩锁，最后复位吸热器。

    带停堆依据（首因）的复位必须先看到首因确实恢复；首因未恢复时复位被拒，
    拒绝理由与依据一起进报告，保证投运依据与审计一致。
    """

    steps: list[dict] = []
    violations: list[str] = []

    if trip_basis is not None:
        if not basis_recovered:
            violations.append(f"首因 {trip_basis['rule']} 未恢复，不允许复位投运")
            return {
                "ok": False,
                "steps": steps,
                "violations": violations,
                "trip_basis": dict(trip_basis),
                "state": receiver.state(),
            }
        steps.append(
            {
                "component": "alarm",
                "action": "verify_first_out",
                "detail": {
                    "group": trip_basis["group"],
                    "rule": trip_basis["rule"],
                    "recovered": True,
                },
            }
        )

    try:
        latch_state = latch.reset(now, operator)
    except PlantError as error:
        violations.append(error.message)
        return {"ok": False, "steps": steps, "violations": violations, "state": receiver.state()}
    steps.append({"component": "alarm", "action": "reset_latch", "detail": latch_state.as_dict()})

    try:
        state = receiver.reset(latch_cleared=not latch.latched())
    except PlantError as error:
        violations.append(error.message)
        return {"ok": False, "steps": steps, "violations": violations, "state": receiver.state()}
    steps.append({"component": "receiver", "action": "reset", "detail": {"state": state}})

    report = {"ok": not violations, "steps": steps, "violations": violations, "state": state}
    if trip_basis is not None:
        report["trip_basis"] = dict(trip_basis)
    return report


def _event(rule: str, reason: str, value: float, threshold: float):
    """构造一条告警事件，供停堆时同时写进告警历史。"""

    return AlarmEvent(
        rule=rule,
        severity=ALARM,
        signal="receiver_temperature_c",
        value=float(value),
        threshold=float(threshold),
        message=reason,
    )
