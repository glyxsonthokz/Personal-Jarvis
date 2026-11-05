r"""Runs every test in this folder against the installed Jarvis and prints a summary.
    set JARVIS_DIR=%USERPROFILE%\Jarvis   (optional: this is the default)
    python tests\run_all.py
Use the SAME Python that Jarvis uses (%USERPROFILE%\Jarvis\.venv\Scripts\python.exe)."""
import glob, os, subprocess, sys, time
here = os.path.dirname(os.path.abspath(__file__))
results = []
for f in sorted(glob.glob(os.path.join(here, "test_*.py"))):
    t = time.time()
    p = subprocess.run([sys.executable, f], capture_output=True, text=True, timeout=900)
    ok = p.returncode == 0
    results.append((os.path.basename(f), ok, time.time() - t))
    print(("PASS " if ok else "FAIL ") + os.path.basename(f) + f"  ({time.time() - t:.0f}s)")
    if not ok:
        print("   " + "\n   ".join((p.stdout + p.stderr).strip().splitlines()[-12:]))
print(f"\n{sum(r[1] for r in results)} of {len(results)} passed")
sys.exit(0 if all(r[1] for r in results) else 1)
