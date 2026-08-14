"""Tests for the deterministic interactive blocker (fast execution path).

The blocker is the absolute-priority guardrail: it caps or denies orders
pre-trade and forces flatten/halt via the monitor. These tests pin the contract
— every rule fires at least once, sizing caps resize rather than reject, and
risk-reducing orders are never blocked. All offline, no LLM, no wall clock.
"""
from __future__ import annotations

import pytest

from tradingagents.execution import (
    AccountState,
    BlockerConfig,
    BlockReason,
    ForcedAction,
    InteractiveBlocker,
    MarketSnapshot,
    PositionState,
    ProposedOrder,
    RiskBudget,
    Side,
)

NOW = 1_700_000_000.0


def snap(symbol="SPY", price=100.0, *, bid=None, ask=None, ts=NOW, prev_close=None, vol=0.20):
    bid = price - 0.01 if bid is None else bid
    ask = price + 0.01 if ask is None else ask
    prev_close = price if prev_close is None else prev_close
    return MarketSnapshot(symbol, price, bid, ask, ts, prev_close, vol)


def account(equity=100_000.0, *, day_start=None, positions=(), ts=NOW):
    return AccountState(ts, equity, equity if day_start is None else day_start, tuple(positions))


def order(symbol="SPY", side=Side.BUY, qty=100.0, **snap_kw):
    return ProposedOrder(symbol, side, qty, snap(symbol=symbol, **snap_kw))


@pytest.mark.unit
class TestPreTradeHappyPath:
    def test_clean_order_passes_unchanged(self):
        b = InteractiveBlocker()
        d = b.check_order(account(), order(qty=100.0))
        assert d.allow and not d.capped
        assert d.approved_quantity == 100.0
        assert d.reasons == ()


@pytest.mark.unit
class TestMarketQualityGuards:
    def test_stale_price_denied(self):
        b = InteractiveBlocker(BlockerConfig(max_price_staleness_s=5.0))
        d = b.check_order(account(ts=NOW), order(ts=NOW - 10))
        assert not d.allow
        assert BlockReason.STALE_PRICE.value in d.reasons

    def test_wide_spread_denied(self):
        b = InteractiveBlocker(BlockerConfig(max_spread_bps=25.0))
        # 100.00 mid, 1.00 wide spread -> ~100 bps
        d = b.check_order(account(), order(bid=99.5, ask=100.5))
        assert not d.allow
        assert BlockReason.WIDE_SPREAD.value in d.reasons

    def test_gap_denied(self):
        b = InteractiveBlocker(BlockerConfig(max_gap_pct=0.05))
        d = b.check_order(account(), order(price=110.0, prev_close=100.0))  # +10%
        assert not d.allow
        assert BlockReason.GAP.value in d.reasons


@pytest.mark.unit
class TestAccountGates:
    def test_below_min_equity_denied(self):
        b = InteractiveBlocker(BlockerConfig(min_equity=2000.0))
        d = b.check_order(account(equity=1500.0), order(qty=1.0))
        assert not d.allow
        assert BlockReason.MIN_EQUITY.value in d.reasons

    def test_trading_disabled_by_budget(self):
        b = InteractiveBlocker()
        d = b.check_order(account(), order(), RiskBudget(trade_enabled=False))
        assert not d.allow
        assert BlockReason.TRADING_DISABLED.value in d.reasons

    def test_symbol_not_in_watchlist(self):
        b = InteractiveBlocker()
        budget = RiskBudget(watchlist=frozenset({"QQQ"}))
        d = b.check_order(account(), order(symbol="SPY"), budget)
        assert not d.allow
        assert BlockReason.NOT_IN_UNIVERSE.value in d.reasons

    def test_watchlist_none_allows_any_symbol(self):
        b = InteractiveBlocker()
        d = b.check_order(account(), order(symbol="SPY"), RiskBudget(watchlist=None))
        assert d.allow


@pytest.mark.unit
class TestRateLimit:
    def test_rate_limit_after_n_accepted(self):
        b = InteractiveBlocker(BlockerConfig(order_rate_limit=3, order_rate_window_s=60.0))
        acct = account()
        for _ in range(3):
            assert b.check_order(acct, order(qty=1.0)).allow
        blocked = b.check_order(acct, order(qty=1.0))
        assert not blocked.allow
        assert BlockReason.RATE_LIMIT.value in blocked.reasons

    def test_window_expiry_frees_slots(self):
        b = InteractiveBlocker(BlockerConfig(order_rate_limit=1, order_rate_window_s=60.0))
        assert b.check_order(account(ts=NOW), order(qty=1.0, ts=NOW)).allow
        assert not b.check_order(account(ts=NOW + 1), order(qty=1.0, ts=NOW + 1)).allow
        # after the window, the earlier acceptance is pruned (fresh quote too)
        assert b.check_order(account(ts=NOW + 61), order(qty=1.0, ts=NOW + 61)).allow

    def test_denied_order_does_not_consume_a_slot(self):
        b = InteractiveBlocker(BlockerConfig(order_rate_limit=1))
        # a denied (stale) order must not count against the rate limit
        b.check_order(account(), order(ts=NOW - 999))
        assert b.check_order(account(), order(qty=1.0)).allow


