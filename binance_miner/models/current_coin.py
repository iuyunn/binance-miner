# https://docs.sqlalchemy.org/en/20/orm/extensions/mypy.html
# mypy: disable-error-code=assignment
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from .base import Base
from .coin import Coin


class CurrentCoin(Base):
    __tablename__ = "current_coin_history"
    id = Column(Integer, primary_key=True)
    coin_id = Column(String, ForeignKey("coins.symbol"))
    coin = relationship("Coin")
    dt = Column(DateTime)

    def __init__(self, coin: Coin):
        self.coin = coin
        self.dt = datetime.utcnow()

    def info(self):
        return {"dt": self.dt.isoformat(), "coin": self.coin.info()}
