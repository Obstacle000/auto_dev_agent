"""ORM 模型基类."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """全局 SQLAlchemy 声明式基类."""
