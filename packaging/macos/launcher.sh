#!/bin/bash
# AI Dev Team.app launcher: starts the local page (127.0.0.1 only) once, then opens it.
APPDIR="$HOME/Library/Application Support/AI Dev Team"
PY="$APPDIR/venv/bin/python"
URLFILE="$APPDIR/ui.url"
PIDFILE="$APPDIR/server.pid"
LOG="$APPDIR/server.log"

alert() {
  /usr/bin/osascript -e "display dialog \"$1\" with title \"AI Dev Team\" buttons {\"好\"} default button 1 with icon caution" >/dev/null 2>&1
}

if [ ! -x "$PY" ]; then
  alert "还没有安装运行环境。请在终端进入 ai-dev-team 文件夹，运行 ./packaging/macos/build_app.sh"
  exit 1
fi

# shellcheck disable=SC1091
[ -f "$APPDIR/app.env" ] && set -a && . "$APPDIR/app.env" && set +a
export AI_DEV_TEAM_CONFIG="$APPDIR/agents.yaml"
export AI_DEV_TEAM_DB="$APPDIR/ai-dev-team.sqlite3"

# Already running? Just bring the page back.
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null && [ -s "$URLFILE" ]; then
  /usr/bin/open "$(cat "$URLFILE")"
  exit 0
fi

rm -f "$URLFILE"
cd "$APPDIR" || exit 1
nohup "$PY" -m ai_dev_team ui --no-browser --port 8765 --url-file "$URLFILE" >>"$LOG" 2>&1 &
echo $! > "$PIDFILE"

for _ in $(seq 1 100); do
  [ -s "$URLFILE" ] && break
  sleep 0.2
done
if [ -s "$URLFILE" ]; then
  /usr/bin/open "$(cat "$URLFILE")"
else
  alert "启动失败。详细信息在：$LOG"
  exit 1
fi
