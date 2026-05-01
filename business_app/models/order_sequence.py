"""
Order Sequence model for generating sequential order numbers
Format: {PREFIX}{SEQUENCE}_{YY}
Example: TG000042_26 (Telegram order #42 of 2026)
"""

from sqlalchemy import Column, Integer, String, UniqueConstraint, Index
from business_app import db
from business_app.models.base import TimestampMixin


class OrderSequence(db.Model, TimestampMixin):
    """
    Tracks order number sequences per source prefix and year.
    Sequences reset annually on January 1st.
    """

    __tablename__ = "order_sequences"

    id = Column(Integer, primary_key=True)
    source_prefix = Column(String(2), nullable=False)
    year = Column(Integer, nullable=False)
    current_sequence = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("source_prefix", "year", name="uq_order_sequences_prefix_year"),
        Index("idx_order_sequences_prefix_year", "source_prefix", "year"),
    )

    def __repr__(self):
        return f"<OrderSequence {self.source_prefix}-{self.year}: {self.current_sequence}>"
