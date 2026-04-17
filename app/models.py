from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String

from app.database import Base


class CustomerOrder(Base):
    __tablename__ = "customer_orders"

    id = Column(Integer, primary_key=True, index=True)
    reference_code = Column(String, nullable=True, index=True)

    customer_name = Column(String, nullable=False)
    phone = Column(String, nullable=False, index=True)
    grain_type = Column(String, nullable=False)
    weight_kg = Column(Float, nullable=False)
    price_per_kg = Column(Float, nullable=False)
    total_price = Column(Float, nullable=False)

    status = Column(String, default="booked", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    

    