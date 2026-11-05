# Model pricing basis

Source: https://developers.openai.com/api/docs/models/gpt-5.6-terra

Retrieved 2026-11-05. Standard text input: USD 2 / million tokens.
Output: USD 12 / million tokens. Cached input: USD 0.20 / million.
Over 272,000 input tokens: whole-request input 2x and output 1.5x.
Cache writes: 1.25x uncached input price.

The application deliberately budgets all input at cache-write price and does not
credit cached-read discounts. Reservations use a conservative input-byte bound
plus protocol allowance and the maximum permitted output tokens. This is a
conservative spend ledger, not an invoice reconciliation report.

The daily cap is USD 100 per deployment, across new-agent users/workers, by UTC
reservation day. API retries are disabled. Unknown usage keeps its reservation.
Other tools/provider calls must also be covered before production activation;
this model ledger alone does not certify the entire application cost boundary.
