"""Create the agent spend ledger using the configured database connection."""
import asyncio
from app.database import engine
from app.models.agent_spend import AgentSpend


async def main():
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync:AgentSpend.__table__.create(sync,checkfirst=True))
    await engine.dispose()
    print("Agent spend schema ready")


if __name__=="__main__":
    asyncio.run(main())
