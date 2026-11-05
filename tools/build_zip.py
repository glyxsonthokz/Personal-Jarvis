"""Builds dist/Jarvis-Installer.zip: what a person who only wants to INSTALL needs.
    python tools/build_zip.py
The zip has the installer, health check, add-ons and the plain-text guides at its top level
(not tests, tools, git history or the publish script)."""
import os, pathlib, zipfile
root = pathlib.Path(__file__).resolve().parent.parent
out = root / "dist" / "Jarvis-Installer.zip"
out.parent.mkdir(exist_ok=True)
top = "Jarvis-Installer"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for name in ("Install_Jarvis.bat", "Diagnose_Jarvis.bat", "diagnose.py"):
        z.write(root / name, f"{top}/{name}")
    for f in sorted((root / "docs").glob("*.txt")):
        z.write(f, f"{top}/{f.name}")
    for f in sorted((root / "addons").rglob("*")):
        if f.is_file() and "__pycache__" not in f.parts:
            z.write(f, f"{top}/addons/{f.relative_to(root / 'addons').as_posix()}")
print("built", out, f"({out.stat().st_size // 1024} KB)")
