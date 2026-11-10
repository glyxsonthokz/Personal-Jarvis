import os, sys, tempfile
APP = os.environ.get("JARVIS_DIR") or os.path.join(os.path.expanduser("~"), "Jarvis")
if not os.path.exists(os.path.join(APP, "main.py")):
    sys.exit("Set JARVIS_DIR to your installed Jarvis folder (run Install_Jarvis.bat first).")
sys.path.insert(0, APP); os.chdir(APP)
import json, pathlib, re
from datetime import datetime
from core import vault as V
tmp = pathlib.Path(tempfile.mkdtemp()); V.CFG = tmp / "sb.json"
now = datetime(2026, 11, 5, 14, 23, 9)
# a brand-new vault: created and marked as an Obsidian vault
v = V.Vault(tmp / "New Vault").ensure(); assert (v.root / ".obsidian").is_dir() and v.notes.is_dir() and v.daily.is_dir(); print("1. new vault marked for Obsidian:", sorted(p.name for p in v.root.iterdir()))
# an EXISTING vault: its settings and the user's own notes are never touched
old = tmp / "My Brain"; (old / ".obsidian").mkdir(parents=True); (old / ".obsidian" / "app.json").write_text('{"vimMode": true}'); (old / "Ideas.md").write_text("My own idea about batteries and algae")
before = {p: p.read_bytes() for p in old.rglob("*") if p.is_file()}
ov = V.Vault(old).ensure(); assert all(p.read_bytes() == b for p, b in before.items()); assert (old / "Jarvis" / "Notes").is_dir(); print("2. existing vault untouched; Jarvis writes only under Jarvis/")
# session note + bullets + daily link
s = ov.new_session("Standup: Q4 / roadmap?", now=now); print("3. session file:", ov.rel(s))
assert s.name == "2026-11-05 1423 - Standup Q4 roadmap.md", s.name
ov.append_bullet(s, "we agreed to ship on Friday\nand test on Thursday", now=datetime(2026, 11, 5, 14, 24, 1)); ov.append_bullet(s, "   ", now=now)
t = s.read_text(encoding="utf-8"); print(t)
assert t.startswith("---\ncreated: 2026-11-05T14:23:09\ntype: spoken-notes\nsource: jarvis\ntags: [jarvis, notes]\n---") and "- **14:24:01** we agreed to ship on Friday and test on Thursday\n" in t and t.count("- **") == 1
try:
    import yaml; fm = yaml.safe_load(t.split("---")[1]); assert fm["tags"] == ["jarvis", "notes"] and fm["source"] == "jarvis"; print("   frontmatter is valid YAML:", fm)
except ImportError: print("   (pyyaml not installed; frontmatter shape checked by string)")
d = (ov.daily / "2026-11-05.md").read_text(encoding="utf-8"); assert "[[Jarvis/Notes/2026-11-05 1423 - Standup Q4 roadmap|Standup Q4 roadmap]]" in d and d.count("type: daily") == 1; print("4. daily page links the note")
s2 = ov.new_session("Standup: Q4 / roadmap?", now=now); assert s2.name.endswith("(2).md"); print("5. same-minute duplicate gets its own file:", s2.name)
r = ov.remember("Asha prefers morning meetings and hates Mondays", tags=["people"], now=datetime(2026, 11, 6, 9, 0)); assert "memory" in r.read_text() and "tags: [jarvis, remembered, people]" in r.read_text(); print("6. remembered:", r.name)
ov.daily_append("took the algae samples to the lab", now=datetime(2026, 11, 6, 9, 5)); assert "- 09:05 took the algae samples" in (ov.daily / "2026-11-06.md").read_text()
# search
hits = ov.search("morning meetings Asha"); print("7. search:", [(h["title"], round(h["score"], 1)) for h in hits]); assert hits[0]["path"] == r and "morning" in hits[0]["snippet"]
assert not ov.search("batteries algae") or all("Ideas" not in h["title"] for h in ov.search("batteries algae")), "default scope must stay inside Jarvis/"
whole = ov.search("batteries algae", scope="vault"); assert any(h["title"] == "Ideas" for h in whole); print("8. scope=vault finds the user's own note:", [h["title"] for h in whole])
(old / ".trash").mkdir(); (old / ".trash" / "Gone.md").write_text("batteries algae trashed"); assert all(".trash" not in str(h["path"]) for h in ov.search("batteries algae", scope="vault"))
assert [p.name for p in ov.recent(1)][0] in {x.name for x in ov.daily.iterdir()} | {x.name for x in ov.notes.iterdir()}
# uri / titles
u = ov.uri(s); print("9. uri:", u); assert u == "obsidian://open?vault=My%20Brain&file=Jarvis/Notes/2026-11-05%201423%20-%20Standup%20Q4%20roadmap".replace("/", "%2F").replace("%2F", "/") or "obsidian://open?vault=My%20Brain" in u
assert V.clean_title('a/b:c*d?"e"<f>|g#h^[i]') == "a b c d e f g h i" and V.clean_title("???") == "Note" and len(V.clean_title("x" * 300)) <= 60
# detection and automatic choice
fake_home = tmp / "home"; (fake_home / ".config" / "obsidian").mkdir(parents=True); (fake_home / "Documents").mkdir()
json.dump({"vaults": {"a1": {"path": str(old), "ts": 1}, "a2": {"path": str(tmp / "gone"), "ts": 2}}}, open(fake_home / ".config" / "obsidian" / "obsidian.json", "w"))
real_home = pathlib.Path.home; pathlib.Path.home = classmethod(lambda cls: fake_home)
try:
    assert V.detect_vaults() == [old]; vv, how = V.configure(); print("10. configure():", vv.root.name, "-", how); assert vv.root == old and "only" in how
    json.dump({"vaults": {}}, open(fake_home / ".config" / "obsidian" / "obsidian.json", "w")); vv, how = V.configure(); print("11. no vault known:", vv.root, "-", how); assert vv.root == fake_home / "Documents" / "Jarvis Vault" and (vv.root / ".obsidian").exists()
    vv, how = V.configure(str(tmp / "custom")); assert vv.root == tmp / "custom" and V.load_cfg()["vault"] == str(tmp / "custom"); assert V.get_vault().root == tmp / "custom"; print("12. explicit path remembered")
finally: pathlib.Path.home = real_home
print("\nVAULT TESTS PASSED")
