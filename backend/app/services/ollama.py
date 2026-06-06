"""Async Ollama HTTP client."""
from __future__ import annotations

import json
import os
from typing import AsyncIterator

import httpx

from ..config import EMBED_MODEL, OLLAMA_CONTAINER, OLLAMA_URL

# URL of the Docker Engine API, reached via the docker-socket-proxy sidecar
# on the compose network. We don't mount the host docker socket directly so
# we don't grant the r2d2 container effective host root.
_DOCKER_API_URL = os.getenv("DOCKER_API_URL", "http://docker-proxy:2375").rstrip("/")


_client = httpx.AsyncClient(
    timeout=httpx.Timeout(600.0, connect=10.0),
    limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
)


class OllamaError(Exception):
    """Raised when Ollama returns a non-2xx response. `detail` is the parsed
    error message from the response body (falls back to the raw body)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def list_models() -> list[dict]:
    r = await _client.get(f"{OLLAMA_URL}/api/tags")
    r.raise_for_status()
    return r.json().get("models", [])


async def show_model(name: str) -> dict:
    r = await _client.post(f"{OLLAMA_URL}/api/show", json={"name": name})
    r.raise_for_status()
    return r.json()


# Cache of model -> capabilities list, populated lazily. Cleared on model
# list refresh would be ideal but capabilities don't change for a given tag,
# so a simple in-process dict is fine; it's reset on container restart.
_caps_cache: dict[str, set[str]] = {}


async def model_capabilities(name: str) -> set[str]:
    """Return the set of capability strings reported by Ollama's /api/show
    (e.g. {'completion', 'tools', 'thinking', 'vision'}). Cached per process.
    On any failure returns an empty set so callers fall back to safe defaults.

    Models that emit `<think>` blocks inline in their content stream rather
    than via Ollama's structured `thinking` field (phi4-mini-reasoning,
    deepseek-r1 distills, …) don't advertise the `thinking` capability via
    /api/show, but `chat_stream` does scrape those tags and route them as
    thinking events. They are thinking models in every observable sense, so
    `thinking` is added to their capability set here — single source of
    truth for both the chat router and the UI."""
    if name in _caps_cache:
        return _caps_cache[name]
    try:
        info = await show_model(name)
        caps = set(info.get("capabilities") or [])
    except Exception:
        caps = set()
    if "thinking" not in caps and _emits_inline_think(name):
        caps.add("thinking")
    _caps_cache[name] = caps
    return caps


# --- Remote registry probe (for models not yet pulled) -----------------------
# Ollama's public registry serves OCI-style manifests at
# https://registry.ollama.ai/v2/library/<repo>/manifests/<tag>. The response is
# unauthenticated for the official library and contains layer sizes we can sum
# to estimate the download size.
_registry = httpx.AsyncClient(
    timeout=httpx.Timeout(15.0, connect=10.0),
    headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
)


async def registry_manifest(name: str) -> dict:
    """Fetch a model's registry manifest. Returns {} on failure."""
    # Normalise to "<namespace>/<repo>:<tag>"
    ref = name.strip()
    if ":" in ref:
        repo, tag = ref.rsplit(":", 1)
    else:
        repo, tag = ref, "latest"
    if "/" not in repo:
        repo = f"library/{repo}"
    url = f"https://registry.ollama.ai/v2/{repo}/manifests/{tag}"
    try:
        r = await _registry.get(url)
        if r.status_code != 200:
            return {"error": f"registry returned {r.status_code}", "ref": f"{repo}:{tag}"}
        data = r.json()
    except Exception as e:
        return {"error": str(e), "ref": f"{repo}:{tag}"}
    layers = data.get("layers") or []
    total = sum(int(l.get("size") or 0) for l in layers)
    parts: list[dict] = []
    for l in layers:
        parts.append({
            "media_type": l.get("mediaType"),
            "size": int(l.get("size") or 0),
            "digest": l.get("digest"),
        })
    return {
        "ref": f"{repo}:{tag}",
        "total_size": total,
        "layers": parts,
        "schema_version": data.get("schemaVersion"),
    }


