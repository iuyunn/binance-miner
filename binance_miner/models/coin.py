# https://docs.sqlalchemy.org/en/20/orm/extensions/mypy.html
# mypy: disable-error-code=assignment
from sqlalchemy import Boolean, Column, String

from .base import Base


class Coin(Base):
    __tablename__ = "coins"
    symbol = Column(String, primary_key=True)
    enabled = Column(Boolean)

    def __init__(self, symbol, enabled: bool = True):
        self.symbol = symbol
        self.enabled = enabled

    def __add__(self, other):
        if isinstance(other, str):
            return self.symbol + other
        if isinstance(other, __class__):
            return self.symbol + other.symbol
        raise TypeError(f"unsupported operand type(s) for +: 'Coin' and '{type(other)}'")

    def __repr__(self):
        return f"<{self.symbol}>"

    def info(self):
        return {"symbol": self.symbol, "enabled": self.enabled}
