"""Multi-user system + dynamic per-user permissions + password reset tokens.

Three tables:

  * ``users``                  -- replaces hardcoded admin credential
  * ``user_permissions``       -- one row per (user, granted permission_key)
  * ``password_reset_tokens``  -- single-use tokens for /reset-password

PROD APPLY (alembic does not work in this env -- run the apply script):

    cd /root/University-and-Course-data &&
    PYTHONPATH=backend-py python3 backend-py/scripts/apply_migration_019.py
"""
from __future__ import annotations

from alembic import op


revision = "019_user_permissions"
down_revision = "018_cricos_coverage_view"
branch_labels = None
depends_on = None


_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    email           TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    full_name       TEXT NOT NULL DEFAULT '',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    is_super_admin  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx ON users (LOWER(email));
"""

_USER_PERMISSIONS_SQL = """
CREATE TABLE IF NOT EXISTS user_permissions (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission_key  TEXT NOT NULL,
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, permission_key)
);
CREATE INDEX IF NOT EXISTS user_permissions_user_id_idx ON user_permissions(user_id);
"""

_RESET_TOKENS_SQL = """
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS password_reset_user_idx ON password_reset_tokens(user_id);
"""


def upgrade() -> None:
    op.execute(_USERS_SQL)
    op.execute(_USER_PERMISSIONS_SQL)
    op.execute(_RESET_TOKENS_SQL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS password_reset_tokens;")
    op.execute("DROP TABLE IF EXISTS user_permissions;")
    op.execute("DROP TABLE IF EXISTS users;")
