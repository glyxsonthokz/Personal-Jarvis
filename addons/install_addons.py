"""
Applies the walkthrough add-on to a Jarvis folder. Safe to run any number of
times: the installer runs it after every download, because a fresh download
replaces prompt.txt and requirements.txt with the upstream versions.

    python install_addons.py <path to the Jarvis folder>
"""
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANCHOR = "[TAGGED MESSAGES]"
SECTIONS = [("[WALKTHROUGHS]", "prompt_walkthroughs.txt"), ("[PHONE LINK]", "prompt_phone.txt"),
            ("[AUTOMATIONS]", "prompt_automations.txt"), ("[VOICE LOCK]", "prompt_voicelock.txt"),
            ("[EARTH]", "prompt_earth.txt"), ("[SCREEN SIGHT]", "prompt_sight.txt")]
PYNPUT = 'pynput; sys_platform != "win32"'


def _newline(raw: bytes) -> str:
    return "\r\n" if b"\r\n" in raw else "\n"


def patch_prompt(app: Path) -> list[str]:
    path = app / "core" / "prompt.txt"
    if not path.exists():
        return ["prompt.txt not found - skipped (the tools still work from their own descriptions)"]
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    nl = _newline(raw)
    notes = []
    for mark, filename in SECTIONS:
        if mark in text:
            notes.append(f"prompt already has {mark}")
            continue
        block = (HERE / filename).read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
        block = block.replace("\n", nl) + nl + nl
        if ANCHOR in text:
            text = text.replace(ANCHOR, block + ANCHOR, 1)
        else:
            text = text.rstrip() + nl + nl + block
        notes.append(f"added {mark} to prompt.txt")
    path.write_bytes(text.encode("utf-8"))
    return notes


def _apply(text: str, nl: str, marker: str, rx, build) -> tuple[str, str]:
    """Apply one anchored patch. Returns (new text, note)."""
    if marker in text:
        return text, f"already has '{marker}'"
    m = rx.search(text)
    if not m or len(rx.findall(text)) != 1:
        return text, f"layout changed - '{marker}' skipped"
    return text[:m.start()] + build(m, nl) + text[m.end():], f"added '{marker}'"


