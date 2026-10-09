#!/usr/bin/env bash
# Record the live booth page to an MP4, the offline fallback for a booth without internet.
#
# Starts server.py live, opens it fullscreen in Chrome on the external 1080p monitor,
# screen-grabs that monitor with ffmpeg, then re-encodes small. Default 20 min = two
# 10-min forecast runs. Leave the Mac alone while it records (Do Not Disturb on).
#
#   brew install ffmpeg      # once; also allow Screen Recording for your terminal app
#   presentation/record_video.sh [out.mp4]
#
# Knobs (env): DURATION_S=1200 SCREEN="Capture screen 1" WINDOW_X=1512 WARMUP_S=60 PORT=8765 CRF=22
#   SCREEN    avfoundation device name or index, see: ffmpeg -f avfoundation -list_devices true -i ""
#   WINDOW_X  left edge of that monitor in points (Chrome opens fullscreen there)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE/booth-offline-$(date +%Y%m%d-%H%M).mp4}"
DURATION_S="${DURATION_S:-1200}" SCREEN="${SCREEN:-Capture screen 1}" WINDOW_X="${WINDOW_X:-1512}"
WARMUP_S="${WARMUP_S:-60}" PORT="${PORT:-8765}" CRF="${CRF:-22}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TMP="$(mktemp -d)"
RAW="$TMP/raw.mp4"

cleanup() {
  [[ -n "${CHROME_PID:-}" ]] && kill "$CHROME_PID" 2>/dev/null || true
  [[ -n "${SERVER_PID:-}" ]] && kill "$SERVER_PID" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

# without the Screen Recording permission avfoundation hangs silently instead of failing
perl -e 'alarm 15; exec @ARGV' ffmpeg -v error -f avfoundation -pixel_format uyvy422 -framerate 30 -i "$SCREEN:none" \
  -frames:v 1 -y "$TMP/probe.png" || {
  echo "no frames from screen $SCREEN: allow Screen Recording for this terminal app in" \
    "System Settings > Privacy & Security, restart the terminal, and check SCREEN" >&2
  exit 1
}

uv run "$HERE/server.py" --port "$PORT" &
SERVER_PID=$!
for _ in $(seq 60); do curl -sf "http://127.0.0.1:$PORT/api/config" >/dev/null && break; sleep 1; done
curl -sf "http://127.0.0.1:$PORT/api/config" >/dev/null || { echo "server did not start" >&2; exit 1; }

# fresh profile: no restore bubbles, extensions or sign-in prompts in the shot
"$CHROME" --user-data-dir="$TMP/chrome" --no-first-run --no-default-browser-check \
  --disable-session-crashed-bubble --disable-features=Translate \
  --window-position="$WINDOW_X,0" --kiosk "http://127.0.0.1:$PORT/" >/dev/null 2>&1 &
CHROME_PID=$!

echo "warming up ${WARMUP_S}s so the first tiles are loaded..."
sleep "$WARMUP_S"

# capture: fast lossless-ish so no frames drop; the second pass makes it small
echo "recording ${DURATION_S}s from screen $SCREEN..."
ffmpeg -hide_banner -loglevel warning -stats \
  -f avfoundation -pixel_format uyvy422 -capture_cursor 0 -framerate 30 -i "$SCREEN:none" \
  -t "$DURATION_S" -fps_mode cfr -r 30 -c:v libx264 -preset ultrafast -crf 12 -pix_fmt yuv420p "$RAW"

kill "$CHROME_PID" "$SERVER_PID" 2>/dev/null || true

# final: mostly static UI compresses well; long GOP + slow preset keeps the size down
echo "encoding $OUT..."
ffmpeg -hide_banner -loglevel warning -stats -y -i "$RAW" \
  -vf "scale=1920:1080:flags=lanczos" -c:v libx264 -preset slow -crf "$CRF" \
  -g 300 -pix_fmt yuv420p -movflags +faststart -an "$OUT"

ls -lh "$OUT"
