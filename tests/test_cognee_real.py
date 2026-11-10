"""The REAL Cognee worker, if Cognee is installed (set COGNEE_PYTHON to test a Cognee install elsewhere).
It deliberately gives Cognee an invalid Gemini key, so it checks what must always hold: the worker starts, answers
the protocol, keeps its output channel clean, turns a failed language-model call into a clean error instead of a
crash or a hang, stays alive afterwards, and keeps its data inside the folder it was given. It cannot check that a
graph is actually built: that needs a working Gemini key and internet."""
import os, sys, tempfile
APP = os.environ.get("JARVIS_DIR") or os.path.join(os.path.expanduser("~"), "Jarvis")
if not os.path.exists(os.path.join(APP, "main.py")):
    sys.exit("Set JARVIS_DIR to your installed Jarvis folder (run Install_Jarvis.bat first).")
import json, pathlib, queue, subprocess, threading, time
py = os.environ.get("COGNEE_PYTHON") or os.path.join(APP, "second_brain", "venv", "Scripts" if sys.platform == "win32" else "bin", "python" + (".exe" if sys.platform == "win32" else ""))
worker = os.path.join(APP, "second_brain", "worker.py")
if not (os.path.exists(py) and os.path.exists(worker)):
    print("SKIPPED: Cognee is not installed (say 'set up my second brain with Cognee' first, or set COGNEE_PYTHON)."); sys.exit(0)
root = pathlib.Path(tempfile.mkdtemp(prefix="jarvis-cognee-"))
env = dict(os.environ, LOG_LEVEL="ERROR", LLM_PROVIDER="gemini", LLM_MODEL="gemini/gemini-2.5-flash", LLM_API_KEY="INVALID-TEST-KEY", EMBEDDING_PROVIDER="gemini",
           EMBEDDING_MODEL="gemini/gemini-embedding-001", EMBEDDING_API_KEY="INVALID-TEST-KEY", EMBEDDING_DIMENSIONS="768",
           SYSTEM_ROOT_DIRECTORY=str(root / "system"), DATA_ROOT_DIRECTORY=str(root / "data"))
p = subprocess.Popen([py, worker], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(root / "worker.log", "w"), text=True, env=env)
q = queue.Queue(); threading.Thread(target=lambda: [q.put(l) for l in p.stdout], daemon=True).start()
def rpc(i, timeout=120, **kw):
    p.stdin.write(json.dumps({"id": i, **kw}) + "\n"); p.stdin.flush(); t = time.time()
    while True:
        m = json.loads(q.get(timeout=max(1, timeout - (time.time() - t))))
        if m.get("id") == i: return m
m = json.loads(q.get(timeout=180)); print("startup:", m); assert m.get("event") == "ready"
r = rpc(1, op="ping"); print("ping:", r); assert r["ok"] and r["has_remember"] and r["has_recall"]
r = rpc(2, op="remember", text="Asha prefers morning meetings.", dataset="t"); print("remember with an invalid key ->", r["ok"], r.get("error", "")[:140]); assert r["ok"] is False and r.get("error")
assert p.poll() is None; assert rpc(3, op="ping")["ok"]; print("worker survived the failure")
r = rpc(4, op="recall", query="who prefers mornings?", dataset="t"); print("recall ->", r["ok"], str(r.get("error", r.get("results")))[:140]); assert "ok" in r
assert rpc(5, op="bogus")["ok"] is False and rpc(6, op="remember", text="  ")["ok"] is False
rpc(7, op="shutdown"); time.sleep(1); assert p.poll() is not None
assert q.empty(), "stray output on the protocol channel"; assert (root / "system").exists() or (root / "data").exists(), "data must land in the folder it was given"
print("\nREAL COGNEE WORKER OK"); sys.stdout.flush(); os._exit(0)
