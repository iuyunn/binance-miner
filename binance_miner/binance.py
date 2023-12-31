from __future__ import annotations

import json
import math
import os
import time
import traceback
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Callable
from typing import Any, TypedDict, TypeVar

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceOrderException, BinanceRequestException
from cachetools import TTLCache, cached

from .binance_ws import BinanceCache, BinanceOrder, BinanceStreamManager, StreamManagerWorker
from .config import Config
from .database import Database
from .logger import AbstractLogger
from .postpone import heavy_call

T = TypeVar("T")


class PaperWallet(TypedDict):
    balances: dict[str, float]
    fake_order_id: int


class AbstractOrderBalanceManager(ABC):
    @staticmethod
    def float_as_decimal_str(num: float):
        return f"{num:0.08f}".rstrip("0").rstrip(".")

    @abstractmethod
    def get_currency_balance(self, currency_symbol: str, force: bool = False) -> float:
        ...

    @abstractmethod
    def create_order(self, **kwargs) -> dict:
        ...

    def make_order(self, side: str, symbol: str, quantity: float, quote_quantity: float):
        kwargs = {
            "symbol": symbol,
            "side": side,
            "quantity": self.float_as_decimal_str(quantity),
            "type": Client.ORDER_TYPE_MARKET,
        }
        if side == Client.SIDE_BUY:
            del kwargs["quantity"]
            kwargs["quoteOrderQty"] = self.float_as_decimal_str(quote_quantity)
        return self.create_order(**kwargs)


class PaperOrderBalanceManager(AbstractOrderBalanceManager):
    PERSIST_FILE_PATH = "data/paper_wallet.json"

    def __init__(
        self,
        bridge_symbol: str,
        client: Client,
        cache: BinanceCache,
        initial_balances: dict[str, float],
        read_persist: bool = True,
    ):
        self.balances = initial_balances
        self.bridge = bridge_symbol
        self.client = client
        self.cache = cache
        self.fake_order_id = 0
        if read_persist:
            data = self._read_persist()
            if data is not None:
                if "balances" in data:
                    self.balances = data["balances"]
                    self.fake_order_id = data["fake_order_id"]
                else:
                    self.balances = data

    def _read_persist(self) -> PaperWallet | None:
        if os.path.exists(self.PERSIST_FILE_PATH):
            with open(self.PERSIST_FILE_PATH) as json_file:
                return json.load(json_file)
        return None

    def _write_persist(self):
        with open(self.PERSIST_FILE_PATH, "w") as json_file:
            json.dump({"balances": self.balances, "fake_order_id": self.fake_order_id}, json_file)

    def get_currency_balance(self, currency_symbol: str, force: bool = False) -> float:
        return self.balances.get(currency_symbol, 0.0)

    def create_order(self, **kwargs):
        return {}

    def make_order(self, side: str, symbol: str, quantity: float, quote_quantity: float):
        symbol_base = symbol[: -len(self.bridge)]
        if side == Client.SIDE_SELL:
            self.balances[self.bridge] = (
                self.get_currency_balance(self.bridge) + quote_quantity * 0.999
            )
            self.balances[symbol_base] = self.get_currency_balance(symbol_base) - quantity
        else:
            self.balances[self.bridge] = self.get_currency_balance(self.bridge) - quote_quantity
            self.balances[symbol_base] = self.get_currency_balance(symbol_base) + quantity * 0.999
        self.cache.balances_changed_event.set()
        super().make_order(side, symbol, quantity, quote_quantity)
        if side == Client.SIDE_BUY:
            self._write_persist()
        self.fake_order_id += 1
        return defaultdict(
            lambda: "",
            orderId=str(self.fake_order_id),
            status="FILLED",
            executedQty=str(quantity),
            cummulativeQuoteQty=str(quote_quantity),
            price="0",
            side=side,
            type=Client.ORDER_TYPE_MARKET,
        )


class BinanceOrderBalanceManager(AbstractOrderBalanceManager):
    def __init__(self, logger: AbstractLogger, binance_client: Client, cache: BinanceCache):
        self.logger = logger
        self.binance_client = binance_client
        self.cache = cache

    def get_currency_balance(self, currency_symbol: str, force: bool = False):
        with self.cache.open_balances() as cache_balances:
            balance = cache_balances.get(currency_symbol, None)
            if force or balance is None:
                cache_balances.clear()
                cache_balances.update(
                    {
                        currency_balance["asset"]: float(currency_balance["free"])
                        for currency_balance in self.binance_client.get_account()["balances"]
                    }
                )
                self.logger.debug(f"Fetched all balances: {cache_balances}")
                if currency_symbol not in cache_balances:
                    cache_balances[currency_symbol] = 0.0
                    return 0.0
                return cache_balances.get(currency_symbol, 0.0)
            return balance

    def create_order(self, **kwargs):
        return self.binance_client.create_order(**kwargs)


