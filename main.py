import os
from contextlib import asynccontextmanager

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

DATABASE_URL = os.environ.get("DATABASE_URL")

# ---- Free AI provider: Groq if GROQ_API_KEY is set, else OpenRouter ----
# The app tries the models in order until one works.
if os.environ.get("GROQ_API_KEY"):
    LLM_URL = "https://api.groq.com/openai/v1/chat/completions"
    LLM_KEY = os.environ["GROQ_API_KEY"]
    LLM_MODELS = ["llama-3.1-8b-instant", "openai/gpt-oss-20b", "openai/gpt-oss-120b"]
elif os.environ.get("OPENROUTER_API_KEY"):
    LLM_URL = "https://openrouter.ai/api/v1/chat/completions"
    LLM_KEY = os.environ["OPENROUTER_API_KEY"]
    LLM_MODELS = ["openrouter/free"]
else:
    LLM_URL = LLM_KEY = None
    LLM_MODELS = []

SYSTEM_PROMPT = (
    "You are a friendly customer support assistant. Answer clearly and briefly. "
    "Use the earlier messages for context. If you don't know something, say so "
    "and offer to connect the customer with a human agent."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS conversations (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def get_conn():
    return psycopg.connect(DATABASE_URL)


@asynccontextmanager
async def lifespan(app):
    if DATABASE_URL:
        with get_conn() as conn:
            conn.execute(SCHEMA)
    yield


app = FastAPI(lifespan=lifespan)


class Msg(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[Msg] = []


@app.get("/")
def home():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/db-check")
def db_check():
    if not DATABASE_URL:
        return {"database": "DATABASE_URL is not set"}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        ).fetchall()
    return {"database": "connected", "tables": [r[0] for r in rows]}


@app.post("/chat")
def chat(req: ChatRequest):
    if not LLM_KEY:
        raise HTTPException(500, "No API key set. Add GROQ_API_KEY or OPENROUTER_API_KEY.")

    messages = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + [m.model_dump() for m in req.history]
        + [{"role": "user", "content": req.message}]
    )

    last_error = ""
    for model in LLM_MODELS:
        try:
            r = httpx.post(
                LLM_URL,
                headers={"Authorization": f"Bearer {LLM_KEY}"},
                json={"model": model, "messages": messages},
                timeout=60,
            )
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Could not reach the AI service: {e}")
        if r.status_code == 200:
            reply = r.json()["choices"][0]["message"]["content"]
            return {"reply": reply, "model": model}
        last_error = r.text[:300]
        if "model" not in last_error.lower():
            break  # not a model problem, so trying another model won't help

    raise HTTPException(502, f"AI service error: {last_error}")


@app.get("/chat-test")
def chat_test(message: str = "Hello"):
    return chat(ChatRequest(message=message))
