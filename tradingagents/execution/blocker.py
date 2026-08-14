"""The deterministic Interactive Blocker — absolute-priority execution guardrail.

Two entry points, both pure and side-effect-free except for the blocker's own
small session state (a halt latch and a rate-limit window):

  * ``check_order(state, order, budget)`` — pre-trade. Returns an
    :class:`OrderDecision`: deny, or allow with an ``approved_quantity`` that may
    be **capped** below the request. Risk-*reducing* orders bypass the exposure
    caps and the halt, because de-risking must never be blocked.
  * ``monitor(state)`` — continuous. Returns a :class:`MonitorDecision` that can
    order a FLATTEN and latch a halt (max daily loss, kill switch), or flag
    per-position stop breaches as a backstop to the broker's resting brackets.

No LLM, no I/O beyond an optional kill-switch file stat, no wall clock (time
comes from ``state.ts``). Once halted, the blocker stays halted until
``reset_session()`` — typically at the next session open.

Design choices recorded from the adversarial review (see
docs/intraday_architecture.md §6): decisions are returned as typed objects, not
positional tuples; the module lives in ``tradingagents/execution/`` (a pure
package), never among the LLM node factories in ``agents/risk_mgmt/``.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path

from .schemas import (
    AccountState,
    BlockerConfig,
    BlockReason,
    ForcedAction,
    MonitorDecision,
    OrderDecision,
    ProposedOrder,
    RiskBudget,
    Side,
)

# Numeric tolerance for "which cap is binding" comparisons (notional dollars).
_EPS = 1e-6


class InteractiveBlocker:
    """Pre-trade gate + continuous monitor. Absolute priority over any signal."""

    def __init__(self, config: BlockerConfig | None = None) -> None:
        self.config = config or BlockerConfig()
        self._halted = False
        self._halt_reasons: tuple[str, ...] = ()
        self._recent_orders: deque[float] = deque()  # acceptance timestamps

    # -- session lifecycle -------------------------------------------------

    def reset_session(self) -> None:
        """Clear the halt latch and rate-limit window (call at session open)."""
        self._halted = False
        self._halt_reasons = ()
        self._recent_orders.clear()

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reasons(self) -> tuple[str, ...]:
        return self._halt_reasons

    def _engage_halt(self, *reasons: str) -> None:
        self._halted = True
        # keep the first cause that latched the halt
        if not self._halt_reasons:
            self._halt_reasons = tuple(reasons)

    def _kill_switch_engaged(self) -> bool:
        path = self.config.kill_switch_file
        return bool(path) and Path(path).exists()

    # -- pre-trade ---------------------------------------------------------

    def check_order(
        self,
        state: AccountState,
        order: ProposedOrder,
        budget: RiskBudget | None = None,
    ) -> OrderDecision:
        cfg = self.config
        req = order.quantity
        snap = order.snapshot
        reducing = self._is_reducing(state, order)

        def deny(*reasons: str) -> OrderDecision:
            return OrderDecision(False, 0.0, req, tuple(reasons))

        # Kill switch stops all new orders. Flattening happens via monitor's
        # forced action, executed directly by the adapter — not through here.
        if self._kill_switch_engaged():
            self._engage_halt(BlockReason.KILL_SWITCH.value)
            return deny(BlockReason.KILL_SWITCH.value)

        # A latched halt blocks new risk, but risk-reducing orders still pass.
        if self._halted and not reducing:
            return deny(BlockReason.HALTED.value)

        # Market-quality guards apply to every order: acting on a stale, gapped,
        # or illiquid quote is unsafe in either direction.
        if state.ts - snap.ts > cfg.max_price_staleness_s:
            return deny(BlockReason.STALE_PRICE.value)
        if snap.spread_bps > cfg.max_spread_bps:
            return deny(BlockReason.WIDE_SPREAD.value)
        if abs(snap.gap_pct) > cfg.max_gap_pct:
            return deny(BlockReason.GAP.value)

        # Reducing orders bypass account/exposure/throttle limits entirely — you
        # must always be able to cut risk.
        if reducing:
            return OrderDecision(True, req, req, ())

        # --- increasing orders: account gates, then sizing caps ---
        if state.equity < cfg.min_equity:
            return deny(BlockReason.MIN_EQUITY.value)
        if budget is not None:
            if not budget.trade_enabled:
                return deny(BlockReason.TRADING_DISABLED.value)
            if budget.watchlist is not None and order.symbol not in budget.watchlist:
                return deny(BlockReason.NOT_IN_UNIVERSE.value)

        self._prune_orders(state.ts)
        if len(self._recent_orders) >= cfg.order_rate_limit:
            return deny(BlockReason.RATE_LIMIT.value)

        caps = self._size_caps(state, order, budget)  # reason -> max added notional
        max_notional = max(0.0, min(caps.values()))
        price = snap.price
        max_qty = max_notional / price if price > 0 else 0.0
        approved = min(req, max_qty)
        binding = tuple(r for r, v in caps.items() if v <= max_notional + _EPS)

        if approved <= 0:
            return deny(*binding, BlockReason.ZERO_AFTER_CAPS.value)

        # Accepted (possibly capped). Record for the rate-limit window.
        self._recent_orders.append(state.ts)
        reasons = binding if approved < req - _EPS else ()
        return OrderDecision(True, approved, req, reasons)

    # -- continuous monitor ------------------------------------------------

    def monitor(self, state: AccountState) -> MonitorDecision:
        cfg = self.config

        if self._kill_switch_engaged():
            self._engage_halt(BlockReason.KILL_SWITCH.value)
            return MonitorDecision(
                True, ForcedAction.FLATTEN_ALL, (), (BlockReason.KILL_SWITCH.value,)
            )

        # Already halted earlier this session: stay halted; the flatten was
        # already ordered on the tick that latched it.
        if self._halted:
            return MonitorDecision(True, ForcedAction.NONE, (), self._halt_reasons)

        if state.day_pnl_pct <= cfg.max_daily_loss_pct:
            self._engage_halt(BlockReason.DAILY_LOSS.value)
            return MonitorDecision(
                True, ForcedAction.FLATTEN_ALL, (), (BlockReason.DAILY_LOSS.value,)
            )

        # Per-position stop: a backstop to the broker's resting bracket, not the
        # primary protection. Flags symbols whose unrealized loss breached.
        stopped = tuple(
            p.symbol
            for p in state.positions
            if p.unrealized_pnl_pct <= cfg.per_position_stop_pct
        )
        if stopped:
            return MonitorDecision(
                False, ForcedAction.FLATTEN_SYMBOL, stopped, (BlockReason.POSITION_STOP.value,)
            )

        return MonitorDecision(False, ForcedAction.NONE, (), ())

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _is_reducing(state: AccountState, order: ProposedOrder) -> bool:
        """True if the order only shrinks the symbol's absolute position.

        The order must be opposite in direction to the current position and no
        larger than it. Overshooting (e.g. selling more than a long position)
        is a *flip*: it opens exposure on the other side, so it counts as
        increasing and goes through the full gate.
        """
        pos = state.position(order.symbol)
        if pos is None or pos.quantity == 0:
            return False
        long_pos = pos.quantity > 0
        opposite = (long_pos and order.side is Side.SELL) or (
            not long_pos and order.side is Side.BUY
        )
        return opposite and order.quantity <= abs(pos.quantity)

    def _prune_orders(self, now: float) -> None:
        window = self.config.order_rate_window_s
        cutoff = now - window
        while self._recent_orders and self._recent_orders[0] <= cutoff:
            self._recent_orders.popleft()

    def _size_caps(
        self,
        state: AccountState,
        order: ProposedOrder,
        budget: RiskBudget | None,
    ) -> dict[str, float]:
        """Max additional notional this (increasing) order may add, per cap.

        Every value is a non-negative dollar amount; the binding cap is the min.
        """
        cfg = self.config
        equity = state.equity
        side_sign = 1.0 if order.side is Side.BUY else -1.0

        max_gross = (
            budget.max_gross_exposure
            if budget is not None and budget.max_gross_exposure is not None
            else cfg.max_gross_exposure
        )
        vol_target = (
            budget.vol_target_annual
            if budget is not None and budget.vol_target_annual is not None
            else cfg.vol_target_annual
        )

        pos = state.position(order.symbol)
        sym_notional = abs(pos.market_value) if pos is not None else 0.0

        # Gross: |net exposure summed as abs| must stay <= cap. An increasing
        # order adds `delta` to gross.
        gross_cap = max_gross * equity - state.gross_exposure_notional

        # Net: bound growth in the trade's direction.
        net_cap = cfg.max_net_exposure * equity - side_sign * state.net_exposure_notional

        # Concentration: one name's absolute weight.
        conc_cap = cfg.max_symbol_weight * equity - sym_notional

        # Vol-target: per-name weight cap = vol_target / max(vol, floor).
        vol = max(order.snapshot.annualized_vol, cfg.vol_floor)
        vol_weight = vol_target / vol
        vol_cap = vol_weight * equity - sym_notional

        # Fractional-Kelly hard ceiling on the name's weight.
        kelly_cap = cfg.kelly_weight_cap * equity - sym_notional

        return {
            BlockReason.GROSS_EXPOSURE.value: max(0.0, gross_cap),
            BlockReason.NET_EXPOSURE.value: max(0.0, net_cap),
            BlockReason.CONCENTRATION.value: max(0.0, conc_cap),
            BlockReason.VOL_TARGET.value: max(0.0, vol_cap),
            BlockReason.KELLY_CAP.value: max(0.0, kelly_cap),
        }
