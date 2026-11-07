"""Create only the access registry on verified local development PostgreSQL."""
import asyncio
import ipaddress
import os
import socket
from app.legacy_migration_database import engine
from app.models.source_access import UniversitySourceAccess


async def main():
    url = engine.url
    addresses = {r[4][0] for r in socket.getaddrinfo(url.host, url.port or 5432)}
    local = addresses and all(ipaddress.ip_address(a).is_loopback for a in addresses)
    replit = (url.host == "helium" and bool(os.environ.get("REPL_ID")) and addresses
              and all(ipaddress.ip_address(a).is_private for a in addresses))
    if url.get_backend_name() != "postgresql" or not (local or replit):
        raise SystemExit("Refusing non-local database migration.")
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: UniversitySourceAccess.__table__.create(c, checkfirst=True))
    await engine.dispose()
    print("Local source registry table ready; no course data or migration history changed.")


if __name__ == "__main__":
    asyncio.run(main())
