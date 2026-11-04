"""Private, durable prompt conversations; separate from scrape request/config evidence."""
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScrapeAssistantThread(Base):
    __tablename__ = "scrape_assistant_threads"
    __table_args__ = (UniqueConstraint("job_id", "actor_id", name="uq_scrape_assistant_owner"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    job_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    actor_id: Mapped[int] = mapped_column(Integer, nullable=False)
    turn_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="idle")
    phase: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error: Mapped[str | None] = mapped_column(Text)
    messages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    activity: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    run_id: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
