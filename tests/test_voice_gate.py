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
import time, wave, shutil, os
import numpy as np
from core import voice_gate as vg
vg._CFG = pathlib.Path(TMP) / "voice_lock.json"
if vg._CFG.exists(): vg._CFG.unlink()
vg._MODEL = pathlib.Path(TMP) / "wespeaker.onnx"; assert vg.ensure_sherpa() is None and vg.ensure_model() is None
def load(f):
    w = wave.open(download("https://github.com/csukuangfj/sr-data/raw/main/test/3d-speaker/" + f, f)); return np.frombuffer(w.readframes(w.getnframes()), np.int16)
A, B, C = load("speaker1_a_cn_16k.wav"), load("speaker1_b_cn_16k.wav"), load("speaker2_a_cn_16k.wav")
print("speech rms (int16):", [int(np.sqrt(np.mean(x.astype(float)**2))) for x in (A, B, C)])
rng = np.random.default_rng(0)
silence = lambda s: (rng.normal(0, 20, int(s * 16000))).astype(np.int16)

class Loop:
    def call_soon_threadsafe(self, fn, *a): fn(*a)
class Q:
    def __init__(self): self.items = []
    def put_nowait(self, m): self.items.append(m)
loop, q = Loop(), Q()
gate = vg.VoiceGate()

def stream(sig, pace=0.004):
    sent = 0
    for i in range(0, len(sig) - 1023, 1024):
        blk = sig[i:i+1024].reshape(-1, 1)
        before = len(q.items)
        if gate.feed(blk, loop, q): q.items.append({"data": blk.tobytes()})
        time.sleep(pace)
    time.sleep(0.4)

# --- no enrolment yet: everything passes -------------------------------------
n0 = len(q.items); stream(silence(0.3)); 
print("not engaged -> passes audio:", len(q.items) - n0 > 0)

# --- enrol on the owner (clips A+B) ------------------------------------------
done = {}
gate.start_enroll(7.0, lambda r: done.update(r))
stream(np.concatenate([silence(0.5), A, silence(0.3), B, silence(0.5)]))
t0 = time.time()
while not done and time.time() - t0 < 10: time.sleep(0.1)
print("enrol result:", done, "| engaged:", gate.engaged())
assert done.get("ok")

def speech_blocks_forwarded(sig):
    q.items.clear(); stream(np.concatenate([silence(1.0), sig, silence(2.2)]))
    return len(q.items), (len(sig) // 1024)

for label, sig, want in (("owner (clip B)", B, True), ("someone else (speaker 2)", C, False), ("owner again (clip A)", A, True), ("someone else again", C, False)):
    got, total = speech_blocks_forwarded(sig)
    print(f"{label:26s} forwarded {got:3d} blocks of ~{total} speech  | recent: {list(gate.recent)[-1]}")
    assert (got >= total * 0.6) if want else (got == 0), label
# --- someone cuts in mid-sentence: gate must close -----------------------------
q.items.clear()
stream(np.concatenate([silence(0.8), B, C, C, silence(2.2)]), pace=0.03)
fw = len(q.items); print(f"owner then other talking over {len(B)//1024 + 2*(len(C)//1024)} blocks -> forwarded {fw}", "| recent:", list(gate.recent)[-2:])
assert fw < 215, f"a talk-over by someone else must close the gate (forwarded {fw})"
print("status:", {k: v for k, v in gate.status().items() if k != "recent"})
gate.set_enabled(False); assert not gate.engaged(); gate.set_enabled(True)
# --- a broken model must fail open, not deafen the assistant ---------------------
g2 = vg.VoiceGate(embedder=lambda a: (_ for _ in ()).throw(RuntimeError("boom"))); g2._centroid = gate._centroid; g2._enabled = True
q.items.clear(); 
for i in range(0, 40*1024, 1024):
    blk = np.concatenate([B])[i:i+1024].reshape(-1, 1)
    if g2.feed(blk, loop, q): q.items.append({"data": 1})
    time.sleep(0.004)
time.sleep(0.5); print("broken model -> engaged:", g2.engaged(), "| problem:", g2.status()["problem"])
assert not g2.engaged()
print("\nVOICE GATE TESTS PASSED")
