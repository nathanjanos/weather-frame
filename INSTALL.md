# Weather Frame — Install Guide

Target: Raspberry Pi 5 (8GB), Raspberry Pi OS Bookworm (64-bit, desktop),
ASUS ProArt PA248QV over HDMI at 1920x1200.

## 1. OS setup

- Flash Raspberry Pi OS (64-bit, with desktop) using Raspberry Pi Imager;
  preconfigure WiFi, hostname (e.g. `weatherframe`), and SSH in the Imager
  settings.
- Boot, then: `sudo apt update && sudo apt full-upgrade -y`
- Install dependencies:

```bash
sudo apt install -y python3-pygame python3-pil python3-requests wlopm
```

## 2. Deploy

The project lives at https://github.com/nathanjanos/weather-frame
(private), so the Pi needs GitHub credentials once. Simplest: an SSH key.

```bash
# on the Pi — one-time setup
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub   # add this at github.com/settings/keys

git clone git@github.com:nathanjanos/weather-frame.git ~/weather-frame
```

To update the Pi after pushing changes from the workstation:

```bash
cd ~/weather-frame && git pull
systemctl --user restart weather-frame   # if the service is running
```

(Config experiments on the Pi itself: edit slides.json, then either
commit/push from the Pi or `git stash` before the next pull.)

Test it interactively first (from an SSH session with the desktop running,
or a keyboard on the Pi):

```bash
cd ~/weather-frame
python3 weather_frame.py            # fullscreen; ESC or Q quits
python3 weather_frame.py --windowed # 1280x800 window for testing
```

## 2a. Local development on macOS

The Pi uses the apt-packaged `python3-pygame` (SDL-tested for that
hardware) — **do not** `pip install pygame` on the Pi. On a Mac for
testing, use a virtualenv instead:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install pygame-ce pillow requests
python weather_frame.py --windowed
```

Note `pygame-ce`, not `pygame`: on current Python (3.13/3.14) mainline
pygame has no prebuilt wheel and pip tries to compile it from source,
which fails without SDL's C headers (`fatal error: 'SDL.h' file not
found`). `pygame-ce` is a maintained drop-in fork that ships prebuilt
wheels and imports as `pygame`, so no code changes are needed. This only
affects Mac testing — the Pi is unaffected.

## 3. Autostart on boot

The boot trigger must be the desktop session's own autostart file —
NOT cron `@reboot` (fires before a display exists, empty environment)
and NOT a systemd user unit alone (Raspberry Pi OS's labwc session
never activates the user `graphical-session.target`, so a unit
"enabled" into it silently never starts). The labwc autostart file
runs exactly when the compositor is up, with the display environment
pygame and wlopm need.

The deployed setup — self-restarting on crash, output logged, and
`$HOME` expands at paste time so a non-`pi` username can't silently
break the path (the classic failure: the app launches, dies instantly
on a wrong hardcoded path, and the loop respawns it invisibly —
indistinguishable from "autostart didn't run"):

```bash
mkdir -p ~/.config/labwc
printf '%s\n' "exec >>/tmp/weather-frame-autostart.log 2>&1" "while true; do /usr/bin/python3 \"$HOME/weather-frame/weather_frame.py\"; sleep 5; done &" >> ~/.config/labwc/autostart
```

Requires Desktop Autologin (see §4). Pi OS runs BOTH
/etc/xdg/labwc/autostart and the user file (its labwc-pi wrapper
passes labwc's -m merge flag), so this is purely additive — the
desktop still starts normally. The file is run with plain `sh`; no
exec bit needed; the trailing `&` is required. On an older
Wayfire-based image, put the same command under `[autostart]` in
~/.config/wayfire.ini instead.

Verify after a reboot (~30 s), including why it died if it did:

```bash
pgrep -af weather_frame.py || tail -5 /tmp/weather-frame-autostart.log
```

Alternative: weather-frame.service is a systemd user unit for those
who prefer journald logging and `systemctl` control — install it per
the comments in the file, and have the labwc autostart run
`systemctl --user restart weather-frame` instead of the loop above.
Note that stock Pi OS keeps the journal in memory only, so
`journalctl --user` reports "No journal files were found" — user-unit
logs land in the system journal (`sudo journalctl -b
_SYSTEMD_USER_UNIT=weather-frame.service`).

## 4. Pi OS kiosk polish

- `sudo raspi-config` → System → Boot / Auto Login → Desktop Autologin
- Appearance Settings → disable screen blanking (the app manages quiet
  hours itself via `wlopm`)
- Optional: hide the desktop taskbar since the app runs fullscreen anyway

## 5. Customizing slides.json

Each slide entry:

```json
{
  "name": "Charleston Radar",              // caption text
  "url": "https://.../KCLX_loop.gif",      // direct image/GIF URL
  "refresh_minutes": 6,                    // re-download interval
  "enabled": true,
  "latest_in_dir": false                   // true = url is a directory
                                           // listing; newest .gif is used
}
```

Global settings: `seconds_per_slide`, `crossfade_seconds`, `show_captions`,
`quiet_hours` (`off`/`on`, 24h clock), `background_color`, and
`display_scale` (shrink slides toward center — e.g. 0.97 leaves a 3%
black margin so a picture mat that overlaps the panel edges never
covers content; captions and the voice dot move inward to match).

Locally rendered tide chart (no image URL — drawn from NOAA CO-OPS
prediction data):

```json
{
  "name": "Charleston Harbor Tides",
  "type": "tides",
  "station": "8665530",             // any CO-OPS tide station id
  "timezone": "America/New_York",   // the STATION's IANA timezone
  "hours_past": 6,                  // chart window behind now
  "hours_ahead": 30,                // chart window ahead of now
  "observed": true,                 // overlay measured water level (amber);
                                    // the gap vs prediction is storm surge
  "refresh_minutes": 30             // re-render cadence (moves "now" marker)
}
```

Always set `timezone` to the station's zone — it keeps the chart and its
"now" marker correct even if the machine's OS timezone is unset or wrong
(a freshly imaged Pi often sits on UTC/Europe/London).

Animated loop assembled from numbered frames (NDFD graphical forecasts
publish loops as separate PNGs):

```json
{
  "name": "Amount of Precipitation (SC, 3-Day Loop)",
  "type": "sequence",
  "url_template": "https://graphical.weather.gov/images/southcarolina/QPF{n}_southcarolina.png",
  "frame_start": 1,            // first frame number substituted for {n}
  "frame_count": 12,           // how many frames
  "frame_seconds": 0.7,        // seconds each frame is shown
  "hold_seconds": 0,           // pause on last frame (0 = 3x frame_seconds)
  "refresh_minutes": 60
}
```

## 6. Optional: voice control

Fully on-device (Vosk) — no cloud, nothing leaves the machine. Say
**"hey jarvis"**, then one of: *next / forward / back / previous /
hold / pause / play / resume* — or any slide's `keyword` from
slides.json to jump straight to it ("hey jarvis, tides"), or
**"instructions"** / **"help"** to overlay a card listing every
command and keyword for `help_seconds` (default 10). An amber dot
appears bottom-right while it listens for the command. Keywords must
be unique lowercase single words the small Vosk model knows (common
English words work; the app refuses duplicates or command collisions
at startup).

```bash
# Mac (in the venv)
pip install sounddevice vosk

