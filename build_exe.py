"""Build dist/Clipwise.exe: a single file that needs no Python install.

Run on Windows via build_exe.bat (PyInstaller can't cross-compile from macOS/Linux).
Bundles: the app, the web UI, ffmpeg.exe and deno.exe.
"""
import io
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
BIN = BUILD / "bin"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def ensure_ffmpeg() -> Path:
    exe = BIN / "ffmpeg.exe"
    if not exe.exists():
        print("Downloading ffmpeg…")
        with urllib.request.urlopen(FFMPEG_URL) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = next(n for n in zf.namelist() if n.endswith("/bin/ffmpeg.exe"))
            exe.write_bytes(zf.read(member))
    return exe


def ensure_deno() -> Path:
    import deno

    src = Path(deno.find_deno_bin())
    dst = BIN / src.name
    if not dst.exists():
        shutil.copy2(src, dst)
    return dst


def main() -> None:
    if sys.platform != "win32":
        sys.exit("Build Clipwise.exe on Windows (run build_exe.bat).")
    BIN.mkdir(parents=True, exist_ok=True)
    tools = [ensure_ffmpeg(), ensure_deno()]

    import PyInstaller.__main__

    sep = os.pathsep
    args = [
        str(ROOT / "hotclip.py"),
        "--name", "Clipwise", "--onefile", "--console", "--noconfirm", "--clean",
        "--distpath", str(ROOT / "dist"), "--workpath", str(BUILD / "work"),
        "--specpath", str(BUILD),
        "--add-data", f"{ROOT / 'web'}{sep}web",
        "--add-data", f"{ROOT / 'VERSION'}{sep}.",
        *[a for t in tools for a in ("--add-binary", f"{t}{sep}bin")],
        "--collect-submodules", "uvicorn",
        *[a for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "av",
                        "yt_dlp", "yt_dlp_ejs", "tokenizers",
                        "shazamio", "shazamio_core", "rapidocr_onnxruntime", "cv2")
          for a in ("--collect-all", pkg)],
    ]
    PyInstaller.__main__.run(args)
    print(f"\nDone: {ROOT / 'dist' / 'Clipwise.exe'}")


if __name__ == "__main__":
    main()
