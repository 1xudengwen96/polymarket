"""概率模型接口 + Baseline 占位 + 训练工件加载。

目标变量：P(Up) = P(TWAP_L(t_end) >= PTB)（docs/05 §1）。

BaselineModel：布朗近似占位——当前 TWAP 距 PTB 的距离 ÷ 剩余时间的预期波动，
经 logistic 变换。仅用于没有训练工件时的兜底；正式模型 = TrainedLogisticModel
（由 `pm5hft.backtest train` 产出 artifacts/logreg_v1.json）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Protocol


class Model(Protocol):
    def predict(self, f: dict) -> float: ...


class BaselineModel:
    def __init__(self, min_vol_1s: float = 1e-6, tau_s: float = 5.0) -> None:
        self.min_vol_1s = min_vol_1s
        self.tau_s = tau_s  # 距离衰减的时间尺度（秒）

    def predict(self, f: dict) -> float:
        dist_bps = f.get("dist_bps") or 0.0
        remaining = max(f.get("remaining_s") or 60.0, 1.0)
        rv_60s = abs(f.get("rv_60s") or 0.0)
        # 每秒波动（小数收益）
        rv_1s = max(rv_60s / math.sqrt(60.0), self.min_vol_1s)
        dist_frac = dist_bps / 1e4  # bps → 小数
        # 剩余时间内的预期 1σ 波动
        sigma_rem = rv_1s * math.sqrt(remaining)
        z = dist_frac / max(sigma_rem, 1e-9)
        z = max(-50.0, min(50.0, z))  # 防 exp 溢出
        p = 1.0 / (1.0 + math.exp(-z))
        return min(max(p, 0.005), 0.995)


class TrainedLogisticModel:
    """训练工件中的逻辑回归（特征缺失按 0 贡献处理，与回测一致）。"""

    def __init__(self, features: list[str], coef: list[float], intercept: float) -> None:
        self.features = features
        self.coef = coef
        self.intercept = intercept

    def predict(self, f: dict) -> float:
        z = self.intercept
        for name, w in zip(self.features, self.coef, strict=True):
            v = f.get(name)
            if v is None:
                continue
            z += w * float(v)
        z = max(-50.0, min(50.0, z))  # 防 exp 溢出
        p = 1.0 / (1.0 + math.exp(-z))
        return min(max(p, 0.005), 0.995)


def load_artifact(path: str | None) -> tuple[TrainedLogisticModel, list[dict]] | None:
    """加载训练工件 → (模型, 校准桶行)。不存在/损坏返回 None（调用方兜底 Baseline）。"""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        art = json.loads(p.read_text(encoding="utf-8"))
        model = TrainedLogisticModel(art["features"], art["coef"], art["intercept"])
        return model, list(art.get("calibration") or [])
    except (KeyError, TypeError, json.JSONDecodeError, OSError, ValueError):
        return None
