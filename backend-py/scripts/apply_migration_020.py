"""Migration 020 — Roles system.

Creates:
  - roles (id, name, description, created_at, updated_at)
  - role_permissions (role_id, permission_key)
  - users.role_id FK (nullable)

Apply on prod:
  cd /root/University-and-Course-data && PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_020.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import Base, engine


async def main() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS roles (
                id          SERIAL PRIMARY KEY,
                name        VARCHAR NOT NULL UNIQUE,
                description VARCHAR NOT NULL DEFAULT '',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS role_permissions (
                role_id        INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                permission_key VARCHAR NOT NULL,
                PRIMARY KEY (role_id, permission_key)
            );
        """))
        await conn.execute(text("""
            ALTER TABLE users
                ADD COLUMN IF NOT EXISTS role_id INTEGER REFERENCES roles(id) ON DELETE SET NULL;
        """))
    print("Migration 020 applied successfully.")


if __name__ == "__main__":
    asyncio.run(main())
