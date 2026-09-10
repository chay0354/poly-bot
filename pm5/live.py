"""Live order placement via py-clob-client-v2.

Isolated here so paper/signal mode never imports signing code or needs a key.
"""

from __future__ import annotations

import logging
import re
import time

from py_clob_client_v2 import (
    ClobClient,
    MarketOrderArgs,
    OrderArgsV2,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)

from .clob import Fill, RestingOrder
from .config import Config

log = logging.getLogger("pm5.live")


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class LiveTrader:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        kwargs = dict(
            host=cfg.clob_url,
            key=cfg.private_key,
            chain_id=cfg.chain_id,
            signature_type=cfg.signature_type,
        )
        if cfg.funder_address:
            kwargs["funder"] = cfg.funder_address
        self.client = ClobClient(**kwargs)
        creds = self.client.create_or_derive_api_key()
        self.client.set_api_creds(creds)
        # After a "not enough balance" reject, stop hammering the CLOB twice
        # a second (20:16 UTC: 60 rejects in a minute). Retry after a pause.
        self._low_balance_until = 0.0
        # Our order events over WebSocket (fills in ms). The bot schedules
        # `user_stream.run()`; until it is healthy we poll over HTTP.
        self.user_stream = None
        if cfg.ws_user:
            from .clobws import UserStream

            self.user_stream = UserStream(creds.api_key, creds.api_secret, creds.api_passphrase)
        self._last_http_poll: dict[str, float] = {}
        log.info("live trader ready (sig_type=%s funder=%s)", cfg.signature_type, bool(cfg.funder_address))

    LOW_BALANCE_PAUSE = 60.0
    # With the user stream healthy, still confirm each resting order over
    # HTTP this often (the stream does not replay what a hiccup dropped).
    RECONCILE_SECS = 5.0
    # Without the stream, one HTTP status read per order per this many secs.
    HTTP_POLL_SECS = 1.0

    def _note_reject(self, e: Exception, want_usdc: float) -> None:
        text = str(e)
        if "not enough balance" not in text:
            return
        m = re.search(r"balance:\s*(\d+)", text)
        have = int(m.group(1)) / 1e6 if m else None
        self._low_balance_until = time.monotonic() + self.LOW_BALANCE_PAUSE
        log.warning(
            "[LIVE] wallet too low to rest a bid: have $%s, need $%.2f. Deposit USDC or "
            "claim resolved winnings; pausing orders for %.0fs",
            f"{have:.2f}" if have is not None else "?", want_usdc, self.LOW_BALANCE_PAUSE,
        )

    def _paused(self) -> bool:
        return time.monotonic() < self._low_balance_until

    def buy(self, token_id: str, side: str, price: float, shares: float,
            min_price: float = 0.0) -> Fill | None:
        """Fill-or-Kill marketable buy. `price` is the protective limit.

        `min_price` is the caller's floor (momentum passes cfg.min_price; a
        maker pair-completion passes 0 — buying the complement at 0.48 to
        lock a 0.46 leg is the whole point, not a "market disagrees" signal).
        """
        amount = round(shares * price, 2)
        if price < min_price:
            log.error("[LIVE] refused %s @ %.3f below floor %.2f", side, price, min_price)
            return None
        args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=Side.BUY,
            price=price,
            order_type=OrderType.FOK,
        )
        try:
            resp = self.client.create_and_post_market_order(
                order_args=args,
                order_type=OrderType.FOK,
            )
        except Exception as e:  # noqa: BLE001
            if "not enough balance" in str(e):
                self._note_reject(e, amount)
            else:
                log.error("[LIVE] order failed for %s: %s", side, e)
            return None

        if not isinstance(resp, dict):
            log.error("[LIVE] unexpected response for %s: %s", side, resp)
            return None
        order_id = resp.get("orderID") or resp.get("orderId")
        success = resp.get("success", True if order_id else False)
        if not success:
            log.error("[LIVE] order rejected for %s: %s", side, resp)
            return None
        cost = round(amount, 4)
        log.info("[LIVE] BUY %s %.2f sh @ %.3f = $%.2f (id=%s)", side, shares, price, cost, order_id)
        return Fill(token_id, side, price, shares, cost, paper=False, order_id=order_id)

    def sell(self, token_id: str, side: str, price: float, shares: float) -> Fill | None:
        """Fill-and-Kill marketable sell down to `price` (the worst level the
        depth walk reached). `amount` is shares (CLOB convention), not USDC.

        FAK, not FOK: on a thin book a FOK at the top bid is killed whole and
        the leg is then carried to resolution. FAK takes every bid ≥ `price`
        and cancels the rest; we report what actually matched.
        """
        amount = round(shares, 2)
        if amount <= 0:
            return None
        args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=Side.SELL,
            price=price,
            order_type=OrderType.FAK,
        )
        try:
            resp = self.client.create_and_post_market_order(
                order_args=args,
                order_type=OrderType.FAK,
            )
        except Exception as e:  # noqa: BLE001
            log.error("[LIVE] sell failed for %s: %s", side, e)
            return None

        if not isinstance(resp, dict):
            log.error("[LIVE] unexpected sell response for %s: %s", side, resp)
            return None
        order_id = resp.get("orderID") or resp.get("orderId")
        success = resp.get("success", True if order_id else False)
        if not success:
            log.error("[LIVE] sell rejected for %s: %s", side, resp)
            return None
        matched, proceeds = self._matched_amounts(resp, order_id, amount, price)
        if matched <= 0:
            log.warning("[LIVE] sell %s matched nothing (status=%s); will retry",
                        side, resp.get("status"))
            return None
        avg = proceeds / matched if matched > 0 else price
        log.info(
            "[LIVE] SELL %s %.2f sh @ %.3f = $%.2f (id=%s%s)",
            side, matched, avg, proceeds, order_id,
            "" if matched >= amount - 1e-9 else f", partial of {amount:.2f}",
        )
        return Fill(token_id, side, round(avg, 4), -matched, -round(proceeds, 4),
                    paper=False, order_id=order_id)

    def _matched_amounts(self, resp: dict, order_id, amount: float, price: float) -> tuple[float, float]:
        """(shares matched, USDC proceeds) for a marketable sell.

        The POST response carries makingAmount (shares given) / takingAmount
        (USDC received) when the order matched. Fall back to the order record,
        then to the limit price × requested amount.
        """
        making = _num(resp.get("makingAmount"))
        taking = _num(resp.get("takingAmount"))
        if making is not None and making > 0:
            return round(making, 2), round(taking if taking is not None else making * price, 4)
        status = str(resp.get("status") or "").lower()
        if status in {"unmatched", "killed", "cancelled", "canceled"}:
            return 0.0, 0.0
        if order_id:
            try:
                o = self.client.get_order(order_id)
                if isinstance(o, dict):
                    m = _num(o.get("size_matched"))
                    if m is not None:
                        return round(m, 2), round(m * price, 4)
            except Exception as e:  # noqa: BLE001
                log.warning("[LIVE] get_order after sell failed: %s", e)
        # Matched status without amounts: assume the whole order at the limit.
        return amount, round(amount * price, 4)

    # ------------------------------------------------------------------ maker

    def place_bid(
        self, token_id: str, side: str, price: float, shares: float,
        tick_size: float, neg_risk: bool,
    ) -> RestingOrder | None:
        """Rest a post-only GTC bid (maker: 0% fee). Rejected if it would cross."""
        if self._paused():
            return None
        args = OrderArgsV2(token_id=token_id, price=price, size=shares, side=Side.BUY)
        opts = PartialCreateOrderOptions(tick_size=str(tick_size), neg_risk=neg_risk)
        try:
            resp = self.client.create_and_post_order(
                args, options=opts, order_type=OrderType.GTC, post_only=True
            )
        except Exception as e:  # noqa: BLE001
            if "not enough balance" in str(e):
                self._note_reject(e, shares * price)
            else:
                log.error("[LIVE] rest bid failed for %s @ %.2f: %s", side, price, e)
            return None
        if not isinstance(resp, dict):
            log.error("[LIVE] unexpected response resting %s: %s", side, resp)
            return None
        order_id = resp.get("orderID") or resp.get("orderId")
        if not resp.get("success", bool(order_id)) or not order_id:
            log.error("[LIVE] rest bid rejected for %s: %s", side, resp)
            return None
        log.info("[LIVE] REST bid %s %.2f sh @ %.2f (id=%s)", side, shares, price, order_id)
        return RestingOrder(token_id, side, price, shares, order_id=order_id, paper=False)

    def poll_bid(self, order: RestingOrder, force: bool = False) -> Fill | None:
        """Return a Fill for shares matched since the last poll.

        Source: the user stream when it is healthy (ms latency, no HTTP),
        reconciled over HTTP every RECONCILE_SECS. Without the stream, HTTP
        at most once per HTTP_POLL_SECS per order. `force` = HTTP now.
        """
        oid = order.order_id or ""
        now = time.monotonic()
        last = self._last_http_poll.get(oid, 0.0)
        state = self.user_stream.state(oid) if self.user_stream is not None else None
        if not force:
            if state is not None:
                if now - last < self.RECONCILE_SECS:
                    return self._apply_status(order, state["matched"], state["status"])
            elif now - last < self.HTTP_POLL_SECS:
                return None
        self._last_http_poll[oid] = now
        try:
            o = self.client.get_order(order.order_id)
        except Exception as e:  # noqa: BLE001
            log.warning("[LIVE] get_order %s failed: %s", order.order_id, e)
            return None
        if not isinstance(o, dict):
            return None
        try:
            matched = float(o.get("size_matched") or 0.0)
        except (TypeError, ValueError):
            matched = 0.0
        return self._apply_status(order, matched, str(o.get("status") or "").upper())

    def _apply_status(self, order: RestingOrder, matched: float, status: str) -> Fill | None:
        new = round(matched - order.filled, 2)
        if status in {"MATCHED", "CANCELLED", "CANCELED"} or matched >= order.size - 1e-9:
            order.done = True
        if new <= 0:
            return None
        order.filled = round(matched, 2)
        cost = round(new * order.price, 4)
        log.info("[LIVE] bid HIT %s %.2f sh @ %.2f = $%.2f (maker, no fee) id=%s",
                 order.side, new, order.price, cost, order.order_id)
        return Fill(order.token_id, order.side, order.price, new, cost,
                    paper=False, order_id=order.order_id, maker=True)

    def cancel_bid(self, order: RestingOrder) -> None:
        try:
            self.client.cancel_orders([order.order_id])
            log.info("[LIVE] cancelled bid %s (%.2f/%.2f filled) id=%s",
                     order.side, order.filled, order.size, order.order_id)
        except Exception as e:  # noqa: BLE001
            log.error("[LIVE] cancel %s failed: %s", order.order_id, e)
