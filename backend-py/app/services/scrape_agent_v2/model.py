"""Responses protocol adapter; preserve opaque reasoning state, never display it."""
from app.services.ai.openai_client import _MODEL


def response_input(messages):
    items=[]
    for message in messages:
        if message.get("_response_items") is not None:
            items.extend(message["_response_items"])
        elif message["role"]=="tool":
            items.append({"type":"function_call_output","call_id":message["tool_call_id"],
                          "output":message["content"]})
        else:
            if message.get("content"):
                items.append({"role":message["role"],"content":message["content"]})
            for call in message.get("tool_calls") or []:
                items.append({"type":"function_call","call_id":call["id"],
                              "name":call["function"]["name"],"arguments":call["function"]["arguments"]})
    return items


async def complete(client,messages,schemas,*,spend_context=None):
    if spend_context is None:
        raise ValueError("A durable dollar-budget context is required before calling the model")
    from .spend import reserve,settle
    factory,settings,thread_id=spend_context
    receipt,prices=await reserve(factory,settings,thread_id,_MODEL,{"messages":messages,"schemas":schemas})
    client=client.with_options(max_retries=0)
    response=await client.responses.create(
        model=_MODEL,input=response_input(messages),
        tools=[{"type":"function",**schema["function"],"strict":False} for schema in schemas],
        parallel_tool_calls=False,max_output_tokens=4096,service_tier="default",
        reasoning={"effort":"low"},store=False,include=["reasoning.encrypted_content"],
        timeout=45,
    )
    await settle(factory,receipt,prices,response.usage)
    if response.status!="completed":
        raise ValueError("Model response did not complete; no partial tool calls will run")
    message={"role":"assistant","content":response.output_text,
             "_response_items":[item.model_dump(exclude_none=True) for item in response.output]}
    calls=[{"id":item.call_id,"type":"function","function":{"name":item.name,"arguments":item.arguments}}
           for item in response.output if item.type=="function_call"]
    if calls:
        message["tool_calls"]=calls
    return {"message":message,"tokens":response.usage.total_tokens if response.usage else 32000}
