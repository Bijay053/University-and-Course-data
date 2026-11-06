from decimal import Decimal
from types import SimpleNamespace
import pytest
from app.services.scrape_agent_v2.access import enabled_for
from app.services.scrape_agent_v2.spend import rates,estimate,SpendLimit


@pytest.mark.parametrize("instant,expected",[
    ("2026-11-05T12:59:59+00:00","2026-11-05"),
    ("2026-11-05T13:00:00+00:00","2026-11-06"),
    ("2026-06-05T13:59:59+00:00","2026-06-05"),
    ("2026-06-05T14:00:00+00:00","2026-06-06"),
    ("2026-10-03T16:00:00+00:00","2026-10-04"),
])
def test_sydney_budget_midnight_and_dst(instant,expected):
    from datetime import datetime
    from app.services.scrape_agent_v2.spend import budget_day
    assert str(budget_day(datetime.fromisoformat(instant)))==expected


def test_master_switch_does_not_enable_team():
    settings=SimpleNamespace(scrape_agent_v2_enabled=True,scrape_agent_v2_user_ids="77")
    assert enabled_for(settings,77)
    assert not enabled_for(settings,78)
    settings.scrape_agent_v2_user_ids=""
    assert not enabled_for(settings,77)
    settings.scrape_agent_v2_user_ids="77"
    settings.scrape_agent_v2_enabled=False
    assert not enabled_for(settings,77)


@pytest.mark.parametrize("value",[0,-1,float("nan"),float("inf")])
def test_unconfigured_or_invalid_prices_fail_closed(value):
    with pytest.raises(SpendLimit):
        rates(SimpleNamespace(scrape_agent_v2_daily_usd=100,
            scrape_agent_v2_input_usd_per_million=value,scrape_agent_v2_output_usd_per_million=10))


def test_cost_is_dollars_not_tokens():
    assert estimate(1000000,1000000,Decimal("2"),Decimal("8"))==Decimal("17")
