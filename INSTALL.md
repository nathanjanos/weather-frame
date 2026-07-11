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

```bash
mkdir -p ~/.config/systemd/user
cp weather-frame.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now weather-frame
loginctl enable-linger pi
```

Logs: `journalctl --user -u weather-frame -f`

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
`quiet_hours` (`off`/`on`, 24h clock), `background_color`.

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
  "refresh_minutes": 30             // re-render cadence (moves "now" marker)
}
```

Always set `timezone` to the station's zone — it keeps the chart and its
"now" marker correct even if the machine's OS timezone is unset or wrong
(a freshly imaged Pi often sits on UTC/Europe/London).

## 6. Optional: true backlight dimming via DDC/CI

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

## 7. Troubleshooting

- A slide shows nothing: check `journalctl` for fetch/decode warnings; the
  app keeps the last good image on network failures and skips slides that
  have never loaded.
- NOAA occasionally reorganizes image URLs. Everything is in slides.json —
  fix the URL there, no code changes needed.
- Feed status as of 2026-07-10: all six enabled feeds verified live.
