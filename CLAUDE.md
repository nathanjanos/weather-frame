# CLAUDE.md — Weather Frame

## What this project is

A wall-hung office weather display for Nathan (Mount Pleasant, SC). A
Raspberry Pi 5 drives a framed matte IPS monitor, rotating through NOAA/NWS
forecast images every ~8 seconds — including animated GIF loops (radar,
satellite). Design goal: minimal, gallery-style — letterboxed on pure
black, crossfade transitions, one thin caption line, hung in a picture
frame so it reads as art, not a TV.

Development/testing happens on this Mac (`--windowed` mode); deployment
target is the Pi.

## Hardware (decided, being purchased)

- Display: ASUS ProArt PA248QV — 24.1" matte IPS, 1920x1200 (16:10),
  thin bezel, VESA 100x100, supports DDC/CI backlight control over HDMI
- Computer: Raspberry Pi 5, 8GB, Raspberry Pi OS Bookworm 64-bit desktop
- Accessories: official 27W USB-C PSU, Active Cooler, 32GB microSD,
  micro-HDMI→HDMI cable
- Enclosure: IKEA Ribba/Hovsta frame + custom-cut mat; Pi zip-tied to the
  monitor's VESA holes behind the frame
- Rejected: e-ink (refresh too slow for GIF loops), Meural Canvas II
  (closed ecosystem, no HDMI input)

## Files

- `weather_frame.py` — the whole app (pygame). Fullscreen slideshow;
  `--windowed` gives a 1280x800 test window. Keys: SPACE/→ next,
  ← previous, H hold current slide (GIF loops keep animating; caption
  shows "held"), P resume cycling, ESC/Q quit.
- `slides.json` — all configuration. Global: seconds_per_slide,
  crossfade_seconds, show_captions, quiet_hours, background_color.
  Per slide: name, url, refresh_minutes, enabled, latest_in_dir.
- `weather-frame.service` — systemd user unit for Pi autostart
- `INSTALL.md` — Pi deployment steps
- `PROJECT_LOG.md` — decision history

## Architecture notes

- `Fetcher` (daemon thread) downloads each slide on its own
  refresh_minutes cadence; atomic writes to `cache/`; falls back to the
  last good cached copy on network failure; decodes off the render path
  and swaps frames in under a per-slide lock.
- `decode_to_frames()` letterboxes onto the background color at screen
  size and caps animations at 24 frames (MAX_FRAMES) to bound memory,
  scaling frame durations to keep loop speed correct.
- `latest_in_dir: true` means the slide URL is a NOAA CDN directory
  listing; the fetcher regexes hrefs and takes the newest `.gif`
  (GOES-19 publishes timestamped files with no "latest" alias).
- `type: "sequence"` slides assemble NDFD-style numbered frames
  (`url_template` with `{n}`, `frame_start`/`frame_count`) into an
  animated GIF in memory (`frame_seconds` per frame, `hold_seconds` on
  the last, default 3x), which then uses the normal cache/decode path.
  Missing frames near forecast-cycle boundaries are skipped, not fatal.
- `type: "tides"` slides are rendered locally, not downloaded: the
  fetcher pulls NOAA CO-OPS predictions (6-min curve + hilo extremes) as
  JSON and `render_tide_chart()` draws a gallery-style tide curve with
  Pillow at screen size, then the PNG bytes flow through the same
  cache/decode path as image slides (so offline fallback is identical).
  With `observed: true` (default) it also fetches measured water level
  and overlays it in amber — the gap between the curves is storm surge;
  the "now" dot rides the observed curve when the last measurement is
  <45 min old, else the prediction. Observed fetch failure is non-fatal
  (chart renders predictions-only).
  Tunables per slide: `station`, `timezone` (the station's IANA zone —
  keeps the window and "now" marker correct even if the OS timezone is
  wrong), `hours_past`, `hours_ahead`. Refresh matters even though
  predictions are static — it repositions the amber "now" marker.
  CO-OPS reports errors as HTTP 200 + `{"error": ...}` (handled); note
  subordinate stations return no 6-min predictions — use harmonic
  stations like 8665530.
- Quiet hours (default 22:00–06:30) blank the app and call `wlopm` to cut
  display power on the Pi; the call fails silently on macOS — disable
  quiet_hours in slides.json if testing at night on the laptop.
