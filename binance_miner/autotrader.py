import time
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime

from sqlalchemy.orm import Session

from .binance import BinanceAPIManager
from .config import Config
from .database import Database, LogScout
from .logger import AbstractLogger
from .models import CoinValue, Pair
from .postpone import postpone_heavy_calls
from .ratios import CoinStub


class AutoTrader(ABC):
    def __init__(
        self,
        logger: AbstractLogger,
        config: Config,
        database: Database,
        binance_manager: BinanceAPIManager,
    ):
        self.logger = logger
        self.config = config
        self.db = database
        self.manager = binance_manager

    @abstractmethod
    def scout(self):
        ...

    def _max_value_in_wallet(self):
        balances = {
            coin.symbol: self.manager.get_currency_balance(coin.symbol)
            for coin in CoinStub.get_all()
        }
        bridge_balance = self.manager.get_currency_balance(self.config.BRIDGE.symbol)
        max_quote_amount = bridge_balance
        while True:
            for symbol, amount in balances.items():
                _, quote_amount = self.manager.get_market_sell_price(
                    symbol + self.config.BRIDGE.symbol, amount
                )
                if quote_amount is None:
                    break
                max_quote_amount = max(max_quote_amount, quote_amount)
            else:
                break
            time.sleep(1)
        return max_quote_amount

    def _get_ratios(
        self,
        coin: CoinStub,
        coin_sell_price: float,
        quote_amount: float,
        enable_scout_log: bool = True,
    ):
        ratio_dict: dict[tuple[int, int], float] = {}
        price_amounts: dict[str, tuple[float, float]] = {}
        scout_logs = []
        for to_idx, target_ratio in enumerate(self.db.ratios_manager.get_from_coin(coin.idx)):
            if coin.idx == to_idx:
                continue
            to_coin = CoinStub.get_by_idx(to_idx)
            optional_coin_buy_price, optional_coin_amount = self.manager.get_market_buy_price(
                to_coin.symbol + self.config.BRIDGE.symbol, quote_amount
            )
            if optional_coin_buy_price is None:
                self.logger.info(
                    f"Market price for coin {to_coin.symbol + self.config.BRIDGE.symbol} can't be calculated, skipping"
                )
                continue
            price_amounts[to_coin.symbol] = (optional_coin_buy_price, optional_coin_amount)
            coin_opt_coin_ratio = coin_sell_price / optional_coin_buy_price
            from_fee = self.manager.get_fee(coin.symbol, self.config.BRIDGE.symbol, selling=True)
            to_fee = self.manager.get_fee(to_coin.symbol, self.config.BRIDGE.symbol, selling=False)
            transaction_fee = from_fee + to_fee - from_fee * to_fee
            if self.config.USE_MARGIN:
                ratio_dict[(coin.idx, to_coin.idx)] = (
                    (1 - transaction_fee) * coin_opt_coin_ratio / target_ratio
                    - 1
                    - self.config.SCOUT_MARGIN / 100
                )
            else:
                ratio_dict[(coin.idx, to_coin.idx)] = (
                    coin_opt_coin_ratio
                    - transaction_fee * self.config.SCOUT_MULTIPLIER * coin_opt_coin_ratio
                ) - target_ratio
            if enable_scout_log:
                scout_logs.append(
                    LogScout(
                        self.db.ratios_manager.get_pair_id(coin.idx, to_idx),
                        ratio_dict[(coin.idx, to_coin.idx)],
                        target_ratio,
                        coin_sell_price,
                        optional_coin_buy_price,
                    )
                )
        if scout_logs:
            self.db.batch_log_scout(scout_logs)
        return ratio_dict, price_amounts

    @postpone_heavy_calls
    def _jump_to_best_coin(
        self, coin: CoinStub, coin_sell_price: float, quote_amount: float, coin_amount: float
    ):
        can_walk_deeper = True
        jump_chain = [coin.symbol]
        last_coin: CoinStub = coin
        last_coin_sell_price = coin_sell_price
        last_coin_buy_price = 0.0
        last_coin_quote = quote_amount
        last_coin_amount = coin_amount
        bridge_balance = self.manager.get_currency_balance(self.config.BRIDGE.symbol)
        is_initial_coin = True
        while can_walk_deeper:
            if not is_initial_coin:
                last_coin_sell_price, last_coin_quote = self.manager.get_market_sell_price(
                    last_coin.symbol + self.config.BRIDGE.symbol, last_coin_amount
                )
                if last_coin_sell_price is None:
                    self.db.ratios_manager.rollback()
                    return
            ratio_dict, prices = self._get_ratios(
                last_coin, last_coin_sell_price, last_coin_quote, enable_scout_log=is_initial_coin
            )
            ratio_dict = {k: v for k, v in ratio_dict.items() if v > 0}
            if ratio_dict:
                new_best_pair = max(ratio_dict, key=ratio_dict.get)
                new_best_coin = CoinStub.get_by_idx(new_best_pair[1])
                if not is_initial_coin:
                    if not self.update_trade_threshold(
                        last_coin,
                        new_best_coin,
                        last_coin_buy_price,
                        last_coin_amount,
                        last_coin_quote,
                    ):
                        self.db.ratios_manager.rollback()
                        return
                last_coin = new_best_coin
                last_coin_buy_price, last_coin_amount = prices[last_coin.symbol]
                jump_chain.append(last_coin.symbol)
                is_initial_coin = False
            else:
                can_walk_deeper = False
        self.db.commit_ratios()
        if not is_initial_coin:
            if len(jump_chain) > 2:
                self.logger.info(f"Squashed jump chain: {jump_chain}")
            if jump_chain[0] != jump_chain[-1]:
                self.logger.info(f"Will be jumping from {coin.symbol} to {last_coin.symbol}")
                result = self.transaction_through_bridge(
                    coin, last_coin, coin_sell_price, last_coin_buy_price
                )
                expected_sold_quantity = self.manager.sell_quantity(
                    coin.symbol, self.config.BRIDGE.symbol, coin_amount
                )
                expected_bridge = expected_sold_quantity * coin_sell_price * 0.999 + bridge_balance
                expected_bought_quantity_no_fees = self.manager.buy_quantity(
                    last_coin.symbol,
                    self.config.BRIDGE.symbol,
                    expected_bridge,
                    last_coin_buy_price,
                )
                self.logger.info(
                    f"Expected: {expected_bought_quantity_no_fees:0.08f}, "
                    f"Actual: {result.cumulative_filled_quantity:0.08f}, "
                    f"Slippage: {expected_bought_quantity_no_fees/result.cumulative_filled_quantity - 1:0.06%}"
                )
            else:
                self.update_trade_threshold(
                    to_coin=coin,
                    from_coin=None,
                    to_coin_buy_price=coin_sell_price,
                    to_coin_amount=0,
                    quote_amount=quote_amount,
                )
                self.logger.info(f"Eliminated jump loop from {coin.symbol} to {coin.symbol}")

    def initialize(self):
        self.initialize_trade_thresholds()

    # XXX: Improve logging semantics
    def transaction_through_bridge(
        self, from_coin: CoinStub, to_coin: CoinStub, sell_price: float, buy_price: float
    ):
        to_coin_original_amount = self.manager.get_currency_balance(to_coin.symbol)
        if self.manager.sell_alt(from_coin.symbol, self.config.BRIDGE.symbol, sell_price) is None:
            self.logger.error(
                f"Market sell failed, from_coin: {from_coin.symbol}, to_coin: {to_coin.symbol}, sell_price: {sell_price}"
            )
        result = self.manager.buy_alt(to_coin.symbol, self.config.BRIDGE.symbol, buy_price)
        if result is not None:
            self.db.set_current_coin(to_coin.symbol)
            price = result.price
            if abs(price) < 1e-15:
                price = result.cumulative_quote_qty / result.cumulative_filled_quantity
            update_successful = False
            while not update_successful:
                to_coin_amount = self.manager.get_currency_balance(to_coin.symbol)
                while to_coin_original_amount >= to_coin_amount:
                    balances_changed = self.manager.cache.balances_changed_event.wait(1.0)
                    self.manager.cache.balances_changed_event.clear()
                    to_coin_amount = self.manager.get_currency_balance(
                        to_coin.symbol, force=(not balances_changed)
                    )
                update_successful = self.update_trade_threshold(
                    to_coin, from_coin, price, to_coin_amount, result.cumulative_quote_qty
                )
                if not update_successful:
                    self.logger.info("Update of ratios failed, retry in 1s")
                    time.sleep(1)
            return result
        self.logger.info("Couldn't buy, going back to scouting mode...")
        return

    # XXX: Improve logging semantics
    def update_trade_threshold(
        self,
        to_coin: CoinStub,
        from_coin: CoinStub | None,
        to_coin_buy_price: float,
        to_coin_amount: float,
        quote_amount: float,
    ):
        if to_coin_buy_price is None:
            self.logger.info(
                f"Skipping update... current coin {to_coin.symbol + self.config.BRIDGE.symbol} not found"
            )
            return False
        for coin in CoinStub.get_all():
            if coin is to_coin:
                continue
            coin_price, _ = self.manager.get_market_sell_price_fill_quote(
                coin.symbol + self.config.BRIDGE.symbol, quote_amount
            )
            if coin_price is None:
                self.logger.info(
                    f"Update for coin {coin.symbol + self.config.BRIDGE.symbol} can't be performed, not enough orders in order book"
                )
                return False
            self.db.ratios_manager.set(coin.idx, to_coin.idx, coin_price / to_coin_buy_price)
        if from_coin is not None:
            from_coin_buy_price, _ = self.manager.get_market_buy_price(
                from_coin.symbol + self.config.BRIDGE.symbol, quote_amount
            )
            to_coin_sell_price, _ = self.manager.get_market_sell_price(
                to_coin.symbol + self.config.BRIDGE.symbol, to_coin_amount
            )
            if from_coin_buy_price is None or to_coin_sell_price is None:
                self.logger.info(
                    f"Can't update reverse pair {to_coin.symbol}->{from_coin.symbol}, not enough orders in order book"
                )
                return False
            self.db.ratios_manager.set(
                to_coin.idx,
                from_coin.idx,
                max(
                    self.db.ratios_manager.get(to_coin.idx, from_coin.idx),
                    to_coin_sell_price / from_coin_buy_price,
                ),
            )
        return True

    # FIXME: Ruff(C901)
    def initialize_trade_thresholds(self):
        ratios_manager = self.db.ratios_manager
        max_quote_amount = self._max_value_in_wallet()
        session: Session
        with self.db.db_session() as session:
            pairs = session.query(Pair).filter(Pair.ratio.is_(None)).all()
            grouped_pairs = defaultdict(list)
            for pair in pairs:
                if pair.from_coin.enabled and pair.to_coin.enabled:
                    grouped_pairs[pair.from_coin.symbol].append(pair)
            for from_coin_symbol, group in grouped_pairs.items():
                from_coin_idx = CoinStub.get_by_symbol(from_coin_symbol).idx
                self.logger.info(
                    f"Initializing {from_coin_symbol} vs [{', '.join([p.to_coin.symbol for p in group])}]"
                )
                for pair in group:
                    for _ in range(10):
                        from_coin_price, _ = self.manager.get_market_sell_price_fill_quote(
                            from_coin_symbol + self.config.BRIDGE.symbol, max_quote_amount
                        )
                        if from_coin_price is not None:
                            break
                        time.sleep(1)
                    if from_coin_price is None:
                        self.logger.info(
                            f"Skipping initializing {pair.from_coin + self.config.BRIDGE}, symbol not found"
                        )
                        continue
                    for _ in range(10):
                        to_coin_price, _ = self.manager.get_market_buy_price(
                            pair.to_coin.symbol + self.config.BRIDGE.symbol, max_quote_amount
                        )
                        if to_coin_price is not None:
                            break
                        time.sleep(10)
                    if to_coin_price is None:
                        self.logger.info(
                            f"Skipping initializing {pair.to_coin + self.config.BRIDGE}, symbol not found"
                        )
                        continue
                    ratios_manager.set(
                        from_coin_idx,
                        CoinStub.get_by_symbol(pair.to_coin.symbol).idx,
                        from_coin_price / to_coin_price,
                    )
        self.db.commit_ratios()

    @postpone_heavy_calls
    def bridge_scout(self):
        bridge_balance = self.manager.get_currency_balance(self.config.BRIDGE.symbol)
        coins = CoinStub.get_all()
        if all(
            bridge_balance <= self.manager.get_min_notional(coin.symbol, self.config.BRIDGE.symbol)
            for coin in coins
        ):
            return
        for coin in coins:
            current_coin_price = self.manager.get_ticker_price(
                coin.symbol + self.config.BRIDGE.symbol
            )
            if current_coin_price is None:
                continue
            ratio_dict, _ = self._get_ratios(coin, current_coin_price, bridge_balance)
            if not any(v > 0 for v in ratio_dict.values()):
                if bridge_balance > self.manager.get_min_notional(
                    coin.symbol, self.config.BRIDGE.symbol
                ):
                    self.logger.info(f"Will be purchasing {coin.symbol} using bridge coin")
                    result = self.manager.buy_alt(
                        coin.symbol,
                        self.config.BRIDGE.symbol,
                        self.manager.get_ticker_price(coin.symbol + self.config.BRIDGE.symbol),
                    )
                    if result is not None:
                        self.db.set_current_coin(coin.symbol)
                        self.db.commit_ratios()
                        return coin
        return

    def update_values(self):
        now = datetime.now()
        coins = self.db.get_coins(only_enabled=False)
        cv_batch = []
        for coin in coins:
            balance = self.manager.get_currency_balance(coin.symbol)
            if balance == 0:
                continue
            usd_value = self.manager.get_ticker_price(coin + self.config.BRIDGE.symbol)
            btc_value = self.manager.get_ticker_price(coin + "BTC")
            cv = CoinValue(coin, balance, usd_value, btc_value, dt=now)
            cv_batch.append(cv)
        self.db.batch_update_coin_values(cv_batch)