async def embed(texts: list[str], model: str = EMBED_MODEL) -> list[list[float]]:
    r = await _client.post(
        f"{OLLAMA_URL}/api/embed",
        json={"model": model, "input": texts},
    )
    r.raise_for_status()
    return r.json()["embeddings"]


async def chat_stream(
    model: str,
    messages: list[dict],
    options: dict | None = None,
    tools: list[dict] | None = None,
    think: bool | None = None,
) -> AsyncIterator[dict]:
    """Yields {'type': 'token'|'thinking', 'text': str} during the stream,
    optionally {'type': 'tool_calls', 'calls': [...]} when the model asks
    to invoke tools, and a single {'type': 'done', ...stats} at the end.

    If ``think`` is False, thinking is explicitly disabled for this call
    even if the model supports it (useful for short utility prompts where
    the reasoning budget would consume the entire response window).
    """
    caps = await model_capabilities(model)
    payload: dict = {"model": model, "messages": messages, "stream": True}
    # Only ask Ollama to enable structured `think` mode for models that
    # natively support it. Inline-think models (phi4-mini-reasoning, …)
    # also report `thinking` in caps (added in model_capabilities) but
    # Ollama itself doesn't accept the `think` payload for them — we
    # scrape <think> tags instead, see below.
    inline_think = _emits_inline_think(model)
    if "thinking" in caps and not inline_think:
        payload["think"] = True if think is None else bool(think)
    if options:
        payload["options"] = options
    if tools:
        # Ollama's /api/chat accepts an OpenAI-style `tools` array. The
        # model's tool calls come back inside the message dict (see below).
        # Note: some Ollama builds don't stream tool calls — they return
        # the whole message in one shot when tools are present. Either way
        # we accumulate `pending_tool_calls` and emit them when `done` fires.
        payload["tools"] = tools

    # Some models emit `<think>...</think>` blocks inline in their content
    # stream rather than via Ollama's structured `thinking` field. For those,
    # scrape the tag out of the token stream and re-route it as thinking
    # events so the UI's Thinking panel works as expected.
    scrape_think = inline_think
    in_think = False
    pending = ""  # holds partial tag fragments across chunks (e.g. "<thi")
    pending_tool_calls: list[dict] = []  # accumulated across stream chunks

    async with _client.stream("POST", f"{OLLAMA_URL}/api/chat", json=payload) as r:
        if r.status_code >= 400:
            # Read the body so callers see Ollama's actual error (OOM, missing
            # template, etc.) instead of a bare HTTPStatusError.
            body = (await r.aread()).decode("utf-8", "replace").strip()
            try:
                detail = json.loads(body).get("error") or body
            except json.JSONDecodeError:
                detail = body
            raise OllamaError(r.status_code, detail or f"HTTP {r.status_code}")
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message", {})
            thought = msg.get("thinking", "")
            if thought:
                yield {"type": "thinking", "text": thought}
            # Tool calls arrive on `message.tool_calls`. They can appear in
            # any chunk; collect them rather than yielding immediately so
            # we can emit them once after `done` (paired with the run's
            # stats and after any final assistant content).
            tcs = msg.get("tool_calls") or []
            for tc in tcs:
                fn = (tc or {}).get("function") or {}
                pending_tool_calls.append({
                    "id": tc.get("id") or fn.get("name") or "",
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments"),
                })
            chunk = msg.get("content", "")
            if chunk:
                if scrape_think:
                    async for ev in _split_think_chunk(chunk, pending, in_think):
                        # _split_think_chunk yields {'type': ..., 'text': ...}
                        # events plus a final ('state', in_think, pending)
                        # tuple so we can carry parser state across chunks.
                        if isinstance(ev, tuple) and ev and ev[0] == "state":
                            in_think, pending = ev[1], ev[2]
                        else:
                            yield ev
                else:
                    yield {"type": "token", "text": chunk}
            if obj.get("done"):
                # Flush any leftover buffered text (unclosed think tag or trailing
                # partial fragment) so we don't lose tokens at end-of-stream.
                if scrape_think and pending:
                    yield {"type": "thinking" if in_think else "token", "text": pending}
                if pending_tool_calls:
                    yield {"type": "tool_calls", "calls": pending_tool_calls}
                yield {
                    "type": "done",
                    "prompt_eval_count": int(obj.get("prompt_eval_count") or 0),
                    "eval_count": int(obj.get("eval_count") or 0),
                    "total_duration": int(obj.get("total_duration") or 0),
                    "eval_duration": int(obj.get("eval_duration") or 0),
                }
                break


