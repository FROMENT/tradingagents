"""Cost-inclusive intraday backtester — the P2 gate.

This is the go/no-go gate of the intraday plan (docs/intraday_architecture.md
§7): replay the real fast path — the :class:`VolTargetSizer` proposing and the
:class:`InteractiveBlocker` gating — over historical bars, filling every order
against a **realistic cost model** (bid/ask spread + slippage + commission), and
report **net** performance. If net edge does not clear costs here, the effort
stops before any broker adapter is written.

Deliberately honest:
  * Fills pay the spread and slippage as a *taker*, plus commission — never mid.
  * Gross vs net are reported side by side, with a cost breakdown, so "it works"
    can't hide the microstructure bill.
  * Intraday flat-by-close is modelled (also side-steps the French FTT on
    overnight positions), and the blocker's halt/flatten fire in the sim exactly
    as they would live.

Pure-Python, no pandas, deterministic (time comes from the frames). Data loading
(CSV / vendor) is a separate, thin concern and intentionally not part of this
engine, so the gate stays testable without network or heavy deps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .blocker import InteractiveBlocker
from .schemas import AccountState, ForcedAction, MarketSnapshot, PositionState, Side
from .sizer import Signal, VolTargetSizer


@dataclass(frozen=True, slots=True)
class Frame:
    """One time step. ``snapshots`` prices every relevant symbol (for MTM and
    flattening); ``signals`` are the trade intents to act on this step."""

    ts: float
    session: int
    is_close: bool
    snapshots: tuple[MarketSnapshot, ...]
    signals: tuple[Signal, ...] = ()


@dataclass
class CostModel:
    """Taker cost model. Spread comes from the snapshot's bid/ask."""

    commission_per_share: float = 0.0035   # IBKR Pro tiered, order of magnitude
    commission_min: float = 1.0
    slippage_bps: float = 1.0              # market impact on top of half-spread

    def fill_price(self, side: Side, snap: MarketSnapshot) -> float:
        mid = (snap.bid + snap.ask) / 2.0
        half_spread = (snap.ask - snap.bid) / 2.0
        slip = mid * self.slippage_bps / 1e4
        return mid + half_spread + slip if side is Side.BUY else mid - half_spread - slip

    @staticmethod
    def mid(snap: MarketSnapshot) -> float:
        return (snap.bid + snap.ask) / 2.0

    def commission(self, quantity: float) -> float:
        return max(self.commission_min, self.commission_per_share * quantity)


@dataclass
class _Pos:
    quantity: float = 0.0
    avg_cost: float = 0.0


@dataclass
class Fill:
    ts: float
    symbol: str
    side: Side
    quantity: float
    price: float          # actual fill price (with spread+slippage)
    mid: float            # reference mid at fill
    commission: float

    @property
    def impact_cost(self) -> float:
        """Spread + slippage paid vs mid (always >= 0 for a taker)."""
        return self.quantity * abs(self.price - self.mid)


@dataclass
class BacktestResult:
    initial_equity: float
    equity_curve: list[tuple[float, float]] = field(default_factory=list)     # (ts, equity)
    session_equity: list[tuple[int, float]] = field(default_factory=list)     # (session, close equity)
    fills: list[Fill] = field(default_factory=list)
    halts: int = 0
    metrics: dict = field(default_factory=dict)


@dataclass
class BacktestConfig:
    initial_equity: float = 100_000.0
    flat_by_close: bool = True
    periods_per_year: int = 252   # for annualizing the session-return Sharpe


