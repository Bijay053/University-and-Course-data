"""Durable conservative model-cost reservations; unresolved calls stay charged."""
from datetime import datetime,date
from decimal import Decimal
from sqlalchemy import Date,DateTime,Numeric,Text,func
from sqlalchemy.orm import Mapped,mapped_column
from app.database import Base


class AgentSpend(Base):
    __tablename__="scrape_agent_spend"
    id:Mapped[str]=mapped_column(Text,primary_key=True)
    day:Mapped[date]=mapped_column(Date,index=True)
    thread_id:Mapped[str]=mapped_column(Text)
    model:Mapped[str]=mapped_column(Text)
    reserved_usd:Mapped[Decimal]=mapped_column(Numeric(18,8))
    charged_usd:Mapped[Decimal]=mapped_column(Numeric(18,8))
    status:Mapped[str]=mapped_column(Text,default="reserved")
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=func.now())
