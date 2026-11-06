"""Opt-in, bounded provider-contract probe; no database writes or web tools.

This is a development protocol check, not proof that the production USD cap
or full agent acceptance suite has passed. Pricing remains a separate gate.
"""
import asyncio,json
from pathlib import Path
from app.services.ai.openai_client import _client,_MODEL


async def main():
    client=_client().with_options(max_retries=0)
    try:
        response=await client.responses.create(
            model=_MODEL,store=False,max_output_tokens=512,reasoning={"effort":"low"},
            input=[{"role":"system","content":"Use the database tool for pending-count questions. Never fetch websites for database status."},
                   {"role":"user","content":"Why does this review show 1 pending row while the provider has 48? Check the counts."}],
            tools=[{"type":"function","name":"review_counts","description":"Read job and provider pending-row counts from the database.",
                    "parameters":{"type":"object","properties":{},"additionalProperties":False},"strict":True}],
            parallel_tool_calls=False,timeout=45)
        calls=[item for item in response.output if item.type=="function_call"]
        passed=response.status=="completed" and len(calls)==1 and calls[0].name=="review_counts"
        result={"passed":passed,"kind":"real-provider protocol smoke only","databaseWrites":0,
                "model":response.model,"usage":response.usage.model_dump() if response.usage else None,
                "pricingVerified":False,"fullAgentAcceptance":False}
        path=Path(__file__).resolve().parents[2]/"reports/agent-real-model-counts-smoke.json"
        path.write_text(json.dumps(result,indent=2))
        print(json.dumps(result,indent=2))
        if not passed:raise SystemExit(1)
    finally:
        await client.close()


if __name__=="__main__":
    asyncio.run(main())
