from pydantic import BaseModel


class PublishedFeeSchedule(BaseModel):
    id: int
    feeYear: int
    entryYear: int
    amount: float
    currency: str
    basis: str
    intakes: list[str]
    sourceUrls: list[str]
