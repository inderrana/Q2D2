"""Chat orchestration: build context, stream, persist, track."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable

from fastapi import HTTPException

from .. import db
from ..config import DEFAULT_MODEL, MAX_HISTORY_MESSAGES as DEFAULT_MAX_HISTORY, UTILITY_MODEL
from ..core.persona import build_system
from ..schemas import ChatRequest
from . import ollama, rag, search as search_svc, tools as tools_svc, usage, users

log = logging.getLogger(__name__)


def _friendly_chat_error(exc: Exception, model: str) -> str:
    """Translate raw Ollama / httpx errors into something users can act on.
    The default `str(exc)` leaks internal URLs and HTTP jargon."""
    detail = getattr(exc, "detail", None) or str(exc)
    low = detail.lower()
    # Memory exhaustion — most common failure mode for big models on small hosts.
    if "more system memory" in low or "more gpu memory" in low or "out of memory" in low:
        return (
            f"Not enough memory to load {model}. "
            "Open Settings → Models and unload other models, or pick a smaller one."
        )
    if "model not found" in low or "no such model" in low or "pull model" in low:
        return f"Model {model} isn't installed on this server. Pull it from Settings → Models."
    if "context" in low and "exceed" in low:
        return "This conversation is too long for the model's context window. Start a new chat or shorten your message."
    if "connection" in low and ("refused" in low or "reset" in low):
        return "Lost connection to the model server. Please try again in a moment."
    # Fallback: scrub the internal hostname so users don't see http://ollama:11434.
    cleaned = detail.replace("http://ollama:11434", "the model server")
    return cleaned or "Sorry, the model failed to respond."


async def _rewrite_search_query(model: str, message: str, history: list[dict]) -> str:
    """Turn a follow-up question like "who is it?" into a standalone web
    search query using the conversation so far. Without this, the raw message
    goes to the search engine and pronouns/ellipsis ("it", "they", "more on
    that?") produce wildly off-topic results.

    Returns the rewritten query, or the original `message` if there's no
    useful history or the rewrite fails. Always returns a non-empty string.
    """
    # Use the last few turns of the chat (excluding the just-saved current
    # message, which is the same as `message`). 4 prior turns = plenty of
    # context for pronoun resolution without bloating the prompt.
    prior = [m for m in history if not (m["role"] == "user" and m["content"] == message)]
    prior = prior[-6:]  # cap
    if not prior:
        return message  # turn 1 — nothing to disambiguate against
    transcript = "\n".join(f"{m['role'].upper()}: {m['content'][:400]}" for m in prior)
    today = datetime.now(timezone.utc).strftime("%B %Y")
    system = (
        f"You rewrite the user's latest message into a single standalone search "
        f"query that captures their intent given the prior conversation. "
        f"Today is {today}. "
        f"Resolve pronouns and references using context. "
        f"If the user asks about 'latest', 'current', 'now', or anything time-sensitive, "
        f"add the current month/year to the query. "
        f"Output ONLY the query — no quotes, no explanation, no prefix, max 15 words."
    )
    prompt = (
        f"Prior conversation:\n{transcript}\n\n"
        f"Latest user message: {message}\n\n"
        "Standalone search query:"
    )
    out = await ollama.generate_once(
        model, prompt, system=system, max_tokens=60, temperature=0.0, timeout=20.0,
    )
    # Sanitize: strip quotes, drop leading prefixes some models add.
    rewritten = (out or "").strip().strip('"\'').strip()
    for prefix in ("Query:", "Search:", "Search query:", "Standalone search query:"):
        if rewritten.lower().startswith(prefix.lower()):
            rewritten = rewritten[len(prefix):].strip().strip('"\'').strip()
    # First line only (some models add explanation on later lines despite
    # the instruction).
    rewritten = rewritten.splitlines()[0].strip() if rewritten else ""
    # Guardrails: too short, too long, or empty -> fall back to raw message.
    if not rewritten or len(rewritten) < 3 or len(rewritten) > 200:
        return message
    return rewritten


# Heuristic: messages this short with no nouns probably need history to be
# interpretable (e.g. "who?", "when?", "tell me more"). Used by both the
# classifier and the RAG rewriter to decide if it's worth burning an LLM
# call on rewriting.
def _is_likely_followup(message: str) -> bool:
    msg = (message or "").strip().lower()
    if len(msg) < 5:
        return True
    short_triggers = (
        "who", "what", "when", "where", "why", "how",
        "tell me more", "go on", "and?", "more", "explain",
        "sources", "really?", "and you?", "why not", "for what",
        "in detail", "summarize", "summarise", "expand", "elaborate",
    )
    return any(msg.startswith(t) for t in short_triggers) and len(msg) < 40


async def _needs_web_search(util_model: str, message: str, history: list[dict]) -> bool:
    """Yes/no classifier: does answering this require live web info, or can
    the model handle it from chat history / its own knowledge?

    Returns True if a web search would actually add value, False to skip.
    On any error returns True (safe default — don't accidentally skip a
    search the user might have wanted).
    """
    # Single-turn chats with no history: trust the user toggled web for a reason.
    if not history:
        return True
    transcript = "\n".join(f"{m['role'].upper()}: {m['content'][:300]}" for m in history[-6:])
    system = (
        "You decide whether the user's latest message in a conversation requires "
        "a live web search to answer well. Reply with exactly one word: YES or NO.\n"
        "Reply YES if the question asks for current events, recent prices/scores/news, "
        "real-time data, specific factual claims that should be verified, or info that "
        "may have changed recently.\n"
        "Reply NO if the user is making small talk, asking for clarification of the "
        "previous answer, asking the model to rephrase/format/summarize the prior reply, "
        "asking a follow-up that the prior context already answers, or asking for "
        "creative/opinion/code-help content."
    )
    prompt = (
        f"Conversation so far:\n{transcript}\n\n"
        f"Latest user message: {message}\n\n"
        "Reply YES or NO:"
    )
    out = await ollama.generate_once(
        util_model, prompt, system=system, max_tokens=8, temperature=0.0, timeout=15.0,
    )
    decision = (out or "").strip().upper()
    if decision.startswith("NO"):
        return False
    # Any other response (YES, garbage, timeout-empty) -> default to searching.
    return True


async def _rerank_and_check(util_model: str, query: str,
                            results: list[dict]) -> tuple[list[dict], str]:
    """Re-rank fetched web results by relevance to `query` and flag
    contradictions between sources in a single utility-LLM call.

    Returns (reordered_results, disagreement_note). The note is a one-line
    summary if 2+ sources directly contradict on a key fact; empty
    otherwise. On any failure (timeout, bad JSON, etc.) returns the
    original ordering and "" — this is a quality boost, never a blocker.
    """
    if not results or len(results) < 2:
        return results, ""
    listing = "\n\n".join(
        f"[{i+1}] {r.get('title','')}\n{(r.get('snippet') or '')[:400]}"
        for i, r in enumerate(results)
    )
    system = (
        "You evaluate web search results for a user's query. "
        "Output ONLY a strict JSON object on a single line with this exact shape:\n"
        '{"order":[1,2,3,...],"disagreement":""}\n'
        "Rules:\n"
        "- order: source numbers ranked best-first by direct relevance to the query. "
        "Include every source number exactly once.\n"
        "- disagreement: ONE short sentence ONLY if 2+ sources directly contradict each "
        "other on a key fact in the query. Otherwise use an empty string.\n"
        "Output the JSON object and nothing else — no prose, no code fence."
    )
    prompt = f"Query: {query}\n\nSources:\n{listing}\n\nJSON:"
    try:
        out = await ollama.generate_once(
            util_model, prompt, system=system,
            max_tokens=220, temperature=0.0, timeout=30.0,
        )
    except Exception as e:
        log.warning("rerank call failed: %s", e)
        return results, ""
    text = (out or "").strip()
    # Extract the JSON object (model may add stray text despite instructions).
    lo, hi = text.find("{"), text.rfind("}")
    if lo == -1 or hi == -1 or hi <= lo:
        return results, ""
    try:
        obj = json.loads(text[lo:hi+1])
    except Exception:
        return results, ""
    raw_order = obj.get("order") or []
    disagree = (obj.get("disagreement") or "").strip()
    seen: set[int] = set()
    new_order: list[int] = []
    for n in raw_order:
        try:
            n = int(n)
        except Exception:
            continue
        if 1 <= n <= len(results) and n not in seen:
            seen.add(n)
            new_order.append(n)
    # Append any results the model dropped to preserve the set.
    for n in range(1, len(results) + 1):
        if n not in seen:
            new_order.append(n)
    if len(new_order) != len(results):
        return results, disagree
    reordered = [results[n - 1] for n in new_order]
    return reordered, disagree


async def _verify_grounding(util_model: str, answer: str, context: str) -> str:
    """Check whether the assistant's answer is supported by the provided
    context. Returns "" when grounded (or unsure), or a short reason string
    when specific factual claims appear unsupported.

    Designed to be quick and lenient — we only want to catch obvious
    hallucinations against a context we explicitly provided, not police
    every hedge or stylistic phrasing.
    """
    if not context or not answer or len(answer.strip()) < 40:
        return ""
    system = (
        "You verify whether an answer is grounded in the provided sources. "
        "Reply with exactly one of:\n"
        "  GROUNDED\n"
        "  UNGROUNDED: <one short sentence naming the unsupported claim>\n"
        "Ignore opinions, hedges, greetings, and stylistic phrasing. "
        "Only flag clear factual claims that are NOT supported by any source. "
        "Be lenient — if you're unsure, reply GROUNDED."
    )
    prompt = (
        f"Sources:\n{context[:6000]}\n\n"
        f"Answer:\n{answer[:2000]}\n\n"
        "Verdict:"
    )
    try:
        out = await ollama.generate_once(
            util_model, prompt, system=system,
            max_tokens=80, temperature=0.0, timeout=20.0,
        )
    except Exception as e:
        log.warning("grounding check failed: %s", e)
        return ""
    text = (out or "").strip()
    # First non-empty line wins — small models sometimes ramble.
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if first.upper().startswith("UNGROUNDED"):
        reason = first.split(":", 1)[1].strip() if ":" in first else ""
        return (reason[:240] or
                "Some factual claims may not be supported by the cited sources.")
    return ""


# ---- Conversation memory: rolling summary + entity cache ----------------
#
# Once a chat grows past `SUMMARIZE_AFTER` messages, every assistant turn
# fires a background utility-LLM call that folds the oldest unsummarized
# turns into a rolling text summary and refreshes a small JSON dict of
# "facts about the user/project so far". Both are stored on the `chats`
# row and re-injected into the system prompt on subsequent turns, so even
# very long chats stay coherent without sending the full transcript every
# time.
SUMMARIZE_AFTER = 12   # don't bother below this many total messages
KEEP_RECENT = 8        # always keep this many recent msgs verbatim in history
ENTITY_KEY_LIMIT = 20  # cap entity dict size so it can't grow unbounded


# Models that are prone to runaway/looping output when given long context.
# Reasoning templates with small parameter counts (phi4-mini-reasoning,
# r1 distills, …) habitually re-state the same thought paragraph dozens
# of times until they hit the context window. Hard-cap their output and
# raise the repetition penalty so they actually terminate.
_LOOP_PRONE_MODELS: tuple[str, ...] = (
    "phi4-mini-reasoning",
    "phi-4-mini-reasoning",
    "deepseek-r1-distill",
    "r1-distill",
)


def _build_chat_options(model: str, temperature: float) -> dict:
    """Sensible default Ollama generation options. Adds anti-loop guards
    (output cap + repetition penalty) for known loop-prone reasoning
    templates; otherwise just sets temperature."""
    opts: dict = {"temperature": float(temperature)}
    m = (model or "").lower()
    if any(needle in m for needle in _LOOP_PRONE_MODELS):
        # Reasoning templates spend most of their token budget inside
        # `<think>` before emitting the actual answer. 2048 was too tight
        # (model ran out mid-think and produced nothing); 8192 gives ~6k
        # for reasoning + ~2k for the answer, which fits comfortably in
        # any 16k+ context model while still bounding the runaway-loop
        # failure mode.
        opts["num_predict"] = 589824
        # Default is 1.1; bumping to 1.2 noticeably reduces "wait perhaps…"
        # style repetition without harming legitimate output.
        opts["repeat_penalty"] = 1.3
    return opts


def _get_chat_memory(chat_id: str) -> tuple[str, int, dict]:
    """Return (summary, summary_until, entities_dict) for a chat. All fields
    default to empty when the chat has no memory yet."""
    with db.connect() as cx:
        row = cx.execute(
            "SELECT summary, summary_until, entities FROM chats WHERE id=?",
            (chat_id,),
        ).fetchone()
    if not row:
        return "", 0, {}
    ents: dict = {}
    raw = row["entities"]
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                ents = {str(k)[:60]: str(v)[:200] for k, v in parsed.items()}
        except Exception:
            ents = {}
    return (row["summary"] or ""), int(row["summary_until"] or 0), ents


def _save_chat_memory(chat_id: str, summary: str, summary_until: int,
                      entities: dict) -> None:
    # Cap fields defensively — summaries can grow large if the model rambles.
    summary = (summary or "").strip()[:4000]
    if entities and len(entities) > ENTITY_KEY_LIMIT:
        # Drop alphabetically last keys past the cap — deterministic + cheap.
        keep = sorted(entities.keys())[:ENTITY_KEY_LIMIT]
        entities = {k: entities[k] for k in keep}
    ent_json = json.dumps(entities, ensure_ascii=False) if entities else None
    with db.connect() as cx:
        cx.execute(
            "UPDATE chats SET summary=?, summary_until=?, entities=?, updated_at=? "
            "WHERE id=?",
            (summary or None, int(summary_until), ent_json, db.now(), chat_id),
        )


async def _summarize_and_extract(util_model: str, prior_summary: str,
                                 prior_entities: dict, new_msgs: list[dict],
                                 ) -> tuple[str, dict]:
    """Ask the utility model to refresh the rolling summary AND the entity
    dict in one JSON-returning call. Returns (new_summary, new_entities) or
    ("", {}) on failure — caller leaves prior state untouched on failure.
    """
    if not new_msgs:
        return prior_summary, prior_entities
    transcript = "\n".join(
        f"{m['role'].upper()}: {(m['content'] or '')[:600]}" for m in new_msgs
    )
    prior_ent_json = json.dumps(prior_entities, ensure_ascii=False) if prior_entities else "{}"
    prior_sum = (prior_summary or "").strip() or "(none yet)"
    system = (
        "You maintain a compact conversation memory for a chat assistant. "
        "Given the prior memory and NEW conversation turns, produce an updated memory. "
        "Output ONLY a strict JSON object on a single line with this exact shape:\n"
        '{"summary":"...","entities":{"key":"value",...}}\n'
        "Rules:\n"
        "- summary: 4-8 sentences capturing decisions made, topics discussed, the user's "
        "goals, and any unresolved questions. Merge prior summary with the new turns. "
        "Past tense. No greetings.\n"
        "- entities: a small flat dict of durable facts worth remembering across turns "
        "(e.g. user's name, project name, stack/tools, preferences, deadlines). "
        "Lowercase snake_case keys, short string values. Carry forward prior entries "
        "unless contradicted. Remove only if explicitly retracted. Skip ephemeral facts."
    )
    prompt = (
        f"PRIOR SUMMARY:\n{prior_sum}\n\n"
        f"PRIOR ENTITIES:\n{prior_ent_json}\n\n"
        f"NEW TURNS:\n{transcript}\n\n"
        "Updated memory JSON:"
    )
    try:
        out = await ollama.generate_once(
            util_model, prompt, system=system,
            max_tokens=600, temperature=0.1, timeout=45.0,
        )
    except Exception as e:
        log.warning("memory update LLM call failed: %s", e)
        return prior_summary, prior_entities
    text = (out or "").strip()
    lo, hi = text.find("{"), text.rfind("}")
    if lo == -1 or hi == -1 or hi <= lo:
        return prior_summary, prior_entities
    try:
        obj = json.loads(text[lo:hi+1])
    except Exception:
        return prior_summary, prior_entities
    new_summary = str(obj.get("summary") or "").strip()
    raw_ents = obj.get("entities") or {}
    new_entities: dict = {}
    if isinstance(raw_ents, dict):
        for k, v in raw_ents.items():
            ks = str(k).strip().lower().replace(" ", "_")[:60]
            if not ks or not isinstance(v, (str, int, float, bool)):
                continue
            new_entities[ks] = str(v).strip()[:200]
    return new_summary or prior_summary, new_entities or prior_entities


async def _refresh_chat_memory(chat_id: str, util_model: str) -> None:
    """Background task: load the chat's messages, fold any new ones past the
    KEEP_RECENT window into the rolling summary, save back. Designed to be
    fire-and-forget after `run_chat` yields `done`."""
    try:
        with db.connect() as cx:
            rows = cx.execute(
                "SELECT role, content FROM messages WHERE chat_id=? "
                "ORDER BY created_at ASC",
                (chat_id,),
            ).fetchall()
        msgs = [{"role": r["role"], "content": r["content"]} for r in rows]
        total = len(msgs)
        if total < SUMMARIZE_AFTER:
            return
        prior_summary, summary_until, prior_entities = _get_chat_memory(chat_id)
        # Summarize everything except the last KEEP_RECENT turns. The recent
        # window is sent verbatim every turn, so summarizing it would be
        # redundant (and would risk losing detail the model still needs).
        cutoff = max(0, total - KEEP_RECENT)
        if cutoff <= summary_until:
            return  # nothing new to fold in yet
        new_msgs = msgs[summary_until:cutoff]
        if not new_msgs:
            return
        new_summary, new_entities = await _summarize_and_extract(
            util_model, prior_summary, prior_entities, new_msgs,
        )
        _save_chat_memory(chat_id, new_summary, cutoff, new_entities)
    except Exception:
        log.exception("chat memory refresh failed for %s", chat_id)


def _format_memory_for_system(summary: str, entities: dict) -> str:
    """Render the chat's rolling memory as a system-prompt section. Empty
    string when there's nothing to inject."""
    parts: list[str] = []
    if summary:
        parts.append("Earlier conversation summary:\n" + summary)
    if entities:
        # Sorted for stable prompt hashing (helps server-side caches when present).
        lines = [f"- {k}: {v}" for k, v in sorted(entities.items())]
        parts.append("Durable facts to remember:\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _resolve_utility_model(chat_model: str) -> str:
    """Pick which model to use for rewrite/classify calls.

    If UTILITY_MODEL is the same family as the chat model (or unset), use the
    chat model — avoids loading a second model into VRAM. Otherwise use the
    configured utility model. The trade-off: a separate small model is faster
    per call but causes a VRAM swap when alternating with a big chat model.
    """
    if not UTILITY_MODEL or UTILITY_MODEL == chat_model:
        return chat_model
    return UTILITY_MODEL


def _ensure_chat(req: ChatRequest, user_id: str) -> tuple[str, str]:
    """Return (chat_id, model). Creates a new chat if needed."""
    settings = users.get_settings(user_id)
    with db.connect() as cx:
        if req.chat_id:
            row = cx.execute(
                "SELECT id, model FROM chats WHERE id=? AND user_id=?",
                (req.chat_id, user_id),
            ).fetchone()
            if not row:
                raise HTTPException(404, "chat not found")
            model = req.model or row["model"] or settings.get("default_model") or DEFAULT_MODEL
            return row["id"], model
        chat_id = db.new_id()
        model = req.model or settings.get("default_model") or DEFAULT_MODEL
        first_line = (req.message or "").strip().splitlines()
        title = (first_line[0] if first_line else "New chat")[:60] or "New chat"
        now = db.now()
        cx.execute(
            "INSERT INTO chats (id, user_id, title, model, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, user_id, title, model, now, now),
        )
        return chat_id, model


async def _build_context(req: ChatRequest, user_id: str,
                         settings: dict, chat_id: str | None = None,
                         history: list[dict] | None = None,
                         model: str | None = None,
                         status_cb: Callable[[dict], Awaitable[None]] | None = None,
                         ) -> tuple[str, list[dict]]:
    parts: list[str] = []
    citations: list[dict] = []
    idx = 1

    async def _status(payload: dict) -> None:
        # No-op when run_chat hasn't wired a callback (e.g. tests).
        if status_cb is not None:
            try:
                await status_cb(payload)
            except Exception:
                pass

    # Pick the model for rewriter / classifier calls. We use a small fast
    # model so we don't swap a big chat model in/out of VRAM for every
    # 1-shot utility call (and so the rewriter latency is sub-second).
    util_model = _resolve_utility_model(model or DEFAULT_MODEL)

    # "Has prior context" = anything beyond the just-saved current user msg.
    # Used to gate the classifier and rewriter so we don't burn LLM calls on
    # the first turn (when there's nothing to disambiguate against).
    has_prior = bool(history and len(history) > 1)

    if req.use_rag:
        # Restrict RAG to user's KB docs (chat_id IS NULL) plus *this* chat's attachments.
        # Other chats' attachments are excluded.
        if req.doc_ids:
            allowed = list(req.doc_ids)
        else:
            with db.connect() as cx:
                rows = cx.execute(
                    "SELECT id FROM documents WHERE user_id=? AND (chat_id IS NULL OR chat_id=?)",
                    (user_id, chat_id),
                ).fetchall()
            allowed = [r["id"] for r in rows]
        # Rewrite RAG queries the same way we rewrite web queries — chunk
        # retrieval is far more accurate when "tell me more about that" gets
        # resolved to the actual topic. Only bother if there's history AND the
        # message looks like a follow-up (short / pronoun-y); otherwise the
        # raw message is already a fine query.
        rag_query = req.message
        if allowed and has_prior and _is_likely_followup(req.message):
            await _status({"phase": "rewriting", "for": "documents"})
            try:
                rag_query = await _rewrite_search_query(util_model, req.message, history)
            except Exception as e:
                log.warning("rag query rewrite failed, using raw message: %s", e)
                rag_query = req.message
        if allowed:
            await _status({"phase": "rag", "query": rag_query, "doc_count": len(allowed)})
        try:
            hits = await rag.query(rag_query, user_id,
                                   k=int(settings.get("rag_top_k", 4)),
                                   doc_ids=allowed) if allowed else []
        except Exception as e:
            log.exception("rag.query failed: %s", e)
            citations.append({"n": 0, "type": "warning",
                              "title": "Document search failed", "note": str(e)})
            hits = []
        for h in hits:
            parts.append(f"[{idx}] (doc: {h['meta'].get('name')})\n{h['text']}")
            citations.append({
                "n": idx, "type": "doc",
                "name": h["meta"].get("name"),
                "doc_id": h["meta"].get("doc_id"),
                "score": h["score"],
            })
            idx += 1

    if req.use_web:
        provider = search_svc.resolve(settings)
        skip_search = False
        override = (req.search_query_override or "").strip()
        # First ask: does this question actually need a live web search, or
        # is it answerable from chat history / model knowledge? Skipping a
        # pointless search saves ~1-3 s and an API call, and it stops the
        # model from being distracted by tangentially-related top results.
        # Skip the classifier entirely if the user explicitly provided an
        # override query — they obviously want that exact search to happen.
        if has_prior and not override:
            await _status({"phase": "classifying"})
            try:
                if not await _needs_web_search(util_model, req.message, history):
                    citations.append({
                        "n": 0, "type": "info",
                        "title": "Skipped web search",
                        "note": "The question is answerable from context — no live lookup needed.",
                    })
                    skip_search = True
                    await _status({"phase": "skipped", "reason": "answerable_from_context"})
            except Exception as e:
                # Classifier failure is non-fatal — fall through and search.
                log.warning("web-search classifier failed: %s", e)
        # Rewrite the query using prior turns so follow-ups like
        # "who is it?" / "tell me more" search for the *intended* topic
        # instead of being taken literally. Or use the override verbatim.
        search_query = req.message
        if not skip_search:
            if override:
                search_query = override
            elif has_prior:
                await _status({"phase": "rewriting", "for": "web"})
                try:
                    search_query = await _rewrite_search_query(util_model, req.message, history)
                except Exception as e:
                    log.warning("query rewrite failed, using raw message: %s", e)
                    search_query = req.message
        if not skip_search:
            # Emit the exact query we're about to send + whether it differs
            # from the user's raw message, so the UI can offer an "edit"
            # action without waiting for the full response.
            await _status({
                "phase": "searching", "query": search_query,
                "provider": provider.id,
                "rewritten": search_query != req.message,
                "override": bool(override),
            })
            try:
                results = await search_svc.search_and_fetch(
                    search_query,
                    max_results=int(settings.get("web_top_k", 4)),
                    max_chars=int(settings.get("web_chars_per_result", 2200)),
                    settings=settings,
                )
                usage.record_web_search(user_id, search_query, len(results), provider.id)
                await _status({"phase": "fetched", "count": len(results)})
                # Re-rank + cross-source disagreement check in one utility
                # LLM call. Reordering puts the most-relevant snippet first
                # so it gets the most weight when the model generates.
                if len(results) >= 2:
                    await _status({"phase": "reranking", "count": len(results)})
                    try:
                        results, disagree = await _rerank_and_check(
                            util_model, search_query, results,
                        )
                        if disagree:
                            citations.append({
                                "n": 0, "type": "warning",
                                "title": "Sources disagree",
                                "note": disagree,
                            })
                            await _status({"phase": "disagreement", "note": disagree})
                    except Exception as e:
                        log.warning("rerank step failed: %s", e)
            except search_svc.WebSearchNotConfigured:
                citations.append({
                    "n": 0, "type": "warning",
                    "title": f"Web search ({provider.label}) not configured",
                    "url": provider.help_url,
                    "note": "Open Settings → Web Search to configure a provider.",
                })
                results = []
            except Exception as e:
                citations.append({"n": 0, "type": "warning",
                                  "title": "Web search failed", "note": str(e)})
                results = []
            for r in results:
                parts.append(f"[{idx}] {r['title']} — {r['url']}\n{r['snippet']}")
                citations.append({"n": idx, "type": "web",
                                  "title": r["title"], "url": r["url"]})
                idx += 1
            # If we rewrote the query (or the user overrode it), expose it so
            # the UI can show ("Searched for: …") and offer an edit affordance.
            if results and search_query and search_query != req.message:
                citations.append({"n": 0, "type": "search_query",
                                  "title": "Searched for", "note": search_query})

    if req.ref_chat_ids:
        # Pull the last N turns from each referenced chat (user-scoped).
        turns = max(2, int(settings.get("ref_chat_turns", 8)))
        with db.connect() as cx:
            for ref_id in req.ref_chat_ids:
                row = cx.execute(
                    "SELECT id, title FROM chats WHERE id=? AND user_id=?",
                    (ref_id, user_id),
                ).fetchone()
                if not row:
                    continue  # silently skip ids that aren't the user's
                msg_rows = cx.execute(
                    "SELECT role, content FROM messages WHERE chat_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (ref_id, turns),
                ).fetchall()
                if not msg_rows:
                    continue
                transcript = "\n".join(
                    f"{m['role'].upper()}: {m['content']}"
                    for m in reversed(msg_rows)
                )
                parts.append(f"[{idx}] (chat: {row['title']})\n{transcript}")
                citations.append({"n": idx, "type": "chat",
                                  "title": row["title"], "chat_id": ref_id})
                idx += 1

    return "\n\n---\n\n".join(parts), citations


def _load_history(chat_id: str, limit: int) -> list[dict]:
    with db.connect() as cx:
        rows = cx.execute(
            "SELECT role, content FROM messages WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
            (chat_id, max(2, int(limit))),
        ).fetchall()
    # Re-order to chronological now that we capped from the tail.
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _save_user_message(chat_id: str, user_id: str, content: str) -> None:
    with db.connect() as cx:
        cx.execute(
            "INSERT INTO messages (id, chat_id, user_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (db.new_id(), chat_id, user_id, "user", content, db.now()),
        )


def _save_assistant_message(chat_id: str, user_id: str, content: str,
                            model: str, citations: list[dict],
                            stats: dict, thinking: str = "") -> None:
    meta: dict = {}
    if citations:
        meta["citations"] = citations
    if thinking:
        meta["thinking"] = thinking
    with db.connect() as cx:
        cx.execute(
            "INSERT INTO messages (id, chat_id, user_id, role, content, model, "
            "tokens_in, tokens_out, duration_ms, meta, created_at) "
            "VALUES (?, ?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?)",
            (db.new_id(), chat_id, user_id, content, model,
             stats.get("prompt_eval_count", 0),
             stats.get("eval_count", 0),
             stats.get("total_duration", 0) // 1_000_000,  # ns -> ms
             json.dumps(meta) if meta else None,
             db.now()),
        )
        cx.execute("UPDATE chats SET updated_at=?, model=? WHERE id=?",
                   (db.now(), model, chat_id))


async def _run_with_tools(req: ChatRequest, user_id: str, settings: dict,
                          chat_id: str, model: str, temperature: float,
                          *, history: list[dict],
                          memory_block: str,
                          ) -> AsyncIterator[tuple[str, dict]]:
    """Tool-calling alternative to the legacy `_build_context` + stream
    pipeline. Hands control to the model: it decides whether (and how
    often) to call web_search / search_documents, with each call's results
    fed back as `tool` role messages. The final assistant turn is streamed
    to the user verbatim.

    Yields the same `(event, data)` SSE pairs as `run_chat` for UI parity.
    """
    schemas = tools_svc.tool_schemas(use_web=req.use_web, use_rag=req.use_rag)
    if not schemas:
        # Defensive: caller already gated on use_web|use_rag, but if we get
        # here with no tools, fall back to a plain chat (no tools advertised).
        schemas = None

    # Build the system prompt — same recipe as the legacy path minus the
    # CONTEXT block (the model will fetch its own via tools).
    extra_parts: list[str] = []
    if memory_block:
        extra_parts.append(memory_block)
    user_extra = req.system or settings.get("persona_extra")
    if user_extra:
        extra_parts.append(user_extra)

    # Pre-fetched cross-chat references — these aren't tools, they're
    # explicit user-chosen context, so we fold them in up front and pre-seed
    # the citation list to match.
    citations: list[dict] = []
    next_idx = 1
    ref_block_parts: list[str] = []
    if req.ref_chat_ids:
        turns = max(2, int(settings.get("ref_chat_turns", 8)))
        with db.connect() as cx:
            for ref_id in req.ref_chat_ids:
                row = cx.execute(
                    "SELECT id, title FROM chats WHERE id=? AND user_id=?",
                    (ref_id, user_id),
                ).fetchone()
                if not row:
                    continue
                msg_rows = cx.execute(
                    "SELECT role, content FROM messages WHERE chat_id=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (ref_id, turns),
                ).fetchall()
                if not msg_rows:
                    continue
                transcript = "\n".join(
                    f"{m['role'].upper()}: {m['content']}"
                    for m in reversed(msg_rows)
                )
                ref_block_parts.append(
                    f"[{next_idx}] (chat: {row['title']})\n{transcript}"
                )
                citations.append({"n": next_idx, "type": "chat",
                                  "title": row["title"], "chat_id": ref_id})
                next_idx += 1
    if ref_block_parts:
        extra_parts.append(
            "REFERENCED CHATS (use as additional context; cite as [n]):\n\n"
            + "\n\n---\n\n".join(ref_block_parts)
        )

    # If document search is enabled, tell the model what's actually in the
    # knowledge base. Without this hint the model frequently declines to
    # call `search_documents` because it assumes there's nothing relevant
    # (it has no other signal about what files exist).
    kb_folders: list[str] = []
    if req.use_rag:
        try:
            with db.connect() as cx:
                if req.doc_ids:
                    placeholders = ",".join("?" * len(req.doc_ids))
                    rows = cx.execute(
                        f"SELECT name, folder FROM documents WHERE user_id=? AND id IN ({placeholders}) "
                        f"AND status='ready' ORDER BY folder, created_at DESC",
                        (user_id, *req.doc_ids),
                    ).fetchall()
                else:
                    rows = cx.execute(
                        "SELECT name, folder FROM documents WHERE user_id=? AND status='ready' "
                        "AND (chat_id IS NULL OR chat_id=?) ORDER BY folder, created_at DESC",
                        (user_id, chat_id),
                    ).fetchall()
            if rows:
                # Group by folder so the model can see what's where and pass
                # a `folder` arg to search_documents for scoped lookups.
                by_folder: dict[str, list[str]] = {}
                for r in rows:
                    f = (r["folder"] or "").strip("/")
                    by_folder.setdefault(f, []).append(r["name"])
                kb_folders = [f for f in by_folder.keys() if f]
                listing_parts: list[str] = []
                # Cap displayed docs per folder to keep the prompt small.
                PER_FOLDER_LIMIT = 25
                for f in sorted(by_folder.keys()):
                    names = by_folder[f]
                    label = f"Folder '{f}'" if f else "Folder (root)"
                    shown = names[:PER_FOLDER_LIMIT]
                    extra = f"  …and {len(names) - len(shown)} more" if len(names) > len(shown) else ""
                    listing_parts.append(
                        f"{label} ({len(names)} docs):\n"
                        + "\n".join(f"  - {n}" for n in shown)
                        + (f"\n{extra}" if extra else "")
                    )
                listing = "\n\n".join(listing_parts)
                extra_parts.append(
                    "KNOWLEDGE BASE: The user has uploaded the following documents, "
                    "grouped by folder. Call `search_documents` whenever the question "
                    "could plausibly be answered from any of these. If the user names "
                    "or implies a folder (e.g. 'in my mortgage docs', 'the tax folder'), "
                    "pass the matching folder path as the `folder` argument so the "
                    "search is scoped to that folder. Do NOT decline for lack of "
                    "context before trying the tool.\n\n"
                    + listing
                )
        except Exception:
            log.exception("failed to build KB inventory hint")

    tool_hint = (
        "You have tools to gather information before answering. Use them liberally — "
        "calling a tool is cheap, guessing is expensive.\n"
        "\n"
        "WHEN TO CALL TOOLS:\n"
        "- web_search: current events, recent data, real-time facts, any claim you "
        "should verify, or anything that may have changed since training.\n"
        "- search_documents: any question that could plausibly be answered from the "
        "user's KNOWLEDGE BASE above (people, places, amounts, dates, topics, "
        "filenames, project names — anything that overlaps with the listed files).\n"
        "\n"
        "CRITICAL — every user turn is fresh:\n"
        "Previous tool results from earlier turns may NOT be in your context any more. "
        "You only see your own prior answer text, not the passages it was based on. "
        "Therefore: on EVERY new turn where the question touches the knowledge base "
        "or the live web, call the relevant tool AGAIN to re-fetch the data you need — "
        "even if you 'already answered' something similar before. Never refuse or hedge "
        "with 'I don't have the data' before actually calling the tool.\n"
        "\n"
        "EFFECTIVE QUERYING:\n"
        "- Issue multiple targeted searches when one wouldn't be enough (e.g. one per "
        "entity/file/aspect). You may call the same tool several times in one turn.\n"
        "- For broad/aggregate questions (totals, lists, comparisons across files), "
        "use a larger k (e.g. 12-20+) and/or run separate searches per facet.\n"
        "- READ the returned passages and extract concrete figures, names, dates. "
        "Do NOT respond 'the documents are just references' unless the passages truly "
        "contain no usable data.\n"
        "\n"
        "After tool calls return, cite numbered sources inline as [n] in your final answer."
    )
    extra_parts.append(tool_hint)

    # Pre-seed a forced search_documents call when RAG is enabled and the
    # user has at least one ready document. Small instruction-tuned models
    # (gemma:2b, phi-mini, etc.) advertise the `tools` capability but
    # unreliably emit structured tool_calls — and even when we feed them
    # tool-result messages they often ignore them and fall back to a
    # persona greeting. So we run the search up front and embed the
    # passages as a CONTEXT block in the system prompt (the same channel
    # the legacy pipeline used and which the persona explicitly knows how
    # to ground on). The model can still call tools again later for
    # follow-up searches.
    seed_context: str | None = None
    if req.use_rag and schemas and any(
        s.get("function", {}).get("name") == "search_documents" for s in schemas
    ):
        try:
            # Heuristic folder-hint detection: if the user's message contains
            # a known folder name (or any path segment of one) as a whole
            # word, scope the pre-seed search to that folder. Longest match
            # wins so "taxes/2025" beats "taxes". Generic — works for any
            # folder layout, not hardcoded for any specific topic.
            seed_folder: str | None = None
            if kb_folders:
                msg_lc = " " + req.message.lower() + " "
                candidates: list[str] = []
                for path in kb_folders:
                    p_lc = path.lower()
                    # Match full path or any segment as a whole word.
                    tokens = {p_lc, *p_lc.split("/")}
                    for tok in tokens:
                        if not tok:
                            continue
                        # Word-boundary check (handles punctuation).
                        if (f" {tok} " in msg_lc
                                or f" {tok}," in msg_lc
                                or f" {tok}." in msg_lc
                                or f" {tok}?" in msg_lc
                                or f" {tok}!" in msg_lc
                                or f" {tok}'" in msg_lc):
                            candidates.append(path)
                            break
                if candidates:
                    seed_folder = max(candidates, key=len)
                    log.info("RAG pre-seed: detected folder hint %r", seed_folder)
            seed_args: dict = {
                "query": req.message,
                "k": int(settings.get("rag_top_k") or 4),
            }
            if seed_folder:
                seed_args["folder"] = seed_folder
            seed_result = await tools_svc.run_tool(
                "search_documents", seed_args,
                user_id=user_id, chat_id=chat_id,
                settings=settings, doc_ids=req.doc_ids,
                citation_start=next_idx,
            )
            seeded_count = len(seed_result.get("citations") or [])
            # If folder-scoped seed returned nothing (folder hint may have
            # been wrong, or folder is empty), retry across the whole KB so
            # we still ground the answer on something.
            if seeded_count == 0 and seed_folder:
                log.info("RAG pre-seed: folder %r empty, retrying unscoped",
                         seed_folder)
                seed_args.pop("folder", None)
                seed_result = await tools_svc.run_tool(
                    "search_documents", seed_args,
                    user_id=user_id, chat_id=chat_id,
                    settings=settings, doc_ids=req.doc_ids,
                    citation_start=next_idx,
                )
                seeded_count = len(seed_result.get("citations") or [])
            if seeded_count:
                next_idx = seed_result["next_idx"]
                citations.extend(seed_result.get("citations") or [])
                seed_context = seed_result["text"]
                log.info("RAG pre-seed: %d passages, %d chars (folder=%r)",
                         seeded_count, len(seed_context), seed_folder)
        except Exception:
            log.exception("forced RAG pre-seed failed; continuing without it")

    # When we have pre-seeded passages, append a hard directive so small
    # models don't fall back to a polite refusal ("I don't have access to
    # records"). The persona already says to ground on CONTEXT, but 2-3B
    # models often imitate prior assistant turns or canned safety replies
    # instead. This block is generic — it applies any time RAG returned
    # passages, regardless of the question.
    if seed_context:
        extra_parts.append(
            "GROUNDING DIRECTIVE (this turn):\n"
            "- The CONTEXT block below contains passages from the user's own "
            "uploaded documents, retrieved for THIS question. They are real "
            "data the user has access to.\n"
            "- You MUST attempt to answer from CONTEXT. Do NOT respond with "
            "\"I don't have access to records / documents / data / specific "
            "documents\" — the records are present in CONTEXT.\n"
            "- Extract concrete figures, names, dates, amounts verbatim from "
            "CONTEXT and cite each as [n].\n"
            "- Only if CONTEXT genuinely contains nothing relevant to the "
            "question, say so briefly and offer to search differently.\n"
            "- Ignore any prior turn where you may have refused for lack of "
            "data — fresh CONTEXT is provided now."
        )

    system = build_system(
        extra="\n\n".join(extra_parts) or None,
        context=seed_context,
    )
    messages: list[dict] = [{"role": "system", "content": system}, *history]

    # Belt-and-braces: small models (2-3B) often ignore CONTEXT in the
    # system prompt and continue with their alignment-trained refusal.
    # They reliably read user-turn content though. So when we have seed
    # passages, also inline them into the most recent user message. This
    # is generic — applies to any RAG-grounded prompt regardless of topic.
    if seed_context:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                original = messages[i].get("content") or ""
                messages[i] = {
                    "role": "user",
                    "content": (
                        "Use the following passages from my uploaded documents "
                        "to answer my question. Cite each fact inline as [n] "
                        "using the numbers shown. Do not refuse for lack of "
                        "data — the data is right here.\n\n"
                        f"PASSAGES:\n{seed_context}\n\n"
                        f"QUESTION: {original}"
                    ),
                }
                break

    # Cap iterations so a misbehaving model can't loop forever calling tools.
    # 3 rounds = web_search → maybe a follow-up search → final answer.
    MAX_TOOL_ROUNDS = 3

    # Send meta up front so the UI gets `chat_id` / `model` before any tokens.
    # `citations` will get patched up live as tool calls land — see below.
    yield "meta", {"chat_id": chat_id, "model": model, "citations": citations}

    answer_text_parts: list[str] = []
    thinking_text_parts: list[str] = []
    stats: dict = {}

    try:
        for round_idx in range(MAX_TOOL_ROUNDS + 1):
            # On the last allowed iteration we strip tools so the model is
            # forced to answer rather than burn another round.
            this_tools = schemas if round_idx < MAX_TOOL_ROUNDS else None
            if this_tools:
                await asyncio.sleep(0)  # cooperative yield before long call
                yield "status", {"phase": "thinking_tools", "round": round_idx + 1}
            else:
                yield "status", {"phase": "generating"}

            buf: list[str] = []
            pending_calls: list[dict] = []
            try:
                async for evt in ollama.chat_stream(
                    model, messages, tools=this_tools,
                    options=_build_chat_options(model, temperature),
                    think=req.think,
                ):
                    if evt["type"] == "token":
                        # Stream tokens live only when we're past tool calls
                        # (i.e. no tools advertised this round) — otherwise
                        # buffer them in case the model also asks for tools
                        # and we'd be streaming a thrown-away preamble.
                        buf.append(evt["text"])
                        if this_tools is None:
                            yield "token", {"t": evt["text"]}
                    elif evt["type"] == "thinking":
                        thinking_text_parts.append(evt["text"])
                        yield "thinking", {"t": evt["text"]}
                    elif evt["type"] == "tool_calls":
                        pending_calls = evt["calls"]
                    elif evt["type"] == "done":
                        stats = evt
            except Exception as e:
                log.exception("tool-loop chat_stream failed")
                yield "error", {"error": _friendly_chat_error(e, model)}
                break

            content = "".join(buf).strip()

            if not pending_calls:
                # Final answer round. If we buffered tokens (this_tools was
                # set), flush them out now so the UI sees the reply.
                if this_tools is not None and content:
                    yield "token", {"t": content}
                answer_text_parts.append(content)
                break

            # Append the assistant message bearing the tool calls so the
            # next round has the conversational pointer it needs.
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {"function": {"name": c["name"], "arguments": c["arguments"]}}
                    for c in pending_calls
                ],
            })

            # Execute each tool call sequentially. Parallelism would shave
            # ~1s on 2-call rounds but reordering issues + provider rate
            # limits aren't worth the complexity at this scale.
            for call in pending_calls:
                yield "status", {
                    "phase": "tool_call", "name": call["name"],
                    "args": call.get("arguments"),
                }
                result = await tools_svc.run_tool(
                    call["name"], call.get("arguments"),
                    user_id=user_id, chat_id=chat_id,
                    settings=settings, doc_ids=req.doc_ids,
                    citation_start=next_idx,
                )
                next_idx = result["next_idx"]
                citations.extend(result.get("citations") or [])
                yield "status", result.get("status") or {
                    "phase": "tool_done", "name": call["name"],
                }
                messages.append({
                    "role": "tool",
                    "name": call["name"],
                    "content": result["text"],
                })
            # Push updated citations to the UI so live source list grows.
            yield "meta", {"chat_id": chat_id, "model": model,
                           "citations": list(citations)}

        answer_text = "".join(answer_text_parts).strip()
        _save_assistant_message(chat_id, user_id, answer_text, model,
                                citations, stats,
                                thinking="".join(thinking_text_parts))
        yield "stats", {
            "tokens_in": stats.get("prompt_eval_count", 0),
            "tokens_out": stats.get("eval_count", 0),
            "duration_ms": stats.get("total_duration", 0) // 1_000_000,
        }
        yield "done", {}
        try:
            asyncio.create_task(
                _refresh_chat_memory(chat_id, _resolve_utility_model(model))
            )
        except Exception:
            log.exception("failed to schedule chat memory refresh")
    finally:
        # No-op: tool-loop doesn't have the partial-save concern of the
        # streaming legacy path (we save once at the end with the full
        # answer), so nothing to clean up here.
        pass


