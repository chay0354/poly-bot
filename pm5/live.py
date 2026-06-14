"""Live order placement via py-clob-client.

Isolated here so paper mode never imports signing code or needs a private key.
"""

from __future__ import annotations

import logging

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

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
        # Derive (or create) L2 API credentials from the wallet key.
        self.client.set_api_creds(self.client.create_or_derive_api_creds())
        log.info("live trader ready (sig_type=%s funder=%s)", cfg.signature_type, bool(cfg.funder_address))

    def buy(self, token_id: str, side: str, price: float, shares: float) -> Fill | None:
        """Fill-or-Kill marketable buy. `price` is the protective limit."""
        args = MarketOrderArgs(
            token_id=token_id,
            amount=round(shares * price, 2),  # USDC amount for a market BUY
            side=BUY,
            price=price,
        )
        try:
            signed = self.client.create_market_order(args)
            resp = self.client.post_order(signed, OrderType.FOK)
        except Exception as e:  # noqa: BLE001
            log.error("[LIVE] order failed for %s: %s", side, e)
            return None

        order_id = resp.get("orderID") or resp.get("orderId")
        success = resp.get("success", False)
        if not success:
            log.error("[LIVE] order rejected for %s: %s", side, resp)
            return None
        cost = round(shares * price, 4)
        log.info("[LIVE] BUY %s %.2f sh @ %.3f = $%.2f (id=%s)", side, shares, price, cost, order_id)
        return Fill(token_id, side, price, shares, cost, paper=False, order_id=order_id)
