"""Live order placement via py-clob-client-v2.

Isolated here so paper/signal mode never imports signing code or needs a key.
"""

from __future__ import annotations

import logging

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
        self.client.set_api_creds(self.client.create_or_derive_api_key())
        log.info("live trader ready (sig_type=%s funder=%s)", cfg.signature_type, bool(cfg.funder_address))

    def buy(self, token_id: str, side: str, price: float, shares: float) -> Fill | None:
        """Fill-or-Kill marketable buy. `price` is the protective limit."""
        amount = round(shares * price, 2)
        if price < self.cfg.min_price:
            log.error(
                "[LIVE] refused %s @ %.3f below floor %.2f",
                side, price, self.cfg.min_price,
            )
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

    # ------------------------------------------------------------------ maker

    def place_bid(
        self, token_id: str, side: str, price: float, shares: float,
        tick_size: float, neg_risk: bool,
    ) -> RestingOrder | None:
        """Rest a post-only GTC bid (maker: 0% fee). Rejected if it would cross."""
        args = OrderArgsV2(token_id=token_id, price=price, size=shares, side=Side.BUY)
        opts = PartialCreateOrderOptions(tick_size=str(tick_size), neg_risk=neg_risk)
        try:
            resp = self.client.create_and_post_order(
                args, options=opts, order_type=OrderType.GTC, post_only=True
            )
        except Exception as e:  # noqa: BLE001
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

    def poll_bid(self, order: RestingOrder) -> Fill | None:
        """Query the order; return a Fill for shares matched since last poll."""
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
        status = str(o.get("status") or "").upper()
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
