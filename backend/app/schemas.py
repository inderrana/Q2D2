"""Pydantic request/response models."""
from __future__ import annotations

from pydantic import BaseModel, Field


# ---- Auth
class RegisterIn(BaseModel):
    username: str
    password: str
    display_name: str | None = None
    email: str  # mandatory


class LoginIn(BaseModel):
    username_or_email: str  # can be username or email
    password: str


class AdminCreateUserIn(BaseModel):
    username: str
    password: str
    display_name: str | None = None
    email: str  # mandatory


class UserOut(BaseModel):
    id: str
    username: str
    display_name: str | None = None
    email: str | None = None
    is_admin: bool
    avatar: str | None = None
    created_at: int


# ---- Chats
class ChatCreate(BaseModel):
    title: str | None = None
    model: str | None = None


class ChatRename(BaseModel):
    title: str


# ---- Messages / chat
class ChatRequest(BaseModel):
    chat_id: str | None = None
    model: str | None = None
    message: str = Field(..., min_length=1, max_length=200_000)
    use_web: bool = False
    use_rag: bool = True
    doc_ids: list[str] | None = None
    ref_chat_ids: list[str] | None = None  # other chats whose history to fold in as context
    temperature: float | None = None
    system: str | None = None
    # If set, bypass the auto-rewriter and use this string verbatim as the
    # web search query. Lets the user correct a bad rewrite via the UI.
    search_query_override: str | None = Field(None, max_length=200)
    # When None, default behaviour: thinking enabled iff the model supports
    # it. Setting True forces it on; setting False disables it for this turn
    # (e.g. when the user wants a faster answer on a reasoning-capable model).
    think: bool | None = None


# ---- Settings
class UserSettings(BaseModel):
    default_model: str | None = None
    temperature: float = 0.7
    use_rag_default: bool = True
    use_web_default: bool = False
    rag_top_k: int = Field(4, ge=1, le=60)
    web_top_k: int = Field(4, ge=1, le=10)
    web_chars_per_result: int = Field(2200, ge=500, le=20000)
    max_history_messages: int = Field(60, ge=4, le=400)
    ref_chat_turns: int = Field(8, ge=2, le=60)  # messages pulled per referenced chat
    theme: str = "dark"
    accent: str | None = None
    sidebar_color: str | None = None
    persona_extra: str | None = None  # appended to system prompt
    # Web search
    search_provider: str | None = None      # tavily | searxng | brave | duckduckgo
    tavily_api_key: str | None = None
    brave_api_key: str | None = None
    searxng_url: str | None = None