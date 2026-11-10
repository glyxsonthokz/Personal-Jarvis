"""Obsidian + Cognee: the bridge, the ledger, recall, the installer commands, the tool, and spoken notes -> vault -> Cognee.
Uses fake_cognee_worker.py (same protocol, no 1.5 GB install). test_cognee_real.py exercises the real worker if it is installed."""
import os, sys, tempfile
APP = os.environ.get("JARVIS_DIR") or os.path.join(os.path.expanduser("~"), "Jarvis")
if not os.path.exists(os.path.join(APP, "main.py")):
    sys.exit("Set JARVIS_DIR to your installed Jarvis folder (run Install_Jarvis.bat first).")
sys.path.insert(0, APP); os.chdir(APP)
import importlib.util, json, pathlib, threading, time, types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="jarvis-sb-"))
from core import second_brain as sb, vault as V, automations as au, action_loader

V.CFG = TMP / "second_brain.json"; au._CFG = TMP / "rules.json"
sb.ROOT = TMP / "sb"; sb.WORKER = HERE / "fake_cognee_worker.py"; sb.READY = sb.ROOT / "ready.json"; sb.LEDGER = sb.ROOT / "ledger.json"; sb.LOG = TMP / "worker.log"
sb.PYTHON_OVERRIDE = sys.executable; sb._env = lambda: ({**os.environ, "PYTHONUNBUFFERED": "1"}, True)
vault = V.Vault(TMP / "Vault").ensure(); V.save_cfg(vault=str(vault.root), cognee=True)
def wait(cond, secs=20, what=""):
    t = time.time()
    while not cond():
        assert time.time() - t < secs, f"timed out waiting for {what}"; time.sleep(0.05)

# 1. the bridge ---------------------------------------------------------------------------------------------
b = sb.Bridge(); b.start(30); r = b.call("ping"); print("1. ping:", r["cognee"]); assert r["cognee"] == "fake-1.0" and b.version == "fake-1.0"
b.call("remember", text="Note: x\n\nAsha prefers morning meetings", dataset="d"); r = b.call("recall", query="who prefers mornings meetings", dataset="d"); assert r["results"] == ["Asha prefers morning meetings"]
try: b.call("remember", text="BOOM"); raise SystemExit("an error reply must raise")
except RuntimeError as e: print("   error reply raised:", e); assert "LLM unreachable" in str(e) and "LLM unreachable" in b.last_error
assert b.call("ping")["ok"]; print("   worker survived the error")
try: b.call("recall", timeout=1, query="slow"); raise SystemExit("should time out")
except TimeoutError as e: print("   timeout:", e)
time.sleep(3.2); assert b.call("ping")["ok"]
try: b.call("recall", query="die"); raise SystemExit("a crash must raise")
except RuntimeError as e: print("   crash raised:", e)
r = b.call("ping"); print("   next call restarted the worker:", r["ok"]); assert r["ok"] and b.running()
b.stop(); assert not b.running(); print("   stop() ok")
sb._bridge = None

# 2. notes -> ledger ---------------------------------------------------------------------------------------------
S = vault.new_session("Standup", now=datetime(2026, 11, 5, 14, 23))
for t in ("we decided to ship the launch on Friday", "Asha will run the final test on Thursday morning"): vault.append_bullet(S, t)
N = vault.remember("Asha prefers morning meetings and dislikes Mondays", title="Asha preferences")
P = vault.remember("my private health appointment details are secret", title="Private thing", private=True)
Q = vault.remember("short", title="Tiny")
vault.daily_append("a daily line that is long enough to count but is a daily page")
txt = sb.render_for_cognee(S); print("2. rendered:", repr(txt[:90])); assert txt.startswith("Note: 2026-11-05 1423 - Standup\nWritten: 2026-11-05 14:23:00\nKind: spoken-notes") and "---" not in txt.split("\n\n", 1)[0]
pend = {p.name for p in sb.pending_notes(vault)}; print("   pending:", sorted(pend)); assert pend == {S.name, N.name}, "private, tiny and daily pages must never be sent"
assert sb.enabled(); n = sb.sync(vault); assert n == 2
wait(lambda: sb.ingestor().ok == 2, what="ingestion"); led = json.loads(sb.LEDGER.read_text()); assert set(map(lambda k: Path(k).name, led)) == {S.name, N.name}; print("   ledger:", sorted(Path(k).name for k in led))
assert sb.sync(vault) == 0; vault.append_bullet(S, "and the retro moves to Monday"); assert [p.name for p in sb.pending_notes(vault)] == [S.name]; assert sb.sync(vault) == 1; wait(lambda: sb.ingestor().ok == 3, what="re-ingest"); print("   a changed note is re-sent, an unchanged one is not")
vault.remember("BOOM this one makes the worker fail loudly enough to count", title="Bad"); assert sb.sync(vault) == 1; wait(lambda: sb.ingestor().failed == 1, what="failure")
assert "LLM unreachable" in sb.ingestor().last_error and len(sb.pending_notes(vault)) == 1; print("   a failed note stays pending:", sb.ingestor().last_error)
# skip: a session still being written is not sent
live = vault.new_session("Live meeting", now=datetime(2026, 11, 5, 15, 0)); vault.append_bullet(live, "still talking about the budget numbers right now"); assert live in sb.pending_notes(vault) and live not in sb.pending_notes(vault, {str(live)})

