# https://docs.sqlalchemy.org/en/20/orm/extensions/mypy.html
# mypy: disable-error-code=assignment
import enum
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Enum, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from .base import Base


class TradeState(enum.Enum):
    STARTING = "STARTING"
    ORDERED = "ORDERED"
    COMPLETE = "COMPLETE"


class Trade(Base):
    __tablename__ = "trade_history"
    id = Column(Integer, primary_key=True)
    alt_coin_id = Column(String, ForeignKey("coins.symbol"))
    alt_coin = relationship("Coin", foreign_keys=[alt_coin_id], lazy="joined")
    crypto_coin_id = Column(String, ForeignKey("coins.symbol"))
    crypto_coin = relationship("Coin", foreign_keys=[crypto_coin_id], lazy="joined")
    selling = Column(Boolean)
    state: Column[str] = Column(Enum(TradeState))
    alt_starting_balance = Column(Float)
    alt_trade_amount = Column(Float)
    crypto_starting_balance = Column(Float)
    crypto_trade_amount = Column(Float)
    dt = Column(DateTime)

    def __init__(self, alt_coin: str, crypto_coin: str, selling: bool):
        self.alt_coin_id = alt_coin
        self.crypto_coin_id = crypto_coin
        self.state = TradeState.STARTING
        self.selling = selling
        self.dt = datetime.utcnow()

    def info(self):
        return {
            "id": self.id,
            "alt_coin": self.alt_coin_id,
            "crypto_coin": self.crypto_coin_id,
            "selling": self.selling,
            "state": self.state.value,
            "alt_starting_balance": self.alt_starting_balance,
            "alt_trade_amount": self.alt_trade_amount,
            "crypto_starting_balance": self.crypto_starting_balance,
            "crypto_trade_amount": self.crypto_trade_amount,
            "dt": self.dt.isoformat(),
        }