async def run_chat(req: ChatRequest, user: dict) -> AsyncIterator[tuple[str, dict]]:
    """Yields (event_name, data_dict) tuples for SSE."""
    user_id = user["id"]
    settings = users.get_settings(user_id)
    temperature = req.temperature if req.temperature is not None else settings.get("temperature", 0.7)

    chat_id, model = _ensure_chat(req, user_id)

    # Persist the user's message FIRST so a client disconnect mid-stream still
    # leaves the message saved (we can re-prompt later).
    _save_user_message(chat_id, user_id, req.message)

    # Load history up front so _build_context can use it for query rewriting
    # (e.g. resolving "who is it?" against the prior turn before web search).
    history = _load_history(chat_id, int(settings.get("max_history_messages") or DEFAULT_MAX_HISTORY))

    # Conversation memory: when the chat is long, prepend a rolling summary
    # + entity dict to the system prompt and trim the verbatim history to
    # the recent window. This keeps prompts compact and the model coherent
    # across very long sessions.
    mem_summary, _mem_until, mem_entities = _get_chat_memory(chat_id)
    memory_block = _format_memory_for_system(mem_summary, mem_entities)
    if memory_block and len(history) > KEEP_RECENT + 2:
        # Always preserve the user's just-saved current message (tail).
        history = history[-(KEEP_RECENT + 1):]

    # Live status pipeline: _build_context pushes structured phase events
    # ({"phase":"searching","query":...}) onto a queue, we drain them
    # concurrently as SSE `status` events so the UI shows "Searching for X"
    # / "Reading 4 sources" / "Skipped web search" in real time instead of
    # after-the-fact in the citations footer.
    status_q: asyncio.Queue[dict] = asyncio.Queue()

    async def _emit_status(payload: dict) -> None:
        await status_q.put(payload)

    # --- Routing: tool-calling vs. legacy pipeline -----------------------
    # If the model advertises the `tools` capability AND the user enabled
    # web or RAG AND tool-calling is on in settings, hand the whole context
    # decision to the model itself via Ollama's `tools` parameter. The
    # model decides when (and how many times) to call web_search /
    # search_documents, with results fed back as `tool` role messages until
    # the model produces a final answer.
    use_tools = bool(settings.get("tool_calling", True)) and (req.use_web or req.use_rag)
    if use_tools:
        caps = await ollama.model_capabilities(model)
        use_tools = "tools" in caps
    if use_tools:
        async for event in _run_with_tools(
            req, user_id, settings, chat_id, model, temperature,
            history=history, memory_block=memory_block,
        ):
            yield event
        return

    ctx_task = asyncio.create_task(
        _build_context(req, user_id, settings, chat_id,
                       history=history, model=model, status_cb=_emit_status)
    )
    while not ctx_task.done() or not status_q.empty():
        try:
            payload = await asyncio.wait_for(status_q.get(), timeout=0.1)
        except asyncio.TimeoutError:
            continue
        yield "status", payload
    context, citations = await ctx_task

    # Stitch the rolling memory into the assistant's system prompt as an
    # extra block, alongside any user-defined persona text.
    extra_parts = []
    if memory_block:
        extra_parts.append(memory_block)
    user_extra = req.system or settings.get("persona_extra")
    if user_extra:
        extra_parts.append(user_extra)
    system = build_system(extra="\n\n".join(extra_parts) or None,
                          context=context or None)
    # history already contains the just-saved user message at the tail.
    messages = [{"role": "system", "content": system}, *history]

    # Belt-and-braces: some models (phi4-mini-reasoning, small reasoning
    # templates) heavily discount system-prompt CONTEXT and lean on their
    # own internal reasoning instead. Inlining the context into the most
    # recent user message reliably gets them to use it. Generic — applies
    # whenever the legacy pipeline gathered any context (RAG, web, refs).
    if context:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                original = messages[i].get("content") or ""
                messages[i] = {
                    "role": "user",
                    "content": (
                        "Use the following sources to answer my question. "
                        "Cite each fact inline as [n] using the numbers shown. "
                        "Do not refuse for lack of data — the data is right here.\n\n"
                        f"SOURCES:\n{context}\n\n"
                        f"QUESTION: {original}"
                    ),
                }
                break

    # Clear the status indicator now that we're about to start generating.
    yield "status", {"phase": "generating"}
    yield "meta", {"chat_id": chat_id, "model": model, "citations": citations}

    buf: list[str] = []
    thinking_buf: list[str] = []
    stats: dict = {}
    saved = False
    try:
        try:
            async for evt in ollama.chat_stream(model, messages,
                                                options=_build_chat_options(model, temperature),
                                                think=req.think):
                if evt["type"] == "token":
                    buf.append(evt["text"])
                    yield "token", {"t": evt["text"]}
                elif evt["type"] == "thinking":
                    thinking_buf.append(evt["text"])
                    yield "thinking", {"t": evt["text"]}
                elif evt["type"] == "done":
                    stats = evt
        except Exception as e:
            log.exception("chat_stream failed")
            yield "error", {"error": _friendly_chat_error(e, model)}

        # Post-generation grounding check: if we gave the model context
        # (RAG / web) and it produced a substantive answer, verify that
        # the answer's factual claims trace back to the sources. Failures
        # surface as a non-blocking warning the UI can pin under the
        # answer; we still save and stream `done` normally.
        answer_text = "".join(buf)
        if context and answer_text and len(answer_text.strip()) >= 40:
            try:
                util = _resolve_utility_model(model)
                warn = await _verify_grounding(util, answer_text, context)
                if warn:
                    citations.append({
                        "n": 0, "type": "warning",
                        "title": "Possibly ungrounded",
                        "note": warn,
                    })
                    yield "warning", {
                        "title": "Possibly ungrounded", "note": warn,
                    }
            except Exception:
                log.exception("grounding verification failed")

        _save_assistant_message(chat_id, user_id, answer_text, model, citations, stats,
                                thinking="".join(thinking_buf))
        saved = True
        yield "stats", {
            "tokens_in": stats.get("prompt_eval_count", 0),
            "tokens_out": stats.get("eval_count", 0),
            "duration_ms": stats.get("total_duration", 0) // 1_000_000,
        }
        yield "done", {}
        # Fire-and-forget memory refresh: never blocks the user-facing
        # stream. If it fails the next turn just uses the previous memory.
        try:
            asyncio.create_task(
                _refresh_chat_memory(chat_id, _resolve_utility_model(model))
            )
        except Exception:
            log.exception("failed to schedule chat memory refresh")
    finally:
        # Safety net: if the generator is torn down (cancellation, GeneratorExit,
        # etc.) before we reached the normal save point, still persist whatever
        # tokens we've buffered so a refresh recovers the in-flight reply.
        if not saved and buf:
            try:
                _save_assistant_message(chat_id, user_id, "".join(buf), model,
                                        citations, stats,
                                        thinking="".join(thinking_buf))
            except Exception:
                log.exception("failed to persist partial assistant message")