async def _split_think_chunk(chunk: str, pending: str, in_think: bool):
    """Stream-safe `<think>...</think>` extractor.

    Yields a sequence of {'type': 'token'|'thinking', 'text': str} events
    followed by a final ('state', new_in_think, new_pending) tuple so the
    caller can persist parser state across chunks. `pending` carries a partial
    tag fragment (e.g. "<thi") forward to the next chunk.
    """
    buf = pending + chunk
    out_tok: list[str] = []
    out_think: list[str] = []
    i = 0
    while i < len(buf):
        if in_think:
            end = buf.find("</think>", i)
            if end == -1:
                # Hold back the tail if it could be the start of "</think>"
                tail_keep = _max_partial_tag_suffix(buf, i, "</think>")
                if tail_keep:
                    out_think.append(buf[i:len(buf) - tail_keep])
                    pending = buf[len(buf) - tail_keep:]
                else:
                    out_think.append(buf[i:])
                    pending = ""
                i = len(buf)
                break
            else:
                out_think.append(buf[i:end])
                i = end + len("</think>")
                in_think = False
        else:
            start = buf.find("<think>", i)
            if start == -1:
                tail_keep = _max_partial_tag_suffix(buf, i, "<think>")
                if tail_keep:
                    out_tok.append(buf[i:len(buf) - tail_keep])
                    pending = buf[len(buf) - tail_keep:]
                else:
                    out_tok.append(buf[i:])
                    pending = ""
                i = len(buf)
                break
            else:
                if start > i:
                    out_tok.append(buf[i:start])
                i = start + len("<think>")
                in_think = True

    tok_text = "".join(out_tok)
    think_text = "".join(out_think)
    if think_text:
        yield {"type": "thinking", "text": think_text}
    if tok_text:
        yield {"type": "token", "text": tok_text}
    yield ("state", in_think, pending)


def _max_partial_tag_suffix(buf: str, start: int, tag: str) -> int:
    """Return how many trailing chars of buf[start:] could be a prefix of `tag`,
    so we can hold them back and wait for the next chunk before deciding."""
    end = len(buf)
    max_check = min(len(tag) - 1, end - start)
    for n in range(max_check, 0, -1):
        if tag.startswith(buf[end - n:end]):
            return n
    return 0


# Allowlist of model name substrings (case-insensitive, matched against the
# Ollama tag) whose answer stream is known to contain literal `<think>...</think>`
# blocks. Add new entries here as you encounter them. Matching is substring-
# based against the full ref including the tag, so "phi4-mini-reasoning"
# matches "phi4-mini-reasoning:3.8b-q8_0".
_INLINE_THINK_MODELS: tuple[str, ...] = (
    "phi4-mini-reasoning",
    "phi-4-mini-reasoning",
    # Common DeepSeek-R1 community quants/distills sometimes ship without
    # the `thinking` capability flag but still emit <think> blocks. The
    # capability check above is the primary gate; this is a belt-and-braces
    # safety net for distilled variants when their capability list is bare.
    "deepseek-r1-distill",
    "r1-distill",
)


def _emits_inline_think(model: str) -> bool:
    m = model.lower()
    return any(needle in m for needle in _INLINE_THINK_MODELS)


async def pull_model(name: str) -> AsyncIterator[str]:
    async with _client.stream(
        "POST", f"{OLLAMA_URL}/api/pull", json={"model": name, "stream": True}
    ) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if line.strip():
                yield line