@pytest.mark.unit
class TestSizingCaps:
    def test_gross_exposure_caps_and_resizes(self):
        # equity 100k, gross cap 1.0 -> max 100k notional; already 60k gross.
        cfg = BlockerConfig(max_gross_exposure=1.0, max_net_exposure=10, max_symbol_weight=10,
                            vol_target_annual=100, kelly_weight_cap=100)
        b = InteractiveBlocker(cfg)
        pos = PositionState("QQQ", 600.0, 100.0, 100.0)  # 60k gross
        # want to buy 500 SPY @100 = 50k; only 40k headroom -> approved 400
        d = b.check_order(account(positions=[pos]), order(symbol="SPY", qty=500.0))
        assert d.allow and d.capped
        assert d.approved_quantity == pytest.approx(400.0)
        assert BlockReason.GROSS_EXPOSURE.value in d.reasons

    def test_concentration_caps(self):
        cfg = BlockerConfig(max_symbol_weight=0.25, max_gross_exposure=100,
                            max_net_exposure=100, vol_target_annual=100, kelly_weight_cap=100)
        b = InteractiveBlocker(cfg)
        # 25% of 100k = 25k in SPY; hold 10k already -> 15k headroom -> 150 sh
        pos = PositionState("SPY", 100.0, 100.0, 100.0)  # 10k
        d = b.check_order(account(positions=[pos]), order(symbol="SPY", qty=500.0))
        assert d.approved_quantity == pytest.approx(150.0)
        assert BlockReason.CONCENTRATION.value in d.reasons

    def test_vol_target_caps(self):
        # vol_target 0.30, asset vol 0.60 -> weight cap 0.5 -> 50k -> 500 sh
        cfg = BlockerConfig(vol_target_annual=0.30, max_symbol_weight=10,
                            max_gross_exposure=100, max_net_exposure=100, kelly_weight_cap=100)
        b = InteractiveBlocker(cfg)
        d = b.check_order(account(), order(symbol="SPY", qty=900.0, vol=0.60))
        assert d.approved_quantity == pytest.approx(500.0)
        assert BlockReason.VOL_TARGET.value in d.reasons

    def test_kelly_cap_is_hard_ceiling(self):
        # low vol would allow a huge vol-target weight; kelly caps it at 0.5
        cfg = BlockerConfig(kelly_weight_cap=0.50, vol_target_annual=1.0, vol_floor=0.05,
                            max_symbol_weight=10, max_gross_exposure=100, max_net_exposure=100)
        b = InteractiveBlocker(cfg)
        d = b.check_order(account(), order(symbol="SPY", qty=10_000.0, vol=0.05))
        assert d.approved_quantity == pytest.approx(500.0)  # 0.5 * 100k / 100
        assert BlockReason.KELLY_CAP.value in d.reasons

    def test_zero_headroom_denies(self):
        cfg = BlockerConfig(max_symbol_weight=0.10, max_gross_exposure=100,
                            max_net_exposure=100, vol_target_annual=100, kelly_weight_cap=100)
        b = InteractiveBlocker(cfg)
        pos = PositionState("SPY", 100.0, 100.0, 100.0)  # 10k == cap already
        d = b.check_order(account(positions=[pos]), order(symbol="SPY", qty=100.0))
        assert not d.allow
        assert BlockReason.ZERO_AFTER_CAPS.value in d.reasons


@pytest.mark.unit
class TestReducingOrdersBypass:
    def test_reducing_order_bypasses_exposure_caps(self):
        cfg = BlockerConfig(max_gross_exposure=0.01)  # everything would be capped
        b = InteractiveBlocker(cfg)
        pos = PositionState("SPY", 500.0, 100.0, 100.0)  # long 500
        d = b.check_order(account(positions=[pos]), order(symbol="SPY", side=Side.SELL, qty=200.0))
        assert d.allow and not d.capped
        assert d.approved_quantity == 200.0

    def test_reducing_order_allowed_even_when_halted(self):
        b = InteractiveBlocker()
        b._engage_halt(BlockReason.DAILY_LOSS.value)
        pos = PositionState("SPY", 500.0, 100.0, 100.0)
        d = b.check_order(account(positions=[pos]), order(symbol="SPY", side=Side.SELL, qty=500.0))
        assert d.allow

    def test_overshoot_is_a_flip_not_a_reduction(self):
        cfg = BlockerConfig(max_gross_exposure=0.01)
        b = InteractiveBlocker(cfg)
        pos = PositionState("SPY", 100.0, 100.0, 100.0)  # long 100
        # sell 300 -> flip to short 200: treated as increasing, hits the caps
        d = b.check_order(account(positions=[pos]), order(symbol="SPY", side=Side.SELL, qty=300.0))
        assert not d.allow


