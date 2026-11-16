# Publishing intake-specific tuition

An intake entry year is not its tuition year. For example, January and April
2027 may use the 2026 tuition schedule while September 2027 uses the 2027
schedule. Equal amounts do not make those schedules interchangeable.

## Approval

Manual approval through the existing review buttons can publish a consolidated
pending course with an intact intake-fee manifest. Automatic workers and history
restoration do not gain this permission. Excluded merge-source rows remain
blocked from separate approval. Approval does not newly verify source evidence.

The transaction writes a fee row per schedule, its entry-year/month association,
dated intake rows, and an audit record containing the retained source snapshot.
It creates one course, not one course per intake. A failed transaction/savepoint
cannot leave a partially approved schedule, and repeated approvals are idempotent.

The initial merge contract supports identical amounts, currency and basis across
different fee years. Conflicting prices or mixed campus/intake scopes still need
a more specific review; they must not be flattened to bypass validation.

## Reads and edits

Course lists/detail, fee resources, public search and comparison return the
schedule mappings. Unqualified scalar tuition fields are suppressed for mapped
courses. Search price/currency filters remain valid because this contract
requires equal prices across all schedules. Missing associations or mismatched
intakes produce a visible review requirement, never a scalar fallback.

Legacy fee and intake editors, published assistant field edits, and ordinary
re-approvals cannot silently remove these relationships. An explicitly reviewed
schedule-aware approval is required to replace them.

## Release prerequisites

Use the existing Alembic workflow to provision revision
`396_fee_intake_applicability` after reviewing prerequisites on the actual target.
Do not use Drizzle push: that schema is only a partial view of this database.
The additive table does not rewrite legacy fees. Until it is installed, schedule
approval fails with an actionable error; legacy fee reads remain available.

Deploy the matching API, worker and frontend code together before allowing any
schedule approval. Once mappings have been published, do not roll back to an API,
worker or frontend that only understands scalar fees. The database migration's
downgrade refuses to discard non-empty mappings.

Building or deploying this support does not itself approve any course.