class BinanceAPIManager:
    def __init__(
        self,
        client: Client,
        cache: BinanceCache,
        config: Config,
        db: Database,
        logger: AbstractLogger,
        order_balance_manager: AbstractOrderBalanceManager,
    ):
        self.binance_client = client
        self.db = db
        self.logger = logger
        self.config = config
        self.cache = cache
        self.order_balance_manager = order_balance_manager
        self.stream_manager: BinanceStreamManager | None = None
        self._setup_websockets()

    @staticmethod
    def _common_factory(
        logger: AbstractLogger,
        config: Config,
        db: Database,
        ob_factory: Callable[[Client, BinanceCache], AbstractOrderBalanceManager],
    ) -> BinanceAPIManager:
        cache = BinanceCache()
        client = Client(config.BINANCE_API_KEY, config.BINANCE_API_SECRET_KEY, tld=config.TLD)
        return BinanceAPIManager(client, cache, config, db, logger, ob_factory(client, cache))

    @staticmethod
    def create_manager(logger: AbstractLogger, config: Config, db: Database) -> BinanceAPIManager:
        return BinanceAPIManager._common_factory(
            logger,
            config,
            db,
            lambda client, cache: BinanceOrderBalanceManager(logger, client, cache),
        )

    @staticmethod
    def create_manager_paper_trading(
        logger: AbstractLogger,
        config: Config,
        db: Database,
        initial_balances: dict[str, float],
    ) -> BinanceAPIManager:
        return BinanceAPIManager._common_factory(
            logger,
            config,
            db,
            lambda client, cache: PaperOrderBalanceManager(
                config.BRIDGE.symbol,
                client,
                cache,
                initial_balances,
            ),
        )

    def _setup_websockets(self):
        self.stream_manager = StreamManagerWorker.create(self.cache, self.config, self.logger)

    def _retry(self, func: Callable[..., T], *args, **kwargs) -> T | None:
        for attempt in range(20):
            try:
                return func(*args, **kwargs)
            except (BinanceOrderException, BinanceAPIException, BinanceRequestException):
                self.logger.warning(f"Failed to Buy/Sell. Retrying... (attempt {attempt}/20)")
                self.logger.warning(traceback.format_exc())
            time.sleep(1)
        return None

    def _buy_alt(self, origin_coin: str, target_coin: str, buy_price: float):
        origin_balance = self.get_currency_balance(origin_coin)
        target_balance = self.get_currency_balance(target_coin)
        order_quantity = self.buy_quantity(origin_coin, target_coin, target_balance, buy_price)
        self.logger.info(f"Buying {order_quantity} <{origin_coin}>")
        order = self.order_balance_manager.make_order(
            side=Client.SIDE_BUY,
            symbol=origin_coin + target_coin,
            quantity=order_quantity,
            quote_quantity=target_balance,
        )
        order = BinanceOrder(order)
        executed_qty = order.cumulative_filled_quantity
        if executed_qty > 0 and order.status == "FILLED":
            order_quantity = executed_qty
        self.logger.info(f"Bought {origin_coin}")

        @heavy_call
        def write_trade_log():
            trade_log = self.db.start_trade_log(origin_coin, target_coin, selling=False)
            trade_log.set_ordered(origin_balance, target_balance, order_quantity)
            trade_log.set_complete(order.cumulative_quote_qty)

        write_trade_log()
        return order

    def _sell_alt(self, origin_coin: str, target_coin: str, sell_price: float):
        origin_balance = self.get_currency_balance(origin_coin)
        target_balance = self.get_currency_balance(target_coin)
        order_quantity = self.sell_quantity(origin_coin, target_coin, origin_balance)
        self.logger.info(f"Selling {order_quantity} <{origin_coin}>")
        self.logger.info(f"Balance is {origin_balance}")
        order = self.order_balance_manager.make_order(
            side=Client.SIDE_SELL,
            symbol=origin_coin + target_coin,
            quantity=order_quantity,
            quote_quantity=sell_price * order_quantity,
        )
        order = BinanceOrder(order)
        new_balance = self.get_currency_balance(origin_coin)
        while new_balance >= origin_balance:
            balances_changed = self.cache.balances_changed_event.wait(1.0)
            self.cache.balances_changed_event.clear()
            new_balance = self.get_currency_balance(origin_coin, force=not balances_changed)
        self.logger.info(f"Sold {origin_coin}")

        @heavy_call
        def write_trade_log():
            trade_log = self.db.start_trade_log(origin_coin, target_coin, selling=True)
            trade_log.set_ordered(origin_balance, target_balance, order_quantity)
            trade_log.set_complete(order.cumulative_quote_qty)

        write_trade_log()
        return order

    def get_currency_balance(self, currency_symbol: str, force: bool = False):
        return self.order_balance_manager.get_currency_balance(currency_symbol, force)

    def get_market_sell_price(self, symbol: str, amount: float):
        return self.stream_manager.get_market_sell_price(symbol, amount)

    def get_market_buy_price(self, symbol: str, quote_amount: float):
        return self.stream_manager.get_market_buy_price(symbol, quote_amount)

    def get_market_sell_price_fill_quote(self, symbol: str, quote_amount: float):
        return self.stream_manager.get_market_sell_price_fill_quote(symbol, quote_amount)

    @cached(cache=TTLCache(maxsize=1, ttl=43200))
    def get_trade_fees(self) -> dict[str, float]:
        return {
            ticker["symbol"]: float(ticker["takerCommission"])
            for ticker in self.binance_client.get_trade_fee()
        }

    @cached(cache=TTLCache(maxsize=1, ttl=60))
    def get_using_bnb_for_fees(self) -> bool:
        return self.binance_client.get_bnb_burn_spot_margin()["spotBNBBurn"]

    def get_fee(self, origin_coin: str, target_coin: str, selling: bool):
        if self.config != "com":
            return 0.001
        base_fee = self.get_trade_fees()[origin_coin + target_coin]
        if not self.get_using_bnb_for_fees():
            return base_fee
        amount_trading = (
            self.sell_quantity(origin_coin, target_coin)
            if selling
            else self.buy_quantity(origin_coin, target_coin)
        )
        fee_amount = amount_trading * base_fee * 0.75
        if origin_coin == "BNB":
            fee_amount_bnb = fee_amount
        else:
            origin_price = self.get_ticker_price(origin_coin + "BNB")
            if origin_price is None:
                return base_fee
            fee_amount_bnb = fee_amount * origin_price
        bnb_balance = self.get_currency_balance("BNB")
        if bnb_balance >= fee_amount_bnb:
            return base_fee * 0.75
        return base_fee

    def close(self):
        if self.stream_manager:
            self.stream_manager.close()

    def get_account(self) -> dict[str, Any]:
        return self.binance_client.get_account()

    def get_ticker_price(self, ticker_symbol: str):
        price = self.cache.ticker_values.get(ticker_symbol, None)
        if price is None and ticker_symbol not in self.cache.non_existent_tickers:
            self.cache.ticker_values = {
                ticker["symbol"]: float(ticker["price"])
                for ticker in self.binance_client.get_symbol_ticker()
            }
            self.logger.debug(f"Fetched all ticker prices: {self.cache.ticker_values}")
            price = self.cache.ticker_values.get(ticker_symbol, None)
            if price is None:
                self.logger.info(
                    f"Ticker does not exist: {ticker_symbol} - will not be fetched from now on"
                )
                self.cache.non_existent_tickers.add(ticker_symbol)
        return price

    def get_symbol_filter(
        self, origin_symbol: str, target_symbol: str, filter_type: str
    ) -> dict[str, Any]:
        return next(
            _filter
            for _filter in self.binance_client.get_symbol_info(origin_symbol + target_symbol)[
                "filters"
            ]
            if _filter["filterType"] == filter_type
        )

    @cached(cache=TTLCache(maxsize=2000, ttl=43200))
    def get_alt_tick(self, origin_symbol: str, target_symbol: str) -> float:
        step_size = self.get_symbol_filter(origin_symbol, target_symbol, "LOT_SIZE")["stepSize"]
        if step_size.find("1") == 0:
            return 1 - step_size.find(".")
        return step_size.find("1") - 1

    @cached(cache=TTLCache(maxsize=2000, ttl=43200))
    def get_min_notional(self, origin_symbol: str, target_symbol: str):
        return float(
            self.get_symbol_filter(origin_symbol, target_symbol, "NOTIONAL")["minNotional"]
        )

    def buy_quantity(
        self,
        origin_symbol: str,
        target_symbol: str,
        target_balance: float | None = None,
        from_coin_price: float | None = None,
    ) -> float:
        target_balance = target_balance or self.get_currency_balance(target_symbol)
        from_coin_price = from_coin_price or self.get_ticker_price(origin_symbol + target_symbol)
        origin_tick = self.get_alt_tick(origin_symbol, target_symbol)
        return math.floor(target_balance * 10**origin_tick / from_coin_price) / float(
            10**origin_tick
        )

    def sell_quantity(
        self, origin_symbol: str, target_symbol: str, origin_balance: float | None = None
    ) -> float:
        origin_balance = origin_balance or self.get_currency_balance(origin_symbol)
        origin_tick = self.get_alt_tick(origin_symbol, target_symbol)
        return math.floor(origin_balance * 10**origin_tick) / float(10**origin_tick)

    def buy_alt(self, origin_coin: str, target_coin: str, buy_price: float) -> BinanceOrder | None:
        return self._retry(self._buy_alt, origin_coin, target_coin, buy_price)

    def sell_alt(
        self, origin_coin: str, target_coin: str, sell_price: float
    ) -> BinanceOrder | None:
        return self._retry(self._sell_alt, origin_coin, target_coin, sell_price)