@pytest.mark.unit
class TestMonitorAndHalt:
    def test_daily_loss_flattens_and_halts(self):
        b = InteractiveBlocker(BlockerConfig(max_daily_loss_pct=-0.03))
        acct = account(equity=96_000.0, day_start=100_000.0)  # −4%
        m = b.monitor(acct)
        assert m.halt and m.forced_action is ForcedAction.FLATTEN_ALL
        assert BlockReason.DAILY_LOSS.value in m.reasons
        assert b.halted

    def test_halt_latches_and_blocks_new_orders(self):
        b = InteractiveBlocker(BlockerConfig(max_daily_loss_pct=-0.03))
        b.monitor(account(equity=96_000.0, day_start=100_000.0))
        d = b.check_order(account(), order())
        assert not d.allow and BlockReason.HALTED.value in d.reasons
        # a later monitor tick stays halted but does not re-issue a flatten
        m = b.monitor(account())
        assert m.halt and m.forced_action is ForcedAction.NONE

    def test_reset_session_clears_halt(self):
        b = InteractiveBlocker(BlockerConfig(max_daily_loss_pct=-0.03))
        b.monitor(account(equity=96_000.0, day_start=100_000.0))
        assert b.halted
        b.reset_session()
        assert not b.halted
        assert b.check_order(account(), order()).allow

    def test_per_position_stop_flags_symbol(self):
        b = InteractiveBlocker(BlockerConfig(per_position_stop_pct=-0.02))
        pos = PositionState("SPY", 100.0, 100.0, 97.0)  # −3% on a long
        m = b.monitor(account(positions=[pos]))
        assert not m.halt
        assert m.forced_action is ForcedAction.FLATTEN_SYMBOL
        assert m.flatten_symbols == ("SPY",)

    def test_short_position_stop_is_sign_aware(self):
        b = InteractiveBlocker(BlockerConfig(per_position_stop_pct=-0.02))
        # short at 100, price rose to 103 -> −3% for a short
        pos = PositionState("SPY", -100.0, 100.0, 103.0)
        m = b.monitor(account(positions=[pos]))
        assert m.flatten_symbols == ("SPY",)


@pytest.mark.unit
class TestKillSwitch:
    def test_kill_switch_denies_and_halts(self, tmp_path):
        kill = tmp_path / "KILL"
        kill.write_text("stop")
        b = InteractiveBlocker(BlockerConfig(kill_switch_file=str(kill)))
        d = b.check_order(account(), order())
        assert not d.allow and BlockReason.KILL_SWITCH.value in d.reasons
        m = b.monitor(account())
        assert m.halt and m.forced_action is ForcedAction.FLATTEN_ALL


@pytest.mark.unit
class TestBudgetOverrides:
    def test_budget_vol_target_override(self):
        cfg = BlockerConfig(vol_target_annual=0.10, max_symbol_weight=10,
                            max_gross_exposure=100, max_net_exposure=100, kelly_weight_cap=100)
        b = InteractiveBlocker(cfg)
        # override raises vol target 0.10 -> 0.40; asset vol 0.40 -> weight 1.0 -> 1000 sh
        budget = RiskBudget(vol_target_annual=0.40)
        d = b.check_order(account(), order(symbol="SPY", qty=5000.0, vol=0.40), budget)
        assert d.approved_quantity == pytest.approx(1000.0)


@pytest.mark.unit
class TestSchemaProperties:
    def test_spread_bps(self):
        assert snap(price=100.0, bid=99.9, ask=100.1).spread_bps == pytest.approx(20.0, rel=1e-3)

    def test_gap_pct_sign(self):
        assert snap(price=105.0, prev_close=100.0).gap_pct == pytest.approx(0.05)

    def test_decision_capped_flag(self):
        cfg = BlockerConfig(max_gross_exposure=1.0, max_net_exposure=10, max_symbol_weight=10,
                            vol_target_annual=100, kelly_weight_cap=100)
        b = InteractiveBlocker(cfg)
        pos = PositionState("QQQ", 900.0, 100.0, 100.0)  # 90k gross
        d = b.check_order(account(positions=[pos]), order(symbol="SPY", qty=500.0))
        assert d.capped and d.approved_quantity == pytest.approx(100.0)
