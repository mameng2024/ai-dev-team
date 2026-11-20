#!/bin/bash
# Build "AI Dev Team.app" on this Mac.
#
#   ./packaging/macos/build_app.sh            build dist/AI Dev Team.app
#   ./packaging/macos/build_app.sh --install  ... and copy it to /Applications
#   ./packaging/macos/build_app.sh --dmg      ... and make dist/AI-Dev-Team-<ver>.dmg
#   ./packaging/macos/build_app.sh --sign "Developer ID Application: Name (TEAMID)"
#
# Run it again after updating the source: it upgrades the app's environment and keeps
# your settings, keys, projects and tasks.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/../.." && pwd)"
APPDIR="$HOME/Library/Application Support/AI Dev Team"
DIST="$SRC/dist"
APP="$DIST/AI Dev Team.app"
INSTALL=0; DMG=0; SIGN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --install) INSTALL=1 ;;
    --dmg) DMG=1 ;;
    --sign) SIGN="$2"; shift ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
  shift
done

[ "$(uname)" = "Darwin" ] || { echo "Run this on macOS."; exit 1; }
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$SRC/pyproject.toml")"

# 1. Python 3.12+
PYBIN=""
for c in ${PYTHON:-} python3 python3.15 python3.14 python3.13 python3.12 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
         /usr/local/bin/python3.12; do
  if command -v "$c" >/dev/null 2>&1 && \
     "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    PYBIN="$(command -v "$c")"; break
  fi
done
[ -n "$PYBIN" ] || { echo "Python 3.12+ not found. Install it: brew install python@3.12"; exit 1; }
echo "==> Python: $PYBIN ($("$PYBIN" --version))"

# 2. App environment (outside the bundle, so the .app can be moved or replaced)
mkdir -p "$APPDIR"
if [ ! -x "$APPDIR/venv/bin/python" ]; then
  "$PYBIN" -m venv "$APPDIR/venv"
fi
VPY="$APPDIR/venv/bin/python"
if ! "$VPY" -m pip --version >/dev/null 2>&1; then
  echo "==> The venv has no pip; adding it"
  "$VPY" -m ensurepip --upgrade >/dev/null 2>&1 || true
fi
echo "==> Installing ai-dev-team $VERSION into the app environment"
if "$VPY" -m pip --version >/dev/null 2>&1; then
  "$VPY" -m pip install -q -U pip
  "$VPY" -m pip install -q --upgrade "$SRC"
else
  # No ensurepip in this Python build: use the system pip3 to install into the venv.
  PIP3="$(command -v pip3 || true)"
  [ -n "$PIP3" ] || { echo "No pip available. Install one: brew reinstall python"; exit 1; }
  echo "==> Using $PIP3 --python $VPY"
  "$PIP3" --python "$VPY" install -q --upgrade "$SRC"
fi

# 3. Settings: copy the default model config once; never overwrite yours.
if [ ! -f "$APPDIR/agents.yaml" ]; then
  if [ -f "$SRC/config/agents.yaml" ]; then cp "$SRC/config/agents.yaml" "$APPDIR/agents.yaml"; fi
fi

# 4. A double-clicked app gets a minimal PATH; remember the login shell's PATH and tools.
LOGIN_PATH="$(/bin/zsh -lic 'printf "\n__AIDT_PATH__%s\n" "$PATH"' 2>/dev/null \
  | sed -n 's/^__AIDT_PATH__//p' | tail -1 || true)"
[ -n "$LOGIN_PATH" ] || LOGIN_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
OC="$(PATH="$LOGIN_PATH" command -v opencode || true)"
{
  echo "# written by build_app.sh; edit if a tool moves"
  printf 'PATH="%s"\n' "$LOGIN_PATH"
  printf 'OPENCODE_BIN="%s"\n' "${OC:-opencode}"
} > "$APPDIR/app.env"
echo "==> OpenCode: ${OC:-NOT FOUND (install it, then run this script again)}"
for tool in git swift; do
  echo "==> $tool: $(PATH="$LOGIN_PATH" command -v $tool || echo 'NOT FOUND (xcode-select --install)')"
done

# 5. The bundle
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
sed "s/__VERSION__/$VERSION/g" "$HERE/Info.plist" > "$APP/Contents/Info.plist"
cp "$HERE/launcher.sh" "$APP/Contents/MacOS/AIDevTeam"
chmod 755 "$APP/Contents/MacOS/AIDevTeam"
ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$HERE/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$HERE/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

if [ -n "$SIGN" ]; then
  codesign --force --deep --options runtime --sign "$SIGN" "$APP"
  echo "==> Signed with $SIGN (notarize with: xcrun notarytool submit ... --wait)"
fi
echo "==> Built $APP"

if [ "$INSTALL" = 1 ]; then
  rm -rf "/Applications/AI Dev Team.app"
  cp -R "$APP" "/Applications/"
  echo "==> Installed to /Applications/AI Dev Team.app"
fi
if [ "$DMG" = 1 ]; then
  hdiutil create -volname "AI Dev Team" -srcfolder "$APP" -ov -format UDZO \
    "$DIST/AI-Dev-Team-$VERSION.dmg" >/dev/null
  echo "==> $DIST/AI-Dev-Team-$VERSION.dmg"
fi
echo "Done. Double-click AI Dev Team to open it."
