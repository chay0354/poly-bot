"""Live order placement via py-clob-client-v2.

Isolated here so paper/signal mode never imports signing code or needs a key.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

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
        # Off-thread CLOB calls (place / cancel / pre-sign). httpx.Client is
        # thread-safe; the CLOB client holds no per-call state.
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="clob")
        self._presigned: dict[tuple, object] = {}
        self._presign_lock = threading.Lock()
        # Thread-safe callable that wakes the trading loop when a background
        # call finishes (set by the bot).
        self.wake = None
        log.info("live trader ready (sig_type=%s funder=%s)", cfg.signature_type, bool(cfg.funder_address))
        if cfg.cancel_on_start:
            # A previous copy that was killed hard (restart, deploy, crash)
            # may have left bids resting — nobody is watching those. Start
            # from a clean book.
            try:
                resp = self.client.cancel_all()
                n = len(resp.get("canceled") or []) if isinstance(resp, dict) else "?"
                log.info("[LIVE] cancelled leftover open orders on start: %s", n)
            except Exception as e:  # noqa: BLE001
                log.warning("[LIVE] cancel-all on start failed: %s", e)

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
    #
    # Placing and cancelling go through a small thread pool: the CLOB round
    # trip (~110ms from Israel, ~5ms from US-East) must never stall the
    # asyncio loop that is ingesting Binance / book / fill events, and a
    # cancel decided on a Binance tick must be on the wire immediately, not
    # after the loop has finished whatever else it was doing.
    #
    # Placement returns a RestingOrder without an id (`pending`); the id is
    # filled in when the worker finishes. Cancel returns at once with the
    # order marked `cancelling`; the final matched count is confirmed by the
    # user stream or a forced HTTP read on a later poll, so a bid that fills
    # in the same instant we yank it is still harvested. `wait=True` (window
    # close) blocks until everything is confirmed.

    # How long after sending a cancel we start forcing HTTP reads if the
    # user stream has not confirmed the final state.
    CANCEL_CONFIRM_SECS = 0.3

    def presign(self, token_id: str, side: str, shares: float, prices: list[float],
                tick_size: float, neg_risk: bool) -> None:
        """Sign the bids we may post on `token_id` ahead of time (worker
        thread) so `place_bid` is a bare HTTP send. Called when the next
        window's market is known, ~20s before it opens."""
        if not self.cfg.presign:
            return
        opts = PartialCreateOrderOptions(tick_size=str(tick_size), neg_risk=neg_risk)

        def work() -> None:
            n = 0
            for price in prices:
                key = (token_id, round(price, 4), round(shares, 2))
                with self._presign_lock:
                    if key in self._presigned:
                        continue
                try:
                    args = OrderArgsV2(token_id=token_id, price=price, size=shares, side=Side.BUY)
                    signed = self.client.create_order(args, options=opts)
                except Exception as e:  # noqa: BLE001
                    log.warning("[LIVE] presign %s @ %.2f failed: %s", side, price, e)
                    continue
                with self._presign_lock:
                    self._presigned[key] = signed
                    n += 1
            if n:
                log.info("[LIVE] pre-signed %d %s bids (%.2f–%.2f)", n, side, min(prices), max(prices))

        self._pool.submit(work)

    def _take_presigned(self, token_id: str, price: float, shares: float):
        key = (token_id, round(price, 4), round(shares, 2))
        with self._presign_lock:
            return self._presigned.pop(key, None)

    def forget_presigned(self, token_ids) -> None:
        """Drop signed orders for markets we are done with."""
        ids = {str(t) for t in token_ids}
        with self._presign_lock:
            for key in [k for k in self._presigned if k[0] in ids]:
                self._presigned.pop(key, None)

    def place_bid(
        self, token_id: str, side: str, price: float, shares: float,
        tick_size: float, neg_risk: bool,
    ) -> RestingOrder | None:
        """Rest a post-only GTC bid (maker: 0% fee), off-thread.

        Returns immediately with `pending` set; `poll_bid` resolves it. A
        reject (e.g. post-only would cross) marks the order `failed` and
        `done` so the maker re-posts next tick.
        """
        if self._paused():
            return None
        order = RestingOrder(token_id, side, price, shares, paper=False)
        opts = PartialCreateOrderOptions(tick_size=str(tick_size), neg_risk=neg_risk)

        def work():
            signed = self._take_presigned(token_id, price, shares)
            if signed is None:
                args = OrderArgsV2(token_id=token_id, price=price, size=shares, side=Side.BUY)
                signed = self.client.create_order(args, options=opts)
            return self.client.post_order(signed, order_type=OrderType.GTC, post_only=True)

        order.pending = self._submit(work)
        return order

    def _submit(self, fn):
        fut = self._pool.submit(fn)
        if self.wake is not None:
            fut.add_done_callback(lambda _f: self.wake())
        return fut

    def _resolve(self, order: RestingOrder) -> None:
        """Apply a finished placement to `order` (no-op while in flight)."""
        fut = order.pending
        if fut is None or not fut.done():
            return
        order.pending = None
        try:
            resp = fut.result()
        except Exception as e:  # noqa: BLE001
            if "not enough balance" in str(e):
                self._note_reject(e, order.size * order.price)
            else:
                log.error("[LIVE] rest bid failed for %s @ %.2f: %s", order.side, order.price, e)
            order.failed = True
            order.done = True
            return
        order_id = resp.get("orderID") or resp.get("orderId") if isinstance(resp, dict) else None
        if not isinstance(resp, dict) or not resp.get("success", bool(order_id)) or not order_id:
            log.error("[LIVE] rest bid rejected for %s: %s", order.side, resp)
            order.failed = True
            order.done = True
            return
        order.order_id = order_id
        log.info("[LIVE] REST bid %s %.2f sh @ %.2f (id=%s)",
                 order.side, order.size, order.price, order_id)

    def poll_bid(self, order: RestingOrder, force: bool = False) -> Fill | None:
        """Return a Fill for shares matched since the last poll.

        Source: the user stream when it is healthy (ms latency, no HTTP),
        reconciled over HTTP every RECONCILE_SECS. Without the stream, HTTP
        at most once per HTTP_POLL_SECS per order. `force` = HTTP now.
        """
        self._resolve(order)
        if order.done or order.order_id is None:
            return None
        oid = order.order_id
        now = time.monotonic()
        last = self._last_http_poll.get(oid, 0.0)
        state = self.user_stream.state(oid) if self.user_stream is not None else None
        if order.cancelling and not force:
            # The cancel is out; the stream usually confirms within ms. If it
            # has not, read over HTTP at a tight cadence — this read is what
            # decides whether the bid filled as we yanked it.
            fut = order.cancel_future
            sent = fut is not None and fut.done()
            if state is not None and state["status"] in {"MATCHED", "CANCELLED", "CANCELED"}:
                return self._apply_status(order, state["matched"], state["status"])
            if not sent or now - order.cancel_sent_at < self.CANCEL_CONFIRM_SECS:
                return None
            if now - last < self.CANCEL_CONFIRM_SECS:
                return None
            force = True
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

    def cancel_bid(self, order: RestingOrder, wait: bool = False) -> Fill | None:
        """Take a bid down. Returns any fill already known; more may surface
        on later polls (the order stays `cancelling` until confirmed).

        `wait=True` blocks until the cancel is acknowledged and the final
        matched count has been read over HTTP — used at window close so no
        fill is left unaccounted when the maker object goes away.
        """
        if order.done:
            return None
        if order.pending is not None:
            # No id yet: the placement must land before it can be cancelled.
            try:
                order.pending.result(timeout=10)
            except Exception:  # noqa: BLE001 - _resolve logs it
                pass
            self._resolve(order)
            if order.done:
                return None
        fill = None
        if not order.cancelling:
            fill = self.poll_bid(order)  # stream-fresh; may already show a full match
            if order.done:
                return fill
            order.cancelling = True
            order.cancel_sent_at = time.monotonic()
            order.cancel_future = self._submit(lambda: self._do_cancel(order))
        if wait:
            try:
                order.cancel_future.result(timeout=10)
            except Exception:  # noqa: BLE001 - _do_cancel logged it
                pass
            extra = self.poll_bid(order, force=True)
            order.done = True
            if extra is not None:
                fill = extra
        return fill

    def _do_cancel(self, order: RestingOrder) -> None:
        try:
            self.client.cancel_orders([order.order_id])
            log.info("[LIVE] cancelled bid %s (%.2f/%.2f filled) id=%s",
                     order.side, order.filled, order.size, order.order_id)
        except Exception as e:  # noqa: BLE001
            log.error("[LIVE] cancel %s failed: %s", order.order_id, e)
            order.cancelling = False  # let the maker try again