def patch_hud(app: Path) -> list[str]:
    """The "orb" HUD style: a third centrepiece next to the face and the reactor core."""
    import ast
    notes = []

    # ---- ui.py ---------------------------------------------------------------
    path = app / "ui.py"
    if path.exists():
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        nl = _newline(raw)
        original = text
        text, n = _apply(
            text, nl, "addon: orb hud",
            re.compile(r"(?P<head>[ ]+p\.fillRect\(self\.rect\(\), qcol\(C\.BG\)\)\r?\n)(?=\r?\n[ ]+W, H = self\.width\(\), self\.height\(\)\r?\n[ ]+cx, cy = W / 2, H / 2)"),
            lambda m, nl: (m.group("head")
                           + "        if self.hud_style == \"orb\":  # addon: orb hud" + nl
                           + "            try:" + nl
                           + "                from core import orb_hud as _orb" + nl
                           + "                _orb.paint(self, p)" + nl
                           + "                p.end()" + nl
                           + "                return" + nl
                           + "            except Exception as _e:" + nl
                           + "                print(f\"[Orb HUD] fell back to the standard HUD: {_e}\")" + nl
                           + "                self.hud_style = \"core\"" + nl))
        notes.append("ui.py paint: " + n)
        # button label: three styles instead of two
        rx = re.compile(r"self\._hud_btn\.setText\((\"[^\"\r\n]*\") if face\r?\n[ ]+else (\"[^\"\r\n]*\")\)")
        if "HOLOGRAM ORB" in text:
            notes.append("ui.py button: already has the orb label")
        elif rx.search(text):
            text = rx.sub(lambda m: ("self._hud_btn.setText({\"face\": " + m.group(1) + ", \"core\": " + m.group(2)
                                     + ", \"orb\": \"\u25cd  HUD: HOLOGRAM ORB\"}.get(get_hud_style(), " + m.group(2) + "))"), text, count=1)
            notes.append("ui.py button: added the orb label")
        else:
            notes.append("ui.py button: layout changed - skipped")
        tip = re.compile(r"(?P<l>[ ]+)\"Tap to switch to the animated head\.\"\)\r?\n")
        if "addon: orb tip" in text:
            notes.append("ui.py tooltip: already patched")
        elif tip.search(text):
            text = tip.sub(lambda m: (m.group(0) + m.group("l")[:-4] + "if get_hud_style() == \"orb\":  # addon: orb tip" + nl
                                      + m.group("l") + "self._hud_btn.setToolTip(\"A holographic orb over a glowing platform: green when listening, amber when thinking, "
                                      + "orange when speaking. Tap to switch to the animated head.\")" + nl), text, count=1)
            notes.append("ui.py tooltip: added")
        # the toggle cycles face -> core -> orb
        old = 'want = "core" if get_hud_style() == "face" else "face"'
        if "addon: orb cycle" in text:
            notes.append("ui.py toggle: already cycles through the orb")
        elif old in text:
            text = text.replace(old, 'want = {"face": "core", "core": "orb"}.get(get_hud_style(), "face")  # addon: orb cycle', 1)
            msg = re.compile(r'"SYS: HUD switched to the animated face\." if want == "face"\r?\n[ ]+else "SYS: HUD switched to the reactor core\."\)')
            text = msg.sub('{"face": "SYS: HUD switched to the animated face.", "core": "SYS: HUD switched to the reactor core.", '
                           '"orb": "SYS: HUD switched to the hologram orb."}[want])', text, count=1)
            notes.append("ui.py toggle: now cycles face, core, orb")
        else:
            notes.append("ui.py toggle: layout changed - skipped")
        try:
            ast.parse(text.replace("\r\n", "\n"))
            if text != original:
                path.write_bytes(text.encode("utf-8"))
        except SyntaxError as e:
            notes.append(f"ui.py PATCH REJECTED ({e.msg}, line {e.lineno}); ui.py left unchanged")
    else:
        notes.append("ui.py not found - skipped")

    # ---- config_manager: accept "orb", and default to it ---------------------------
    cpath = app / "memory" / "config_manager.py"
    if cpath.exists():
        raw = cpath.read_bytes()
        text = raw.decode("utf-8")
        before = text
        text = text.replace('HUD_STYLES = ("face", "core")', 'HUD_STYLES = ("face", "core", "orb")')
        text = text.replace('load_api_keys().get("hud_style", "face")', 'load_api_keys().get("hud_style", "orb")')
        text = text.replace('return v if v in HUD_STYLES else "face"', 'return v if v in HUD_STYLES else "orb"')
        try:
            ast.parse(text.replace("\r\n", "\n"))
            if text != before:
                cpath.write_bytes(text.encode("utf-8"))
            notes.append("config: orb accepted and default" if text != before else "config: already accepts the orb")
        except SyntaxError as e:
            notes.append(f"config PATCH REJECTED ({e.msg}); left unchanged")
    # ---- switch an existing install over ONCE; later choices are never overridden -----
    flag = app / "config" / ".orb_hud_applied"
    keys = app / "config" / "api_keys.json"
    if not flag.exists() and keys.exists():
        try:
            import json
            data = json.loads(keys.read_text(encoding="utf-8"))
            data["hud_style"] = "orb"
            keys.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            flag.write_text("1", encoding="utf-8")
            notes.append("switched your HUD to the orb (one time; toggle it back any time)")
        except Exception as e:  # noqa: BLE001
            notes.append(f"could not switch the saved HUD style ({e}) - use the HUD button to pick it")
    return notes


