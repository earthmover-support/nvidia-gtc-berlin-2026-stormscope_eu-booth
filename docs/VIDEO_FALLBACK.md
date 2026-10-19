# Offline video fallback

If the booth loses internet, the live page has nothing to show. As a last resort, loop a
20-minute recording of the live booth page instead. 20 minutes covers two full 10-minute forecast runs: the
ingest strip, countdown, commits, globe playback, and the explainer panels (7 x 25 s).

`presentation/record_video.sh` records it. Record it while the live pipeline is running and
the tiles service is up, the day before the event or on the morning of it.

## Recipe

Needs: macOS, Google Chrome, an external 1920x1080 monitor, `.env` with `ARRAYLAKE_API_TOKEN`
at the repo root, and `presentation/node_modules` (`cd presentation && npm ci`).

1. Install ffmpeg: `brew install ffmpeg`.
2. Give your terminal app the Screen Recording permission: System Settings > Privacy & Security >
   Screen & System Audio Recording. Restart the terminal. Without the permission, ffmpeg gets no frames,
   and the script stops after 15 s with a message.
3. Find the external monitor's capture device and left edge:

   ```sh
   ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | grep "Capture screen"
   osascript -l JavaScript -e 'ObjC.import("AppKit"); var s=$.NSScreen.screens, o=[];
     for (var i=0;i<s.count;i++){var f=s.objectAtIndex(i).frame;
     o.push(f.origin.x+","+f.origin.y+" "+f.size.width+"x"+f.size.height)}; o.join("\n")'
   ```

   The defaults are `SCREEN="Capture screen 1"` and `WINDOW_X=1512` (the original Mac). Pass the
   device by name, because its index shifts whenever cameras are plugged in or removed.
4. Check that the pipeline is committing runs and the live page looks right
   (`uv run presentation/server.py`, then open http://127.0.0.1:8000).
5. Turn on Do Not Disturb, close anything that could pop up on the external monitor, then:

   ```sh
   presentation/record_video.sh ~/Desktop/booth-offline.mp4
   ```

   Don't touch the Mac for about 25 minutes: 1 min warm-up, 20 min recording, then a few minutes of
   encoding. The cursor isn't captured.
6. Watch the result once from start to end, then copy it to the booth laptop *and* a USB stick.

Knobs, all set through env variables: `DURATION_S=1200`, `SCREEN="Capture screen 1"`, `WINDOW_X=1512`, `WARMUP_S=60`,
`PORT=8765`, `CRF=22`, `SCALE=1920:1080`, `FIRST_VAR=true_color`, `LOOPS=2`. The page itself flips between
true_color and airmass after every `LOOPS` full playback loops (`?alternate=LOOPS`), and only picks up a newer init
at a flip, so each variable plays whole loops of one init. Without an external monitor, record the built-in screen with
`SCREEN="Capture screen 0" WINDOW_X=0 SCALE=1920:-2` (keeps its 16:10 aspect) and don't touch the Mac. For a quick trial, use `DURATION_S=30 WARMUP_S=20`.

## What the script does

1. Probes one frame from the screen, to fail fast on a missing permission or a wrong index.
2. Starts `server.py` live on its own port, so it doesn't collide with a running booth server.
3. Opens Chrome in kiosk mode on the external monitor with a throwaway profile, so no restore
   bubbles, extensions or sign-in prompts end up in the shot.
4. Waits `WARMUP_S` so the first tiles are on screen, then grabs `DURATION_S` at 30 fps.
   This pass encodes fast and at near-lossless quality (`ultrafast`, CRF 12), so no frames are dropped.
   It also forces a constant 30 fps (`-fps_mode cfr -r 30`), because avfoundation timestamps frames
   at 1 MHz. ffmpeg prints `Configuration of video device failed` and `not enough frames to estimate rate`;
   both are harmless.
5. Re-encodes with H.264 `slow`, CRF 22, a 10 s GOP, `yuv420p` and `+faststart`. Most of the screen is
   static UI. A 30 s test came out at 7.5 MB, so 20 min lands around 300 MB. Lower `CRF` gives better quality and a bigger file.

## Playback at the booth

Loop it fullscreen: QuickTime (View > Loop), or `open -a VLC --args --loop --fullscreen file.mp4`.
On a 4K screen the 1080p video gets upscaled. The page layout is designed for 1920x1080, so it looks
the same as the live page, just slightly softer.

## Why 1080p and not 4K

A screen grab can't be bigger than the screen it captures, and macOS won't make a window larger than
its screen. On the recording Mac, the external monitor is 1920x1080 and the built-in Retina panel is
1512x982 points. Headless Chrome could render 4K, but its screencast only delivers about 10-20 fps,
which makes the globe and playback choppy. To get real 4K, run the same script on a 4K monitor and
change the `scale=` in the final encode.
