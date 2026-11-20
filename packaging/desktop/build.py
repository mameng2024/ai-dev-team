"""Build the desktop app for THIS platform with PyInstaller.

    pip install -e '.[package]'
    python packaging/desktop/build.py            # -> dist/ (app + archive)
    python packaging/desktop/build.py --smoke    # also run the built app's self-check

macOS: dist/AI Dev Team.app and dist/AI-Dev-Team-<ver>-macos-<arch>.dmg
Windows: dist/AI Dev Team/AI Dev Team.exe and dist/AI-Dev-Team-<ver>-windows-x64.zip
Linux: dist/ai-dev-team/ai-dev-team and dist/AI-Dev-Team-<ver>-linux-x64.tar.gz

PyInstaller cannot cross-compile: build each platform on that platform (CI does this).
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DIST = ROOT / "dist"
SYSTEM = platform.system()
VERSION = re.search(r'^version = "(.+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
NAME = "ai-dev-team" if SYSTEM == "Linux" else "AI Dev Team"


def icon() -> list[str]:
    png = ROOT / "packaging" / "macos" / "icon.png"
    if SYSTEM == "Darwin":
        icns = HERE / "build" / "AppIcon.icns"
        if not icns.exists():
            iconset = HERE / "build" / "AppIcon.iconset"
            iconset.mkdir(parents=True, exist_ok=True)
            for s in (16, 32, 128, 256, 512):
                for scale, suffix in ((1, ""), (2, "@2x")):
                    subprocess.run(["sips", "-z", str(s * scale), str(s * scale), str(png),
                                    "--out", str(iconset / f"icon_{s}x{s}{suffix}.png")],
                                   check=True, capture_output=True)
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
        return ["--icon", str(icns)]
    if SYSTEM == "Windows":
        try:
            from PIL import Image
        except ImportError:
            return []
        ico = HERE / "build" / "app.ico"
        ico.parent.mkdir(parents=True, exist_ok=True)
        Image.open(png).save(ico, sizes=[(16, 16), (32, 32), (64, 64), (256, 256)])
        return ["--icon", str(ico)]
    return []


def pyinstaller() -> None:
    sep = ";" if SYSTEM == "Windows" else ":"
    args = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", NAME, "--windowed",
        "--distpath", str(DIST), "--workpath", str(HERE / "build" / "work"),
        "--specpath", str(HERE / "build"),
        "--osx-bundle-identifier", "com.mameng2024.aidevteam",
        "--collect-all", "litellm",
        "--collect-all", "tiktoken",
        "--collect-all", "tiktoken_ext",
        "--collect-submodules", "langgraph",
        "--collect-data", "ai_dev_team",
        "--copy-metadata", "litellm",
        "--copy-metadata", "ai-dev-team",
        "--hidden-import", "ai_dev_team.cli",
        "--add-data", f"{ROOT / 'src' / 'ai_dev_team' / 'presets'}{sep}ai_dev_team/presets",
        *icon(),
        str(HERE / "entry.py"),
    ]
    subprocess.run(args, check=True, cwd=ROOT)


def archive() -> Path:
    arch = {"x86_64": "x64", "AMD64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform.machine(), platform.machine())
    if SYSTEM == "Darwin":
        app = DIST / f"{NAME}.app"
        out = DIST / f"AI-Dev-Team-{VERSION}-macos-{arch}.dmg"
        stage = HERE / "build" / "dmg"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        subprocess.run(["cp", "-R", str(app), str(stage)], check=True)
        (stage / "Applications").symlink_to("/Applications")
        out.unlink(missing_ok=True)
        subprocess.run(["hdiutil", "create", "-volname", "AI Dev Team", "-srcfolder", str(stage),
                        "-ov", "-format", "UDZO", str(out)], check=True)
        return out
    folder = DIST / NAME
    if SYSTEM == "Windows":
        out = DIST / f"AI-Dev-Team-{VERSION}-windows-{arch}.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in folder.rglob("*"):
                z.write(f, Path("AI Dev Team") / f.relative_to(folder))
        return out
    out = DIST / f"AI-Dev-Team-{VERSION}-linux-{arch}.tar.gz"
    with tarfile.open(out, "w:gz") as t:
        t.add(folder, arcname="ai-dev-team")
    return out


def executable() -> Path:
    if SYSTEM == "Darwin":
        return DIST / f"{NAME}.app" / "Contents" / "MacOS" / NAME
    if SYSTEM == "Windows":
        return DIST / NAME / f"{NAME}.exe"
    return DIST / NAME / NAME


def smoke() -> None:
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, AI_DEV_TEAM_APP_DIR=tmp, AI_DEV_TEAM_HOME=tmp)
        r = subprocess.run([str(executable()), "--smoke"], capture_output=True, text=True,
                           env=env, timeout=180)
        result = Path(tmp, "smoke.txt")
        text = result.read_text() if result.exists() else ""
        print(r.stdout, r.stderr, text)
        if r.returncode != 0 or "smoke ok" not in text:
            raise SystemExit("smoke test failed")


if __name__ == "__main__":
    pyinstaller()
    if "--smoke" in sys.argv:
        smoke()
    print(f"==> {archive()}")
