"""Usage tracking & aggregation."""
from __future__ import annotations

from .. import db
from . import search as search_svc, users


def record_web_search(user_id: str, query: str, results: int,
                      provider: str | None = None) -> None:
    # Cap stored query to avoid log abuse / DB bloat.
    q = (query or "")[:2000]
    with db.connect() as cx:
        cx.execute(
            "INSERT INTO web_searches (user_id, query, results, provider, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, q, results, provider, db.now()),
        )


def user_stats(user_id: str) -> dict:
    now_ms = db.now()
    month_start = now_ms - 30 * 86400 * 1000
    today_start = now_ms - 86400 * 1000
    settings = users.get_settings(user_id)
    active = search_svc.resolve(settings)

    with db.connect() as cx:
        totals = cx.execute(
            "SELECT COUNT(*) AS msgs, "
            "COALESCE(SUM(tokens_in),0) AS tok_in, "
            "COALESCE(SUM(tokens_out),0) AS tok_out, "
            "COALESCE(SUM(duration_ms),0) AS dur "
            "FROM messages WHERE user_id=? AND role='assistant'",
            (user_id,),
        ).fetchone()
        today = cx.execute(
            "SELECT COUNT(*) AS msgs, "
            "COALESCE(SUM(tokens_in),0) AS tok_in, "
            "COALESCE(SUM(tokens_out),0) AS tok_out "
            "FROM messages WHERE user_id=? AND role='assistant' AND created_at>=?",
            (user_id, today_start),
        ).fetchone()
        chats = cx.execute("SELECT COUNT(*) AS c FROM chats WHERE user_id=?",
                           (user_id,)).fetchone()["c"]
        docs = cx.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(chunks),0) AS chunks, "
            "COALESCE(SUM(bytes),0) AS bytes FROM documents WHERE user_id=?",
            (user_id,),
        ).fetchone()
        web_total = cx.execute(
            "SELECT COUNT(*) AS c FROM web_searches WHERE user_id=?", (user_id,),
        ).fetchone()["c"]
        web_month = cx.execute(
            "SELECT COUNT(*) AS c FROM web_searches WHERE user_id=? AND created_at>=?",
            (user_id, month_start),
        ).fetchone()["c"]
        active_month = cx.execute(
            "SELECT COUNT(*) AS c FROM web_searches "
            "WHERE user_id=? AND created_at>=? AND COALESCE(provider,'')=?",
            (user_id, month_start, active.id),
        ).fetchone()["c"]
        by_model = cx.execute(
            "SELECT model, COUNT(*) AS msgs, "
            "COALESCE(SUM(tokens_in),0) AS tok_in, "
            "COALESCE(SUM(tokens_out),0) AS tok_out "
            "FROM messages WHERE user_id=? AND role='assistant' AND model IS NOT NULL "
            "GROUP BY model ORDER BY tok_out DESC",
            (user_id,),
        ).fetchall()
        by_provider = cx.execute(
            "SELECT COALESCE(provider,'unknown') AS p, COUNT(*) AS c "
            "FROM web_searches WHERE user_id=? GROUP BY p ORDER BY c DESC",
            (user_id,),
        ).fetchall()

    quota = active.monthly_quota
    return {
        "totals": {
            "chats": chats,
            "messages": totals["msgs"],
            "tokens_in": totals["tok_in"],
            "tokens_out": totals["tok_out"],
            "total_seconds": round(totals["dur"] / 1000, 2),
        },
        "today": {
            "messages": today["msgs"],
            "tokens_in": today["tok_in"],
            "tokens_out": today["tok_out"],
        },
        "documents": {
            "count": docs["c"],
            "chunks": docs["chunks"],
            "bytes": docs["bytes"],
        },
        "web_search": {
            "active": active.id,
            "active_label": active.label,
            "configured": active.is_configured(settings),
            "total": web_total,
            "month": web_month,
            "active_month": active_month,
            "monthly_quota": quota,
            "remaining_estimate": (max(0, quota - active_month) if quota else None),
            "providers": search_svc.available_providers(settings),
            "by_provider": [dict(r) for r in by_provider],
        },
        "by_model": [dict(r) for r in by_model],
    }


def admin_stats() -> dict:
    with db.connect() as cx:
        users_n = cx.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        chats = cx.execute("SELECT COUNT(*) AS c FROM chats").fetchone()["c"]
        msgs = cx.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        toks = cx.execute(
            "SELECT COALESCE(SUM(tokens_in),0) AS i, COALESCE(SUM(tokens_out),0) AS o FROM messages"
        ).fetchone()
    return {
        "users": users_n, "chats": chats, "messages": msgs,
        "tokens_in": toks["i"], "tokens_out": toks["o"],
    }