def patch_earth(app: Path) -> list[str]:
    """While the Earth is up the HUD underneath must stop drawing (it would be repainted ~20 times
    a second for nothing). One early return, placed in front of the orb hook."""
    import ast
    path = app / "ui.py"
    if not path.exists():
        return ["ui.py not found - skipped"]
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    nl = _newline(raw)
    if "addon: earth cover" in text:
        return ["ui.py: already stops drawing under the Earth"]
    block = lambda i: (f"{i}if getattr(self, \"_earth_cover\", False):  # addon: earth cover{nl}"
                       f"{i}    p.end(){nl}{i}    return{nl}")
    rx = re.compile(r"(?P<i>[ ]+)if self\.hud_style == \"orb\":  # addon: orb hud")
    m = rx.search(text)
    if m:
        text = text[:m.start()] + block(m.group("i")) + text[m.start():]
    else:
        rx2 = re.compile(r"(?P<head>[ ]+p\.fillRect\(self\.rect\(\), qcol\(C\.BG\)\)\r?\n)(?=\r?\n[ ]+W, H = self\.width\(\), self\.height\(\)\r?\n[ ]+cx, cy = W / 2, H / 2)")
        m = rx2.search(text)
        if not m or len(rx2.findall(text)) != 1:
            return ["ui.py layout changed - Earth cover skipped (the Earth still works, the HUD just keeps painting underneath)"]
        text = text[:m.end()] + block("        ") + text[m.end():]
    try:
        ast.parse(text.replace("\r\n", "\n"))
    except SyntaxError as e:
        return [f"ui.py PATCH REJECTED ({e.msg}, line {e.lineno}); left unchanged"]
    path.write_bytes(text.encode("utf-8"))
    return ["ui.py: the HUD stops drawing while the Earth is up"]


_SIGHT_METHOD = (
    "    def _send_media(self, data: bytes, mime: str) -> bool:  # addon: screen sight method\n"
    "        \"\"\"Called from a worker thread. A frame is never sent while asleep, disconnected, or when the\n"
    "        audio queue is busy: the microphone always has priority over the picture.\"\"\"\n"
    "        try:\n"
    "            if getattr(self, \"_wake_enabled\", False) and not getattr(self, \"_awake\", True):\n"
    "                return False\n"
    "            q, loop = getattr(self, \"out_queue\", None), getattr(self, \"_loop\", None)\n"
    "            if getattr(self, \"session\", None) is None or q is None or loop is None or q.qsize() > 120:\n"
    "                return False\n"
    "            loop.call_soon_threadsafe(q.put_nowait, {\"data\": data, \"mime_type\": mime})\n"
    "            return True\n"
    "        except Exception:\n"
    "            return False\n\n")


def patch_sight(app: Path) -> list[str]:
    """Three hooks so screen frames can ride the existing outgoing queue to the live session."""
    import ast
    path = app / "main.py"
    if not path.exists():
        return ["main.py not found - skipped"]
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    nl = _newline(raw)
    notes = []
    # 1. _send_realtime forwards image items as video; a refusal switches sight off instead of breaking audio
    if "addon: screen sight frames" in text:
        notes.append("main.py: already forwards screen frames")
    else:
        rx = re.compile(r"(?P<head>async def _send_realtime\(self\):\r?\n[ ]+while True:\r?\n)(?P<i>[ ]+)msg = await self\.out_queue\.get\(\)\r?\n")
        m = rx.search(text)
        if not m:
            notes.append("main.py layout changed - screen frames skipped")
        else:
            i = m.group("i")
            block = (f"{i}if str(msg.get(\"mime_type\", \"\")).startswith(\"image/\"):  # addon: screen sight frames{nl}"
                     f"{i}    try:{nl}"
                     f"{i}        await self.session.send_realtime_input(video=types.Blob(data=msg[\"data\"], mime_type=msg[\"mime_type\"])){nl}"
                     f"{i}    except Exception as _e:{nl}"
                     f"{i}        print(f\"[ScreenSight] the live session refused a frame: {{_e}}\"){nl}"
                     f"{i}        try:{nl}"
                     f"{i}            from core import screen_sight as _ss{nl}"
                     f"{i}            _ss.stop(f\"the live model would not accept video frames ({{str(_e)[:60]}})\", tell=True){nl}"
                     f"{i}        except Exception:{nl}"
                     f"{i}            pass{nl}"
                     f"{i}    continue{nl}")
            text = text[:m.end()] + block + text[m.end():]
            notes.append("main.py: screen frames are forwarded as video")
    # 2. the helper method
    if "addon: screen sight method" in text:
        notes.append("main.py: already has _send_media")
    else:
        anchor = "    async def _send_realtime(self):"
        k = text.find(anchor)
        if k < 0:
            notes.append("main.py layout changed - _send_media skipped")
        else:
            text = text[:k] + _SIGHT_METHOD.replace("\n", nl) + text[k:]
            notes.append("main.py: added _send_media")
    # 3. hand the UI the helper and let screen sight bind to it
    if "addon: screen sight bind" in text:
        notes.append("main.py: already binds screen sight")
    else:
        rx = re.compile(r"(?P<i>[ ]+)self\.ui\.request_say = self\.plugin_say[^\r\n]*\r?\n")
        m = rx.search(text)
        if not m:
            notes.append("main.py layout changed - screen sight bind skipped")
        else:
            i = m.group("i")
            block = (f"{i}self.ui.send_media = self._send_media  # addon: screen sight bind{nl}"
                     f"{i}try:{nl}{i}    from core import screen_sight as _ss{nl}{i}    _ss.bind(self.ui){nl}"
                     f"{i}except Exception as _e:{nl}{i}    print(f\"[ScreenSight] could not start: {{_e}}\"){nl}")
            text = text[:m.end()] + block + text[m.end():]
            notes.append("main.py: screen sight bound to the window")
    try:
        ast.parse(text.replace("\r\n", "\n"))
    except SyntaxError as e:
        return notes + [f"PATCH REJECTED - it would break main.py ({e.msg}, line {e.lineno}); main.py left unchanged"]
    if text.encode("utf-8") != raw:
        path.write_bytes(text.encode("utf-8"))
    return notes


