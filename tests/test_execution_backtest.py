"""Tests for the cost-inclusive intraday backtester (the P2 gate).

Pins the engine's honesty: fills pay spread+slippage+commission (never mid),
gross and net are reported separately with a cost breakdown, intraday flat-by-
close is enforced, and the blocker's halt/flatten fire in the sim. Offline,
deterministic, pure-stdlib.
"""
from __future__ import annotations

import pytest

from tradingagents.execution import (
    BacktestConfig,
    Backtester,
    BlockerConfig,
    CostModel,
    Frame,
    InteractiveBlocker,
    MarketSnapshot,
    Side,
    Signal,
    SizerConfig,
    VolTargetSizer,
)


def snap(symbol="SPY", price=100.0, ts=0.0, vol=0.30, spread=0.02):
    half = spread / 2.0
    return MarketSnapshot(symbol, price, price - half, price + half, ts, price, vol)


def sig(strength, symbol="SPY", **kw):
    return Signal(symbol, strength, snap(symbol=symbol, **kw))


def loose_blocker(**overrides):
    cfg = {
        "max_gross_exposure": 100, "max_net_exposure": 100, "max_symbol_weight": 100,
        "vol_target_annual": 100, "kelly_weight_cap": 100, "max_spread_bps": 1e9,
        "max_gap_pct": 1e9, "per_position_stop_pct": -0.99, "order_rate_limit": 10**9,
        "max_daily_loss_pct": -0.99,
    }
    cfg.update(overrides)
    return InteractiveBlocker(BlockerConfig(**cfg))


def sizer():
    return VolTargetSizer(SizerConfig(base_vol_target_annual=0.30))


def zero_costs():
    return CostModel(commission_per_share=0.0, commission_min=0.0, slippage_bps=0.0)


# A single session: open long, price rises, flatten at close.
def trend_session(prices, session=0, strengths=None):
    frames = []
    n = len(prices)
    for i, px in enumerate(prices):
        is_close = i == n - 1
        strength = 0.0 if strengths is None else strengths[i]
        signals = (sig(strength, price=px, ts=float(i)),) if strength else ()
        frames.append(Frame(float(i), session, is_close, (snap(price=px, ts=float(i)),), signals))
    return frames


@pytest.mark.unit
class TestCostModel:
    def test_buy_pays_above_mid_sell_below(self):
        c = CostModel(slippage_bps=1.0)
        s = snap(price=100.0, spread=0.10)  # mid 100, half-spread 0.05
        buy = c.fill_price(Side.BUY, s)
        sell = c.fill_price(Side.SELL, s)
        assert buy > 100.0 and sell < 100.0
        assert buy == pytest.approx(100.0 + 0.05 + 100.0 * 1e-4)

    def test_commission_floor(self):
        c = CostModel(commission_per_share=0.0035, commission_min=1.0)
        assert c.commission(10) == 1.0          # floored
        assert c.commission(1000) == pytest.approx(3.5)


@pytest.mark.unit
class TestProfitAndCosts:
    def test_rising_trend_is_net_profitable_with_low_costs(self):
        bt = Backtester(sizer(), loose_blocker(), zero_costs(),
                        BacktestConfig(initial_equity=100_000.0))
        # open at 100 with full conviction, ride to 102, flatten at close
        res = bt.run(trend_session([100.0, 101.0, 102.0], strengths=[1.0, 0.0, 0.0]))
        assert res.metrics["net_return"] > 0
        # a BUY then a SELL (flatten) of equal size
        sides = [f.side for f in res.fills]
        assert sides[0] is Side.BUY and Side.SELL in sides
        buy = next(f for f in res.fills if f.side is Side.BUY)
        sell = next(f for f in res.fills if f.side is Side.SELL)
        assert buy.quantity == pytest.approx(sell.quantity)

    def test_costs_reduce_return_and_show_drag(self):
        low = Backtester(sizer(), loose_blocker(), zero_costs(),
                         BacktestConfig(initial_equity=100_000.0))
        high = Backtester(sizer(), loose_blocker(),
                          CostModel(commission_per_share=0.02, commission_min=1.0, slippage_bps=20.0),
                          BacktestConfig(initial_equity=100_000.0))
        frames = trend_session([100.0, 101.0, 102.0], strengths=[1.0, 0.0, 0.0])
        r_low = low.run(frames)
        r_high = high.run(frames)
        assert r_high.metrics["net_return"] < r_low.metrics["net_return"]
        assert r_high.metrics["total_cost"] > 0
        assert r_high.metrics["gross_return"] > r_high.metrics["net_return"]
        assert r_high.metrics["cost_drag_return"] > 0
        bd = r_high.metrics["cost_breakdown"]
        assert bd["spread_slippage"] > 0 and bd["commission"] > 0

    def test_no_signal_no_trades(self):
        bt = Backtester(sizer(), loose_blocker(), zero_costs(),
                        BacktestConfig(initial_equity=100_000.0))
        res = bt.run(trend_session([100.0, 101.0, 102.0], strengths=[0.0, 0.0, 0.0]))
        assert res.metrics["num_fills"] == 0
        assert res.metrics["total_cost"] == 0
        assert res.metrics["net_return"] == pytest.approx(0.0)


