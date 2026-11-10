"""A stand-in for second_brain/worker.py that speaks the same JSON-lines protocol without Cognee.
Used by the tests so the whole pipeline (bridge, queue, ledger, tool) can be exercised in seconds.
Special inputs: text containing BOOM -> an error reply; query 'warm' -> Cognee's empty-graph message;
query 'slow' -> never answers in time; query 'die' -> the process exits."""
import json, sys, time
_out = sys.stdout; sys.stdout = sys.stderr
def send(o): _out.write(json.dumps(o) + "\n"); _out.flush()
store = {}
send({"event": "ready", "cognee": "fake-1.0"})
for line in sys.stdin:
    req = json.loads(line); op, i, ds = req.get("op"), req.get("id"), req.get("dataset", "jarvis")
    if op == "shutdown": send({"id": i, "ok": True}); break
    if op == "ping": send({"id": i, "ok": True, "cognee": "fake-1.0", "has_remember": True, "has_recall": True})
    elif op == "remember":
        if "BOOM" in req.get("text", ""): send({"id": i, "ok": False, "error": "RuntimeError: LLM unreachable"})
        else: store.setdefault(ds, []).append(req["text"]); send({"id": i, "ok": True})
    elif op == "recall":
        q = req.get("query", "")
        if q == "die": sys.exit(1)
        if q == "slow": time.sleep(3); continue
        if q == "warm": send({"id": i, "ok": True, "results": ["Memory is still warming up: no knowledge graph data exists yet."]}); continue
        words = [w for w in q.lower().replace("?", "").split() if len(w) > 3]
        hits = [t for t in store.get(ds, []) if any(w in t.lower() for w in words)]
        send({"id": i, "ok": True, "results": [h.split("\n\n", 1)[-1][:300] for h in hits][: req.get("top_k", 8)]})
    elif op == "forget": store.pop(ds, None); send({"id": i, "ok": True})
    else: send({"id": i, "ok": False, "error": f"unknown op {op!r}"})
