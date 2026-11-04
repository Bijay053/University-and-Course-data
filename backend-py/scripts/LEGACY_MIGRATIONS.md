# Narrow legacy database repairs

Normal upgrades use the supported Alembic launcher (`PYTHONPATH=. alembic
upgrade head` from `backend-py`) through the guarded release procedure. Historical
inline `psql`, local-password and “Alembic does not work” examples in these old
scripts are not production deployment instructions.

The `apply_migration_*.py` programs are retained for explicitly selected repairs.
They load the host-managed database environment before building their connection
and use the application's certificate- and hostname-verifying TLS policy. Missing
managed configuration or an invalid configured CA bundle stops execution; there
is no insecure retry. An unmanaged development host retains the application's
development TLS setting.

Use `PYTHONPATH=. python scripts/apply_migration_<revision>.py` from `backend-py`
only when that exact repair has been approved. These entrypoints are not wrappers
for a full upgrade, do not run intervening migrations, and do not stamp the head.
The two `_local` repairs retain their remote-host rejection and their existing
conditional predecessor-only ledger updates.

The former `psql` entrypoint now keeps its same fixed DDL in one transaction
without putting a database URL in a process argument. The simple SQL blocks in
032/033 execute one statement at a time, as required by asyncpg, within a single
transaction. Neither script retries after a statement failure.
