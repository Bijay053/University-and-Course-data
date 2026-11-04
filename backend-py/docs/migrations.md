# Supported migration launcher

Run from `backend-py` with `PYTHONPATH=. python -m alembic upgrade <reviewed-revision>`.
Use only the separately approved migration target; this does not authorize an
upgrade of a production database to head.

The launcher loads `/etc/university-portal/database.env` before creating settings.
Its database values override inherited environment and `.env` values. Managed
configuration must explicitly require TLS. Missing or unreadable configuration
on a provisioned host fails closed. On unmanaged development hosts (without that
directory), the normal environment/`.env` configuration and non-TLS default remain
available.

Both application and migration engines use the shared certificate-verifying TLS
configuration. `SSL_CERT_FILE`, when set with TLS enabled, must name a valid CA
bundle; a missing or invalid bundle is an error, not a reason to disable
verification. URL normalization removes libpq-only options; `sslmode` in a URL
alone does not enable TLS. Set `DATABASE_REQUIRE_TLS=true` outside managed hosts
when TLS is required.

For a fenced transaction, set `config.attributes["connection"]` to an already
opened synchronous SQLAlchemy connection before calling Alembic. With an async
connection, invoke that code through `await connection.run_sync(...)`. Alembic
uses the supplied connection without opening another engine or committing,
rolling back, or closing the enclosing transaction. The caller must create that
connection with the shared TLS configuration and owns its transaction lifecycle.
Do not pass an AsyncConnection directly or log database URLs/configuration.

Offline SQL generation uses the normalized URL without opening a connection.
