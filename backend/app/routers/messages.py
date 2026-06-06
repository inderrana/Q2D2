"""Streaming chat endpoint (SSE)."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..deps import current_user
from ..schemas import ChatRequest
from ..services import chat as chat_svc

log = logging.getLogger(__name__)

router = APIRouter(tags=["messages"])


@router.post("/api/chat")
async def chat(req: ChatRequest, request: Request, user: dict = Depends(current_user)):
    async def sse():
        # Producer/consumer split so heartbeat timeouts never cancel the
        # underlying chat generator (which would tear down the Ollama stream
        # mid-response and silently drop tokens — e.g. when first-token latency
        # exceeds the heartbeat interval on a cold model).
        queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        SENTINEL: object = object()

        async def produce() -> None:
            try:
                async for event, data in chat_svc.run_chat(req, user):
                    await queue.put((event, data))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("chat generator failed")
                await queue.put(("error", {"error": str(e)}))
            finally:
                await queue.put(SENTINEL)

        async def drain() -> None:
            # Consumer is gone; keep pulling from the queue so the producer
            # can run to completion and persist the assistant message. That
            # way a page refresh during streaming still recovers the reply.
            try:
                while True:
                    item = await queue.get()
                    if item is SENTINEL:
                        return
            except asyncio.CancelledError:
                pass

        producer = asyncio.create_task(produce())
        detached = False
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        log.info("client disconnected; letting chat finish in background")
                        asyncio.create_task(drain())
                        detached = True
                        return
                    yield ": keepalive\n\n"
                    continue
                if item is SENTINEL:
                    return
                event, data = item
                yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
        finally:
            # If we exited for any reason while the producer is still working
            # (e.g. an exception in the consumer loop), make sure something
            # drains the queue so produce() doesn't block on a full queue and
            # the assistant message still gets saved.
            if not producer.done() and not detached:
                asyncio.create_task(drain())

    return StreamingResponse(sse(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