# Pi
sudo apt install -y libportaudio2
pip3 install --break-system-packages sounddevice vosk
```

Enable in slides.json (all keys optional except enabled):

```json
"voice": {
  "enabled": true,
  "wake_word": "hey jarvis",   // grammar phrase to listen for
  "command_seconds": 3.0,      // listening window after the wake word
  "mic_device": null           // sounddevice input; null = system default
}
```

Notes:
- First run downloads the small Vosk model (~40 MB) to `models/`.
- macOS: the first mic access pops a permission dialog for your
  terminal — grant it once. The app logs "voice control on" when ready.
- The Pi needs a USB microphone; mount it peeking out of the frame
  edge, not sealed behind it, or it will be muffled.
- If the deps, model, or mic are missing, voice control disables
  itself with a log line and the slideshow is unaffected.
- "Invalid sample rate [PaErrorCode -9997]": the mic can't capture at
  the requested rate. The app now opens the mic at its native rate
  automatically (Vosk downsamples internally), so update to a build
  with this fix if you see it. The log shows the device and rate used.
- Voice worked when launched by hand but not at boot: the app can
  start before USB audio is enumerated. The voice thread now retries
  the mic every 30 s forever (and reopens it after unplug/replug), so
  it comes up on its own; check progress with
  `grep voice /tmp/weather-frame.log`.
- Wrong mic picked up? List devices and set "mic_device" in the voice
  block to the right index (or a name substring):

```bash
python3 -c "import sounddevice; print(sounddevice.query_devices())"
```

## 7. Optional: true backlight dimming via DDC/CI

The PA248QV supports DDC/CI, so the Pi can control the monitor's actual
backlight over the HDMI cable:

```bash
sudo apt install -y ddcutil
ddcutil detect                 # confirm the monitor is seen
ddcutil setvcp 10 35           # brightness 0-100
```

Cron example — dim to 25 at sunset hours, full at morning:

```
0 20 * * *  ddcutil setvcp 10 25
30 6 * * *  ddcutil setvcp 10 90
```

## 8. Troubleshooting

- A slide shows nothing: check `journalctl` for fetch/decode warnings; the
  app keeps the last good image on network failures and skips slides that
  have never loaded.
- NOAA occasionally reorganizes image URLs. Everything is in slides.json —
  fix the URL there, no code changes needed.
- Feed status as of 2026-07-10: all six enabled feeds verified live.