- Main loop: 30fps; GIF frames step by their own durations inside each
  slide's dwell time; crossfade blends the outgoing frame via set_alpha.
- Voice control (optional, `voice` block in slides.json): `VoiceControl`
  daemon thread runs Vosk fully on-device with a two-stage constrained
  grammar — ["hey jarvis", "[unk]"] continuously, then the command
  grammar (next/forward/back/previous/hold/pause/play/resume) for a few
  seconds after wake — and posts KEYDOWN events to the main loop. Amber
  dot bottom-right while capturing. Needs `pip install sounddevice
  vosk` (optional deps — the app runs without them); Vosk model
  auto-downloads to `models/` (gitignored). Gotcha: openWakeWord was
  tried for wake-word detection but silently returns ~zero scores on
  numpy>=2 (required on Python 3.14) — verified broken even on its own
  test clips; that's why Vosk handles the wake phrase too.

## Verified feeds (all live-tested 2026-07-11)

| Slide | URL | Refresh |
|---|---|---|
| Charleston Radar | https://radar.weather.gov/ridge/standard/KCLX_loop.gif | 6 min |
| Southeast Radar Mosaic | https://radar.weather.gov/ridge/standard/SOUTHEAST_loop.gif | 10 min |
| GOES-East GEOCOLOR (SE) | https://cdn.star.nesdis.noaa.gov/GOES19/ABI/SECTOR/se/GEOCOLOR/GOES19-SE-GEOCOLOR-600x600.gif | 15 min |
| GOES-East Air Mass (SE) | https://cdn.star.nesdis.noaa.gov/GOES19/ABI/SECTOR/se/AirMass/GOES19-SE-AirMass-600x600.gif | 15 min |
| GOES-East Infrared / Band 13 (SE) | https://cdn.star.nesdis.noaa.gov/GOES19/ABI/SECTOR/se/13/GOES19-SE-13-600x600.gif | 15 min |
| GOES-East Sandwich (SE) | https://cdn.star.nesdis.noaa.gov/GOES19/ABI/SECTOR/se/Sandwich/GOES19-SE-Sandwich-600x600.gif | 15 min |
| GOES-East Lightning / GLM FED (SE) | https://cdn.star.nesdis.noaa.gov/GOES19/GLM/SECTOR/se/EXTENT3/GOES19-SE-EXTENT3-600x600.gif | 15 min |
| Mount Pleasant Meteogram | forecast.weather.gov/meteograms/Plotter.php (lat 32.8323, lon -79.8284) | 60 min |
| Temperature (SC loop) | https://graphical.weather.gov/images/southcarolina/T{n}_southcarolina.png, n=1–24 (sequence) | 60 min |
| Wind Speed & Direction (SC loop) | https://graphical.weather.gov/images/southcarolina/WindSpd{n}_southcarolina.png, n=1–24 (sequence) | 60 min |
| Sky Cover (SC loop) | https://graphical.weather.gov/images/southcarolina/Sky{n}_southcarolina.png, n=1–24 (sequence) | 60 min |
| Amount of Precipitation (SC loop) | https://graphical.weather.gov/images/southcarolina/QPF{n}_southcarolina.png, n=1–12 (sequence) | 60 min |
| US Surface Analysis (barometric) | https://www.wpc.ncep.noaa.gov/sfc/bwsfc.gif | 2 hr |
| Surface Forecast Loop (~60h fronts) | https://www.wpc.ncep.noaa.gov/basicwx/{n}fndfd.gif, n=92–99 (sequence; 97 missing by design, 404s every fetch — expected log noise) | 3 hr |
| Surface Forecast Day 3 / Day 5 (WPC) | https://www.wpc.ncep.noaa.gov/medr/9jhwbg_conus.gif / 9lhwbg_conus.gif (letter-coded: j/k/l/m/n = Day 3–7) | 6 hr |
| 500mb Upper-Air Analysis (SPC) | https://www.spc.noaa.gov/exper/mesoanalysis/s19/500mb/500mb.gif | 60 min |
| 300mb Jet Stream (SPC) | https://www.spc.noaa.gov/exper/mesoanalysis/s19/300mb/300mb.gif | 60 min |
| Atlantic Surface Analysis (OPC) | https://ocean.weather.gov/A_sfc_full_ocean_color.png | 3 hr |
| 7-Day Precip (WPC) | https://www.wpc.ncep.noaa.gov/qpf/p168i.gif | 3 hr |
| 6-10 Day Temp Outlook (CPC) | https://www.cpc.ncep.noaa.gov/products/predictions/610day/610temp.new.gif (keep the ".new") | 6 hr |
| 8-14 Day Precip Outlook (CPC) | https://www.cpc.ncep.noaa.gov/products/predictions/814day/814prcp.new.gif | 6 hr |
| Severe Outlook (SPC Day 1) | https://www.spc.noaa.gov/partners/outlooks/national/swody1.png | 60 min |
| Severe Outlook (SPC Day 2) | https://www.spc.noaa.gov/partners/outlooks/national/swody2.png | 3 hr |
| Atlantic Tropical (NHC) | https://www.nhc.noaa.gov/xgtwo/two_atl_7d0.png | 2 hr |
| Saharan Dust Layer | https://tropic.ssec.wisc.edu/real-time/sal/g16split/g16split.jpg (non-NOAA host, stable for years) | 3 hr |
| Gulf Stream SST | coastwatch.pfeg.noaa.gov ERDDAP jplMURSST41.transparentPng (transparent land → black; ~60 s server render; colorBar 24–31 °C tuned for summer, widen to ~16\|30 in winter) | 12 hr |
| Charleston Harbor Tides | CO-OPS API, station 8665530 (rendered locally, type: "tides"; observed water-level overlay = surge) | 30 min |
| US Drought Monitor (Southeast) | https://droughtmonitor.unl.edu/data/png/current/current_southeast_trd.png | 12 hr |
| The Sun Today (SDO HMI) | https://sdo.gsfc.nasa.gov/assets/img/latest/latest_2048_HMIIC.jpg (occasionally serves stale "latest") | 60 min |
| Solar Corona (SOHO LASCO C3) | https://soho.nascom.nasa.gov/data/realtime/c3/1024/latest.jpg (periodic multi-day CCD-bakeout gaps) | 60 min |

