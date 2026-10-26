"""Tests for the ORB signal, the CSV loader, and the end-to-end wiring.

Covers the breakout logic (long/short/no-trade/opposite-stop), a stdlib CSV
round-trip, frame building, and a synthetic end-to-end run through the
backtester. Offline, deterministic, pure-stdlib.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from tradingagents.execution import (
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
from tradingagents.execution.data import BacktestBar


@dataclass
class B:
    """Minimal bar for ORB unit tests (high/low/session)."""

    high: float
    low: float
    session: int = 0


@pytest.mark.unit
class TestORBLogic:
    def test_breakout_up_goes_long(self):
        orb = OpeningRangeBreakout(ORBConfig(opening_bars=1))
        # OR = [99, 101]; next bar breaks above -> long, held to close
        bars = [B(101, 99), B(102, 100), B(103, 101)]
        assert orb.session_strengths(bars) == [0.0, 1.0, 1.0]

    def test_breakout_down_goes_short(self):
        orb = OpeningRangeBreakout(ORBConfig(opening_bars=1))
        bars = [B(101, 99), B(100, 98), B(99, 97)]
        assert orb.session_strengths(bars) == [0.0, -1.0, -1.0]

    def test_no_breakout_stays_flat(self):
        orb = OpeningRangeBreakout(ORBConfig(opening_bars=1))
        bars = [B(101, 99), B(100.5, 99.5), B(100.8, 99.2)]
        assert orb.session_strengths(bars) == [0.0, 0.0, 0.0]

    def test_opposite_stop_exits(self):
        orb = OpeningRangeBreakout(ORBConfig(opening_bars=1, stop_at_opposite=True))
        # long on bar2, then bar3 low touches OR low (99) -> stop to flat, stays flat
        bars = [B(101, 99), B(102, 100), B(101, 98), B(104, 102)]
        assert orb.session_strengths(bars) == [0.0, 1.0, 0.0, 0.0]

    def test_long_only_ignores_downside(self):
        orb = OpeningRangeBreakout(ORBConfig(opening_bars=1, allow_short=False))
        bars = [B(101, 99), B(100, 98), B(99, 97)]
        assert orb.session_strengths(bars) == [0.0, 0.0, 0.0]

    def test_opening_bars_window(self):
        orb = OpeningRangeBreakout(ORBConfig(opening_bars=2))
        # OR formed over first 2 bars: high 102, low 98; bar3 breaks above
        bars = [B(101, 99), B(102, 98), B(103, 100)]
        assert orb.session_strengths(bars) == [0.0, 0.0, 1.0]

    def test_too_few_bars_all_flat(self):
        orb = OpeningRangeBreakout(ORBConfig(opening_bars=2))
        assert orb.session_strengths([B(101, 99)]) == [0.0]


CSV = """ts,session,symbol,high,low,close,prev_close,vol,is_close
0,0,SPY,101,99,100,100,0.30,0
1,0,SPY,102,100,101.5,100,0.30,0
2,0,SPY,103,101,102.5,100,0.30,1
"""


@pytest.mark.unit
class TestCsvLoader:
    def test_round_trip(self, tmp_path):
        p = tmp_path / "bars.csv"
        p.write_text(CSV)
        bars = load_bars_csv(p)
        assert len(bars) == 3
        assert bars[0] == BacktestBar(0.0, 0, "SPY", 101, 99, 100, 100, 0.30, False)
        assert bars[2].is_close is True

    def test_spread_bps_optional(self, tmp_path):
        p = tmp_path / "bars.csv"
        p.write_text(CSV.replace("vol,is_close", "vol,is_close,spread_bps").replace(
            "0.30,0\n", "0.30,0,5\n", 1))
        bars = load_bars_csv(p)
        assert bars[0].spread_bps == 5.0
        assert bars[1].spread_bps is None


@pytest.mark.unit
class TestBuildFrames:
    def test_frames_attach_orb_signals(self, tmp_path):
        p = tmp_path / "bars.csv"
        p.write_text(CSV)
        bars = load_bars_csv(p)
        frames = build_frames(bars, OpeningRangeBreakout(ORBConfig(opening_bars=1)))
        assert len(frames) == 3
        assert [f.signals[0].strength for f in frames] == [0.0, 1.0, 1.0]
        assert frames[-1].is_close is True
        assert frames[0].snapshots[0].symbol == "SPY"

    def test_multi_symbol_rejected(self, tmp_path):
        p = tmp_path / "bars.csv"
        p.write_text(CSV + "3,0,QQQ,50,49,49.5,50,0.30,1\n")
        bars = load_bars_csv(p)
        with pytest.raises(ValueError, match="one symbol"):
            build_frames(bars, OpeningRangeBreakout())


@pytest.mark.unit
class TestEndToEnd:
    def test_orb_uptrend_is_net_profitable_low_cost(self, tmp_path):
        # Opening range [99,101], then a clean breakout up to 106 held to close.
        rows = ["ts,session,symbol,high,low,close,prev_close,vol,is_close"]
        prices = [(101, 99, 100), (103, 100, 102), (105, 103, 104), (106, 104, 105.9)]
        for i, (hi, lo, cl) in enumerate(prices):
            rows.append(f"{i},0,SPY,{hi},{lo},{cl},100,0.30,{1 if i == len(prices)-1 else 0}")
        p = tmp_path / "bars.csv"
        p.write_text("\n".join(rows) + "\n")

        frames = build_frames(load_bars_csv(p), OpeningRangeBreakout(ORBConfig(opening_bars=1)))
        bt = Backtester(
            VolTargetSizer(SizerConfig(base_vol_target_annual=0.30)),
            InteractiveBlocker(BlockerConfig()),
            CostModel(commission_per_share=0.0, commission_min=0.0, slippage_bps=0.0),
            BacktestConfig(initial_equity=100_000.0),
        )
        res = bt.run(frames)
        assert res.metrics["net_return"] > 0
        assert res.metrics["num_fills"] >= 2  # entry + flat-by-close
