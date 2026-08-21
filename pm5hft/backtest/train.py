"""模型训练：特征集 → 逻辑回归 + 校准桶 → 产物 JSON。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select

from ..db import init_db, session_factory
from ..models import FeatureRow
from ..probability.calibration import Calibrator


async def train(db: str, out: str) -> int:
    init_db(f"sqlite+aiosqlite:///{db}")
    async with session_factory()() as sess:
        rows = list((await sess.execute(select(FeatureRow))).scalars().all())
    if not rows:
        print("no feature rows — run `python -m pm5hft.backtest collect` first", file=sys.stderr)
        return 2
    print(f"feature rows: {len(rows)}")

    cols = sorted({k for r in rows for k in json.loads(r.features)})
    X, y = [], []
    for r in rows:
        f = json.loads(r.features)
        X.append([float(f.get(c, 0.0) or 0.0) for c in cols])
        y.append(int(r.label))
    # 时间切分：前 70% 训练，后 30% 验证（防前视）
    n_train = int(len(X) * 0.7)
    X_train, y_train = X[:n_train], y[:n_train]
    X_val, y_val = X[n_train:], y[n_train:]

    from sklearn.linear_model import LogisticRegression

    model = LogisticRegression(max_iter=1000, C=0.1)
    model.fit(X_train, y_train)
    train_acc = model.score(X_train, y_train)
    val_acc = model.score(X_val, y_val)
    print(f"train acc={train_acc:.4f} val acc={val_acc:.4f}")

    # 校准桶（训练集预测 vs 真实）
    cal = Calibrator(min_n=50)
    for x, yy in zip(X_train, y_train, strict=True):
        pred = model.predict_proba([x])[0][1]
        cal.record(float(pred), bool(yy))
    brier = cal.brier()
    ece = cal.ece()
    print(f"brier={brier:.4f} ece={ece:.4f}")

    artifact = {
        "model": "logistic",
        "features": cols,
        "coef": [float(c) for c in model.coef_[0]],
        "intercept": float(model.intercept_[0]),
        "calibration": cal.rows(),
        "train_acc": train_acc,
        "val_acc": val_acc,
        "brier": brier,
        "ece": ece,
        "n_train": len(X_train),
        "n_val": len(X_val),
    }
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(f"artifact written: {out_path}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/backtest.db")
    p.add_argument("--out", default="artifacts/logreg_v1.json")
    args = p.parse_args()
    return asyncio.run(train(args.db, args.out))


if __name__ == "__main__":
    sys.exit(main())
