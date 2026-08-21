"""概率校准（docs/05 §4）。

分桶：模型输出概率维度。每桶记录 n / 胜场 / 实际胜率 / Brier / ECE 贡献。
冷启动（样本不足 min_n）：收缩因子保守降置信。
99¢ 陷阱：0.99+ 桶实际胜率若低于市场价，TAIL_CAPTURE 在该桶禁用（策略层读取）。
"""

from __future__ import annotations

from dataclasses import dataclass

# [lo, hi) 分桶；最后一桶含 1.0
BUCKETS: list[tuple[float, float]] = [
    (0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 0.75),
    (0.75, 0.80), (0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 0.97),
    (0.97, 0.98), (0.98, 0.99), (0.99, 1.01),
]


@dataclass
class Bucket:
    lo: float
    hi: float
    n: int = 0
    wins: int = 0
    pred_sum: float = 0.0
    brier_sum: float = 0.0

    @property
    def actual_rate(self) -> float | None:
        return self.wins / self.n if self.n else None

    @property
    def mean_pred(self) -> float | None:
        return self.pred_sum / self.n if self.n else None


@dataclass
class CalibrationResult:
    cal_prob: float
    bucket: tuple[float, float]
    bucket_n: int
    actual_rate: float | None
    cold: bool
    # 0.99+ 桶实际胜率不足市场价的信号（TAIL_CAPTURE 禁用依据）
    tail_capture_unsafe: bool = False


class Calibrator:
    def __init__(self, min_n: int = 200, cold_start_shrink: float = 0.85) -> None:
        self.min_n = min_n
        self.cold_start_shrink = cold_start_shrink
        self.buckets: dict[tuple[float, float], Bucket] = {
            (lo, hi): Bucket(lo=lo, hi=hi) for lo, hi in BUCKETS
        }

    @staticmethod
    def _find(pred: float) -> tuple[float, float]:
        # 按"偏好方向的置信度"分桶：pred<0.5 时取其镜像（0.42 → 0.58），
        # 桶语义 = 模型对偏好方的置信度 vs 偏好方实际胜率。
        if pred < 0.5:
            pred = 1.0 - pred
        for lo, hi in BUCKETS:
            if lo <= pred < hi:
                return (lo, hi)
        return BUCKETS[-1]

    def record(self, pred: float, won: bool) -> None:
        """记录一条样本：pred = P(Up)；won = Up 是否获胜。

        桶语义按"偏好方向"：pred<0.5 时镜像置信度并翻转胜负。
        """
        if pred < 0.5:
            pred = 1.0 - pred
            won = not won
        key = self._find(pred)
        b = self.buckets[key]
        b.n += 1
        b.wins += int(won)
        b.pred_sum += pred
        b.brier_sum += (pred - float(won)) ** 2

    def calibrate(self, pred: float, market_price: float | None = None) -> CalibrationResult:
        """返回偏好方向的校准概率（调用方传 pred ≥ 0.5，内部自动镜像）。"""
        pred = min(max(pred, 0.005), 0.995)
        if pred < 0.5:
            pred = 1.0 - pred
        key = self._find(pred)
        b = self.buckets[key]
        if b.n == 0:
            cal = pred * self.cold_start_shrink
            return CalibrationResult(cal, key, 0, None, True, tail_capture_unsafe=False)
        w = min(1.0, b.n / self.min_n)
        actual = b.actual_rate if b.actual_rate is not None else pred
        cal = w * actual + (1 - w) * pred * self.cold_start_shrink
        cold = w < 1.0
        # 0.99+ 陷阱：实际胜率必须支撑市场价，否则尾部策略禁用
        tail_unsafe = key == (0.99, 1.01) and b.n >= self.min_n and actual < (market_price or 0.0)
        return CalibrationResult(cal, key, b.n, b.actual_rate, cold, tail_capture_unsafe=tail_unsafe)

    def brier(self) -> float | None:
        total_n = sum(b.n for b in self.buckets.values())
        total_b = sum(b.brier_sum for b in self.buckets.values())
        if total_n == 0 or total_b == 0:
            return None  # 无记录或仅加载 n/wins（load_rows 场景）
        return total_b / total_n

    def ece(self) -> float | None:
        total_n = sum(b.n for b in self.buckets.values())
        if total_n == 0:
            return None
        if sum(b.pred_sum for b in self.buckets.values()) == 0:
            return None  # 未加载预测均值（load_rows 场景）
        ece = 0.0
        for b in self.buckets.values():
            if b.n == 0:
                continue
            ece += (b.n / total_n) * abs((b.mean_pred or 0.0) - (b.actual_rate or 0.0))
        return ece

    def rows(self) -> list[dict]:
        out = []
        for (lo, hi), b in sorted(self.buckets.items()):
            out.append({
                "bucket_low": f"{lo:.2f}",
                "bucket_high": f"{hi:.2f}",
                "n": b.n,
                "wins": b.wins,
                "actual_rate": None if b.actual_rate is None else f"{b.actual_rate:.4f}",
                "brier": None if b.n == 0 else f"{b.brier_sum / b.n:.4f}",
                "ece_contrib": None,
            })
        return out

    def load_rows(self, rows: list[dict]) -> None:
        for r in rows:
            key = (float(r["bucket_low"]), float(r["bucket_high"]))
            b = self.buckets.get(key)
            if b is None:
                continue
            b.n = int(r["n"])
            b.wins = int(r["wins"])
