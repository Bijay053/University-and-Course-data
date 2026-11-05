from decimal import Decimal
import pytest
from pydantic import ValidationError
from app.services.scrape_agent_v2.contracts import Fee, FieldValue, Duration


@pytest.mark.parametrize("data,annual", [
    ({"amount":50000,"basis":"full_course","duration_years":3},Decimal(50000)/3),
    ({"amount":1450,"basis":"per_credit"},None),
    ({"amount":1450,"basis":"per_credit","standard_load":24},Decimal(34800)),
    ({"amount":10000,"basis":"per_semester"},None),
    ({"amount":10000,"basis":"per_semester","terms_per_year":2},Decimal(20000)),
    ({"amount":0,"basis":"annual"},Decimal(0)),
])
def test_fee_conversion_never_assumes_load(data,annual):
    fee=Fee(**data,currency="AUD")
    assert fee.annual_equivalent()==annual
    assert fee.amount==Decimal(data["amount"])


def test_missing_currency_evidence_and_invalid_range_fail_closed():
    with pytest.raises(ValidationError):
        Fee(amount=100,basis="annual")
    with pytest.raises(ValidationError):
        FieldValue(value=6.5,status="verified")
    with pytest.raises(ValidationError):
        FieldValue(value=6.5,status="conflict")
    with pytest.raises(ValidationError):
        Duration(minimum_years=2,maximum_years=1)
