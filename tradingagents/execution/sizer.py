"""Volatility-target position sizer (fast path) — bounded-aggressive.

Turns a directional signal into a concrete :class:`ProposedOrder`, which the
:class:`~tradingagents.execution.blocker.InteractiveBlocker` then caps or denies.
The two are complementary: the sizer proposes the *intended* bounded-aggressive
size, the blocker enforces the hard limits (docs/intraday_architecture.md §4.2).

Sizing model:
  * **Vol-target** is primary. A name's target weight is
    ``(vol_target / max(σ, floor)) * strength`` — higher target vol ⇒ larger
    positions, scaled by the signal's conviction (``strength`` in ``[-1, 1]``,
    sign = direction).
  * **Fractional Kelly** is an optional *upper bound* on the magnitude, used only
    when the signal carries an edge/variance estimate: ``f · edge / variance``.
  * A static ``max_weight`` safety cap sits below the blocker's hard ceiling.
  * A **dead-band** (``rebalance_band`` + ``min_order_notional``) suppresses tiny
    rebalances so the strategy doesn't churn (and pay spread) chasing noise.

Pure-Python, deterministic, no LLM, no wall clock — same discipline as the
blocker. The sizer never sends anything; it returns a proposal or ``None``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .schemas import AccountState, MarketSnapshot, ProposedOrder, RiskBudget, Side


@dataclass(frozen=True, slots=True)
class Signal:
    """A directional trade intent for one symbol.

    ``strength`` is signed conviction in ``[-1, 1]`` (0 means "target flat").
    ``edge`` and ``variance`` are optional; when both are present they enable the
    fractional-Kelly magnitude cap.
    """

    symbol: str
    strength: float
    snapshot: MarketSnapshot
    edge: float | None = None       # expected per-period return
    variance: float | None = None   # variance of returns (Kelly denominator)


@dataclass
class SizerConfig:
    base_vol_target_annual: float = 0.30   # overridden by RiskBudget when set
    vol_floor: float = 0.05                # floor on σ to bound the weight
    kelly_fraction: float = 0.25           # fraction of full Kelly (upper bound)
    max_weight: float = 1.0                # per-name safety cap (below the blocker's)
    min_order_notional: float = 100.0      # skip orders smaller than this
    rebalance_band: float = 0.005          # dead-band as a fraction of equity
    lot_size: float = 1.0                  # share rounding increment


def _clamp_magnitude(value: float, limit: float) -> float:
    """Clamp ``|value|`` to ``limit``, preserving sign."""
    if limit < 0:
        limit = 0.0
    if value > limit:
        return limit
    if value < -limit:
        return -limit
    return value


class VolTargetSizer:
    """Deterministic vol-target sizer with an optional fractional-Kelly cap."""

    def __init__(self, config: SizerConfig | None = None) -> None:
        self.config = config or SizerConfig()

    def target_weight(self, signal: Signal, budget: RiskBudget | None = None) -> float:
        """Signed target weight for the name, before converting to an order."""
        cfg = self.config
        sigma = max(signal.snapshot.annualized_vol, cfg.vol_floor)
        vol_target = (
            budget.vol_target_annual
            if budget is not None and budget.vol_target_annual is not None
            else cfg.base_vol_target_annual
        )
        strength = _clamp_magnitude(signal.strength, 1.0)
        weight = (vol_target / sigma) * strength

        # Optional fractional-Kelly upper bound on the magnitude.
        if signal.edge is not None and signal.variance:
            kelly_weight = cfg.kelly_fraction * signal.edge / signal.variance
            weight = _clamp_magnitude(weight, abs(kelly_weight))

        # Static per-name safety cap (the blocker enforces the hard ceiling).
        return _clamp_magnitude(weight, cfg.max_weight)

    def size(
        self,
        state: AccountState,
        signal: Signal,
        budget: RiskBudget | None = None,
    ) -> ProposedOrder | None:
        """Return the order to move toward the target weight, or ``None``.

        ``None`` means "do nothing" — target already met within the dead-band,
        or the resulting order is below the minimum size. A ``strength`` of 0
        targets flat, so this also produces the order that closes a position.
        """
        cfg = self.config
        price = signal.snapshot.price
        if price <= 0 or state.equity <= 0:
            return None

        weight = self.target_weight(signal, budget)
        target_notional = weight * state.equity

        pos = state.position(signal.symbol)
        current_notional = pos.market_value if pos is not None else 0.0
        delta_notional = target_notional - current_notional

        # Dead-band: ignore small rebalances (avoid churn / spread bleed).
        band = max(cfg.min_order_notional, cfg.rebalance_band * state.equity)
        if abs(delta_notional) < band:
            return None

        raw_qty = abs(delta_notional) / price
        lot = cfg.lot_size if cfg.lot_size > 0 else 1.0
        quantity = math.floor(raw_qty / lot) * lot
        if quantity <= 0:
            return None

        side = Side.BUY if delta_notional > 0 else Side.SELL
        return ProposedOrder(signal.symbol, side, quantity, signal.snapshot)
