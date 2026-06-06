"""Tool-calling support for the chat loop.

Defines the JSON-schema tools we expose to capable models (Ollama's
`tools` capability — qwen3, gemma3, llama3.1, mistral-nemo, etc.) and a
single dispatch entrypoint that runs the requested tool and returns a
short structured result the model can read back.

Each tool result returns:
- text:   the string the model sees (gets appended as a `tool` role msg)
- citations: list of citation dicts to append to the answer's metadata
- status: short status payload to surface live in the UI

Tools are intentionally narrow and side-effect-free from the user's
perspective so the model can call them aggressively without surprises.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import rag, search as search_svc, usage

log = logging.getLogger(__name__)


def tool_schemas(use_web: bool, use_rag: bool) -> list[dict]:
    """Return the OpenAI-style tool schemas to advertise on this turn.
    Empty list means no tools (caller should skip the tool-loop path)."""
    tools: list[dict] = []
    if use_web:
        tools.append({
            "type": "function",
            "function": {
                "name": "web_search",
                "description": (
                    "Search the live web for information. Use this when the user "
                    "asks about current events, recent data, real-time facts, "
                    "specific people/products, or anything that changes over time. "
                    "Returns ranked snippets with titles and URLs."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query. Be specific and include all relevant context (e.g. resolve pronouns from the conversation).",
                        },
                        "num_results": {
                            "type": "integer",
                            "description": "How many results to fetch (1-6). Default 4.",
                            "minimum": 1, "maximum": 6,
                        },
                    },
                    "required": ["query"],
                },
            },
        })
    if use_rag:
        tools.append({
            "type": "function",
            "function": {
                "name": "search_documents",
                "description": (
                    "Search the user's uploaded documents (knowledge base) for "
                    "passages relevant to a question. Use this when the user "
                    "references their own files, asks about content they have "
                    "uploaded, or when a question is best answered from their "
                    "private knowledge base."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to look up in the knowledge base. Be specific.",
                        },
                        "k": {
                            "type": "integer",
                            "description": "How many passages to retrieve (1-60). Default 4. Use a larger value for broad/aggregate questions.",
                            "minimum": 1, "maximum": 60,
                        },
                        "folder": {
                            "type": "string",
                            "description": "Optional. Restrict search to documents in this folder path (exact match, e.g. 'mortgage' or 'taxes/2025'). Use the folder names listed in the KNOWLEDGE BASE section. Omit to search across all the user's documents.",
                        },
                    },
                    "required": ["query"],
                },
            },
        })
    return tools


# --- Argument coercion ----------------------------------------------------
# Models occasionally hand us args as a JSON string instead of a dict, or
# omit required keys, or pass an int where we want a string. Be lenient.

def _coerce_args(args: Any) -> dict:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return {}
    if not isinstance(args, dict):
        return {}
    return args


async def run_tool(name: str, raw_args: Any, *, user_id: str,
                   chat_id: str | None, settings: dict,
                   doc_ids: list[str] | None,
                   citation_start: int) -> dict:
    """Execute a tool call and return:
      {
        "text": str (appended to the conversation as a tool message),
        "citations": list[dict] (to add to the assistant's meta.citations),
        "status": dict (to forward to the SSE UI),
        "next_idx": int (next citation index to use),
        "error": str (only set on failure),
      }
    """
    args = _coerce_args(raw_args)
    idx = citation_start
    try:
        if name == "web_search":
            return await _do_web_search(args, user_id, settings, idx)
        if name == "search_documents":
            return await _do_doc_search(args, user_id, chat_id, settings, doc_ids, idx)
    except Exception as e:
        log.exception("tool %s failed", name)
        return {
            "text": f"Tool {name} failed: {e}",
            "citations": [], "status": {"phase": "tool_error", "name": name, "error": str(e)},
            "next_idx": idx, "error": str(e),
        }
    return {
        "text": f"Unknown tool: {name}",
        "citations": [], "status": {"phase": "tool_error", "name": name, "error": "unknown tool"},
        "next_idx": idx, "error": "unknown tool",
    }


async def _do_web_search(args: dict, user_id: str, settings: dict,
                         idx: int) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        return {
            "text": "web_search called with empty query.",
            "citations": [], "status": {"phase": "tool_error", "name": "web_search", "error": "empty query"},
            "next_idx": idx, "error": "empty query",
        }
    n = int(args.get("num_results") or settings.get("web_top_k", 4))
    n = max(1, min(6, n))
    provider = search_svc.resolve(settings)
    try:
        results = await search_svc.search_and_fetch(
            query, max_results=n,
            max_chars=int(settings.get("web_chars_per_result", 2200)),
            settings=settings,
        )
        usage.record_web_search(user_id, query, len(results), provider.id)
    except search_svc.WebSearchNotConfigured:
        return {
            "text": (f"web_search unavailable: provider '{provider.label}' is not "
                     f"configured. Inform the user and answer from general knowledge."),
            "citations": [{
                "n": 0, "type": "warning",
                "title": f"Web search ({provider.label}) not configured",
                "url": provider.help_url,
                "note": "Open Settings → Web Search to configure a provider.",
            }],
            "status": {"phase": "tool_error", "name": "web_search", "error": "not configured"},
            "next_idx": idx, "error": "not configured",
        }
    citations: list[dict] = []
    lines: list[str] = []
    for r in results:
        lines.append(f"[{idx}] {r['title']} — {r['url']}\n{r['snippet']}")
        citations.append({
            "n": idx, "type": "web",
            "title": r["title"], "url": r["url"],
        })
        idx += 1
    text = ("\n\n".join(lines) if lines
            else "No results returned. Try a different query or answer without it.")
    return {
        "text": text,
        "citations": citations,
        "status": {
            "phase": "tool_done", "name": "web_search",
            "query": query, "count": len(results),
        },
        "next_idx": idx,
    }


async def _do_doc_search(args: dict, user_id: str, chat_id: str | None,
                         settings: dict, doc_ids: list[str] | None,
                         idx: int) -> dict:
    from .. import db
    query = str(args.get("query") or "").strip()
    if not query:
        return {
            "text": "search_documents called with empty query.",
            "citations": [], "status": {"phase": "tool_error", "name": "search_documents", "error": "empty query"},
            "next_idx": idx, "error": "empty query",
        }
    k = int(args.get("k") or settings.get("rag_top_k", 4))
    # Hard upper bound matches the schema cap on rag_top_k. The model is
    # allowed to ask for fewer than the user's configured default but never
    # more than the global ceiling.
    k = max(1, min(60, k))
    folder_filter = (args.get("folder") or "").strip().strip("/") or None
    with db.connect() as cx:
        if doc_ids:
            placeholders = ",".join("?" * len(doc_ids))
            rows = cx.execute(
                f"SELECT id, folder FROM documents WHERE user_id=? AND id IN ({placeholders}) "
                f"AND status='ready'",
                (user_id, *doc_ids),
            ).fetchall()
        else:
            rows = cx.execute(
                "SELECT id, folder FROM documents WHERE user_id=? AND status='ready' "
                "AND (chat_id IS NULL OR chat_id=?)",
                (user_id, chat_id),
            ).fetchall()
    if folder_filter:
        f_lc = folder_filter.lower()
        allowed = [r["id"] for r in rows
                   if (r["folder"] or "").strip("/").lower() == f_lc
                   or (r["folder"] or "").strip("/").lower().startswith(f_lc + "/")]
        if not allowed:
            available = sorted({(r["folder"] or "").strip("/") for r in rows})
            available_list = ", ".join(repr(f) for f in available if f) or "(none)"
            return {
                "text": (f"No documents found in folder '{folder_filter}'. "
                         f"Available folders: {available_list}. "
                         "Retry without the folder filter or use one of the listed folders."),
                "citations": [],
                "status": {"phase": "tool_done", "name": "search_documents",
                           "query": query, "count": 0, "folder": folder_filter},
                "next_idx": idx,
            }
    else:
        allowed = [r["id"] for r in rows]
    if not allowed:
        return {
            "text": "No documents in the knowledge base. Answer from general knowledge.",
            "citations": [],
            "status": {"phase": "tool_done", "name": "search_documents",
                       "query": query, "count": 0},
            "next_idx": idx,
        }
    # When the search is scoped to a small set (folder filter or explicit
    # doc_ids), prefer pulling the FULL text of each document instead of
    # top-k embedding chunks. Aggregate questions ("how much have been
    # paid?", "list all dates", "compare X across files") need totals,
    # headers, and footers that an embedding may not rank highly. With a
    # small scope we can afford the tokens.
    folder_by_doc = {r["id"]: (r["folder"] or "").strip("/") for r in rows}
    SCOPED_FULL_DOC_LIMIT = 12  # max docs to return in full-doc mode
    PER_DOC_CHAR_CAP = 12000     # safety cap per doc to avoid prompt blow-up
    if (folder_filter or doc_ids) and len(allowed) <= SCOPED_FULL_DOC_LIMIT:
        try:
            full_docs = rag.get_full_documents(allowed)
            if full_docs:
                citations: list[dict] = []
                lines: list[str] = []
                for fd in full_docs:
                    folder = folder_by_doc.get(fd["doc_id"], "")
                    name = fd["name"]
                    loc = f"{folder}/{name}" if folder else name
                    body = fd["text"] or ""
                    truncated = False
                    if len(body) > PER_DOC_CHAR_CAP:
                        body = body[:PER_DOC_CHAR_CAP]
                        truncated = True
                    suffix = "\n[…document truncated]" if truncated else ""
                    lines.append(f"[{idx}] (doc: {loc})\n{body}{suffix}")
                    citations.append({
                        "n": idx, "type": "doc",
                        "name": name,
                        "folder": folder or None,
                        "doc_id": fd["doc_id"],
                        "score": 1.0,
                    })
                    idx += 1
                log.info("doc_search: full-doc mode for %d scoped docs",
                         len(full_docs))
                return {
                    "text": "\n\n".join(lines),
                    "citations": citations,
                    "status": {
                        "phase": "tool_done", "name": "search_documents",
                        "query": query, "count": len(full_docs),
                        "mode": "full_doc",
                        **({"folder": folder_filter} if folder_filter else {}),
                    },
                    "next_idx": idx,
                }
        except Exception:
            log.exception("full-doc fetch failed; falling back to top-k chunks")
    try:
        hits = await rag.query(query, user_id, k=k, doc_ids=allowed)
    except Exception as e:
        log.exception("rag.query failed")
        return {
            "text": f"Document search failed: {e}",
            "citations": [{"n": 0, "type": "warning",
                           "title": "Document search failed", "note": str(e)}],
            "status": {"phase": "tool_error", "name": "search_documents", "error": str(e)},
            "next_idx": idx, "error": str(e),
        }
    citations: list[dict] = []
    lines: list[str] = []
    for h in hits:
        doc_id = h["meta"].get("doc_id")
        name = h["meta"].get("name")
        folder = folder_by_doc.get(doc_id, "")
        loc = f"{folder}/{name}" if folder else name
        lines.append(f"[{idx}] (doc: {loc})\n{h['text']}")
        citations.append({
            "n": idx, "type": "doc",
            "name": name,
            "folder": folder or None,
            "doc_id": doc_id,
            "score": h["score"],
        })
        idx += 1
    text = ("\n\n".join(lines) if lines
            else "No matching passages found. Answer from general knowledge.")
    return {
        "text": text,
        "citations": citations,
        "status": {
            "phase": "tool_done", "name": "search_documents",
            "query": query, "count": len(hits),
        },
        "next_idx": idx,
    }
