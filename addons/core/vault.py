"""
core/vault.py — notes live in an Obsidian vault.

An Obsidian vault is just a folder of Markdown files, so Jarvis writes ordinary .md files and Obsidian
shows them live; nothing is needed from Obsidian itself, and nothing is locked in. Everything Jarvis
writes goes under one folder of the vault (default "Jarvis/"), so it never mixes with or edits the
user's own notes:

    <vault>/Jarvis/Notes/2026-11-05 1423 - Standup.md     a spoken-notes session or a remembered thing
    <vault>/Jarvis/Daily/2026-11-05.md                    one page per day, linking to that day's notes
    <vault>/Jarvis/Memory.md                              facts imported from Jarvis's older memory

Notes have YAML properties (created, type, source, tags), a wikilink back to the day's page, and
plain bullets, so they work with Obsidian's search, graph, backlinks and tags.

Choosing the vault, in order: the path the user gave; the single vault Obsidian already knows about;
otherwise a new vault at ~/Documents/Jarvis Vault. An existing vault's own settings are never touched.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

_BASE = Path(__file__).resolve().parent.parent
CFG = _BASE / "config" / "second_brain.json"
FOLDER = "Jarvis"
_STOP = frozenset("the and for are but not you all can her was one our out has his how its who did this that with from have what when where which "
                  "about there their they them then than into your just like note notes any some tell show find open".split())
_BAD = re.compile(r'[\\/:*?"<>|#^\[\]\r\n\t]')


def load_cfg() -> dict:
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:                                         # noqa: BLE001
        return {}


def save_cfg(**kv) -> None:
    cfg = load_cfg()
    cfg.update(kv)
    CFG.parent.mkdir(parents=True, exist_ok=True)
    CFG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def detect_vaults() -> list[Path]:
    """The vaults Obsidian itself has registered on this machine."""
    cands = []
    if sys.platform == "win32":
        cands.append(Path(os.environ.get("APPDATA", "")) / "obsidian" / "obsidian.json")
    elif sys.platform == "darwin":
        cands.append(Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json")
    else:
        cands += [Path.home() / ".config" / "obsidian" / "obsidian.json",
                  Path.home() / ".var" / "app" / "md.obsidian.Obsidian" / "config" / "obsidian" / "obsidian.json"]
    for c in cands:
        try:
            data = json.loads(c.read_text(encoding="utf-8"))
            paths = [Path(v["path"]) for v in data.get("vaults", {}).values() if v.get("path")]
            return [p for p in paths if p.is_dir()]
        except Exception:                                     # noqa: BLE001
            continue
    return []


def clean_title(text: str, limit: int = 60) -> str:
    t = _BAD.sub(" ", text or "").strip()
    t = re.sub(r"\s+", " ", t)[:limit].strip(" .")
    return t or "Note"


class Vault:
    def __init__(self, root: Path, folder: str = FOLDER):
        self.root, self.folder = Path(root), folder
        self.base = self.root / folder
        self.notes = self.base / "Notes"
        self.daily = self.base / "Daily"

    # ---- structure ---------------------------------------------------------------
    def ensure(self) -> "Vault":
        fresh = not self.root.exists()
        self.notes.mkdir(parents=True, exist_ok=True)
        self.daily.mkdir(parents=True, exist_ok=True)
        if fresh:                                             # a vault we created: mark it so Obsidian opens it as one
            (self.root / ".obsidian").mkdir(exist_ok=True)
        return self

    @property
    def name(self) -> str:
        return self.root.name

    def rel(self, path: Path) -> str:
        try:
            return Path(path).resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return str(path)

    # ---- writing --------------------------------------------------------------------
    @staticmethod
    def _front(kind: str, tags: list[str], now: datetime, extra: Optional[dict] = None) -> str:
        lines = ["---", f"created: {now:%Y-%m-%dT%H:%M:%S}", f"type: {kind}", "source: jarvis", f"tags: [{', '.join(['jarvis', *tags])}]"]
        for k, v in (extra or {}).items():
            lines.append(f"{k}: {v}")
        return "\n".join(lines + ["---", ""]) + "\n"

    def _unique(self, d: Path, stem: str) -> Path:
        p = d / f"{stem}.md"
        n = 2
        while p.exists():
            p = d / f"{stem} ({n}).md"
            n += 1
        return p

    def _link_in_daily(self, note: Path, label: str, now: datetime) -> None:
        link = f"[[{self.folder}/Notes/{note.stem}|{label}]]"
        self.daily_append(f"{link}", now=now, bullet=True)

    def new_note(self, title: str, body: str = "", kind: str = "note", tags: Optional[list] = None, now: Optional[datetime] = None,
                 extra: Optional[dict] = None) -> Path:
        self.ensure()
        now = now or datetime.now()
        title = clean_title(title)
        p = self._unique(self.notes, f"{now:%Y-%m-%d %H%M} - {title}")
        text = self._front(kind, tags or [], now, extra) + f"# {title}\n\nDay: [[{self.folder}/Daily/{now:%Y-%m-%d}|{now:%A %d %B %Y}]]\n\n" + (body.strip() + "\n" if body.strip() else "")
        p.write_text(text, encoding="utf-8")
        self._link_in_daily(p, title, now)
        return p

    def new_session(self, title: str = "Spoken notes", tags: Optional[list] = None, now: Optional[datetime] = None, private: bool = False) -> Path:
        return self.new_note(title, kind="spoken-notes", tags=["notes", *(tags or [])], now=now, extra={"ingest": "false"} if private else None)

    def append_bullet(self, path: Path, text: str, now: Optional[datetime] = None) -> None:
        append_bullet(path, text, now)

    def daily_append(self, text: str, now: Optional[datetime] = None, bullet: bool = True) -> Path:
        self.ensure()
        now = now or datetime.now()
        p = self.daily / f"{now:%Y-%m-%d}.md"
        if not p.exists():
            p.write_text(self._front("daily", ["daily"], now) + f"# {now:%A %d %B %Y}\n\n", encoding="utf-8")
        line = re.sub(r"\s*\n\s*", " ", text.strip())
        with open(p, "a", encoding="utf-8") as f:
            f.write((f"- {now:%H:%M} {line}\n") if bullet else line + "\n")
        return p

    def remember(self, text: str, title: Optional[str] = None, tags: Optional[list] = None, now: Optional[datetime] = None, private: bool = False) -> Path:
        text = text.strip()
        words = re.sub(r"\s+", " ", text).split(" ")
        return self.new_note(title or " ".join(words[:8]), body=text, kind="memory", tags=["remembered", *(tags or [])], now=now,
                             extra={"ingest": "false"} if private else None)

    # ---- reading -----------------------------------------------------------------------------
    def _files(self, scope: str):
        base = self.root if scope == "vault" else self.base
        if not base.exists():
            return
        for p in base.rglob("*.md"):
            if ".obsidian" in p.parts or ".trash" in p.parts:
                continue
            yield p

    def recent(self, n: int = 5, scope: str = "jarvis") -> list[Path]:
        return sorted(self._files(scope), key=lambda p: p.stat().st_mtime, reverse=True)[:n]

    def read(self, path: Path, limit: int = 6000) -> str:
        t = Path(path).read_text(encoding="utf-8", errors="replace")
        t = re.sub(r"\A---\n.*?\n---\n", "", t, count=1, flags=re.S)
        return t[:limit]

    def search(self, query: str, limit: int = 6, scope: str = "jarvis") -> list[dict]:
        words = re.findall(r"\w+", query.lower())
        terms = [t for t in words if t not in _STOP and (len(t) >= 3 or len(words) == 1)]   # filler words match everything
        if not terms:
            return []
        need = (len(terms) + 1) // 2                               # at least half of the meaningful words must appear
        hits = []
        for p in self._files(scope):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            low = text.lower()
            score = sum(low.count(t) for t in terms) + 3 * sum(t in p.stem.lower() for t in terms)
            matched = sum(t in low for t in terms)
            if matched < need:
                continue
            score *= matched / len(terms)                      # notes mentioning ALL the words come first
            best, bs = "", -1
            for line in text.splitlines():
                s = sum(line.lower().count(t) for t in terms)
                if s > bs and not line.startswith("---"):
                    best, bs = line.strip(" -*"), s
            hits.append({"path": p, "title": p.stem, "score": score, "snippet": best[:240], "when": datetime.fromtimestamp(p.stat().st_mtime)})
        hits.sort(key=lambda h: (-h["score"], -h["when"].timestamp()))
        return hits[:limit]

    # ---- opening in Obsidian --------------------------------------------------------------------------
    def uri(self, path: Optional[Path] = None) -> str:
        q = {"vault": self.name}
        if path is not None:
            q["file"] = re.sub(r"\.md$", "", self.rel(path))
        return "obsidian://open?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)

    def open(self, path: Optional[Path] = None) -> str:
        """Opens the note in Obsidian if it is installed, else in the default Markdown app."""
        uri = self.uri(path)
        try:
            if sys.platform == "win32":
                os.startfile(uri)                             # noqa: S606
            else:
                webbrowser.open(uri)
            return "obsidian"
        except Exception:                                     # noqa: BLE001
            if path is not None and sys.platform == "win32":
                os.startfile(str(path))                       # noqa: S606
                return "default app"
            raise


def append_bullet(path, text: str, now: Optional[datetime] = None) -> None:
    """One timestamped bullet on the end of a note. Safe to call from any thread."""
    now = now or datetime.now()
    line = re.sub(r"\s*\n\s*", " ", (text or "").strip())
    if not line:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"- **{now:%H:%M:%S}** {line}\n")


def configure(path: Optional[str] = None) -> tuple[Vault, str]:
    """Pick (and remember) the vault. Returns (vault, how it was chosen)."""
    if path:
        root, how = Path(path).expanduser(), "the folder you gave"
    else:
        found = detect_vaults()
        if len(found) == 1:
            root, how = found[0], "your only Obsidian vault"
        else:
            root, how = Path.home() / "Documents" / "Jarvis Vault", "a new vault"
    v = Vault(root).ensure()
    save_cfg(vault=str(root))
    return v, how


def get_vault(create: bool = True) -> Optional[Vault]:
    """The configured vault; on first use it is chosen automatically."""
    cfg = load_cfg()
    if cfg.get("vault"):
        return Vault(Path(cfg["vault"])).ensure()
    return configure()[0] if create else None
