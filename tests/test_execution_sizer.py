"""Tests for the volatility-target sizer (fast execution path).

Pins the sizing contract: vol-target × conviction, the optional fractional-Kelly
magnitude cap, the rebalance dead-band, delta-from-current sizing, and the
flatten-on-zero-strength behaviour. Offline, deterministic, no LLM.
"""
from __future__ import annotations

import pytest

from tradingagents.execution import (
    AccountState,
    MarketSnapshot,
    PositionState,
    RiskBudget,
    Side,
    Signal,
    SizerConfig,
    VolTargetSizer,
)

NOW = 1_700_000_000.0


def snap(symbol="SPY", price=100.0, vol=0.30):
    return MarketSnapshot(symbol, price, price - 0.01, price + 0.01, NOW, price, vol)


def account(equity=100_000.0, *, positions=()):
    return AccountState(NOW, equity, equity, tuple(positions))


def signal(symbol="SPY", strength=1.0, price=100.0, vol=0.30, edge=None, variance=None):
    return Signal(symbol, strength, snap(symbol, price, vol), edge, variance)


@pytest.mark.unit
class TestVolTarget:
    def test_full_conviction_hits_vol_target_weight(self):
        # vol_target 0.30, σ 0.30 -> weight 1.0 -> 100k notional -> 1000 sh @100
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.30))
        order = s.size(account(), signal(strength=1.0, vol=0.30))
        assert order is not None
        assert order.side is Side.BUY
        assert order.quantity == pytest.approx(1000.0)

    def test_conviction_scales_linearly(self):
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.30))
        order = s.size(account(), signal(strength=0.5, vol=0.30))
        assert order.quantity == pytest.approx(500.0)

    def test_higher_vol_shrinks_size(self):
        # σ 0.60 -> weight 0.30/0.60 = 0.5 -> 500 sh
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.30))
        order = s.size(account(), signal(strength=1.0, vol=0.60))
        assert order.quantity == pytest.approx(500.0)

    def test_negative_strength_sells(self):
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.30))
        order = s.size(account(), signal(strength=-1.0, vol=0.30))
        assert order.side is Side.SELL
        assert order.quantity == pytest.approx(1000.0)

    def test_vol_floor_bounds_tiny_vol(self):
        # σ below floor 0.05 is treated as 0.05 -> weight 0.30/0.05 = 6, capped
        # by max_weight below.
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.30, vol_floor=0.05, max_weight=2.0))
        w = s.target_weight(signal(strength=1.0, vol=0.001))
        assert w == pytest.approx(2.0)


@pytest.mark.unit
class TestDeltaSizing:
    def test_sizes_delta_from_current_position(self):
        # target 1000 sh; already hold 400 -> buy 600
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.30))
        pos = PositionState("SPY", 400.0, 100.0, 100.0)
        order = s.size(account(positions=[pos]), signal(strength=1.0, vol=0.30))
        assert order.side is Side.BUY
        assert order.quantity == pytest.approx(600.0)

    def test_zero_strength_flattens(self):
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.30, min_order_notional=1.0))
        pos = PositionState("SPY", 300.0, 100.0, 100.0)
        order = s.size(account(positions=[pos]), signal(strength=0.0))
        assert order.side is Side.SELL
        assert order.quantity == pytest.approx(300.0)

    def test_reducing_target_sells_down(self):
        # target weight 0.5 -> 500 sh; hold 800 -> sell 300
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.30))
        pos = PositionState("SPY", 800.0, 100.0, 100.0)
        order = s.size(account(positions=[pos]), signal(strength=0.5, vol=0.30))
        assert order.side is Side.SELL
        assert order.quantity == pytest.approx(300.0)


@pytest.mark.unit
class TestDeadBand:
    def test_within_rebalance_band_returns_none(self):
        # target 1000 sh; hold 999 -> 100 notional delta < 0.5% band (500) -> None
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.30, rebalance_band=0.005))
        pos = PositionState("SPY", 999.0, 100.0, 100.0)
        assert s.size(account(positions=[pos]), signal(strength=1.0, vol=0.30)) is None

    def test_below_min_order_notional_returns_none(self):
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.30, min_order_notional=100_000.0))
        assert s.size(account(), signal(strength=0.001, vol=0.30)) is None


@pytest.mark.unit
class TestKellyCap:
    def test_kelly_caps_magnitude_when_edge_small(self):
        # vol-target would give weight 1.0 (1000 sh); Kelly cap:
        # 0.25 * edge(0.01) / var(0.01) = 0.25 weight -> 250 sh
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.30, kelly_fraction=0.25))
        order = s.size(account(), signal(strength=1.0, vol=0.30, edge=0.01, variance=0.01))
        assert order.quantity == pytest.approx(250.0)

    def test_kelly_not_applied_without_edge(self):
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.30, kelly_fraction=0.25))
        order = s.size(account(), signal(strength=1.0, vol=0.30, edge=None, variance=None))
        assert order.quantity == pytest.approx(1000.0)

    def test_kelly_does_not_increase_size(self):
        # generous Kelly must not push above the vol-target size
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.30, kelly_fraction=1.0, max_weight=10))
        order = s.size(account(), signal(strength=0.5, vol=0.30, edge=1.0, variance=0.01))
        assert order.quantity == pytest.approx(500.0)


@pytest.mark.unit
class TestSafetyAndEdges:
    def test_max_weight_safety_cap(self):
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=1.0, vol_floor=0.05, max_weight=0.5))
        # 1.0/0.05 = 20 weight, capped to 0.5 -> 500 sh
        order = s.size(account(), signal(strength=1.0, vol=0.01))
        assert order.quantity == pytest.approx(500.0)

    def test_zero_price_returns_none(self):
        s = VolTargetSizer()
        assert s.size(account(), signal(price=0.0)) is None

    def test_lot_rounding_floors(self):
        # target 0.30/0.30 * 0.333 weight -> ~333.3 sh -> floor to lot 10 -> 330
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.30, lot_size=10.0))
        order = s.size(account(), signal(strength=0.3333, vol=0.30))
        assert order.quantity == pytest.approx(330.0)

    def test_budget_vol_target_override(self):
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.10))
        order = s.size(account(), signal(strength=1.0, vol=0.30), RiskBudget(vol_target_annual=0.60))
        # 0.60/0.30 = 2.0 weight but max_weight default 1.0 -> 1000 sh
        assert order.quantity == pytest.approx(1000.0)


@pytest.mark.unit
class TestComposesWithBlocker:
    def test_sizer_proposal_is_capped_by_blocker(self):
        from tradingagents.execution import BlockerConfig, InteractiveBlocker

        # Sizer wants 1000 sh (weight 1.0 = 100k); blocker concentration cap 25%.
        s = VolTargetSizer(SizerConfig(base_vol_target_annual=0.30))
        order = s.size(account(), signal(strength=1.0, vol=0.30))
        assert order.quantity == pytest.approx(1000.0)

        b = InteractiveBlocker(
            BlockerConfig(max_symbol_weight=0.25, max_gross_exposure=100,
                          max_net_exposure=100, vol_target_annual=100, kelly_weight_cap=100)
        )
        decision = b.check_order(account(), order)
        assert decision.allow and decision.capped
        assert decision.approved_quantity == pytest.approx(250.0)  # 25% of 100k / 100