def patch_branding(app: Path) -> list[str]:
    """The window and footer say JARVIS, not MARK LVI."""
    import ast
    path = app / "ui.py"
    if not path.exists():
        return ["ui.py not found - skipped"]
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    before = text
    text = re.sub(r'(APP_VERSION\s*=\s*)"MARK LVI"', r'\1"JARVIS"', text, count=1)
    text = re.sub(r'self\.setWindowTitle\(f"\{(_display|display)\} [^{"\r\n]{1,3}\{APP_VERSION\}"\)', r'self.setWindowTitle(f"{\1}")', text)
    if text == before:
        return ["ui.py: already says JARVIS"]
    try:
        ast.parse(text.replace("\r\n", "\n"))
    except SyntaxError as e:
        return [f"ui.py branding REJECTED ({e.msg}); left unchanged"]
    path.write_bytes(text.encode("utf-8"))
    return ["ui.py: window title and footer now say JARVIS"]


def patch_dashboard_ip(app: Path) -> str:
    """The dashboard works out this PC's address once, when Jarvis starts, so after
    moving to another Wi-Fi (or a phone hotspot) the QR code still carried the old
    address. Re-read it whenever a new pairing key is made, which is exactly when
    the Remote Control button is pressed."""
    path = app / "dashboard" / "server.py"
    if not path.exists():
        return "dashboard/server.py not found - skipped"
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    if "# addon: refresh ip" in text:
        return "dashboard already refreshes its address"
    nl = "\r\n" if b"\r\n" in raw else "\n"
    anchor = "    def new_key(self, expiry_secs: int = 600) -> str:" + nl
    if anchor not in text:
        return "dashboard layout changed - address refresh skipped"
    patch = (anchor + "        self._ip = _local_ip()  # addon: refresh ip (the network may have changed since start)" + nl)
    path.write_bytes(text.replace(anchor, patch, 1).encode("utf-8"))
    return "dashboard now refreshes its address on every Remote Control press"


