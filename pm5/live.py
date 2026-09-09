"""Live order placement via py-clob-client-v2.

Isolated here so paper/signal mode never imports signing code or needs a key.
"""

from __future__ import annotations

import logging

from py_clob_client_v2 import ClobClient, MarketOrderArgs, OrderType, Side

from .clob import Fill
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
