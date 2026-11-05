"""
Installs the add-ons onto a FRESH download of the original Mark-LVI and checks every patch.

This is the test that tells you the day an upstream change breaks one of the hooks: each patch is
anchored on exact upstream lines, and if an anchor moves the installer reports "layout changed".
Needs internet. Does not touch your installed copy.
"""
import ast, io, os, re, subprocess, sys, tempfile, urllib.request, zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
URL = "https://github.com/FatihMakes/Mark-LVI/archive/refs/heads/main.zip"
tmp = Path(tempfile.mkdtemp(prefix="jarvis-patch-test-"))
with urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": "jarvis-tests"}), timeout=120) as r:
    zipfile.ZipFile(io.BytesIO(r.read())).extractall(tmp)
app = tmp / "Mark-LVI-main"
(app / "config").mkdir(exist_ok=True)
(app / "config" / "api_keys.json").write_text('{"hud_style": "face", "gemini_api_key": "x"}', encoding="utf-8")

def install():
    p = subprocess.run([sys.executable, str(REPO / "addons" / "install_addons.py"), str(app)], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout
first = install()
print(first)
bad = [l for l in first.splitlines() if "layout changed" in l or "REJECTED" in l or "not found" in l]
assert not bad, "an upstream change broke a patch:\n" + "\n".join(bad)

MARKERS = {"main.py": ["addon: voice lock", "addon: notes hook", "addon: quiet", "addon: screen sight frames", "addon: screen sight method", "addon: screen sight bind"],
           "ui.py": ["addon: orb hud", "addon: earth cover", "HOLOGRAM ORB", "addon: orb cycle"],
           "dashboard/server.py": ["addon: refresh ip"], "memory/config_manager.py": ['("face", "core", "orb")'],
           "core/prompt.txt": ["[WALKTHROUGHS]", "[PHONE LINK]", "[AUTOMATIONS]", "[VOICE LOCK]", "[EARTH]", "[SCREEN SIGHT]"]}
for f, marks in MARKERS.items():
    t = (app / f).read_text(encoding="utf-8")
    for m in marks:
        assert m in t, f"{f} is missing {m!r}"
    if f.endswith(".py"):
        ast.parse(t.replace("\r\n", "\n"))
assert 'APP_VERSION  = "JARVIS"' in (app / "ui.py").read_text(encoding="utf-8")
for name in ("guide_me", "phone_link", "voice_lock", "automations", "earth", "screen_sight"):
    assert (app / "actions" / f"{name}.py").exists(), name
for name in ("voice_gate", "automations", "earth_geo", "earth_data", "earth_view", "orb_hud", "screen_sight", "gui_call"):
    assert (app / "core" / f"{name}.py").exists(), name
before = {p: p.read_bytes() for p in (app / "main.py", app / "ui.py", app / "core" / "prompt.txt", app / "requirements.txt")}
install()
for p, b in before.items():
    assert p.read_bytes() == b, f"{p.name} changed on the second run: the installer is not idempotent"
for p in (app / "main.py", app / "ui.py"):
    raw = p.read_bytes()
    assert (b"\r\n" in raw) == (b"\r\n" in before[p]) and not (b"\r\n" in raw and re.search(rb"[^\r]\n", raw)), f"{p.name}: mixed line endings"
print("\nINSTALLER PATCHES OK on a fresh download of upstream")
