import hashlib
import hmac
import os
import secrets
from contextlib import asynccontextmanager

import httpx
import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

DATABASE_URL = os.environ.get("DATABASE_URL")

# ---- Free AI provider: Groq if GROQ_API_KEY is set, else OpenRouter ----
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
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
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


# ---------------- Passwords and sessions ----------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + "$" + digest.hex()


def check_password(password: str, stored: str) -> bool:
    salt_hex, digest_hex = stored.split("$")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
    return hmac.compare_digest(digest.hex(), digest_hex)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(conn, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id) VALUES (%s, %s)",
        (token_hash(token), user_id),
    )
    return token


def current_user(authorization: str | None = Header(default=None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Please log in.")
    token = authorization[7:]
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM sessions WHERE token_hash = %s "
            "AND created_at > now() - interval '7 days'",
            (token_hash(token),),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Session expired. Please log in again.")
    return row[0]


# ---------------- AI call ----------------
def ask_ai(history: list, message: str):
    if not LLM_KEY:
        raise HTTPException(500, "No API key set. Add GROQ_API_KEY or OPENROUTER_API_KEY.")

    messages = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + history
        + [{"role": "user", "content": message}]
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
            content = r.json()["choices"][0]["message"].get("content") or ""
            reply = content.strip() or "Sorry, I couldn't come up with an answer. Please try again."
            return reply, model
        last_error = r.text[:300]
        if "model" not in last_error.lower():
            break  # not a model problem, so trying another model won't help
    raise HTTPException(502, f"AI service error: {last_error}")


# ---------------- Request shapes ----------------
class Credentials(BaseModel):
    email: str
    password: str


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = None


# ---------------- Pages and checks ----------------
@app.get("/")
def home():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/db-check")
def db_check():
    # Testing only. Delete this endpoint before your final demo.
    if not DATABASE_URL:
        return {"database": "DATABASE_URL is not set"}
    with get_conn() as conn:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' ORDER BY table_name"
            ).fetchall()
        ]
        counts = {n: conn.execute(f"SELECT count(*) FROM {n}").fetchone()[0] for n in names}
    return {"database": "connected", "rows_per_table": counts}


# ---------------- Accounts ----------------
@app.post("/register")
def register(c: Credentials):
    email = c.email.strip().lower()
    if "@" not in email or len(c.password) < 6:
        raise HTTPException(400, "Enter a valid email and a password of at least 6 characters.")
    with get_conn() as conn:
        try:
            row = conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
                (email, hash_password(c.password)),
            ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "That email is already registered. Try logging in.")
        token = new_session(conn, row[0])
    return {"token": token}


@app.post("/login")
def login(c: Credentials):
    email = c.email.strip().lower()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE email = %s", (email,)
        ).fetchone()
        if not row or not check_password(c.password, row[1]):
            raise HTTPException(401, "Wrong email or password.")
        token = new_session(conn, row[0])
    return {"token": token}


@app.post("/logout")
def logout(authorization: str | None = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE token_hash = %s",
                (token_hash(authorization[7:]),),
            )
    return {"ok": True}


# ---------------- Past chats list ----------------
@app.get("/conversations")
def list_conversations(user_id: int = Depends(current_user)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.id, c.created_at, "
            "(SELECT content FROM messages m WHERE m.conversation_id = c.id "
            " AND m.role = 'user' ORDER BY m.id LIMIT 1) "
            "FROM conversations c "
            "WHERE c.user_id = %s "
            "AND EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id) "
            "ORDER BY c.id DESC LIMIT 50",
            (user_id,),
        ).fetchall()
    return [
        {"id": r[0], "created_at": r[1].isoformat(), "title": (r[2] or "New chat")[:40]}
        for r in rows
    ]


@app.get("/conversations/{conv_id}")
def get_conversation(conv_id: int, user_id: int = Depends(current_user)):
    with get_conn() as conn:
        owner = conn.execute(
            "SELECT id FROM conversations WHERE id = %s AND user_id = %s",
            (conv_id, user_id),
        ).fetchone()
        if not owner:
            raise HTTPException(404, "Conversation not found.")
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = %s ORDER BY id",
            (conv_id,),
        ).fetchall()
    return {
        "conversation_id": conv_id,
        "messages": [{"role": role, "content": content} for role, content in rows],
    }


@app.delete("/conversations/{conv_id}")
def delete_conversation(conv_id: int, user_id: int = Depends(current_user)):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM conversations WHERE id = %s AND user_id = %s",
            (conv_id, user_id),
        )
    return {"ok": True}


# ---------------- Chat with saved history ----------------
@app.post("/chat")
def chat(req: ChatRequest, user_id: int = Depends(current_user)):
    with get_conn() as conn:
        if req.conversation_id is None:
            conv_id = conn.execute(
                "INSERT INTO conversations (user_id) VALUES (%s) RETURNING id", (user_id,)
            ).fetchone()[0]
        else:
            row = conn.execute(
                "SELECT id FROM conversations WHERE id = %s AND user_id = %s",
                (req.conversation_id, user_id),
            ).fetchone()
            if not row:
                raise HTTPException(404, "Conversation not found.")
            conv_id = row[0]
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = %s "
            "ORDER BY id DESC LIMIT 10",
            (conv_id,),
        ).fetchall()
    history = [{"role": role, "content": content} for role, content in reversed(rows)]

    reply, model = ask_ai(history, req.message)

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content) "
            "VALUES (%s, 'user', %s), (%s, 'assistant', %s)",
            (conv_id, req.message, conv_id, reply),
        )
    return {"reply": reply, "model": model, "conversation_id": conv_id}


@app.get("/history")
def history(user_id: int = Depends(current_user)):
    with get_conn() as conn:
        conv = conn.execute(
            "SELECT id FROM conversations WHERE user_id = %s ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not conv:
            return {"conversation_id": None, "messages": []}
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = %s ORDER BY id",
            (conv[0],),
        ).fetchall()
    return {
        "conversation_id": conv[0],
        "messages": [{"role": role, "content": content} for role, content in rows],
    }
