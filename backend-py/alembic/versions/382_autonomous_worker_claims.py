"""Durable autonomous execution generations, additive for existing databases."""
from alembic import op

revision = "382_worker_claims"
down_revision = "381_acad_req_option"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS autonomous_worker_claims (
            claim_key TEXT PRIMARY KEY,
            generation TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('active', 'stopped', 'revoked')),
            task_id TEXT NOT NULL,
            process_identity TEXT NOT NULL,
            proof TEXT,
            claimed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_autonomous_worker_claims_task
        ON autonomous_worker_claims (task_id, process_identity)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS autonomous_worker_budgets (
            claim_key TEXT PRIMARY KEY REFERENCES autonomous_worker_claims(claim_key),
            live_pages INTEGER NOT NULL DEFAULT 0,
            live_seconds DOUBLE PRECISION NOT NULL DEFAULT 0
        )
    """)


def downgrade():
    op.drop_table("autonomous_worker_budgets")
    op.drop_table("autonomous_worker_claims")