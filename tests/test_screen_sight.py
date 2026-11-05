import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
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
import time, threading, importlib.util, pathlib
from PIL import Image, ImageDraw
from PyQt6.QtWidgets import QApplication, QWidget
app = QApplication([])
from core import screen_sight as ss, action_loader
ss._CFG = pathlib.Path(TMP) / "sight_cfg.json"
if ss._CFG.exists(): ss._CFG.unlink()

def frame(shade=40, box=None):
    im = Image.new("RGB", (1920, 1080), (shade, shade, shade))
    if box: ImageDraw.Draw(im).rectangle(box, fill=(250, 250, 250))
    return im
class Player(QWidget):
    def __init__(self): super().__init__(); self.sent = []; self.said = []; self.accept = True; self.resize(900, 600)
    def send_media(self, data, mime): 
        if not self.accept: return False
        self.sent.append((len(data), mime)); return True
    def request_say(self, t): self.said.append(t)
pl = Player(); pl.show()
clock = [100.0]; state = {"img": frame(), "title": "Notepad"}
e = ss.ScreenSight(capture=lambda: state["img"], fg_title=lambda: state["title"], clock=lambda: clock[0])
e.player = pl; e.keepalive = 30
# tick() is the whole decision; run it directly with a fake clock
e.running = True
assert e.tick() == "sent" and pl.sent[0][1] == "image/jpeg" and 5_000 < pl.sent[0][0] < 400_000; print("1. first look sent:", pl.sent[0])
clock[0] += 3; assert e.tick() == "unchanged"; clock[0] += 3; assert e.tick() == "unchanged"; print("2. still screen not resent:", e.skipped_static, "skipped")
state["img"] = frame(box=(200, 200, 900, 700)); clock[0] += 3; assert e.tick() == "sent"; print("3. changed screen sent")
clock[0] += 31; assert e.tick() == "sent", "an unchanged screen is refreshed after the keepalive"; print("4. keepalive refresh sent")
for t in ("Chrome - Bitwarden", "Sign in to your bank - Google Chrome", "InPrivate browsing - Edge", "Firefox Private Browsing", "Password Manager"):
    state["title"] = t; state["img"] = frame(shade=90); clock[0] += 3; n = len(pl.sent); assert e.tick() == "private" and len(pl.sent) == n, t
print("5. private windows never sent:", e.skipped_private, "skipped | last blocked:", repr(e.last_blocked))
state["title"] = "Notepad"; clock[0] += 3; assert e.tick() == "sent", "after leaving the private window the screen is shown afresh"; print("6. resumed after the private window")
e.extra_words = ["secret project"]; state["title"] = "Secret Project plan.docx"; state["img"] = frame(shade=130); clock[0] += 3; assert e.tick() == "private"; print("7. user-added word respected")
state["title"] = "Notepad"; pl.accept = False; state["img"] = frame(shade=160); clock[0] += 3; assert e.tick() == "dropped" and e.dropped == 1; print("8. frame refused by the session -> dropped, not counted as sent")
pl.accept = True; e.until = clock[0] + 10; clock[0] += 3; assert e.tick() in ("sent", "unchanged"); clock[0] += 10
assert e.tick() == "expired" and not e.running and pl.said and "SCREEN SIGHT" in pl.said[-1]; print("9. timer ran out -> stopped and the model told:", pl.said[-1][:80])

# the real thread, badge and tool, with real time
real = ss.ScreenSight(capture=lambda: state["img"], fg_title=lambda: "Notepad"); real.player = pl; ss._engine = real
spec = importlib.util.spec_from_file_location("sight_tool", "actions/screen_sight.py"); tool = importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)
print("loader:", action_loader._validate(tool, "screen_sight.py").name)
from google.genai import types
cfg = types.LiveConnectConfig(tools=[{"function_declarations": action_loader.strip_meta([{k: v for k, v in tool.TOOL.items() if k != "handler"}])}]); print("SDK accepts declaration")
out = []
def script():
    pl2 = Player(); 
    r = tool.screen_sight({"action": "on", "interval": 1, "avoid": "diary, taxes"}, player=pl); out.append(r); print("on    ->", r[:210].replace("\n", " "))
    assert "on=true" in r and "Gemini" in r and "badge" in r
    time.sleep(2.6); st = real.status(); print("status:", {k: st[k] for k in ("on", "frames_sent", "skipped_unchanged", "extra_private_words")}); assert st["on"] and st["frames_sent"] >= 1
    b = pl._sight_badge; print("badge visible:", b.isVisible(), "| text:", b.text()); assert b.isVisible()
    r = tool.screen_sight({"action": "avoid", "avoid": "payroll"}, player=pl); assert "payroll" in r
    r = tool.screen_sight({"action": "on"}, player=pl); assert "already_on=true" in r
    out.append("done")
t = threading.Thread(target=script, daemon=True); t.start(); t0 = time.time()
while t.is_alive() and time.time() - t0 < 30: app.processEvents(); time.sleep(0.01)
assert out and out[-1] == "done", out
# click the badge: the kill switch
pl.said.clear(); pl._sight_badge.mousePressEvent(None); time.sleep(0.2); app.processEvents()
print("after clicking the badge: running =", real.running, "| badge visible =", pl._sight_badge.isVisible(), "| model told:", bool(pl.said))
assert not real.running and not pl._sight_badge.isVisible() and pl.said
# voice off + persistence + bind
real.stop(); pl.said.clear()
r = tool.screen_sight({"action": "on", "persist": True, "interval": 2}, player=pl); assert "persists_after_restart=true" in r
import json; print("saved:", json.loads(ss._CFG.read_text())); assert json.loads(ss._CFG.read_text())["persist"] is True
real.running = False; real._stop.set(); time.sleep(0.1)   # simulate quitting the app without a clean stop
fresh = ss.ScreenSight(capture=lambda: state["img"], fg_title=lambda: "Notepad"); ss._engine = fresh; ss.bind(pl)
print("fresh engine after a restart resumed:", fresh.running); assert fresh.running
r = tool.screen_sight({"action": "off"}, player=pl); assert "was_on=true" in r and not fresh.running and not json.loads(ss._CFG.read_text())["persist"]
print("off clears persistence:", not json.loads(ss._CFG.read_text())["persist"])
# no hook available (old main.py): must say so rather than pretend
class Bare(QWidget): pass
ss._engine = ss.ScreenSight(); assert "status=failed" in tool.screen_sight({"action": "on"}, player=Bare())
print("\nSCREEN SIGHT TESTS PASSED"); sys.stdout.flush(); os._exit(0)
