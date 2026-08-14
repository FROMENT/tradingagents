"""Intraday deployment heartbeat runner — P3 scaffold.

⚠️  SCAFFOLD. This process does **not** trade. It exists to stand up and exercise
the deployment plumbing (container, secrets, broker connectivity, host liveness)
before any order-path code is written.

What it does, and only this:
  * Connects to IBKR through IB Gateway (``readonly=True``; paper by default).
  * Reconciles against the broker on every (re)connect — positions and open
    orders are read from IBKR, which is the source of truth after any host
    restart. Local state is never trusted.
  * Logs a periodic heartbeat, which also keeps the OCI Always Free host above
    the idle-reclamation threshold.

What it never does: place, modify, or cancel an order. The order path
(signal → sizer → interactive blocker → broker, with bracket/OCO protection
resting at the broker) is added in later phases. See
``docs/intraday_architecture.md``.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("intraday.heartbeat")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


HOST = _env("IB_GATEWAY_HOST", "ib-gateway")
# gnzsnz/ib-gateway exposes the API inside the Docker network on 4003 (live) and
# 4004 (paper). Verify against the image tag you pin — see deploy/README.md.
PORT = int(_env("IB_GATEWAY_PORT", "4004"))
CLIENT_ID = int(_env("IB_CLIENT_ID", "1"))
TRADING_MODE = (_env("TRADING_MODE", "paper") or "paper").lower()
INTERVAL = int(_env("HEARTBEAT_INTERVAL", "60"))
CONNECT_TIMEOUT = int(_env("IB_CONNECT_TIMEOUT", "20"))
KILL_SWITCH = Path(_env("KILL_SWITCH_FILE", "/home/appuser/.tradingagents/KILL"))

_stop = False


def _handle_signal(signum, _frame) -> None:
    global _stop
    log.info("received signal %s — shutting down", signum)
    _stop = True


def _guard_live() -> None:
    """Refuse to run against a live account unless explicitly unlocked.

    The scaffold is paper-only. Trading real money requires both TRADING_MODE=live
    AND ALLOW_LIVE=1 — a deliberate two-key gate so a stray env var can't arm it.
    """
    if TRADING_MODE == "live" and _env("ALLOW_LIVE", "0") != "1":
        log.error(
            "TRADING_MODE=live but ALLOW_LIVE!=1 — refusing to start. "
            "This scaffold is paper-only until explicitly unlocked."
        )
        sys.exit(2)


def _reconcile(ib) -> None:
    """Log the broker-side truth after a (re)connect. Never mutates state."""
    accounts = ib.managedAccounts()
    positions = ib.positions()
    open_trades = ib.openTrades()
    log.info(
        "reconcile: accounts=%s positions=%d open_orders=%d",
        ",".join(accounts) or "?",
        len(positions),
        len(open_trades),
    )
    for p in positions:
        symbol = p.contract.localSymbol or p.contract.symbol
        log.info("  position: %s x%s avg_cost=%s", symbol, p.position, p.avgCost)
    for t in open_trades:
        c, o = t.contract, t.order
        symbol = c.localSymbol or c.symbol
        log.info(
            "  open order: %s %s %s %s status=%s",
            o.action, o.totalQuantity, symbol, o.orderType, t.orderStatus.status,
        )


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    _guard_live()

    try:
        from ib_async import IB
    except ImportError:
        log.error(
            "ib_async not installed — install the intraday extra: "
            "pip install 'tradingagents[intraday]'"
        )
        sys.exit(3)

    ib = IB()
    backoff = 5
    log.info(
        "heartbeat runner starting (SCAFFOLD, no orders) host=%s port=%s mode=%s",
        HOST, PORT, TRADING_MODE,
    )

    while not _stop:
        if not ib.isConnected():
            try:
                log.info(
                    "connecting to IB Gateway %s:%s (clientId=%s)",
                    HOST, PORT, CLIENT_ID,
                )
                # readonly=True blocks order submission at the API layer — a
                # second line of defence on top of the paper-only gate.
                ib.connect(
                    HOST, PORT, clientId=CLIENT_ID,
                    timeout=CONNECT_TIMEOUT, readonly=True,
                )
                backoff = 5
                _reconcile(ib)
            except Exception as exc:  # noqa: BLE001 - log and retry, never crash the loop
                log.warning("connect failed: %s — retrying in %ss", exc, backoff)
                ib.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue

        if KILL_SWITCH.exists():
            log.error("kill switch present at %s — disconnecting and exiting", KILL_SWITCH)
            break

        try:
            server_time = ib.reqCurrentTime()
            positions = ib.positions()
            open_trades = ib.openTrades()
            log.info(
                "heartbeat: server_time=%s positions=%d open_orders=%d",
                server_time, len(positions), len(open_trades),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("heartbeat query failed: %s", exc)

        ib.sleep(INTERVAL)

    if ib.isConnected():
        ib.disconnect()
    log.info("stopped")


if __name__ == "__main__":
    main()
