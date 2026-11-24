"""Build a clean zip to send to a user: python make_release.py -> ../Clipwise-<version>.zip

Contains only the app (no .venv, no data/ with your clips, settings or API keys, no test media)."""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INCLUDE = ["app", "web", "VERSION", "README.md", "requirements.txt", "start.bat", "update.bat",
           "build_exe.bat", "build_exe.py", "hotclip.py", ".gitignore", ".env.example"]
SKIP = {"__pycache__", ".DS_Store"}

version = (ROOT / "VERSION").read_text().strip()
out = ROOT.parent / f"Clipwise-{version}.zip"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
    for name in INCLUDE:
        path = ROOT / name
        files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for f in files:
            if SKIP & set(f.parts) or f.suffix == ".pyc":
                continue
            zf.write(f, Path("Clipwise") / f.relative_to(ROOT))
print(f"{out}  ({out.stat().st_size / 1e6:.1f} MB)")
