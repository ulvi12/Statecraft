"""Statecraft: the class starter's Gemini harness, with session-scoped tools."""

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import google.auth
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from data_source import STATE_NAMES
from tools import TOOLS, StatecraftTools

SYSTEM_PROMPT = """You are Statecraft, an economic sandbox for creating imaginary countries from US states.
Help the user build named countries, change their borders, and compare their economies.
Use only tool results for quantitative claims. Never fabricate observations or calculations.
There are exactly three tools. Fetch observations, assemble countries, then compare as needed.
You may fetch all required states in one call before assembling multiple countries.
Tools automatically use the latest complete annual GDP, population, and industry year across all 50 states,
and keep that year consistent within this chat. Never ask users to select a year or invent the
latest year. Report the year returned by the tools. If asked about 2026 versus 2025, distinguish
full-year annual data from quarterly figures. Historical-year selection is not supported in this UI.
Remember country names and definitions across the conversation. For 'add Nevada' or 'remove Oregon',
pass the COMPLETE updated state list to assemble_country using the existing name.
For a rename, call assemble_country with previous_name set to the current country name,
name set to the new name, and its unchanged complete state list (unless borders also change).
This replaces the existing country; never create a second country to fulfill a rename.
Use the new name in subsequent tool calls. If the new name is already taken, ask for another.
If a name is not supplied, propose a short creative name and create the country; ask when the
state selection or intended country is ambiguous. Countries may overlap: report the overlap.
Tool errors are actionable: fix invalid inputs or fetch missing observations; if data is unavailable,
explain the issue and do not substitute invented numbers. Don't repeatedly retry an unavailable service.
Keep answers concise. Use names, year, nominal GDP, population, and GDP per person when useful.
Describe billions/trillions clearly. GDP per person measures economic output, NOT income, wealth,
or living standards. Summed state GDP does NOT predict economic outcomes after independence.
Explain that distinction briefly when relevant, not as a repeated disclaimer in every response.
Industry mix is supported: assemble_country returns 20 broad industry GDP totals and shares,
and compare_countries with metric="industry_mix" compares them across countries. For a single
existing country's industry mix, reuse its current name and complete states in assemble_country.
Report the largest sectors and explain differences simply; the interface displays the full table.
Compare industry shares in percentage points, not percent. Do not append a generic industry
note or disclaimer about employment or the scope of the Information sector; explain those
definitions only when the user asks about them.
If users ask for growth, real countries, capitals, flags, or independence forecasts,
explain the current data tools cover GDP, population, industry mix, state contributions and GDP per person.
You can suggest fictional names and narratives, clearly labeled as imagination.
Do not use tools for greetings or general explanations. User-facing answers should read naturally;
the interface already displays tool calls. Cite BEA as the data source when presenting new results.
"""
MAX_TOOL_ROUNDS = 8
SESSION_TTL_SECONDS = 4 * 3600
MODEL = os.getenv("GEMINI_MODEL", "vertex_ai/gemini-3.5-flash-lite")


@dataclass
class Session:
    messages: list = field(default_factory=lambda: [{"role": "system", "content": SYSTEM_PROMPT}])
    tools: StatecraftTools = field(default_factory=StatecraftTools)
    lock: threading.Lock = field(default_factory=threading.Lock)
    touched: float = field(default_factory=time.monotonic)


sessions: dict[str, Session] = {}
sessions_lock = threading.Lock()


def model_completion(messages, tool_choice=None):
    # LiteLLM may initialize tokenizer assets over the network on first import.
    # Keep offline tools/tests and the frontend independent of model startup.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("VERTEXAI_PROJECT")
    if not project:
        _, project = google.auth.default()
    options = {"model": MODEL, "vertex_location": os.getenv("VERTEXAI_LOCATION", "global"),
               "vertex_project": project, "messages": messages, "tools": TOOLS,
               "timeout": 90, "num_retries": 0}
    if tool_choice:
        options["tool_choice"] = tool_choice
    return litellm.completion(**options).choices[0].message


def run_agent(session: Session, tool_calls: list[dict]) -> str:
    """Keep assistant/tool messages paired; retain every call even on a later failure."""
    for _ in range(MAX_TOOL_ROUNDS):
        reply = model_completion(session.messages)
        session.messages.append(reply.model_dump(exclude_none=True))
        if not reply.tool_calls:
            return reply.content or "Please describe the states you want to combine or countries you want to compare."
        for call in reply.tool_calls:
            try:
                args = json.loads(call.function.arguments)
            except (ValueError, TypeError):
                args = {"unparsed_arguments": call.function.arguments}
                result = json.dumps({"error": "invalid_json", "message": "Send arguments as a valid JSON object and try again."})
            else:
                result = session.tools.run_tool(call.function.name, args)
            tool_calls.append({"name": call.function.name, "args": args, "result": result})
            session.messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    # A final answer must not leave a successful chain without a conclusion.
    reply = model_completion(session.messages, tool_choice="none")
    if reply.tool_calls:
        return "I reached the tool limit. Your completed country results are shown below; ask a narrower follow-up to continue."
    session.messages.append(reply.model_dump(exclude_none=True))
    return reply.content or "Your completed tool results are shown below. Ask a follow-up to continue."


app = FastAPI(title="Statecraft")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: uuid.UUID | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


class ClearRequest(BaseModel):
    session_id: uuid.UUID


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/us-map.svg")
def us_map():
    return FileResponse(Path(__file__).parent / "assets" / "us-map.svg", media_type="image/svg+xml")


@app.get("/health")
def health():
    return {"status": "ok", "agent": "Statecraft"}


@app.get("/states")
def states():
    return [{"code": code, "name": name} for code, name in STATE_NAMES.items()]


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    text = request.message.strip()
    if not text:
        raise HTTPException(422, "Enter a message first.")
    with sessions_lock:
        for key, old in list(sessions.items()):
            if time.monotonic() - old.touched > SESSION_TTL_SECONDS and not old.lock.locked():
                del sessions[key]
        requested_id = str(request.session_id) if request.session_id else None
        session_id = requested_id if requested_id in sessions else str(uuid.uuid4())
        if session_id not in sessions:
            if len(sessions) >= 100:
                raise HTTPException(503, "The sandbox is full. Try again later.")
            sessions[session_id] = Session()
        session = sessions[session_id]
        session.touched = time.monotonic()
    calls = []
    with session.lock:
        if len(session.messages) > 160:
            return ChatResponse(response="This chat has reached its history limit. Start a new world to continue.", session_id=session_id, tool_calls=[])
        # Work on a copy of the conversation. Failed model calls should not poison history.
        previous = list(session.messages)
        session.messages.append({"role": "user", "content": text})
        try:
            response = run_agent(session, calls)
        except Exception as exc:
            logging.warning("Model call failed (%s)", type(exc).__name__)
            # Completed tool calls may have changed countries; preserve their paired history.
            if not calls:
                session.messages = previous
            response = ("Gemini could not finish this request. Check Google Cloud authentication, project billing, "
                        "and model access, then retry. Completed tool results, if any, are still shown.")
        session.touched = time.monotonic()
    if requested_id and requested_id != session_id:
        response = "Your previous chat expired or the server restarted, so this is a new world. " + response
    return ChatResponse(response=response, session_id=session_id, tool_calls=calls)


@app.post("/clear")
def clear(request: ClearRequest):
    with sessions_lock:
        session = sessions.get(str(request.session_id))
        if session and session.lock.locked():
            raise HTTPException(409, "Wait for the current response before starting a new world.")
        sessions.pop(str(request.session_id), None)
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
