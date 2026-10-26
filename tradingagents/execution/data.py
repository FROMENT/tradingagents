"""CSV bar loading and frame building for the intraday backtester.

Keeps the P2 gate runnable on **real data** without a vendor SDK: drop a CSV of
intraday bars and run `scripts/backtest_orb.py`. Loading uses only the stdlib
`csv` module, so it stays testable here and dependency-light (an optional
yfinance/vendor fetcher can live behind the `intraday` extra later).

CSV schema (header row required), one row per bar, sorted by time:

    ts,session,symbol,high,low,close,prev_close,vol,is_close[,spread_bps]

  * ``ts``         epoch seconds (float)
  * ``session``    integer session/day id (resets the blocker + day anchor)
  * ``symbol``     single symbol per file (ORB is per-name)
  * ``high/low``   bar extremes (used by the ORB breakout test)
  * ``close``      reference/last price (used for MTM and as the fill mid base)
  * ``prev_close`` prior-session close (gap guard)
  * ``vol``        annualized realized vol (vol-target input)
  * ``is_close``   1/true on the last bar of the session (optional; the last row
                   of each session is treated as close regardless)
  * ``spread_bps`` optional per-bar spread; falls back to ``default_spread_bps``
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .backtest import Frame
from .orb import OpeningRangeBreakout
from .schemas import MarketSnapshot
from .sizer import Signal

_TRUE = {"1", "true", "yes", "y", "t"}


@dataclass(frozen=True, slots=True)
class BacktestBar:
    ts: float
    session: int
    symbol: str
    high: float
    low: float
    close: float
    prev_close: float
    vol: float
    is_close: bool
    spread_bps: float | None = None

    def snapshot(self, default_spread_bps: float) -> MarketSnapshot:
        bps = self.spread_bps if self.spread_bps is not None else default_spread_bps
        half = self.close * bps / 2.0 / 1e4
        return MarketSnapshot(
            self.symbol, self.close, self.close - half, self.close + half,
            self.ts, self.prev_close, self.vol,
        )


def load_bars_csv(path: str | Path) -> list[BacktestBar]:
    """Load intraday bars from a CSV (stdlib only). Rows must be time-sorted.

    Raises ``ValueError`` with the offending row number on a missing column or
    an unparseable number, rather than leaking a raw ``KeyError``/``ValueError``.
    """
    bars: list[BacktestBar] = []
    with Path(path).open(newline="") as fh:
        for lineno, row in enumerate(csv.DictReader(fh), start=2):  # row 1 is the header
            try:
                spread = row.get("spread_bps")
                bars.append(
                    BacktestBar(
                        ts=float(row["ts"]),
                        session=int(row["session"]),
                        symbol=row["symbol"].strip(),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        prev_close=float(row["prev_close"]),
                        vol=float(row["vol"]),
                        is_close=str(row.get("is_close", "")).strip().lower() in _TRUE,
                        spread_bps=float(spread) if spread not in (None, "") else None,
                    )
                )
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError(f"malformed bar CSV at line {lineno}: {exc}") from exc
    if not bars:
        raise ValueError("bar CSV contains no data rows")
    return bars


def _group_sessions(bars: list[BacktestBar]) -> list[list[BacktestBar]]:
    """Split a time-sorted bar list into consecutive same-session runs."""
    groups: list[list[BacktestBar]] = []
    for bar in bars:
        if groups and groups[-1][-1].session == bar.session:
            groups[-1].append(bar)
        else:
            groups.append([bar])
    return groups


def build_frames(
    bars: list[BacktestBar],
    orb: OpeningRangeBreakout,
    default_spread_bps: float = 2.0,
) -> list[Frame]:
    """Run the ORB per session and build backtester frames (single symbol).

    Emits a signal on every bar (the sizer is idempotent within its dead-band),
    including strength 0 which targets flat.
    """
    symbols = {b.symbol for b in bars}
    if len(symbols) > 1:
        raise ValueError(
            f"build_frames handles one symbol per run; got {sorted(symbols)}"
        )

    frames: list[Frame] = []
    for session_bars in _group_sessions(bars):
        strengths = orb.session_strengths(session_bars)
        last = len(session_bars) - 1
        for i, (bar, strength) in enumerate(zip(session_bars, strengths, strict=True)):
            snap = bar.snapshot(default_spread_bps)
            is_close = bar.is_close or i == last
            frames.append(
                Frame(bar.ts, bar.session, is_close, (snap,), (Signal(bar.symbol, strength, snap),))
            )
    return frames
