"""用纸面/实盘真实特征重训模型 + 校准桶（修复回测合成簿特征漂移）。

数据源：data/pm5hft.db 的 decision_log（每秒真实特征 + 最终结算结果）。
用法: python scripts/retrain_live.py [--db data/pm5hft.db] [--out artifacts/logreg_v1_live.json]
部署: 训练完成后把 config/probability.yaml 的 model_artifact 指到新工件并重启机器人。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/pm5hft.db")
    p.add_argument("--out", default="artifacts/logreg_v1_live.json")
    p.add_argument("--min-windows", type=int, default=300)
    args = p.parse_args()

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "SELECT d.market_id, d.extra, COALESCE(s.gamma_result, s.self_result) "
        "FROM decision_log d LEFT JOIN settlements s ON s.market_id = d.market_id "
        "WHERE COALESCE(s.gamma_result, s.self_result) IN ('UP','DOWN') "
        "AND d.extra IS NOT NULL ORDER BY d.id"
    ).fetchall()
    conn.close()

    # 每窗口取一行（T-60 附近的决策，模型的主要决策时点），避免同一窗口 1s 级自相关样本
    per_win: dict[int, tuple[dict, str]] = {}
    for mid, ejson, final in rows:
        if mid in per_win:
            continue
        try:
            f = json.loads(ejson)
        except json.JSONDecodeError:
            continue
        per_win[mid] = (f, final)
    samples = list(per_win.values())
    n_windows = len(samples)
    print(f"已结算窗口（每窗口1样本）: {n_windows}")
    if n_windows < args.min_windows:
        print(f"[skip] 独立窗口不足 {args.min_windows}，继续积累（当前 {n_windows}）。"
              "建议积累 5-14 天（~1500-4000 窗口）再训练。")
        return

    samples = [(f, 1 if final == "UP" else 0) for f, final in samples]

    cols = sorted({k for f, _ in samples for k in f if f[k] is not None})
    X = [[float(f.get(c, 0.0) or 0.0) for c in cols] for f, _ in samples]
    y = [lb for _, lb in samples]

    n_train = int(len(X) * 0.7)
    X_train, y_train = X[:n_train], y[:n_train]
    X_val, y_val = X[n_train:], y[n_train:]

    from sklearn.linear_model import LogisticRegression  # noqa: PLC0415

    model = LogisticRegression(max_iter=1000, C=0.1)
    model.fit(X_train, y_train)
    val_acc = model.score(X_val, y_val)
    print(f"train={len(X_train)} val={len(X_val)} val_acc={val_acc:.4f}")

    from pm5hft.probability.calibration import Calibrator  # noqa: PLC0415

    cal = Calibrator(min_n=50)
    for x, yy in zip(X_train, y_train, strict=True):
        pred = model.predict_proba([x])[0][1]
        cal.record(float(pred), bool(yy))
    print(f"brier={cal.brier():.4f} ece={cal.ece():.4f}")

    artifact = {
        "model": "logistic",
        "source": "live decision_log",
        "features": cols,
        "coef": [float(c) for c in model.coef_[0]],
        "intercept": float(model.intercept_[0]),
        "calibration": cal.rows(),
        "val_acc": val_acc,
        "brier": cal.brier(),
        "ece": cal.ece(),
        "n_train": len(X_train),
        "n_val": len(X_val),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(f"artifact written: {out_path}")


if __name__ == "__main__":
    main()