async def auto_title(chat_id: str, user_id: str) -> str:
    with db.connect() as cx:
        msgs = cx.execute(
            "SELECT role, content FROM messages WHERE chat_id=? AND user_id=? "
            "ORDER BY created_at ASC LIMIT 4",
            (chat_id, user_id),
        ).fetchall()
        chat = cx.execute(
            "SELECT model FROM chats WHERE id=? AND user_id=?", (chat_id, user_id),
        ).fetchone()
    if not msgs or not chat:
        raise HTTPException(404, "chat not found")
    prompt = (
        "Summarize the following conversation in 4 words or fewer as a short chat title. "
        "Reply with only the title, no quotes.\n\n"
        + "\n".join(f"{m['role'].upper()}: {m['content']}" for m in msgs)
    )
    title_parts: list[str] = []
    # Use the utility (fast) model so we don't tie up a heavy reasoning model
    # for a 4-word summary. Force thinking OFF so reasoning models (qwen3 etc.)
    # don't spend their entire token budget on chain-of-thought and emit an
    # empty title (which falls back to "New chat").
    title_model = _resolve_utility_model(chat["model"] or DEFAULT_MODEL)
    async for evt in ollama.chat_stream(
        title_model,
        [{"role": "user", "content": prompt}],
        options={"temperature": 0.2, "num_predict": 64},
        think=False,
    ):
        if evt["type"] == "token":
            title_parts.append(evt["text"])
    raw = "".join(title_parts).strip().strip('"')
    lines = raw.splitlines() if raw else []
    title = (lines[0][:60].strip() if lines else "") or "New chat"
    with db.connect() as cx:
        cx.execute("UPDATE chats SET title=?, updated_at=? WHERE id=? AND user_id=?",
                   (title, db.now(), chat_id, user_id))
    return title
