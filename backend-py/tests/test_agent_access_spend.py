from decimal import Decimal
from types import SimpleNamespace
import pytest
from app.services.scrape_agent_v2.access import enabled_for
from app.services.scrape_agent_v2.spend import rates,estimate,SpendLimit


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
