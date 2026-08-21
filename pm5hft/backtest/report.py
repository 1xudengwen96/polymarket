"""回测指标 → Markdown 报告（docs/08 §4-5）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path


def _fmt(v, nd=4):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        if v == float("inf"):
            return "inf"
        return f"{v:.{nd}f}"
    return str(v)


def write_markdown_report(path: str, metrics: dict, calibrator=None) -> None:
    m = metrics
    lines = [
        "# Backtest Report",
        "",
        f"生成时间: {datetime.now(UTC).isoformat()}",
        "",
        "## 核心指标",
        "",
        "| 指标 | 值 |",
        "|------|-----|",
        f"| 参与交易窗口数 | {m.get('n_windows_traded', 0)} |",
        f"| 总 PnL (USDC) | {_fmt(m.get('total_pnl'))} |",
        f"| 期末 Equity | {_fmt(m.get('equity_final'), 2)} |",
        f"| Win Rate | {_fmt((m.get('win_rate') or 0) * 100, 2)}% |",
        f"| Avg Win | {_fmt(m.get('avg_win'))} |",
        f"| Avg Loss | {_fmt(m.get('avg_loss'))} |",
        f"| Expectancy | {_fmt(m.get('expectancy'), 6)} |",
        f"| Profit Factor | {_fmt(m.get('profit_factor'))} |",
        f"| Max Drawdown | {_fmt((m.get('max_drawdown') or 0) * 100, 2)}% |",
        f"| Sharpe (per-window, ×√288) | {_fmt(m.get('sharpe'), 3)} |",
        f"| Sortino | {_fmt(m.get('sortino'), 3)} |",
        "",
    ]
    if m.get("label_stats"):
        ls = m["label_stats"]
        lines += [
            "## TWAP 标签对账",
            "",
            f"- 有 gamma 标签窗口: {ls.get('n', 0)}",
            f"- 可重建 TWAP 标签: {ls.get('n_labeled', 0)}",
            f"- 不一致数: {ls.get('n_mismatch', 0)}（原始不一致率 {_fmt((ls.get('mismatch_rate') or 0) * 100, 2)}%）",
            f"- 平局附近窗口（|margin|<1.5bps，已排除交易）: {ls.get('near_tie_windows', 0)}",
            f"- 过滤后不一致数: {ls.get('filtered_mismatch', 0)}"
            f"（过滤后不一致率 {_fmt((ls.get('filtered_rate') or 0) * 100, 2)}%，门槛 <2%）",
            "",
        ]
    if calibrator is not None:
        lines += ["## 概率校准分桶（偏好方向置信度）", "", "| 桶 | n | 实际胜率 |", "|----|---|---------|"]
        for r in calibrator.rows():
            lo, hi = r["bucket_low"], r["bucket_high"]
            lines.append(f"| {lo}-{hi} | {r['n']} | {_fmt(r['actual_rate'], 4) if r['actual_rate'] else 'n/a'} |")
        brier = calibrator.brier()
        ece = calibrator.ece()
        lines += [
            "",
            f"- 全局 Brier: {_fmt(brier, 4) if brier else 'n/a'}",
            f"- ECE: {_fmt(ece, 4) if ece else 'n/a'}",
            "",
        ]
    lines += ["## 验收门槛对照（docs/08 §5）", "",
              "- Profit Factor ≥ 1.15、Max DD ≤ 3%、Expectancy > 0、标签不一致率 < 2%", ""]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines), encoding="utf-8")
