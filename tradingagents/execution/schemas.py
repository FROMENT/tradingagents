"""Typed inputs and decisions for the deterministic interactive blocker.

These are plain ``dataclasses``, **not** Pydantic models. The blocker sits in the
fast execution path — a pre-trade check on every order plus a continuous monitor
— and must be allocation-light and effectively sub-millisecond, over trusted
in-process inputs. Validation-on-construction would be pure overhead here. The
RMATS *slow-path* schemas stay Pydantic (see ``tradingagents/agents/schemas.py``);
this is the hot path and is intentionally different.

Conventions:
  * All money is USD notional.
  * Weights and pct thresholds are fractions of account equity (``0.03`` == 3%).
  * Losses are negative (``max_daily_loss_pct = -0.03`` means "stop at −3%").
  * Timestamps are epoch seconds and are always taken from the passed-in state,
    never from a wall clock, so decisions are deterministic and testable.

See ``docs/intraday_architecture.md`` §4 for where this fits: the slow LLM path
emits a regime / watchlist / risk budget; the fast path sizes an order and this
blocker gates it. The blocker is the absolute-priority guardrail — it can cap an
order's size, deny it outright, or (via the monitor) force a flatten and halt.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ForcedAction(str, Enum):
    """A protective action the monitor orders the executor to take now."""

    NONE = "NONE"
    FLATTEN_ALL = "FLATTEN_ALL"
    FLATTEN_SYMBOL = "FLATTEN_SYMBOL"


class BlockReason(str, Enum):
    """Structured codes for why an order was capped/denied or a halt fired.

    Codes (not free text) so callers, logs, and tests can assert on them.
    """

    HALTED = "halted_for_session"
    KILL_SWITCH = "kill_switch_engaged"
    MIN_EQUITY = "below_min_equity"
    NOT_IN_UNIVERSE = "symbol_not_in_watchlist"
    TRADING_DISABLED = "trading_disabled_by_risk_budget"
    STALE_PRICE = "price_stale"
    WIDE_SPREAD = "spread_too_wide"
    GAP = "price_gap_too_large"
    RATE_LIMIT = "order_rate_limited"
    GROSS_EXPOSURE = "gross_exposure_cap"
    NET_EXPOSURE = "net_exposure_cap"
    CONCENTRATION = "symbol_concentration_cap"
    VOL_TARGET = "vol_target_cap"
    KELLY_CAP = "kelly_weight_cap"
    ZERO_AFTER_CAPS = "size_zero_after_caps"
    DAILY_LOSS = "max_daily_loss"
    POSITION_STOP = "per_position_stop"


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Point-in-time market data for one symbol, as seen by the executor."""

    symbol: str
    price: float          # reference/last price used for sizing
    bid: float
    ask: float
    ts: float             # epoch seconds of this quote's last update
    prev_close: float     # prior session close, for gap detection
    annualized_vol: float # realized annualized vol, for vol-targeting (e.g. 0.30)

    @property
    def spread_bps(self) -> float:
        mid = (self.bid + self.ask) / 2.0
        return 0.0 if mid <= 0 else (self.ask - self.bid) / mid * 1e4

    @property
    def gap_pct(self) -> float:
        """Signed move of the reference price vs the prior close."""
        return 0.0 if self.prev_close <= 0 else (self.price - self.prev_close) / self.prev_close


@dataclass(frozen=True, slots=True)
class PositionState:
    symbol: str
    quantity: float       # signed: >0 long, <0 short
    avg_cost: float
    price: float

    @property
    def market_value(self) -> float:
        return self.quantity * self.price

    @property
    def unrealized_pnl_pct(self) -> float:
        """Sign-aware unrealized P&L as a fraction of cost basis."""
        if self.avg_cost == 0 or self.quantity == 0:
            return 0.0
        direction = 1.0 if self.quantity > 0 else -1.0
        return direction * (self.price - self.avg_cost) / self.avg_cost


@dataclass(frozen=True, slots=True)
class RiskBudget:
    """Slow-path (LLM committee) output consumed by the fast path. Optional.

    When ``None`` is passed to the blocker, no budget constraints apply and the
    config defaults govern. ``watchlist=None`` means "no symbol restriction".
    """

    trade_enabled: bool = True
    watchlist: frozenset[str] | None = None
    vol_target_annual: float | None = None   # overrides config.vol_target_annual
    max_gross_exposure: float | None = None  # overrides config.max_gross_exposure


@dataclass(frozen=True, slots=True)
class AccountState:
    """Broker-truth account snapshot. Positions come from reconciliation."""

    ts: float
    equity: float
    day_start_equity: float
    positions: tuple[PositionState, ...] = ()

    @property
    def day_pnl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return (self.equity - self.day_start_equity) / self.day_start_equity

    @property
    def gross_exposure_notional(self) -> float:
        return sum(abs(p.market_value) for p in self.positions)

    @property
    def net_exposure_notional(self) -> float:
        return sum(p.market_value for p in self.positions)

    def position(self, symbol: str) -> PositionState | None:
        for p in self.positions:
            if p.symbol == symbol:
                return p
        return None


@dataclass(frozen=True, slots=True)
class ProposedOrder:
    """An order the sizer wants to send, before the blocker gates it."""

    symbol: str
    side: Side
    quantity: float          # always positive; direction is in ``side``
    snapshot: MarketSnapshot

    @property
    def signed_quantity(self) -> float:
        return self.quantity if self.side is Side.BUY else -self.quantity

    @property
    def notional(self) -> float:
        return self.quantity * self.snapshot.price


@dataclass
class BlockerConfig:
    """Hard limits. Bounded-aggressive: high vol-target, but every cap is hard.

    Defaults mirror ``docs/intraday_architecture.md`` §4.2. Tune per deployment;
    the blocker never relaxes a limit on its own.
    """

    # Portfolio-level circuit breakers (monitor)
    max_daily_loss_pct: float = -0.03      # flatten + halt for the day at −3%
    per_position_stop_pct: float = -0.02   # backstop to the broker bracket at −2%
    # Exposure caps (pre-trade)
    max_gross_exposure: float = 2.0        # 200% of equity
    max_net_exposure: float = 1.5          # 150% of equity
    max_symbol_weight: float = 0.25        # 25% of equity in one name
    # Bounded-aggressive sizing caps (pre-trade)
    vol_target_annual: float = 0.30        # per-name vol-target -> weight cap
    kelly_weight_cap: float = 0.50         # hard fractional-Kelly ceiling on weight
    vol_floor: float = 0.05                # floor on annualized_vol to avoid huge caps
    # Market-quality guards (pre-trade)
    max_spread_bps: float = 25.0
    max_price_staleness_s: float = 5.0
    max_gap_pct: float = 0.05
    # Account & throttle
    min_equity: float = 2000.0             # FINRA margin minimum (post-PDT-removal)
    order_rate_limit: int = 20             # max accepted orders per window
    order_rate_window_s: float = 60.0
    # Operational
    kill_switch_file: str | None = None    # if this path exists -> deny + halt


@dataclass(frozen=True, slots=True)
class OrderDecision:
    """Result of a pre-trade check. ``approved_quantity`` may be < requested."""

    allow: bool
    approved_quantity: float
    requested_quantity: float
    reasons: tuple[str, ...] = ()

    @property
    def capped(self) -> bool:
        return self.allow and self.approved_quantity < self.requested_quantity


@dataclass(frozen=True, slots=True)
class MonitorDecision:
    """Result of a continuous monitor tick."""

    halt: bool
    forced_action: ForcedAction = ForcedAction.NONE
    flatten_symbols: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