The GOES-19 SECTOR CDN publishes a stable-named animated loop per product
(`GOES19-SE-<PRODUCT>-600x600.gif`); SE-sector products include GEOCOLOR,
AirMass, Sandwich, 13 (Clean IR), Dust, FireTemperature, and numbered
bands 01–16. SE sector maxes out at 600x600. These direct URLs replaced
the old `latest_in_dir` GEOCOLOR entry (the feature remains in the code as
a fallback but no slide currently uses it).

Gotchas already hit: SPC removed `day1otlk.gif` (use the partners
endpoint above); GOES-16 was replaced by GOES-19 as GOES-East;
`graphical.weather.gov` served HTTP 500 across the board earlier on
2026-07-11 but recovered the same day (the old disabled "Max Temperature
Forecast (SC)" slide was retired during the outage; QPF loop added after
recovery). NDFD publishes images past the 72 h data horizon — QPF frames
13+ repeat frame 13's map with only the caption changed (loop stops at
12), and for 3-hourly elements (T/WindSpd/Sky) even-numbered frames
freeze past ~27, so those loops stop at 24 (which also matches
MAX_FRAMES). Water-vapor band 09 has no stable SE loop file (404).
Slide cache filenames strip URL query strings (ERDDAP/meteogram URLs
contain dots in their queries). If a feed 404s, fix the URL in
slides.json — no code changes needed.

## Conventions

- Python 3, stdlib + pygame/Pillow/requests only; keep it a single file
  (exception: sounddevice + vosk as *optional* imports for voice
  control — the app must keep working when they're absent)
- All tunables live in slides.json, never hardcoded
- Set a User-Agent on NOAA requests (they ask for identification)
- Windows/macOS compatibility matters for testing (e.g. no `%-I` strftime)

## Open items

- Physical build: mat sizing to the PA248QV visible panel, cable routing
- Possible future slides: marine forecast (tides done 2026-07-11)
- Final compiled build document (Claude in the chat app is tracking this
  in PROJECT_LOG.md)