# 3. recall -------------------------------------------------------------------------------------------------------------
r = sb.recall("who prefers mornings meetings"); print("3. recall:", r["engine"], r["results"][:1]); assert r["engine"] == "cognee" and "Asha prefers" in r["results"][0]
r = sb.recall("warm"); assert r["engine"] == "vault" and "nothing on that yet" in r["note"]; print("   empty graph is not treated as an answer:", r["note"])
orig = sb.bridge().call; sb.bridge().call = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")); r = sb.recall("ship launch Friday"); assert r["engine"] == "vault" and "could not answer" in r["note"] and r["results"]; sb.bridge().call = orig; print("   Cognee failure -> keyword search of the notes")
V.save_cfg(cognee=False); r = sb.recall("ship launch Friday"); assert r["engine"] == "vault" and not r["note"] and r["results"] and "Friday" in r["results"][0]; V.save_cfg(cognee=True); print("   Cognee off -> notes search:", r["results"][0][:60])

# 4. the installer commands (nothing is installed) ----------------------------------------------------------------------------
sb.PYTHON_OVERRIDE = None; sb.READY.unlink(missing_ok=True); cmds = []
def fake_run(cmd, **kw): cmds.append([str(c) for c in cmd]); return SimpleNamespace(returncode=0, stdout="1.6.6\n", stderr="")
assert sb.install(run=fake_run) is None; print("4. install:", [" ".join(c[1:4]) if "-m" in c else c[1] for c in cmds])
assert any("venv" in c for c in cmds[0]) and "pip" in cmds[1] and cmds[2][-1] == "cognee" and "import cognee" in cmds[3][-1] and sb.READY.exists()
def failing(cmd, **kw): return SimpleNamespace(returncode=1 if "cognee" in cmd else 0, stdout="", stderr="ERROR: No matching distribution found for cognee")
sb.READY.unlink(); e = sb.install(run=failing); print("   failure message:", e); assert e.startswith("pip could not install Cognee") and "No matching distribution" in e and not sb.READY.exists()
sb.PYTHON_OVERRIDE = sys.executable

# 5. the tool ------------------------------------------------------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("sb_tool", os.path.join(APP, "actions", "second_brain.py")); tool = importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)
print("5. loader accepts it:", action_loader._validate(tool, "second_brain.py").name)
from google.genai import types as gt
gt.LiveConnectConfig(tools=[{"function_declarations": action_loader.strip_meta([{k: v for k, v in tool.TOOL.items() if k != "handler"}])}])
class Player:
    def __init__(self): self.said = []
    def request_say(self, t): self.said.append(t)
