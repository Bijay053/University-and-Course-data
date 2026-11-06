"""Tool-calling runtime. Policy lives in tools, never in model compliance."""
import json


class AgentBudgetExceeded(RuntimeError):
    pass


async def run_loop(messages, tools, complete, checkpoint, *, max_rounds=24, max_tokens=32000, initial_tokens=0):
    """Persist each tool receipt before asking the model what to do next."""
    history = list(messages)
    tokens = initial_tokens
    receipts = {}
    required=set()
    definitions={}
    for message in history:
        for call in message.get("tool_calls") or []:
            definitions[call["id"]]=json.dumps(call["function"],sort_keys=True)
        if message["role"]=="tool":
            receipts[message["tool_call_id"]]=(definitions[message["tool_call_id"]],json.loads(message["content"]))
            result=json.loads(message["content"])
            if result.get("ok"):
                required.discard(json.loads(definitions[message["tool_call_id"]])["name"])
            required.update(result.get("requires_tools",[]))
    async def execute_calls(calls):
        if tokens > max_tokens:
            raise AgentBudgetExceeded("Model token limit reached; no further tools executed")
        for call in calls:
            identity = call["id"]
            fingerprint = json.dumps(call["function"], sort_keys=True)
            if identity in receipts:
                old_fingerprint, result = receipts[identity]
                if old_fingerprint != fingerprint:
                    raise ValueError("Tool-call identity was reused with different arguments")
            else:
                try:
                    arguments = json.loads(call["function"]["arguments"])
                    result = await tools.execute(call["function"]["name"], arguments, identity)
                except (ValueError, TypeError, KeyError) as exc:
                    result = {"ok": False, "error": str(exc), "changed": False}
                receipts[identity] = (fingerprint, result)
            if result.get("ok"):
                required.discard(call["function"]["name"])
                required.update(result.get("requires_tools",[]))
            history.append({"role": "tool", "tool_call_id": identity,
                            "content": json.dumps(result, default=str)})
            await checkpoint(history, tokens)
    latest = next((m for m in reversed(history) if m["role"]=="assistant"),{})
    pending=[c for c in latest.get("tool_calls",[]) if c["id"] not in receipts]
    if pending:
        await execute_calls(pending)
    used_rounds=sum(bool(m.get("tool_calls")) for m in history if m["role"]=="assistant")
    for _ in range(max(0,max_rounds-used_rounds)):
        response = await complete(history, tools.schemas)
        tokens += response.get("tokens", 0)
        if tokens > max_tokens:
            raise AgentBudgetExceeded("Model token limit reached; no further tools executed")
        message = response["message"]
        history.append(message)
        calls = message.get("tool_calls") or []
        if not calls:
            if required:
                if any(not receipt[1].get("ok") for receipt in receipts.values()):
                    return {"answer":"Required follow-up work was blocked; no complete result is verified.",
                            "history":history,"tokens":tokens}
                history.append({"role":"system","content":"Completion verification failed: required follow-up tools have not succeeded: "+", ".join(sorted(required))+". Continue the requested work or explicitly report the block."})
                continue
            return {"answer": message.get("content") or "", "history": history, "tokens": tokens}
        await checkpoint(history,tokens)
        await execute_calls(calls)
    raise AgentBudgetExceeded("Tool-round limit reached; completed work is retained")
