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
# Knobs (env): DURATION_S=1200 SCREEN="Capture screen 1" WINDOW_X=1512 WARMUP_S=60 PORT=8765 CRF=22 LOOP_S="" FIRST_VAR=true_color
#   SCREEN    avfoundation device name or index, see: ffmpeg -f avfoundation -list_devices true -i ""
#   WINDOW_X  left edge of that monitor in points (Chrome opens fullscreen there)
#   LOOP_S    seconds per forecast playback loop (+0 -> +120min); empty = auto from
#             config.json (leads * speed + hold_ms). Override if you change playback speed live.
#   FIRST_VAR variable shown first; alternates with airmass after every playback loop.
#             Needs Accessibility permission for the terminal app the first time osascript
#             sends keystrokes (System Settings > Privacy & Security > Accessibility).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE/booth-offline-$(date +%Y%m%d-%H%M).mp4}"
DURATION_S="${DURATION_S:-1200}" SCREEN="${SCREEN:-Capture screen 1}" WINDOW_X="${WINDOW_X:-1512}"
WARMUP_S="${WARMUP_S:-60}" PORT="${PORT:-8765}" CRF="${CRF:-22}"
LOOP_S="${LOOP_S:-}" FIRST_VAR="${FIRST_VAR:-true_color}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TMP="$(mktemp -d)"
RAW="$TMP/raw.mp4"

# seconds for one +0 -> +120min playback loop at the configured speed + end hold;
# app.js SPEEDS: slow 0.75 / normal 0.5 / fast 0.25 s per frame
if [[ -z "$LOOP_S" ]]; then
  LOOP_S="$(HERE="$HERE" python3 -c 'import json, os; c=json.load(open(os.path.join(os.environ["HERE"], "config.json"))); n=c["lead_steps"]+1; sp={"slow":0.75,"normal":0.5,"fast":0.25}[c.get("playback_speed","normal")]; print("%.1f" % (n*sp + c.get("hold_ms",2500)/1000))')"
fi
case "$FIRST_VAR" in true_color) SECOND_VAR="airmass" ;; airmass) SECOND_VAR="true_color" ;; *)
  echo "FIRST_VAR must be true_color or airmass" >&2; exit 1 ;; esac

# app.js keys: t = true_color, a = airmass
send_var() {
  local var="$1" key
  case "$var" in true_color) key="t" ;; airmass) key="a" ;; esac
  osascript -e 'tell application "Google Chrome" to activate' \
    -e "tell application \"System Events\" to keystroke \"$key\"" >/dev/null || {
    echo "osascript keystroke failed: allow Accessibility for this terminal app in System Settings > Privacy & Security" >&2
    return 1
  }
  echo "$(date +%H:%M:%S) variable -> $var"
}

cleanup() {
  [[ -n "${FFMPEG_PID:-}" ]] && kill "$FFMPEG_PID" 2>/dev/null || true
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

# capture in the background so this script can flip the variable after each playback
# loop: fast lossless-ish so no frames drop; the second pass makes it small
echo "recording ${DURATION_S}s from screen $SCREEN (loop ${LOOP_S}s, starting at $FIRST_VAR)..."
ffmpeg -hide_banner -loglevel warning -stats \
  -f avfoundation -pixel_format uyvy422 -capture_cursor 0 -framerate 30 -i "$SCREEN:none" \
  -t "$DURATION_S" -fps_mode cfr -r 30 -c:v libx264 -preset ultrafast -crf 12 -pix_fmt yuv420p "$RAW" &
FFMPEG_PID=$!

# pin the first variable (page boots on true_color), then alternate after each loop
sleep 1 # let the capture settle before stealing focus for the keystroke
send_var "$FIRST_VAR"
current="$FIRST_VAR"
LOOP_INT="${LOOP_S%.*}"
LOOP_FRAC="$(LOOP_S="$LOOP_S" python3 -c 'import os; s=float(os.environ["LOOP_S"]); print("%.1f" % (s - int(s)))')"
while kill -0 "$FFMPEG_PID" 2>/dev/null; do
  # sleep in 1s slices so we stop promptly when ffmpeg ends instead of overshooting DURATION_S
  for _ in $(seq "$LOOP_INT"); do kill -0 "$FFMPEG_PID" 2>/dev/null || break 2; sleep 1; done
  # fractional remainder keeps long recordings from drifting (e.g. LOOP_S=12.2)
  if [[ "$LOOP_FRAC" != "0.0" ]]; then kill -0 "$FFMPEG_PID" 2>/dev/null || break; sleep "$LOOP_FRAC"; fi
  kill -0 "$FFMPEG_PID" 2>/dev/null || break
  if [[ "$current" == "$FIRST_VAR" ]]; then current="$SECOND_VAR"; else current="$FIRST_VAR"; fi
  send_var "$current" || break
done
wait "$FFMPEG_PID" || true
FFMPEG_PID=""

kill "$CHROME_PID" "$SERVER_PID" 2>/dev/null || true

# final: mostly static UI compresses well; long GOP + slow preset keeps the size down
echo "encoding $OUT..."
ffmpeg -hide_banner -loglevel warning -stats -y -i "$RAW" \
  -vf "scale=1920:1080:flags=lanczos" -c:v libx264 -preset slow -crf "$CRF" \
  -g 300 -pix_fmt yuv420p -movflags +faststart -an "$OUT"

ls -lh "$OUT"