async def delete_model(name: str) -> None:
    r = await _client.request(
        "DELETE", f"{OLLAMA_URL}/api/delete", json={"model": name}
    )
    r.raise_for_status()


async def list_loaded() -> list[dict]:
    """Return currently-loaded models (Ollama /api/ps). Each entry includes
    `name`, `size` (total bytes), `size_vram` (bytes on GPU), `expires_at`
    (when Ollama will auto-unload), and `details`."""
    r = await _client.get(f"{OLLAMA_URL}/api/ps")
    r.raise_for_status()
    return r.json().get("models", [])


async def unload_model(name: str) -> None:
    """Ask Ollama to unload `name` immediately by sending an empty generate
    with keep_alive=0. Idempotent; succeeds even if the model wasn't loaded."""
    # Use /api/generate (not /api/chat) so we don't need a valid chat template
    # for embedding-only models, and pass an empty prompt for a no-op call.
    r = await _client.post(
        f"{OLLAMA_URL}/api/generate",
        json={"model": name, "prompt": "", "keep_alive": 0, "stream": False},
    )
    # Some Ollama versions return 200 with an empty response; others may 400
    # when the model isn't installed. Treat 404 as "already gone".
    if r.status_code in (200, 404):
        return
    r.raise_for_status()


async def generate_once(model: str, prompt: str, *, system: str | None = None,
                        max_tokens: int = 64, temperature: float = 0.0,
                        timeout: float = 20.0) -> str:
    """One-shot, non-streaming completion. Used for cheap utility calls
    (query rewriting, classification) where we want a single short string back.
    Returns "" on any error so callers can fall back gracefully."""
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        # Disable "thinking" output for reasoning models (qwen3, deepseek-r1,
        # phi4-reasoning, etc.). Without this they emit a long <think>...</think>
        # block before the actual answer, which (a) makes our short num_predict
        # cap eat the whole reply and (b) is pointless for a 1-shot rewrite.
        "think": False,
        "options": {"temperature": float(temperature), "num_predict": int(max_tokens)},
    }
    if system:
        payload["system"] = system
    try:
        r = await _client.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=timeout)
        if r.status_code >= 400:
            return ""
        text = (r.json().get("response") or "").strip()
    except Exception:
        return ""
    # Belt-and-braces: even with think:False some models (or older Ollama
    # builds that ignore the flag) still emit <think>…</think>. Strip any
    # such blocks and take whatever the model actually said.
    if "<think>" in text:
        import re
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        # If the close tag was cut off by num_predict, drop everything after
        # the open tag — better empty than half a thought.
        if "<think>" in text:
            text = text.split("<think>", 1)[0].strip()
    return text


_DOCKER_SOCK = "/var/run/docker.sock"


async def restart_service() -> dict:
    """Restart the Ollama container via the docker-socket-proxy sidecar.
    Used to forcibly release memory when a crashed/aborted load leaves the
    runner holding weights it didn't free."""
    url = f"{_DOCKER_API_URL}/containers/{OLLAMA_CONTAINER}/restart?t=5"
    try:
        async with httpx.AsyncClient(timeout=30.0) as cx:
            r = await cx.post(url)
    except httpx.ConnectError as e:
        raise OllamaError(
            503,
            "Docker proxy is unreachable. The r2d2-docker-proxy sidecar must "
            f"be running and DOCKER_API_URL must point at it ({_DOCKER_API_URL}).",
        ) from e
    if r.status_code == 404:
        raise OllamaError(404, f"container '{OLLAMA_CONTAINER}' not found")
    if r.status_code == 403:
        raise OllamaError(
            403,
            "Docker proxy refused the call. Make sure CONTAINERS=1 and POST=1 "
            "are set on the docker-proxy service.",
        )
    if r.status_code >= 400:
        raise OllamaError(r.status_code, r.text.strip() or f"HTTP {r.status_code}")
    return {"ok": True, "container": OLLAMA_CONTAINER}
