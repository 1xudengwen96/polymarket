"""回测 CLI：labels / collect / run / train。

用法:
  python -m pm5hft.backtest labels  --db data/backtest.db
  python -m pm5hft.backtest collect --db data/backtest.db [--max-windows N] [--split train]
  python -m pm5hft.backtest train   --db data/backtest.db --out artifacts/logreg_v1.json
  python -m pm5hft.backtest run     --db data/backtest.db [--model artifacts/logreg_v1.json]
                                    [--max-windows N] [--report docs/backtest-report.md]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from pm5hft.config import Config  # noqa: E402
from pm5hft.logging_setup import setup_logging  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pm5hft.backtest")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("labels", "collect", "run"):
        sp = sub.add_parser(name)
        sp.add_argument("--db", default="data/backtest.db")
        sp.add_argument("--max-windows", type=int, default=0)
        if name == "collect":
            sp.add_argument("--split", default="train")
            sp.add_argument("--offsets", default="60,120,180,240,270,285,290",
                            help="逗号分隔的窗口内采样偏移（秒）")
        if name == "run":
            sp.add_argument("--model", default=None)
            sp.add_argument("--report", default="docs/backtest-report.md")
            sp.add_argument("--loose", action="store_true",
                            help="放宽入场阈值（gross 3%/net 1%/模型误差 1.5%），用于冒烟验证")
            sp.add_argument("--so", action="append", default=[],
                            help="策略参数覆盖 key=value（可重复），如 entry.entry_max_remaining_s=60")
    tp = sub.add_parser("train")
    tp.add_argument("--db", default="data/backtest.db")
    tp.add_argument("--out", default="artifacts/logreg_v1.json")
    return p


def main() -> int:
    args = build_parser().parse_args()
    setup_logging("INFO")

    if args.cmd == "labels":
        from pm5hft.backtest.data import build_twap_labels, sensitivity_analysis

        stats = build_twap_labels(args.db)
        sens = sensitivity_analysis(args.db)
        print(json.dumps(stats, indent=2))
        print(json.dumps(sens, indent=2))
        return 0

    if args.cmd == "train":
        from pm5hft.backtest.train import train

        return asyncio.run(train(args.db, args.out))

    config = Config()
    if getattr(args, "loose", False):
        config.strategy["entry"]["entry_min_gross_edge"] = 0.03
        config.strategy["entry"]["entry_min_net_edge"] = 0.01
        config.probability["model_err_cold_start"] = 0.015
        config.probability["risk_buffer_global"] = 0.003
    # --so key=value 策略覆盖
    for so in getattr(args, "so", []) or []:
        if "=" not in so:
            print(f"invalid --so {so!r} (expected key=value)", file=sys.stderr)
            return 2
        key, val = so.split("=", 1)
        node = config.strategy
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        # 数值自动转换
        try:
            if "." in val:
                node[parts[-1]] = float(val)
            else:
                node[parts[-1]] = int(val)
        except ValueError:
            node[parts[-1]] = val
    mode = "collect" if args.cmd == "collect" else "backtest"

    from pm5hft.backtest.runner import BacktestRunner, TrainedLogisticModel
    from pm5hft.probability.calibration import Calibrator

    model = None
    calibrator = None
    if args.cmd == "run" and args.model:
        art = json.loads(Path(args.model).read_text(encoding="utf-8"))
        model = TrainedLogisticModel(art["features"], art["coef"], art["intercept"])
        calibrator = Calibrator(min_n=200, cold_start_shrink=0.85)
        calibrator.load_rows(art.get("calibration") or [])
        print(f"model loaded: val_acc={art.get('val_acc')} brier={art.get('brier')}")

    async def _run() -> int:
        from pm5hft.backtest.data import label_stats

        runner = BacktestRunner(config, mode=mode)
        offsets = None
        if args.cmd == "collect":
            offsets = tuple(int(x) for x in args.offsets.split(",") if x.strip())
        metrics = await runner.run(
            db_path=args.db,
            max_windows=args.max_windows,
            sample_offsets=offsets or (60, 120, 180, 240),
            model=model,
            calibrator=calibrator,
            collect_split=getattr(args, "split", "train"),
            report_path=getattr(args, "report", None) if args.cmd == "run" else None,
            label_stats=label_stats(args.db) if args.cmd == "run" else None,
        )
        print(json.dumps(metrics, indent=2))
        return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
