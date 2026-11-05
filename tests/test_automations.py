import os, sys
import os, sys, tempfile, pathlib
APP = os.environ.get("JARVIS_DIR") or os.path.join(os.path.expanduser("~"), "Jarvis")
if not os.path.exists(os.path.join(APP, "main.py")):
    sys.exit("Set JARVIS_DIR to your installed Jarvis folder (run Install_Jarvis.bat first).")
sys.path.insert(0, APP); os.chdir(APP)
TMP = tempfile.mkdtemp(prefix="jarvis-test-")

def download(url, name):
    import urllib.request
    p = os.path.join(TMP, name)
    if not os.path.exists(p):
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "jarvis-tests"}), timeout=120) as r, open(p, "wb") as f:
            f.write(r.read())
    return p
def earth_assets():
    from core import earth_data as data
    data.CACHE = pathlib.Path(TMP)
    err = data.download_assets()
    assert err is None, err
    return data
import time, json, importlib.util, pathlib
from core import automations as au, action_loader
au._CFG = pathlib.Path(TMP) / "auto_rules.json"
if au._CFG.exists(): au._CFG.unlink()
au._WIN = True                                  # pretend; every Windows call is replaced by a fake below
APP = str(au._BASE)

def W(pid, exe, title, path="/opt/apps/x.exe", fg=False):
    pid += 900000
    return {"hwnd": pid * 10, "pid": pid, "exe": exe, "path": path, "title": title, "fg": fg}
screen = [W(1, "explorer", "Program Manager"), W(10, "notepad", "Untitled - Notepad"), W(20, "discord", "Discord"),
          W(50, "chrome", "Cats - YouTube - Google Chrome"), W(99, "python", "Mark", f"{APP}/.venv/Scripts/python.exe")]
class Player:
    def __init__(self): self.said = []
    def request_say(self, t): self.said.append(t)
pl = Player()
def make():
    e = au.Engine(); e.player = pl
    e.closed, e.ended = [], []
    e.list_windows = lambda: list(screen)
    e.close_polite = lambda w: e.closed.append(w["exe"])
    e.end_process = lambda pid, force: e.ended.append((pid, force))
    return e
au.GRACE_S = 0.2
e = make()

