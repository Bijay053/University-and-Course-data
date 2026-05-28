"""Country → currency mapping.

Single source of truth for the canonical currency a university's fees
should be quoted in. Used by API response paths to override any stray
currency token (e.g. "GBP" / "£") that the fee extractor picked up from
a noisy fee-page context window — when a university is in Australia the
fee MUST display as AUD, regardless of what the page text said.

Apply via :func:`coerce_currency_for_country` at every response layer
that surfaces a fee.currency value. Do NOT mutate the underlying DB row;
this is a presentation-layer normaliser only, so re-scrapes can still
record the true detected token for diagnostics.
"""
from __future__ import annotations

# Lower-cased country name → ISO 4217 currency code.
# Add new entries as universities from new countries are onboarded.
COUNTRY_CURRENCY: dict[str, str] = {
    "australia": "AUD",
    "new zealand": "NZD",
    "united kingdom": "GBP",
    "uk": "GBP",
    "england": "GBP",
    "scotland": "GBP",
    "wales": "GBP",
    "northern ireland": "GBP",
    "united states": "USD",
    "usa": "USD",
    "us": "USD",
    "canada": "CAD",
    "singapore": "SGD",
    "ireland": "EUR",
    "germany": "EUR",
    "netherlands": "EUR",
    "france": "EUR",
    "spain": "EUR",
    "italy": "EUR",
    "india": "INR",
    "malaysia": "MYR",
    "uae": "AED",
    "united arab emirates": "AED",
}


def currency_for_country(country: str | None) -> str | None:
    """Return the canonical currency for *country*, or ``None`` if unknown.

    Match is case-insensitive and trims whitespace. ``None`` / empty
    string both return ``None`` so the caller can leave the value
    untouched (defensive — never invent a currency for a uni with no
    country recorded).
    """
    if not country:
        return None
    return COUNTRY_CURRENCY.get(country.strip().lower())


def coerce_currency_for_country(
    detected: str | None, country: str | None
) -> str | None:
    """Override *detected* currency with the country's canonical currency.

    Behaviour:
      - Country unknown (not in :data:`COUNTRY_CURRENCY`) → return
        *detected* unchanged so we never lose data.
      - Country known → return the country's currency, even if
        *detected* was non-null. This is the whole point: an Australian
        uni page that mistakenly produced "GBP" must surface as "AUD".
      - If *detected* is null and country is known → fill in the
        country's currency so the column is never blank for a uni with
        a known country.
    """
    canon = currency_for_country(country)
    if canon is None:
        return detected
    return canon