def install_adb(app: Path) -> str:
    """Google's platform-tools (adb) next to the app, for phone_link. Windows only
    here; other systems install it with their package manager. Never fatal."""
    if sys.platform != "win32":
        return "phone tools: install adb with your package manager (android-tools / android-platform-tools)"
    adb = app / "tools" / "platform-tools" / "adb.exe"
    if adb.exists():
        return "phone tools already installed"
    try:
        import io
        import urllib.request
        import zipfile
        url = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
        (app / "tools").mkdir(exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            z.extractall(app / "tools")
        return "phone tools installed" if adb.exists() else "phone tools: download did not contain adb"
    except Exception as e:  # noqa: BLE001
        return f"phone tools not installed ({e}) - say 'set up my phone' later and Jarvis will retry"


def patch_requirements(app: Path) -> str:
    path = app / "requirements.txt"
    if not path.exists():
        return "requirements.txt not found - skipped"
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    nl = _newline(raw)
    wanted = [("sherpa-onnx", "sherpa-onnx>=1.10", "voice lock: recognises the owner's voice"),
              ("sgp4", "sgp4>=2.20", "earth view: works out where satellites are"),
              ("pynput", PYNPUT, "guide_me: sees the user's click on macOS / Linux (Windows needs nothing)")]
    added = []
    for name, line, why in wanted:
        if name not in text:
            text = text.rstrip() + nl + nl + "# " + why + nl + line + nl
            added.append(name)
    if added:
        path.write_bytes(text.encode("utf-8"))
    return "added to requirements.txt: " + ", ".join(added) if added else "requirements already complete"


_MAIN_PATCHES = [
    ("addon: voice lock", re.compile(r"(?P<i>[ ]+)if self\._ptt_enabled and not self\._ptt_held:\r?\n[ ]+return\r?\n"),
     lambda i, nl: (
         f"{i}# addon: voice lock - only the enrolled voice goes to Gemini{nl}"
         f"{i}try:{nl}"
         f"{i}    from core import voice_gate as _vg, automations as _au{nl}"
         f"{i}    if _vg.active() and not _au.notes_all_voices() and not _vg.feed(indata, loop, self.out_queue):{nl}"
         f"{i}        return{nl}"
         f"{i}except Exception:{nl}"
         f"{i}    pass{nl}")),
    ("addon: notes hook", re.compile(r"(?P<i>[ ]+)self\.ui\.write_log\(f\"You: \{full_in\}\"\)\r?\n"),
     lambda i, nl: (
         f"{i}try:  # addon: notes hook - the note-taker hears every finished sentence{nl}"
         f"{i}    from core import automations as _au{nl}"
         f"{i}    _au.on_speech(full_in, self.ui){nl}"
         f"{i}except Exception:{nl}"
         f"{i}    pass{nl}")),
]


def patch_main(app: Path) -> list[str]:
    """Three small hooks in main.py. Each is anchored on exact upstream lines, applied
    once, and the result must still be valid Python or nothing is written."""
    import ast
    path = app / "main.py"
    if not path.exists():
        return ["main.py not found - skipped"]
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    nl = _newline(raw)
    notes = []
    for marker, rx, make in _MAIN_PATCHES:
        if marker in text:
            notes.append(f"main.py already has '{marker}'")
            continue
        m = rx.search(text)
        if not m:
            notes.append(f"main.py layout changed - '{marker}' skipped")
            continue
        text = text[:m.end()] + make(m.group("i"), nl) + text[m.end():]
        notes.append(f"main.py: added '{marker}'")
    # mute the model's voice while notes are being taken
    quiet_old = "if self._interrupted:" + nl + " " * 28 + "pass  # discard: interrupted"
    quiet_new = ("if self._interrupted or __import__(\"core.automations\", fromlist=[\"quiet\"]).quiet():  # addon: quiet"
                 + nl + " " * 28 + "pass  # discard: interrupted (or muted while taking notes)")
    if "# addon: quiet" in text:
        notes.append("main.py already has 'addon: quiet'")
    elif quiet_old in text:
        text = text.replace(quiet_old, quiet_new, 1)
        notes.append("main.py: added 'addon: quiet'")
    else:
        notes.append("main.py layout changed - 'addon: quiet' skipped")
    try:
        ast.parse(text.replace("\r\n", "\n"))
    except SyntaxError as e:
        return notes + [f"PATCH REJECTED - it would break main.py ({e.msg}, line {e.lineno}); main.py left unchanged"]
    if text.encode("utf-8") != raw:
        path.write_bytes(text.encode("utf-8"))
    return notes


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    app = Path(sys.argv[1]).resolve()
    if not (app / "main.py").exists():
        print(f"{app} does not look like a Jarvis folder (no main.py).")
        return 1
    (app / "actions").mkdir(exist_ok=True)
    for sub in ("actions", "core"):
        (app / sub).mkdir(exist_ok=True)
        for src in sorted((HERE / sub).glob("*.py")):
            shutil.copyfile(src, app / sub / src.name)
            print(f"  {sub}/{src.name} installed")
    for note in patch_prompt(app):
        print(" ", note)
    for note in patch_main(app):
        print(" ", note)
    print(" ", patch_requirements(app))
    for note in patch_hud(app):
        print(" ", note)
    for note in patch_earth(app):
        print(" ", note)
    for note in patch_sight(app):
        print(" ", note)
    for note in patch_branding(app):
        print(" ", note)
    print(" ", patch_dashboard_ip(app))
    print(" ", install_adb(app))
    return 0


if __name__ == "__main__":
    sys.exit(main())