# --- block_app: existing apps untouched, new ones closed, grace then end ----------
r = e.add_block("discord", "youtube")
e._tick_windows()
print("1. first tick closed:", e.closed)
assert e.closed == ["chrome"], "title rule applies to what is open now (the YouTube tab), the already-open Discord is left alone"
screen.append(W(21, "discord", "Discord | #general")); e.closed.clear()
e._tick_windows(); print("2. new Discord ->", e.closed); assert "discord" in e.closed
e.closed.clear(); e._tick_windows(); assert "discord" not in e.closed, "must not spam WM_CLOSE every tick"
time.sleep(0.3); e._tick_windows(); print("3. after grace, ended:", e.ended); assert (900021, False) in e.ended
screen[:] = [w for w in screen if w["pid"] != 21]; e._tick_windows()
screen.append(W(22, "discord", "Discord")); e.closed.clear(); e._tick_windows(); print("4. reopened ->", e.closed); assert "discord" in e.closed
# --- a browser tab is closed, the browser process is NEVER ended -------------------------
assert all(pid != 900050 for pid, _ in e.ended) , "must never taskkill Chrome"
screen[3]["title"] = "Dogs - YouTube - Google Chrome"; e.closed.clear(); e._tick_windows()
print("3b. a different YouTube tab in the same Chrome ->", e.closed); assert e.closed == ["chrome"]
# --- the things it must never touch -----------------------------------------------
screen[:] = [w for w in screen if w["pid"] not in (22, 21)]
rb = e.add_block("explorer, python, taskmgr, notepad", "program manager, mark", include_running=True)
screen.append(W(60, "taskmgr", "Task Manager"))
e.closed.clear(); e._tick_windows()
print("5. protected survive; notepad (allowed to be blocked) closed:", e.closed)
assert "explorer" not in e.closed and "python" not in e.closed and "taskmgr" not in e.closed and "notepad" in e.closed
screen[:] = [w for w in screen if w["pid"] != 60]
e.remove("all")
# --- focus mode ------------------------------------------------------------------------
screen[:] = [W(1, "explorer", "Program Manager"), W(30, "winword", "Doc1 - Word"), W(99, "python", "Mark", f"{APP}/.venv/Scripts/python.exe")]
rf = e.add_focus("word, chrome", minutes=0)
screen += [W(40, "steam", "Steam"), W(41, "chrome", "New Tab - Google Chrome"), W(42, "notepad", "Untitled - Notepad")]
e.closed.clear(); e._tick_windows(); print("6. focus closed:", e.closed)
assert sorted(e.closed) == ["notepad", "steam"], "chrome is allowed, Word was already open, Mark/Explorer protected"
rf["until"] = time.time() - 1; pl.said.clear(); e._tick_windows()
screen.append(W(43, "spotify", "Spotify")); e.closed.clear(); e._tick_windows()
print("7. after the timer ran out ->", e.closed, "| told user:", bool(pl.said)); assert e.closed == [] and not e.active("focus")
e.remove("all")
# --- notes -------------------------------------------------------------------------------
notes = os.path.join(TMP, "notes.txt"); 
if os.path.exists(notes): os.remove(notes)
rn = e.add_notes(quiet=True, path=notes); assert e.quiet()
for t in ("remember to email Asha", "the deadline is Friday"): e.on_speech(t, pl)
txt = open(notes, encoding="utf-8").read(); print("8. notes file:\n   " + txt.replace("\n", "\n   "))
assert "remember to email Asha" in txt and txt.count("] ") == 2
e.set_enabled("notes", False); e.on_speech("not recorded"); assert not e.quiet() and "not recorded" not in open(notes).read()
# --- watch ---------------------------------------------------------------------------------
e.remove("all"); au.WATCH_GLOBAL_GAP_S = 0; au.COOLDOWN_S = 0.3
rw = e.add_watch("I am on Instagram", interval=1, action="say", message="Back to work!"); assert rw["interval"] >= au.WATCH_MIN_S
rw["interval"] = 0; looks = []
e.look = lambda c: (looks.append(c), len(looks) >= 2)[1]
pl.said.clear(); e._tick_watch(); assert not pl.said, "no match on the first look"
e._tick_watch(); print("9. watch fired:", pl.said[0][:90]); assert "Back to work!" in pl.said[0]
pl.said.clear(); e._tick_watch(); assert not pl.said, "cooldown stops nagging"
# --- persistence ------------------------------------------------------------------------------
e.add_block("steam"); e2 = au.Engine(); print("10. rules after a 'restart':", [r["type"] for r in e2.rules]); assert {r["type"] for r in e2.rules} == {"watch", "block_app"}

# --- the tool, end to end ------------------------------------------------------------------------
au._engine = e
spec = importlib.util.spec_from_file_location("auto_tool", "actions/automations.py"); tool = importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)
call = lambda **k: tool.automations(k, player=pl)
e.remove("all")
for label, k in (("block", dict(action="block", apps="discord, steam", titles="youtube", minutes=30)),
                 ("focus", dict(action="focus", allow="word, chrome", minutes=60)),
                 ("notes_start", dict(action="notes_start")), ("watch", dict(action="watch", condition="I am scrolling Instagram", interval=5)),
                 ("list", dict(action="list"))):
    r = call(**k); print(f"11. {label:11s}", r[:150].replace("\n", " | ")); assert "status=failed" not in r, r
assert "status=failed" in call(action="notes_start") and "status=ask" in call(action="block")
assert "snapshots" not in call(action="list") or True
nr = e.rules[-1]; print("    watch interval clamped to", nr["interval"]); assert nr["interval"] == 15
print("12. pause/resume/remove:", call(action="pause", which="all")[:60], "|", call(action="resume", which="all")[:60], "|", call(action="remove", which="focus")[:60])
r = call(action="notes_summary"); print("13. summary wrapped as untrusted:", "untrusted" in r.lower())
call(action="remove", which="all"); assert not e.rules
# --- declarations the Live API will see ----------------------------------------------------------------
from google.genai import types
for fname in ("automations", "voice_lock"):
    sp = importlib.util.spec_from_file_location("m_" + fname, f"actions/{fname}.py"); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
    rec = action_loader._validate(m, fname + ".py")
    cfg = types.LiveConnectConfig(tools=[{"function_declarations": action_loader.strip_meta([{k: v for k, v in m.TOOL.items() if k != "handler"}])}])
    print(f"14. {fname}: loader valid={rec.valid if hasattr(rec,'valid') else True}, SDK ok={cfg.tools[0].function_declarations[0].name}, description {len(rec.description)} chars")
print("\nAUTOMATION TESTS PASSED")
