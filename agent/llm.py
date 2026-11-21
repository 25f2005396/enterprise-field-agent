# agent/llm.py
"""Single seam for the LLM so tests can replace it with a scripted planner."""
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from api.config import settings


def invoke_llm(system_prompt: str, user_prompt: str) -> str:
    llm = ChatGroq(model=settings.LLM_MODEL, api_key=settings.GROQ_API_KEY, temperature=0.0,
                   max_retries=2, timeout=settings.LLM_TIMEOUT_SECONDS)
    llm = llm.bind(response_format={"type": "json_object"})  # Groq JSON mode
    res = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
    return res.content if isinstance(res.content, str) else str(res.content)