class Backtester:
    """Event-driven replay of sizer + blocker with realistic fills."""

    def __init__(
        self,
        sizer: VolTargetSizer,
        blocker: InteractiveBlocker,
        cost_model: CostModel | None = None,
        config: BacktestConfig | None = None,
    ) -> None:
        self.sizer = sizer
        self.blocker = blocker
        self.costs = cost_model or CostModel()
        self.config = config or BacktestConfig()

    def run(self, frames: list[Frame]) -> BacktestResult:
        cfg = self.config
        res = BacktestResult(initial_equity=cfg.initial_equity)
        cash = cfg.initial_equity
        positions: dict[str, _Pos] = {}
        total_impact = 0.0
        total_commission = 0.0
        traded_notional = 0.0

        current_session: int | None = None
        day_start_equity = cfg.initial_equity

        def price_map(frame: Frame) -> dict[str, MarketSnapshot]:
            return {s.symbol: s for s in frame.snapshots}

        def mark(pm: dict[str, MarketSnapshot]) -> float:
            eq = cash
            for sym, p in positions.items():
                snap = pm.get(sym)
                if snap is not None and p.quantity != 0:
                    eq += p.quantity * self.costs.mid(snap)
            return eq

        def execute(ts: float, snap: MarketSnapshot, side: Side, qty: float) -> None:
            nonlocal cash, total_impact, total_commission, traded_notional
            if qty <= 0:
                return
            fill_price = self.costs.fill_price(side, snap)
            mid = self.costs.mid(snap)
            commission = self.costs.commission(qty)
            signed = qty if side is Side.BUY else -qty
            cash -= signed * fill_price      # buy reduces cash, sell adds
            cash -= commission
            _apply_fill(positions.setdefault(snap.symbol, _Pos()), signed, fill_price)
            fill = Fill(ts, snap.symbol, side, qty, fill_price, mid, commission)
            res.fills.append(fill)
            total_impact += fill.impact_cost
            total_commission += commission
            traded_notional += qty * mid

        def flatten(ts: float, symbols, pm: dict[str, MarketSnapshot]) -> None:
            for sym in symbols:
                p = positions.get(sym)
                if p is None or p.quantity == 0:
                    continue
                snap = pm.get(sym)
                if snap is None:
                    continue
                side = Side.SELL if p.quantity > 0 else Side.BUY
                execute(ts, snap, side, abs(p.quantity))

        def account_state(ts: float, pm: dict[str, MarketSnapshot]) -> AccountState:
            pos_tuple = tuple(
                PositionState(sym, p.quantity, p.avg_cost, self.costs.mid(pm[sym]))
                for sym, p in positions.items()
                if p.quantity != 0 and sym in pm
            )
            return AccountState(ts, mark(pm), day_start_equity, pos_tuple)

        for frame in frames:
            pm = price_map(frame)

            # New session: reset the day anchor and the blocker's halt/rate state.
            if frame.session != current_session:
                current_session = frame.session
                day_start_equity = mark(pm)
                self.blocker.reset_session()

            # 1) Monitor first — halt/flatten has absolute priority.
            state = account_state(frame.ts, pm)
            decision = self.blocker.monitor(state)
            if decision.forced_action is ForcedAction.FLATTEN_ALL:
                flatten(frame.ts, list(positions.keys()), pm)
                res.halts += 1
            elif decision.forced_action is ForcedAction.FLATTEN_SYMBOL:
                flatten(frame.ts, decision.flatten_symbols, pm)

            # 2) New orders, unless halted for the session.
            if not self.blocker.halted:
                for sig in frame.signals:
                    snap = pm.get(sig.symbol)
                    if snap is None:
                        continue
                    state = account_state(frame.ts, pm)  # recompute so caps compound
                    order = self.sizer.size(state, sig)
                    if order is None:
                        continue
                    od = self.blocker.check_order(state, order)
                    if od.allow and od.approved_quantity > 0:
                        execute(frame.ts, snap, order.side, od.approved_quantity)

            # 3) Intraday flat-by-close.
            if frame.is_close and cfg.flat_by_close:
                flatten(frame.ts, list(positions.keys()), pm)

            equity = mark(pm)
            res.equity_curve.append((frame.ts, equity))
            if frame.is_close:
                res.session_equity.append((frame.session, equity))

        res.metrics = _compute_metrics(
            res, total_impact, total_commission, traded_notional, cfg.periods_per_year
        )
        return res


def _apply_fill(pos: _Pos, signed_qty: float, price: float) -> None:
    """Update quantity and average cost for a signed fill (handles flips)."""
    old_qty = pos.quantity
    new_qty = old_qty + signed_qty
    if old_qty == 0 or (old_qty > 0) == (signed_qty > 0):
        # opening or increasing in the same direction -> weighted average
        denom = abs(old_qty) + abs(signed_qty)
        pos.avg_cost = (abs(old_qty) * pos.avg_cost + abs(signed_qty) * price) / denom
    elif abs(signed_qty) > abs(old_qty):
        # flip through zero -> new position at fill price
        pos.avg_cost = price
    # pure reduction: avg_cost unchanged
    pos.quantity = new_qty
    if pos.quantity == 0:
        pos.avg_cost = 0.0


def _compute_metrics(
    res: BacktestResult,
    total_impact: float,
    total_commission: float,
    traded_notional: float,
    periods_per_year: int,
) -> dict:
    init = res.initial_equity
    final = res.equity_curve[-1][1] if res.equity_curve else init
    total_cost = total_impact + total_commission

    net_return = final / init - 1.0 if init else 0.0
    # Gross ~ same trades without the cost bill added back.
    gross_return = (final + total_cost) / init - 1.0 if init else 0.0

    # Sharpe on session-close returns, annualized.
    eq = [e for _, e in res.session_equity]
    rets = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq)) if eq[i - 1]]
    if len(rets) >= 2:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        std = math.sqrt(var)
        sharpe = (mean / std) * math.sqrt(periods_per_year) if std > 0 else 0.0
        positive_ratio = sum(1 for r in rets if r > 0) / len(rets)
    else:
        sharpe = 0.0
        positive_ratio = 0.0

    # Max drawdown on the full equity curve.
    peak = -math.inf
    max_dd = 0.0
    for _, e in res.equity_curve:
        peak = max(peak, e)
        if peak > 0:
            max_dd = min(max_dd, e / peak - 1.0)

    return {
        "net_return": net_return,
        "gross_return": gross_return,
        "total_cost": total_cost,
        "cost_breakdown": {
            "spread_slippage": total_impact,
            "commission": total_commission,
        },
        "cost_drag_return": gross_return - net_return,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "num_fills": len(res.fills),
        "turnover": traded_notional / init if init else 0.0,
        "positive_session_ratio": positive_ratio,
        "halts": res.halts,
        "final_equity": final,
    }
