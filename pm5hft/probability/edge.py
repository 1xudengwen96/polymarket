"""Fair → Net Edge 管线（docs/05 §5）。

gross_edge = cal_prob - ask（买 Up 视角）
net_edge = gross - taker费 - 滑点 - 执行风险 - 时间风险 - 模型误差 - 风险缓冲
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class EdgeResult:
    gross_edge: float
    net_edge: float
    components: dict[str, float] = field(default_factory=dict)


def compute_edge(
    cal_prob: float,
    ask: float,
    taker_fee: float,
    remaining_s: float,
    model_err: float,
    risk_buffer: float,
    slippage_est: float = 0.01,
    exec_risk: float = 0.003,
    time_risk_k: float = 0.002,
) -> EdgeResult:
    gross = cal_prob - ask
    # 剩余时间越少，模型对未来路径的把握越差：时间风险按 sqrt(60/remaining) 放大
    time_risk = time_risk_k * math.sqrt(60.0 / max(1.0, remaining_s))
    comps = {
        "taker_fee": taker_fee,
        "slippage_est": slippage_est,
        "exec_risk": exec_risk,
        "time_risk": time_risk,
        "model_err": model_err,
        "risk_buffer": risk_buffer,
    }
    net = gross - sum(comps.values())
    return EdgeResult(gross_edge=gross, net_edge=net, components=comps)


def depth_weighted_ask(levels: list[tuple[float, float]], qty: float) -> tuple[float, float] | None:
    """按簿口深度加权计算可成交均价。

    levels: [(price, size), ...] 升序；qty: 目标数量。
    返回 (均价, 可成交数量)；深度不足时返回可成交部分的均价。
    """
    remaining = qty
    cost = 0.0
    filled = 0.0
    for price, size in levels:
        if remaining <= 0:
            break
        take = min(remaining, size)
        cost += price * take
        filled += take
        remaining -= take
    if filled == 0:
        return None
    return cost / filled, filled
