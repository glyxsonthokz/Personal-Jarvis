"""
core/second_brain.py — Obsidian for the notes, Cognee for remembering them.

OBSIDIAN (core/vault.py) holds everything as plain Markdown the user can read and edit. That is the
source of truth; it works with nothing else installed.

COGNEE is optional and sits on top: it reads those notes, builds a knowledge graph from them (people,
projects, decisions, dates and how they connect), and answers questions such as "what did we decide about
the launch?" from the whole history rather than from keyword matches. It lives in its own folder:

    second_brain/venv/        a private Python environment holding Cognee (~1.5 GB; nothing else needs it)
    second_brain/worker.py    the process that talks to Cognee (JSON lines over stdin/stdout)
    second_brain/data/        Cognee's databases: SQLite, a graph file and LanceDB, all local
    second_brain/ledger.json  which notes have already been given to Cognee

PRIVACY, plainly: building the graph sends the TEXT OF THE NOTES to Gemini (the same provider Jarvis already
uses, with the same key) so that a language model can extract the entities and relations, and the
questions asked are embedded the same way. Cognee's own databases never leave the PC. Notes marked
`ingest: false` (the "private" option) are never sent. Cognee is OFF until the user turns it on.

If Cognee is off, not installed, or fails, everything still works: remembering writes to Obsidian and
recalling searches the vault by keyword.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from core import vault as vault_mod

_BASE = Path(__file__).resolve().parent.parent
ROOT = _BASE / "second_brain"
WORKER = ROOT / "worker.py"
READY = ROOT / "ready.json"
LEDGER = ROOT / "ledger.json"
LOG = ROOT / "worker.log"
PYTHON_OVERRIDE: Optional[str] = None        # tests point this at a stand-in interpreter
DEFAULT_LLM = "gemini/gemini-2.5-flash"
DEFAULT_EMBED = "gemini/gemini-embedding-001"
DATASET = "jarvis"
_NOWIN = 0x08000000 if sys.platform == "win32" else 0
WARMING = "still warming up"                 # what Cognee answers when its graph is empty: not an answer


def venv_python() -> Path:
    if PYTHON_OVERRIDE:
        return Path(PYTHON_OVERRIDE)
    return ROOT / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def installed() -> bool:
    return venv_python().exists() and WORKER.exists() and (READY.exists() or bool(PYTHON_OVERRIDE))


def enabled() -> bool:
    """Cognee is used only when it is installed AND the user has turned it on."""
    return bool(vault_mod.load_cfg().get("cognee")) and installed()


def _tail(r) -> str:
    out = ((getattr(r, "stderr", "") or "") + (getattr(r, "stdout", "") or "")).strip().splitlines()
    return out[-1][:200] if out else f"exit code {getattr(r, 'returncode', '?')}"


def install(progress: Callable[[str], None] = lambda m: None, run=subprocess.run) -> Optional[str]:
    """Create the private environment and install Cognee into it. Blocking (minutes). Returns an error or None."""
    if not WORKER.exists():
        return "the worker file is missing - run the Jarvis installer again to update"
    if not ((3, 10) <= sys.version_info[:2] < (3, 15)):
        return f"Cognee needs Python 3.10 to 3.14; this is {sys.version_info[0]}.{sys.version_info[1]}"
    ROOT.mkdir(parents=True, exist_ok=True)
    base = getattr(sys, "_base_executable", None) or sys.executable
    try:
        if not venv_python().exists():
            progress("creating a private Python environment")
            r = run([base, "-m", "venv", str(ROOT / "venv")], capture_output=True, text=True, timeout=300, creationflags=_NOWIN)
            if r.returncode != 0:
                return "could not create the environment: " + _tail(r)
        py = str(venv_python())
        progress("installing Cognee (about 1.5 GB, several minutes)")
        run([py, "-m", "pip", "install", "--upgrade", "pip"], capture_output=True, text=True, timeout=600, creationflags=_NOWIN)
        r = run([py, "-m", "pip", "install", "cognee"], capture_output=True, text=True, timeout=3600, creationflags=_NOWIN)
        if r.returncode != 0:
            return "pip could not install Cognee: " + _tail(r)
        r = run([py, "-c", "import cognee; print(cognee.__version__)"], capture_output=True, text=True, timeout=300, creationflags=_NOWIN)
        if r.returncode != 0:
            return "Cognee installed but would not start: " + _tail(r)
        READY.write_text(json.dumps({"version": (r.stdout or "").strip().splitlines()[-1:] or ["?"], "at": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
        return None
    except subprocess.TimeoutExpired:
        return "the install took too long and was stopped; run setup again to resume"
    except Exception as e:                                    # noqa: BLE001
        return str(e)[:200]


def _env() -> tuple[dict, bool]:
    cfg = vault_mod.load_cfg()
    key = ""
    try:
        from memory.config_manager import load_api_keys
        key = str(load_api_keys().get("gemini_api_key", "") or "")
    except Exception:                                         # noqa: BLE001
        pass
    env = dict(os.environ)
    env.update(LOG_LEVEL="ERROR", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
               LLM_PROVIDER="gemini", LLM_MODEL=cfg.get("llm_model", DEFAULT_LLM), LLM_API_KEY=key,
               EMBEDDING_PROVIDER="gemini", EMBEDDING_MODEL=cfg.get("embedding_model", DEFAULT_EMBED), EMBEDDING_API_KEY=key,
               EMBEDDING_DIMENSIONS=str(cfg.get("embedding_dims", 768)),
               SYSTEM_ROOT_DIRECTORY=str(ROOT / "data" / "system"), DATA_ROOT_DIRECTORY=str(ROOT / "data" / "files"))
    return env, bool(key)


# ─────────────────────────────────────────────────────────────────────────────
#  The worker process
# ─────────────────────────────────────────────────────────────────────────────
class Bridge:
    def __init__(self):
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._pending: dict[int, tuple] = {}              # id -> (the process it was sent to, its future)
        self._next = 1
        self._ready = threading.Event()
        self.version = ""
        self.last_error = ""

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, wait: float = 120.0) -> None:
        with self._lock:
            if self.running() and self._ready.is_set():
                return
            self.stop()
            env, has_key = _env()
            if not has_key:
                raise RuntimeError("there is no Gemini API key to give Cognee")
            ROOT.mkdir(parents=True, exist_ok=True)
            self._ready.clear()
            self._proc = subprocess.Popen([str(venv_python()), str(WORKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=open(LOG, "ab"), text=True, encoding="utf-8", bufsize=1, env=env,
                                          cwd=str(ROOT), creationflags=_NOWIN)
            threading.Thread(target=self._read, args=(self._proc,), daemon=True, name="cognee-reader").start()
        if not self._ready.wait(wait):
            self.stop()
            raise RuntimeError("Cognee did not start in time" + (f" ({self.last_error})" if self.last_error else ""))

    def _read(self, proc) -> None:
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("event") == "ready":
                    self.version = str(msg.get("cognee", ""))
                    self._ready.set()
                elif msg.get("event") == "fatal":
                    self.last_error = str(msg.get("error", ""))
                else:
                    entry = self._pending.pop(msg.get("id"), None)
                    if entry and not entry[1].done():
                        entry[1].set_result(msg)
        finally:
            # This worker is gone. Mark it so the next call starts a fresh one immediately (relying on poll() alone
            # races: the pipe closes a moment before the process is reaped), and fail only the requests that were
            # sent to THIS process, never a newer one's.
            with self._lock:
                if self._proc is proc:
                    self._ready.clear()
                for rid, (pr, fut) in list(self._pending.items()):
                    if pr is proc:
                        self._pending.pop(rid, None)
                        if not fut.done():
                            fut.set_exception(RuntimeError("the Cognee worker stopped"))

    def call(self, op: str, timeout: float = 60.0, **args) -> dict:
        """One request. Returns the reply dict (ok is true), or raises with Cognee's own error message."""
        self.start()
        with self._lock:
            rid, self._next = self._next, self._next + 1
            fut: concurrent.futures.Future = concurrent.futures.Future()
            self._pending[rid] = (self._proc, fut)
            try:
                self._proc.stdin.write(json.dumps({"id": rid, "op": op, **args}, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
            except Exception as e:                            # noqa: BLE001
                self._pending.pop(rid, None)
                raise RuntimeError(f"could not reach the Cognee worker: {e}")
        try:
            msg = fut.result(timeout)
        except concurrent.futures.TimeoutError:
            self._pending.pop(rid, None)
            raise TimeoutError(f"Cognee did not answer '{op}' in {timeout:.0f}s")
        if not msg.get("ok"):
            self.last_error = str(msg.get("error", "unknown error"))
            raise RuntimeError(self.last_error)
        return msg

    def stop(self) -> None:
        with self._lock:
            p, self._proc = self._proc, None
            self._ready.clear()
        if p and p.poll() is None:
            try:
                p.stdin.write(json.dumps({"id": 0, "op": "shutdown"}) + "\n")
                p.stdin.flush()
                p.wait(5)
            except Exception:                                 # noqa: BLE001
                p.kill()


_bridge: Optional[Bridge] = None
_blk = threading.Lock()


def bridge() -> Bridge:
    global _bridge
    with _blk:
        if _bridge is None:
            _bridge = Bridge()
        return _bridge


# ─────────────────────────────────────────────────────────────────────────────
#  Notes -> Cognee
# ─────────────────────────────────────────────────────────────────────────────
def split_front(text: str) -> tuple[dict, str]:
    m = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.S)
    if not m:
        return {}, text
    props = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            props[k.strip()] = v.strip()
    return props, text[m.end():]


def render_for_cognee(path: Path) -> str:
    props, body = split_front(Path(path).read_text(encoding="utf-8", errors="replace"))
    head = [f"Note: {Path(path).stem}"]
    if props.get("created"):
        head.append(f"Written: {props['created'].replace('T', ' ')}")
    if props.get("type"):
        head.append(f"Kind: {props['type']}")
    return "\n".join(head) + "\n\n" + body.strip()


def _ledger() -> dict:
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except Exception:                                         # noqa: BLE001
        return {}


def _digest(path: Path) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


class Ingestor:
    def __init__(self):
        self.q: "queue.Queue[Path]" = queue.Queue()
        self.queued: set = set()
        self.ok = 0
        self.failed = 0
        self.last_error = ""
        self.last_ok = 0.0
        self._t: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.on_done: Optional[Callable[[], None]] = None

    def submit(self, path: Path) -> bool:
        with self._lock:
            if str(path) in self.queued:
                return False
            self.queued.add(str(path))
            self.q.put(Path(path))
            if self._t is None or not self._t.is_alive():
                self._t = threading.Thread(target=self._run, daemon=True, name="cognee-ingest")
                self._t.start()
        return True

    def _run(self) -> None:
        while True:
            try:
                path = self.q.get(timeout=5)
            except queue.Empty:
                return
            try:
                text = render_for_cognee(path)
                bridge().call("remember", timeout=900, text=text, dataset=vault_mod.load_cfg().get("dataset", DATASET))
                led = _ledger()
                led[str(path)] = {"sha1": _digest(path), "at": datetime.now().isoformat(timespec="seconds")}
                ROOT.mkdir(parents=True, exist_ok=True)
                LEDGER.write_text(json.dumps(led, indent=1), encoding="utf-8")
                self.ok += 1
                self.last_ok = time.time()
                self.last_error = ""
            except Exception as e:                            # noqa: BLE001
                self.failed += 1
                self.last_error = str(e)[:240]
            finally:
                with self._lock:
                    self.queued.discard(str(path))
            if self.q.empty() and self.on_done:
                try:
                    self.on_done()
                except Exception:                             # noqa: BLE001
                    pass

    def pending(self) -> int:
        return self.q.qsize()


_ingestor = Ingestor()


def ingestor() -> Ingestor:
    return _ingestor


def _content_len(body: str) -> int:
    """Characters of real content: not the title, the day link or blank lines. An empty session is not worth sending."""
    keep = [l.strip() for l in body.splitlines() if l.strip() and not l.startswith("# ") and not l.startswith("Day: [[")]
    return len(" ".join(keep))


def pending_notes(v: vault_mod.Vault, skip: Optional[set] = None) -> list[Path]:
    """Jarvis notes that Cognee has not seen in their current form, minus private ones and any still being written."""
    led, out = _ledger(), []
    skip = skip or set()
    for p in sorted(v.base.rglob("*.md")):
        if p.parent == v.daily or str(p) in skip or ".obsidian" in p.parts:
            continue
        props, body = split_front(p.read_text(encoding="utf-8", errors="replace"))
        if props.get("ingest", "true").lower() == "false" or _content_len(body) < 20:
            continue
        if led.get(str(p), {}).get("sha1") != _digest(p):
            out.append(p)
    return out


def sync(v: vault_mod.Vault, skip: Optional[set] = None, limit: int = 40) -> int:
    """Queue every unseen note for Cognee. Returns how many were queued."""
    if not enabled():
        return 0
    n = 0
    for p in pending_notes(v, skip)[:limit]:
        n += 1 if _ingestor.submit(p) else 0
    return n


def on_notes_closed(path: str) -> None:
    """Called when a spoken-notes session ends: the finished note goes to Cognee. Never raises."""
    try:
        if enabled():
            v = vault_mod.get_vault(create=False)
            if v:
                skip: set = set()
                try:                                          # a session still being spoken is not finished
                    from core import automations as au
                    skip = {r["file"] for r in au.engine().active("notes")}
                except Exception:                             # noqa: BLE001
                    pass
                sync(v, skip)
    except Exception:                                         # noqa: BLE001
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  Asking
# ─────────────────────────────────────────────────────────────────────────────
def recall(question: str, top_k: int = 6, scope: str = "jarvis") -> dict:
    """Cognee if it is on and has something; otherwise a keyword search of the vault.
    -> {"engine": "cognee"|"vault", "results": [str], "note": str, "hits": [vault hits]}"""
    v = vault_mod.get_vault(create=False)
    note = ""
    if enabled():
        try:
            msg = bridge().call("recall", timeout=90, query=question, dataset=vault_mod.load_cfg().get("dataset", DATASET), top_k=top_k)
            res = [r for r in msg.get("results", []) if r and WARMING not in r]
            if res:
                return {"engine": "cognee", "results": res, "note": "", "hits": v.search(question, 3, scope) if v else []}
            note = "Cognee has nothing on that yet, so this is a keyword search of the notes"
        except Exception as e:                                # noqa: BLE001
            note = f"Cognee could not answer ({str(e)[:120]}), so this is a keyword search of the notes"
    hits = v.search(question, top_k, scope) if v else []
    return {"engine": "vault", "results": [f"{h['title']}: {h['snippet']}" for h in hits], "note": note, "hits": hits}


def forget_graph() -> None:
    bridge().call("forget", timeout=120, dataset=vault_mod.load_cfg().get("dataset", DATASET))
    try:
        LEDGER.unlink()
    except OSError:
        pass


def import_old_memory(v: vault_mod.Vault) -> Optional[Path]:
    """Jarvis's earlier long-term memory (identity, preferences, people, notes ...) as one Markdown page."""
    try:
        from memory.memory_manager import load_memory
        mem = load_memory()
    except Exception:                                         # noqa: BLE001
        return None
    lines: list[str] = []

    def walk(prefix: str, node) -> None:
        if isinstance(node, dict) and "value" in node and not isinstance(node.get("value"), dict):
            lines.append(f"- **{prefix}**: {str(node['value']).strip()}" + (f" _(updated {node['updated']})_" if node.get("updated") else ""))
        elif isinstance(node, dict):
            for k, val in node.items():
                walk(f"{prefix} / {k}" if prefix else str(k), val)
    walk("", mem)
    if not lines:
        return None
    v.ensure()
    p = v.base / "Memory.md"
    p.write_text(Bridge_front("memory-import") + "# What Jarvis already knew about you\n\nImported from Jarvis's earlier memory.\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return p


def Bridge_front(kind: str) -> str:
    return vault_mod.Vault._front(kind, ["memory"], datetime.now())


def status() -> dict:
    v = vault_mod.get_vault(create=False)
    cfg = vault_mod.load_cfg()
    b = _bridge
    return {"vault": str(v.root) if v else None, "folder": vault_mod.FOLDER, "cognee_installed": installed(), "cognee_on": bool(cfg.get("cognee")),
            "cognee_running": bool(b and b.running()), "cognee_version": (b.version if b else "") or "",
            "queued": _ingestor.pending(), "ingested_ok": _ingestor.ok, "ingest_failed": _ingestor.failed,
            "last_error": _ingestor.last_error or (b.last_error if b else ""), "model": cfg.get("llm_model", DEFAULT_LLM)}
