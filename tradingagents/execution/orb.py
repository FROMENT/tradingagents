"""Opening Range Breakout (ORB) signal — pure, deterministic.

The reference intraday signal (Zarattini, Barbon & Aziz, "A Profitable Day
Trading Strategy for the U.S. Equity Market") wired to the fast path as a signal
generator for the backtester. It emits a per-bar ``strength`` in {-1, 0, +1};
the :class:`~tradingagents.execution.sizer.VolTargetSizer` turns that into a
sized order and the :class:`~tradingagents.execution.blocker.InteractiveBlocker`
gates it.

Rules, per session:
  * The **opening range** is the high/low of the first ``opening_bars`` bars.
  * After it forms, the first bar whose high breaks above the range opens a
    **long** (+1); a break below the low opens a **short** (-1). First breakout
    wins; one entry per session.
  * With ``stop_at_opposite`` (default), touching the opposite range bound exits
    to flat for the rest of the session. Otherwise the position is held to the
    close.
  * The backtester enforces flat-by-close, so no explicit EOD exit is needed.

Simplification recorded: entry is evaluated on the breakout bar itself (not the
next bar's open). It is a signal, not a fill model — the backtester applies the
realistic cost on execution. A one-bar entry delay can be added later if a
backtest shows it matters.

Pure-Python, no I/O, no look-ahead beyond the current bar.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class _Bar(Protocol):
    high: float
    low: float
    session: int


@dataclass
class ORBConfig:
    opening_bars: int = 1          # bars forming the opening range (e.g. one 5-min bar)
    allow_long: bool = True
    allow_short: bool = True
    stop_at_opposite: bool = True  # exit to flat if the opposite range bound is touched


class OpeningRangeBreakout:
    """Stateless per call: computes the strength series for a session's bars."""

    def __init__(self, config: ORBConfig | None = None) -> None:
        self.config = config or ORBConfig()

    def session_strengths(self, bars: list[_Bar]) -> list[float]:
        """Return a strength in {-1, 0, +1} for each bar of one session, in order."""
        cfg = self.config
        n = len(bars)
        if n <= cfg.opening_bars:
            return [0.0] * n

        opening = bars[: cfg.opening_bars]
        or_high = max(b.high for b in opening)
        or_low = min(b.low for b in opening)

        strengths: list[float] = [0.0] * cfg.opening_bars
        state = 0
        entered = False
        stopped = False

        for bar in bars[cfg.opening_bars:]:
            if stopped:
                strengths.append(0.0)
                continue
            if not entered:
                if cfg.allow_long and bar.high > or_high:
                    state = 1
                    entered = True
                elif cfg.allow_short and bar.low < or_low:
                    state = -1
                    entered = True
                else:
                    state = 0
            else:
                if (state == 1 and bar.low <= or_low) or (state == -1 and bar.high >= or_high):
                    state = 0
                    stopped = True
            strengths.append(float(state))

        return strengths
