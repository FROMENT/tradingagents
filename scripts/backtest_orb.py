#!/usr/bin/env python3
"""Run the cost-inclusive intraday backtest of the ORB signal on a CSV.

This is the P2 gate made runnable on real data (docs/intraday_architecture.md
§7). Provide a CSV of intraday bars (schema in
``tradingagents/execution/data.py``) and it prints net-of-cost performance.

    python scripts/backtest_orb.py bars.csv --equity 100000 --slippage-bps 1

Pure-stdlib + the execution package; no vendor SDK. A real verdict needs real
intraday data — export it from your data source into the CSV schema.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the script runnable from anywhere without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tradingagents.execution import (  # noqa: E402  (after sys.path bootstrap)
    BacktestConfig,
    Backtester,
    BlockerConfig,
    CostModel,
    InteractiveBlocker,
    OpeningRangeBreakout,
    ORBConfig,
    SizerConfig,
    VolTargetSizer,
    build_frames,
    load_bars_csv,
)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv", help="intraday bars CSV")
    p.add_argument("--equity", type=float, default=100_000.0)
    p.add_argument("--opening-bars", type=int, default=1)
    p.add_argument("--vol-target", type=float, default=0.30)
    p.add_argument("--commission-per-share", type=float, default=0.0035)
    p.add_argument("--commission-min", type=float, default=1.0)
    p.add_argument("--slippage-bps", type=float, default=1.0)
    p.add_argument("--default-spread-bps", type=float, default=2.0)
    p.add_argument("--long-only", action="store_true", help="disable short entries")
    args = p.parse_args()

    bars = load_bars_csv(args.csv)
    orb = OpeningRangeBreakout(ORBConfig(opening_bars=args.opening_bars, allow_short=not args.long_only))
    frames = build_frames(bars, orb, default_spread_bps=args.default_spread_bps)

    bt = Backtester(
        VolTargetSizer(SizerConfig(base_vol_target_annual=args.vol_target)),
        InteractiveBlocker(BlockerConfig()),
        CostModel(
            commission_per_share=args.commission_per_share,
            commission_min=args.commission_min,
            slippage_bps=args.slippage_bps,
        ),
        BacktestConfig(initial_equity=args.equity),
    )
    m = bt.run(frames).metrics

    print(f"\nORB backtest — {args.csv}")
    print(f"  bars={len(bars)} frames={len(frames)}")
    print(f"  net return      : {m['net_return']:+.2%}")
    print(f"  gross return    : {m['gross_return']:+.2%}")
    print(f"  cost drag       : {m['cost_drag_return']:.2%}  "
          f"(spread+slippage ${m['cost_breakdown']['spread_slippage']:.0f}, "
          f"commission ${m['cost_breakdown']['commission']:.0f})")
    print(f"  Sharpe (ann.)   : {m['sharpe']:.2f}")
    print(f"  max drawdown    : {m['max_drawdown']:.2%}")
    print(f"  turnover        : {m['turnover']:.1f}x")
    print(f"  fills / halts   : {m['num_fills']} / {m['halts']}")
    verdict = "PASS (net > 0)" if m["net_return"] > 0 else "FAIL (net <= 0)"
    print(f"  gate            : {verdict}\n")


if __name__ == "__main__":
    main()