@pytest.mark.unit
class TestFlatByClose:
    def test_position_flattened_at_close(self):
        bt = Backtester(sizer(), loose_blocker(), zero_costs(),
                        BacktestConfig(initial_equity=100_000.0, flat_by_close=True))
        res = bt.run(trend_session([100.0, 102.0], strengths=[1.0, 0.0]))
        # equal buy and sell -> net flat by end
        bought = sum(f.quantity for f in res.fills if f.side is Side.BUY)
        sold = sum(f.quantity for f in res.fills if f.side is Side.SELL)
        assert bought == pytest.approx(sold) and bought > 0

    def test_new_session_resets_blocker(self):
        blk = loose_blocker(max_daily_loss_pct=-0.03)
        bt = Backtester(sizer(), blk, zero_costs(), BacktestConfig(initial_equity=100_000.0))
        # session 0 crashes to trigger a halt, session 1 should trade again
        s0 = [Frame(0.0, 0, False, (snap(price=100.0, ts=0.0),), (sig(1.0, price=100.0, ts=0.0),)),
              Frame(1.0, 0, True, (snap(price=93.0, ts=1.0),), ())]
        s1 = [Frame(2.0, 1, True, (snap(price=100.0, ts=2.0),), (sig(1.0, price=100.0, ts=2.0),))]
        res = bt.run(s0 + s1)
        assert res.metrics["halts"] >= 1
        # a fill happened in session 1 (ts 2.0) -> blocker was reset
        assert any(f.ts == 2.0 for f in res.fills)


@pytest.mark.unit
class TestHaltInSim:
    def test_daily_loss_flattens_and_halts(self):
        blk = loose_blocker(max_daily_loss_pct=-0.03)
        bt = Backtester(sizer(), blk, zero_costs(), BacktestConfig(initial_equity=100_000.0))
        frames = [
            Frame(0.0, 0, False, (snap(price=100.0, ts=0.0),), (sig(1.0, price=100.0, ts=0.0),)),
            Frame(1.0, 0, False, (snap(price=93.0, ts=1.0),), (sig(1.0, price=93.0, ts=1.0),)),
            Frame(2.0, 0, True, (snap(price=93.0, ts=2.0),), ()),
        ]
        res = bt.run(frames)
        assert res.metrics["halts"] >= 1
        assert blk.halted
        # no new BUY was accepted after the halt at ts 1.0
        assert not any(f.ts == 1.0 and f.side is Side.BUY for f in res.fills)


@pytest.mark.unit
class TestMetricsAndDeterminism:
    def test_metrics_keys_present(self):
        bt = Backtester(sizer(), loose_blocker(), zero_costs())
        res = bt.run(trend_session([100.0, 101.0, 102.0], strengths=[1.0, 0.0, 0.0]))
        for key in ("net_return", "gross_return", "sharpe", "max_drawdown",
                    "turnover", "total_cost", "cost_breakdown", "num_fills"):
            assert key in res.metrics

    def test_turnover_positive_when_trading(self):
        bt = Backtester(sizer(), loose_blocker(), zero_costs())
        res = bt.run(trend_session([100.0, 102.0], strengths=[1.0, 0.0]))
        assert res.metrics["turnover"] > 0

    def test_deterministic(self):
        frames = trend_session([100.0, 101.0, 102.0], strengths=[1.0, 0.0, 0.0])
        a = Backtester(sizer(), loose_blocker(), zero_costs()).run(frames)
        b = Backtester(sizer(), loose_blocker(), zero_costs()).run(frames)
        assert a.metrics["net_return"] == b.metrics["net_return"]
        assert a.metrics["num_fills"] == b.metrics["num_fills"]

    def test_max_drawdown_non_positive(self):
        bt = Backtester(sizer(), loose_blocker(), zero_costs())
        res = bt.run(trend_session([100.0, 99.0, 101.0], strengths=[1.0, 0.0, 0.0]))
        assert res.metrics["max_drawdown"] <= 0.0
