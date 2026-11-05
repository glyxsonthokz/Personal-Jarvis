"""
Jarvis health check. Writes a report you can paste back to whoever is helping.
Secrets are never printed: API keys are reported only as "set" / "not set", and
anything shaped like a key or token is blanked out of the captured output.

    python diagnose.py <Jarvis folder> [--seconds N]
"""
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

APP = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Jarvis"
SECONDS = 40
if "--seconds" in sys.argv:
    SECONDS = int(sys.argv[sys.argv.index("--seconds") + 1])
WIN = sys.platform == "win32"
PY = sys.executable
lines: list[str] = []

_SECRET = re.compile(r"AIza[0-9A-Za-z_\-]{20,}|Bearer\s+[\w\-\.=]{10,}|sk-[A-Za-z0-9_\-]{16,}|(?i:key|token)=[\w\-]{8,}")


def out(s: str = "") -> None:
    s = _SECRET.sub("<hidden>", s)
    print(s)
    lines.append(s)


def section(t: str) -> None:
    out()
    out("== " + t)


def run(cmd, timeout=30, cwd=None):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -9, "TIMED OUT after %ss" % timeout
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


section("This computer")
import platform
out(f"{platform.platform()} | Python {sys.version.split()[0]} | {PY}")
out(f"app folder: {APP} | exists: {APP.exists()}")
if not (APP / "main.py").exists():
    out("main.py NOT FOUND - Jarvis is not installed in that folder.")
    sys.exit(1)

section("Add-on files")
acts = sorted(p.name for p in (APP / "actions").glob("*.py"))
out(f"{len(acts)} action files. guide_me.py: {'guide_me.py' in acts} | phone_link.py: {'phone_link.py' in acts}")
prompt = (APP / "core" / "prompt.txt").read_text(encoding="utf-8", errors="replace") if (APP / "core" / "prompt.txt").exists() else ""
out(f"prompt has [WALKTHROUGHS]: {'[WALKTHROUGHS]' in prompt} | [PHONE LINK]: {'[PHONE LINK]' in prompt}")

section("Needed packages (each imported on its own, so a crash cannot hide)")
for mod in ("fastapi", "uvicorn", "cryptography", "multipart", "qrcode", "PyQt6.QtWidgets",
            "google.genai", "pyautogui", "sounddevice"):
    rc, text = run([PY, "-c", f"import {mod}"], timeout=60)
    out(f"{mod:18s} {'ok' if rc == 0 else 'PROBLEM: ' + text.strip().splitlines()[-1][:140] if text.strip() else 'PROBLEM (rc %s)' % rc}")

section("Settings (names only - values are never shown)")
cfg = APP / "config" / "api_keys.json"
if cfg.exists():
    try:
        import json
        data = json.loads(cfg.read_text(encoding="utf-8"))
        for k, v in data.items():
            if "key" in k.lower() or "token" in k.lower() or "secret" in k.lower():
                out(f"  {k}: {'set' if v else 'NOT SET'}")
            elif isinstance(v, (bool, int)) or k in ("dashboard_https",):
                out(f"  {k}: {v}")
            else:
                out(f"  {k}: (present)")
    except Exception as e:  # noqa: BLE001
        out(f"could not read settings: {e}")
else:
    out("config/api_keys.json does not exist yet (first-run setup not completed?)")

section("Is Jarvis running right now?")
if WIN:
    for exe in ("pythonw.exe", "python.exe"):
        rc, text = run(["tasklist", "/fi", f"imagename eq {exe}"])
        out(f"{exe}: " + ("running" if exe in text and "No tasks" not in text else "not running"))
else:
    rc, text = run(["pgrep", "-fa", "main.py"])
    out(text.strip() or "no main.py process")

def port_state(port: int) -> str:
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", port))
        return "LISTENING (something answered)"
    except Exception as e:  # noqa: BLE001
        return f"nothing listening ({e.__class__.__name__})"
    finally:
        s.close()

section("Dashboard ports (before we start the app)")
for p in (8000, 8001):
    out(f"port {p}: {port_state(p)}")

section("Dashboard module")
rc, text = run([PY, "-c", "import dashboard.server as d; print('deps_ok =', d._DEPS_OK); print('url:', d.PORT, d.SECURE_PORT)"], cwd=str(APP), timeout=90)
out(text.strip()[-600:])

section("Every action file loads")
bad = 0
for name in acts:
    code = ("import sys,importlib.util as u;sys.path.insert(0,'.');"
            f"s=u.spec_from_file_location('x',r'actions/{name}');m=u.module_from_spec(s);s.loader.exec_module(m)")
    rc, text = run([PY, "-c", code], cwd=str(APP), timeout=60)
    if rc != 0:
        bad += 1
        out(f"  {name}: FAILED - {text.strip().splitlines()[-1][:160] if text.strip() else rc}")
out(f"  {len(acts) - bad} of {len(acts)} load cleanly")

if WIN:
    section("Windows Firewall (inbound rules that mention Jarvis / Python / ports 8000-8001)")
    rc, text = run(["netsh", "advfirewall", "firewall", "show", "rule", "name=all", "dir=in", "verbose"], timeout=90)
    shown = 0
    for block in re.split(r"\r?\n\r?\n", text):
        low = block.lower()
        if ("mark-lvi" in low or "jarvis" in low or "python" in low or re.search(r"localport:\s*.*800[01]", low)):
            keep = [l.strip() for l in block.splitlines() if re.match(r"\s*(Rule Name|Enabled|Action|Program|LocalPort|Profiles):", l)]
            if keep:
                out("  " + " | ".join(keep)[:300])
                shown += 1
    if not shown:
        out("  (no rules found for Jarvis / Python / ports 8000-8001 - Windows will block phones by default)")
    rc, text = run(["netsh", "advfirewall", "show", "allprofiles", "state"])
    out("  " + " ".join(text.split())[:200])
    section("Network addresses")
    rc, text = run(["ipconfig"])
    for l in text.splitlines():
        if "IPv4" in l or l.strip().endswith(":") and "adapter" in l:
            out("  " + l.strip())

section(f"Starting Jarvis for {SECONDS}s to capture what it says")
env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
captured: list[str] = []
try:
    proc = subprocess.Popen([PY, "main.py"], cwd=str(APP), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    import threading
    def pump():
        for raw in iter(proc.stdout.readline, b""):
            captured.append(raw.decode("utf-8", "replace").rstrip())
    t = threading.Thread(target=pump, daemon=True); t.start()
    t0 = time.time()
    while time.time() - t0 < SECONDS and proc.poll() is None:
        time.sleep(0.5)
        if time.time() - t0 > SECONDS * 0.75 and not any("port" in l for l in lines[-3:]):
            pass
    out(f"after {int(time.time() - t0)}s: " + ("process had already EXITED with code %s" % proc.returncode if proc.poll() is not None else "still running"))
    for p in (8000, 8001):
        out(f"port {p}: {port_state(p)}")
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:  # noqa: BLE001
            proc.kill()
    t.join(3)
except Exception as e:  # noqa: BLE001
    out(f"could not start Jarvis: {e}")
out("--- last lines Jarvis printed ---")
for l in captured[-70:]:
    out("  " + l[:220])

report = "\n".join(lines)
dest = None
for folder in (Path.home() / "Desktop", Path.home() / "OneDrive" / "Desktop", APP):
    try:
        (folder / "Jarvis-report.txt").write_text(report, encoding="utf-8")
        dest = folder / "Jarvis-report.txt"
        break
    except Exception:  # noqa: BLE001
        continue
print(f"\nReport saved to: {dest}")
