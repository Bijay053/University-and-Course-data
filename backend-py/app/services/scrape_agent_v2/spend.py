"""Global Sydney-day USD cap with PostgreSQL-serialized reservations across workers."""
import json
import uuid
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from decimal import Decimal,ROUND_UP
from sqlalchemy import select,func,text
from app.models.agent_spend import AgentSpend


class SpendLimit(ValueError):
    pass


def budget_day(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("Australia/Sydney")).date()


def rates(settings):
    values=[Decimal(str(v)) for v in (
        settings.scrape_agent_v2_daily_usd,settings.scrape_agent_v2_input_usd_per_million,
        settings.scrape_agent_v2_output_usd_per_million)]
    if any(not v.is_finite() or v<=0 for v in values):
        raise SpendLimit("Agent stopped: a positive daily USD cap and verified model prices must be configured.")
    return values


def estimate(input_tokens,output_tokens,price_in,price_out):
    # Conservatively charge every input token at cache-write price; cached-read
    # discounts are deliberately not credited. >272K has whole-request premiums.
    long=input_tokens>272000
    price_in*=Decimal("1.25")*(2 if long else 1)
    price_out*=Decimal("1.5") if long else 1
    return ((Decimal(input_tokens)*price_in+Decimal(output_tokens)*price_out)/1000000).quantize(Decimal(".00000001"),rounding=ROUND_UP)


async def reserve(factory,settings,thread_id,model,payload):
    if model!=settings.scrape_agent_v2_priced_model:
        raise SpendLimit("Agent stopped: this model has no verified pricing configuration.")
    cap,price_in,price_out=rates(settings)
    # UTF-8 byte count deliberately overestimates text tokens. Extra allowance
    # covers protocol overhead; opaque reasoning state is included in payload.
    upper_input=len(json.dumps(payload,ensure_ascii=False,default=str).encode("utf-8"))+8192
    amount=estimate(upper_input,4096,price_in,price_out)
    day=budget_day()
    receipt=uuid.uuid4().hex
    async with factory() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(807126418)"))
        unsafe=(await db.execute(select(AgentSpend.id).where(
            AgentSpend.status=="over_reservation").limit(1))).scalar_one_or_none()
        if unsafe:
            raise SpendLimit("Agent stopped: a prior cost-bound overrun needs accounting review before more calls.")
        # Derive the local date from timestamps so pre-switch UTC ledger rows
        # cannot create a second allowance during the timezone transition.
        local_day=func.date(func.timezone("Australia/Sydney",AgentSpend.created_at))
        used=(await db.execute(select(func.coalesce(func.sum(AgentSpend.charged_usd),0)).where(local_day==day))).scalar_one()
        if used+amount>cap:
            raise SpendLimit(f"Agent stopped: daily USD budget reached. Reserved/spent ${used:.4f}; next call reserves ${amount:.4f}; daily cap ${cap:.2f}.")
        db.add(AgentSpend(id=receipt,day=day,thread_id=thread_id,model=model,
            reserved_usd=amount,charged_usd=amount,status="reserved"))
        await db.commit()
    return receipt,(price_in,price_out)


async def settle(factory,receipt,prices,usage):
    # Missing usage, transport failures, and process crashes retain the full
    # reservation. Never free possibly billed spend merely because a call failed.
    if not usage:
        return
    actual=estimate(usage.input_tokens,usage.output_tokens,*prices)
    async with factory() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(807126418)"))
        row=(await db.execute(select(AgentSpend).where(AgentSpend.id==receipt).with_for_update())).scalar_one()
        row.charged_usd=actual
        row.status="settled" if actual<=row.reserved_usd else "over_reservation"
        await db.commit()
        if actual>row.reserved_usd:
            raise SpendLimit("Agent stopped: model usage exceeded the reserved cost bound; pricing/accounting needs review.")
