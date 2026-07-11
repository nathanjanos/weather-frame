# Weather Frame — Project Decision Log

Running record of decisions; will be compiled into the final build document.

## Concept
- Wall-hung office display rotating 5–10 NOAA/NWS forecast images,
  ~8 seconds each, supporting animated GIF loops (radar, satellite)
- Aesthetic: minimal, gallery-style — letterboxed on black, crossfades,
  single thin caption, framed like art

## Decisions
- **E-ink rejected** — refresh too slow for GIF loops and 5–10s rotation
- **Meural Canvas II rejected** — matte framed IPS but closed ecosystem,
  no HDMI input, can't be driven by a Pi
- **Display: ASUS ProArt PA248QV** (~$200) — 24.1" matte IPS, 1920x1200
  (16:10 frames like a print), thin bezels, VESA 100x100
- **Computer: Raspberry Pi 5, 8GB** (~$110 at official resellers —
  PiShop/CanaKit/Adafruit; avoid marked-up Amazon listings)
- **Accessories:** official 27W USB-C PSU, Active Cooler, 32GB microSD,
  micro-HDMI→HDMI cable
- **Frame:** IKEA Ribba/Hovsta + custom-cut mat; Pi mounted to monitor
  VESA holes behind the frame
- **Budget:** ~$350–400 total

## Software (built & tested 2026-07-10)
- Python 3 / pygame slideshow: `weather_frame.py` + `slides.json` config
- Background fetcher thread, per-slide refresh intervals, atomic cache,
  last-good-image fallback on network failure
- GIF loops play natively; frames capped at 24 (memory bound on Pi)
- Crossfade transitions, quiet hours (22:00–06:30, screen off via wlopm)
- systemd user service for boot autostart
- Optional DDC/CI backlight dimming via ddcutil (PA248QV supports it)

## Verified feeds (Charleston / Mount Pleasant, SC)
| Slide | Source | Refresh |
|---|---|---|
| Charleston Radar (KCLX loop) | radar.weather.gov/ridge/standard/KCLX_loop.gif | 6 min |
| GOES-East SE Satellite loop | cdn.star.nesdis.noaa.gov GOES19 SE GEOCOLOR dir (newest .gif) | 15 min |
| Mount Pleasant Hourly Meteogram | forecast.weather.gov Plotter.php (lat 32.8323, lon -79.8284) | 60 min |
| 7-Day Precip Outlook | wpc.ncep.noaa.gov/qpf/p168i.gif | 3 hr |
| Severe Wx Outlook Day 1 | spc.noaa.gov/partners/outlooks/national/swody1.png | 60 min |
| Atlantic Tropical Outlook | nhc.noaa.gov/xgtwo/two_atl_7d0.png | 2 hr |
| Max Temp SC (disabled by default) | graphical.weather.gov/images/sc/MaxT1_sc.png | 3 hr |

## Open items
- Physical build: frame/mat sizing to the PA248QV's visible panel area,
  cable management, wall mount choice
- Final compiled build document
