"""
second_brain/worker.py — Cognee, in its own process.

This file runs INSIDE Cognee's private virtual environment (second_brain/venv), never inside Jarvis's.
Cognee pulls in around 1.5 GB of libraries (language-model clients, vector and graph databases, ...);
keeping them in a separate environment means they cannot conflict with, slow down or break Jarvis, and
removing the second brain is deleting one folder.

PROTOCOL: one JSON object per line on stdin and stdout. Every request has an "id"; every reply carries
the same id and "ok": true/false. Cognee itself prints to stdout, which would corrupt the channel, so
the real stdout is kept private and sys.stdout is pointed at stderr (second_brain/worker.log).

    {"id": 1, "op": "ping"}                                         -> {"id":1,"ok":true,"cognee":"1.6.6",...}
    {"id": 2, "op": "remember", "text": "...", "dataset": "jarvis"} -> add the text and build the knowledge graph
    {"id": 3, "op": "recall", "query": "...", "dataset": "jarvis", "top_k": 8} -> {"results": ["...", ...]}
    {"id": 4, "op": "forget", "dataset": "jarvis"}                  -> drop that dataset from the graph
    {"id": 5, "op": "shutdown"}

Writes are serialised (Cognee's databases do not like two writers); recalls run alongside them.
Configuration (Gemini keys, models, data folders) arrives as environment variables from the client.
"""
import asyncio
import json
import sys
import threading

_proto = sys.stdout
sys.stdout = sys.stderr


def send(obj: dict) -> None:
    _proto.write(json.dumps(obj, ensure_ascii=False) + "\n")
    _proto.flush()


def as_text(item, limit: int = 1500) -> str:
    """Whatever Cognee hands back, as a readable string."""
    if isinstance(item, str):
        return item[:limit]
    if isinstance(item, dict):
        for k in ("text", "search_result", "content", "answer", "result", "summary"):
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v[:limit]
            if isinstance(v, list) and v:
                return as_text(v[0], limit)
        return json.dumps(item, ensure_ascii=False, default=str)[:limit]
    for k in ("text", "search_result", "content", "answer"):
        v = getattr(item, k, None)
        if isinstance(v, str) and v.strip():
            return v[:limit]
    return str(item)[:limit]


def flatten(res) -> list:
    out = []
    for r in (res if isinstance(res, (list, tuple)) else [res]):
        if isinstance(r, (list, tuple)):
            out.extend(flatten(r))
        elif r is not None:
            t = as_text(r).strip()
            if t:
                out.append(t)
    return out


async def handle(cognee, req: dict, write_lock: asyncio.Lock) -> dict:
    op = req.get("op")
    ds = str(req.get("dataset") or "jarvis")
    if op == "ping":
        return {"cognee": getattr(cognee, "__version__", "?"), "has_remember": hasattr(cognee, "remember"), "has_recall": hasattr(cognee, "recall")}
    if op == "remember":
        text = str(req.get("text") or "").strip()
        if not text:
            raise ValueError("nothing to remember")
        async with write_lock:
            if hasattr(cognee, "remember"):                     # newer Cognee: add + cognify in one call
                await cognee.remember(text, dataset_name=ds)
            else:
                await cognee.add(text, dataset_name=ds)
                await cognee.cognify(datasets=[ds])
        return {}
    if op == "recall":
        q, k = str(req.get("query") or "").strip(), int(req.get("top_k") or 8)
        if not q:
            raise ValueError("no question")
        if hasattr(cognee, "recall"):
            res = await cognee.recall(q, datasets=[ds], top_k=k)
        else:
            from cognee import SearchType
            res = await cognee.search(query_text=q, query_type=SearchType.GRAPH_COMPLETION, datasets=[ds])
        return {"results": flatten(res)[:k]}
    if op == "forget":
        async with write_lock:
            await cognee.forget(dataset=ds)
        return {}
    raise ValueError(f"unknown op {op!r}")


async def main() -> None:
    import cognee                                              # slow the first time: say so only once it is done
    send({"event": "ready", "cognee": getattr(cognee, "__version__", "?")})
    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue = asyncio.Queue()

    def read_stdin():
        for line in sys.stdin:
            loop.call_soon_threadsafe(inbox.put_nowait, line)
        loop.call_soon_threadsafe(inbox.put_nowait, None)

    threading.Thread(target=read_stdin, daemon=True).start()
    write_lock = asyncio.Lock()
    tasks: set = set()

    async def run(req):
        try:
            send({"id": req.get("id"), "ok": True, **(await handle(cognee, req, write_lock))})
        except Exception as e:                                  # noqa: BLE001
            send({"id": req.get("id"), "ok": False, "error": f"{type(e).__name__}: {str(e)[:400]}"})

    while True:
        line = await inbox.get()
        if line is None:
            break
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if req.get("op") == "shutdown":
            send({"id": req.get("id"), "ok": True})
            break
        t = asyncio.create_task(run(req))
        tasks.add(t)
        t.add_done_callback(tasks.discard)
    if tasks:
        await asyncio.wait(tasks, timeout=30)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:                                      # noqa: BLE001
        send({"event": "fatal", "error": f"{type(e).__name__}: {str(e)[:400]}"})
        raise
    # Cognee's libraries start threads of their own that would keep this process alive for a long while after
    # a clean shutdown. Everything has been answered and flushed by now, so leave at once.
    _proto.flush()
    sys.stderr.flush()
    import os
    os._exit(0)