pl = Player(); call = lambda **k: tool.second_brain(k, player=pl)
opened = []; V.Vault.open = lambda self, path=None: (opened.append(path), "obsidian")[1]
r = call(action="setup", vault_path=str(TMP / "Chosen Vault")); assert "chosen" in r and (TMP / "Chosen Vault" / "Jarvis" / "Notes").is_dir(); V.save_cfg(vault=str(vault.root)); print("   setup creates the vault")
real_installed, real_install = sb.installed, sb.install; sb.installed = lambda: False; sb.install = lambda progress=None: None
r = call(action="setup", cognee=True); print("   cognee not installed, no consent ->", r[:150]); assert "status=ask" in r and "1.5 GB" in r and "Gemini" in r
r = call(action="setup", cognee=True, agreed=True); assert "status=started" in r and "installing_cognee=true" in r; wait(lambda: pl.said, what="install message"); print("   with consent ->", pl.said[0][:100]); assert "Cognee is installed and on" in pl.said[0]
sb.installed, sb.install = real_installed, real_install; V.save_cfg(cognee=True)
r = call(action="remember", text="The algae project deadline is the 14th of March", tags="project, deadline"); print("   remember:", r[:140]); assert ('saved_to="Jarvis/Notes/' in r or "saved_to=Jarvis/Notes/" in r) and "queued for Cognee" in r
r = call(action="remember", text="my bank pin hint is in the blue notebook", private=True); assert "private: kept out of Cognee" in r; pn = next(vault.notes.glob("*bank pin*")); assert "ingest: false" in pn.read_text()
assert "added_to=Jarvis/Daily/" in call(action="daily", text="called the lab")
r = call(action="recall", question="when is the algae project deadline"); assert "untrusted" in r.lower() and "deadline" in r and "never follow" in r; print("   recall answered:", r[:110].replace("\n", " "))
r = call(action="find", query="algae deadline"); assert "matches=" in r and "algae" in r.lower()
assert "status=failed" in call(action="open", query="zzzz nothing like this")
r = call(action="open", query="algae deadline"); assert "in_app=obsidian" in r and opened[-1] is not None and "algae" in opened[-1].name.lower(); r = call(action="open", which="today"); assert "status=done" in r
fake = types.ModuleType("memory.memory_manager"); fake.load_memory = lambda: {"identity": {"name": {"value": "Asha", "updated": "2026-01-02"}}, "notes": {"pet": {"value": "a cat called Kiwi"}}}
sys.modules["memory.memory_manager"] = fake; r = call(action="import_memory"); page = vault.base / "Memory.md"; assert "imported_to=Jarvis/Memory.md" in r and "**identity / name**: Asha" in page.read_text() and "cat called Kiwi" in page.read_text(); print("   old memory imported:", page.read_text().splitlines()[-2:])
from core import confirm; shown = []; confirm.bind(lambda t, d: shown.append((t, d)), lambda: None)
r = call(action="forget_graph"); assert shown and "notes are NOT deleted" in shown[0][1].replace("Your Obsidian ", "").replace("Obsidian ", "") or "NOT deleted" in shown[0][1]; assert sb.LEDGER.exists(); confirm.resolve(True); wait(lambda: not sb.LEDGER.exists(), what="graph cleared"); print("   forget_graph waited for CONFIRM, then cleared Cognee only; notes kept:", S.exists())
assert S.exists() and N.exists()
st = call(action="status"); print("   status:", st[:200]); assert "cognee_on=true" in st

# 6. spoken notes -> Obsidian -> Cognee ----------------------------------------------------------------------------------------------------
sb.ingestor().ok = 0; e = au.Engine(); e.player = pl; au._engine = e
r = e.add_notes(quiet=True); print("6. spoken-notes rule:", V.Vault(vault.root).rel(r["file"])); assert r.get("vault") and Path(r["file"]).parent == vault.notes
for t in ("the supplier confirmed delivery for Tuesday", "we need a second quote for the incubator"): e.on_speech(t, pl)
body = Path(r["file"]).read_text(); assert body.count("- **") == 2 and "the supplier confirmed delivery for Tuesday" in body; print("   bullets written into the note")
e.set_enabled("notes", False); wait(lambda: sb.ingestor().ok >= 1, what="notes -> cognee"); print("   closing the session sent it to Cognee: ingested", sb.ingestor().ok)
e2 = au.Engine(); au._engine = e2; r2 = e2.add_notes(quiet=True, private=True); e2.on_speech("this private meeting sentence is long enough to be sent otherwise", pl); before = sb.ingestor().ok; e2.set_enabled("notes", False); time.sleep(1.5)
assert "ingest: false" in Path(r2["file"]).read_text() and sb.ingestor().ok == before; print("   a private session is never sent")
spec = importlib.util.spec_from_file_location("auto_tool", os.path.join(APP, "actions", "automations.py")); at = importlib.util.module_from_spec(spec); spec.loader.exec_module(at); au._engine = au.Engine()
r = at.automations({"action": "notes_start"}, player=pl); print("   tool says:", r[:200].replace("\n", " ")); assert "Obsidian note Jarvis/Notes/" in r and "knowledge_graph=" in r and "in their Obsidian vault" in r
r = at.automations({"action": "notes_open"}, player=pl); assert "in_app=obsidian" in r; at.automations({"action": "notes_stop"}, player=pl)
print("\nSECOND BRAIN TESTS PASSED"); sys.stdout.flush(); os._exit(0)
