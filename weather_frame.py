#!/usr/bin/env python3
"""
weather_frame.py — minimal wall-mounted weather display
Target: Raspberry Pi 5 (8GB) + ASUS ProArt PA248QV (1920x1200)

- Rotates through NOAA/NWS forecast images defined in slides.json
- Animated GIFs (radar/satellite loops) play their frames natively
- Background thread refreshes each image on its own schedule
- Crossfade transitions, letterboxed on pure black
- Optional quiet hours (screen blanks on a schedule)

Run:  python3 weather_frame.py [--config slides.json] [--windowed]
Keys: ESC/Q quit, RIGHT/SPACE next slide, LEFT previous slide,
      H hold current slide (loops keep animating), P resume cycling
"""

import argparse
import io
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pygame
import requests
from PIL import Image, ImageDraw, ImageFont, ImageSequence

log = logging.getLogger("weather_frame")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DEFAULTS = {
    "seconds_per_slide": 8,
    "crossfade_seconds": 0.8,
    "show_captions": True,
    "caption_font_size": 26,
    "quiet_hours": {"enabled": True, "off": "22:00", "on": "06:30"},
    "background_color": [0, 0, 0],
    "user_agent": "weather-frame/1.0 (personal wall display)",
}


@dataclass
class Slide:
    name: str
    url: str = ""                 # unused for locally rendered types
    type: str = "image"           # "image" | "tides" | "sequence"
    refresh_minutes: int = 10
    enabled: bool = True
    latest_in_dir: bool = False   # url is a directory listing; fetch newest .gif
    station: str = ""             # tides: NOAA CO-OPS station id
    timezone: str = ""            # tides: station IANA tz, e.g. America/New_York
    hours_past: int = 6           # tides: chart window behind now
    hours_ahead: int = 30         # tides: chart window ahead of now
    url_template: str = ""        # sequence: url with {n} placeholder
    frame_start: int = 1          # sequence: first frame number
    frame_count: int = 0          # sequence: how many frames
    frame_seconds: float = 0.7    # sequence: seconds per frame
    hold_seconds: float = 0.0     # sequence: pause on last frame (0 = 3x frame)
    # runtime state
    frames: list = field(default_factory=list)      # [(Surface, duration_s)]
    updated_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def cache_name(self) -> str:
        safe = "".join(c if c.isalnum() else "_" for c in self.name.lower())
        if self.type == "tides":
            return f"{safe}.png"
        if self.type == "sequence":
            return f"{safe}.gif"
        return f"{safe}{Path(self.url).suffix or '.img'}"


def load_config(path: Path):
    with open(path) as f:
        raw = json.load(f)
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k != "slides"}}
    # fail loudly on config mistakes: a typo'd slide silently skipped at
    # runtime is invisible on a headless wall display
    for s in raw["slides"]:
        if not s.get("enabled", True):
            continue
        kind = s.get("type", "image")
        if kind not in ("image", "tides", "sequence"):
            sys.exit(f"slides.json: unknown type {kind!r} in {s['name']!r}")
        if kind == "image" and not s.get("url"):
            sys.exit(f"slides.json: {s['name']!r} needs a url")
        if kind == "tides" and not s.get("station"):
            sys.exit(f"slides.json: {s['name']!r} needs a station id")
        if kind == "sequence" and ("{n}" not in s.get("url_template", "")
                                   or s.get("frame_count", 0) < 1):
            sys.exit(f"slides.json: {s['name']!r} needs a url_template "
                     "containing {n} and a frame_count")
    slides = [
        Slide(name=s["name"], url=s.get("url", ""),
              type=s.get("type", "image"),
              refresh_minutes=s.get("refresh_minutes", 10),
              enabled=s.get("enabled", True),
              latest_in_dir=s.get("latest_in_dir", False),
              station=s.get("station", ""),
              timezone=s.get("timezone", ""),
              hours_past=s.get("hours_past", 6),
              hours_ahead=s.get("hours_ahead", 30),
              url_template=s.get("url_template", ""),
              frame_start=s.get("frame_start", 1),
              frame_count=s.get("frame_count", 0),
              frame_seconds=s.get("frame_seconds", 0.7),
              hold_seconds=s.get("hold_seconds", 0.0))
        for s in raw["slides"] if s.get("enabled", True)
    ]
    return cfg, slides


# --------------------------------------------------------------------------
# Fetching + decoding
# --------------------------------------------------------------------------

def decode_to_frames(data: bytes, screen_size, bg_color):
    """Decode image bytes (static or animated GIF) into scaled pygame
    surfaces, letterboxed to the screen on the background color."""
    MAX_FRAMES = 24                       # bound memory for long loops
    sw, sh = screen_size
    img = Image.open(io.BytesIO(data))
    n = getattr(img, "n_frames", 1)
    step = max(1, -(-n // MAX_FRAMES))    # ceil division -> keep every Nth
    frames = []
    for i, frame in enumerate(ImageSequence.Iterator(img)):
        if i % step:
            continue
        duration = frame.info.get("duration", 0) / 1000.0 * step
        rgba = frame.convert("RGBA")
        # scale to fit, preserve aspect
        scale = min(sw / rgba.width, sh / rgba.height)
        new_size = (max(1, int(rgba.width * scale)),
                    max(1, int(rgba.height * scale)))
        rgba = rgba.resize(new_size, Image.LANCZOS)
        canvas = Image.new("RGB", (sw, sh), tuple(bg_color))
        canvas.paste(rgba, ((sw - new_size[0]) // 2, (sh - new_size[1]) // 2),
                     rgba)
        surf = pygame.image.frombytes(canvas.tobytes(), (sw, sh), "RGB")
        frames.append((surf.convert(), duration))
    # NWS radar loops hold the last frame briefly; give static images a
    # nominal duration and slow crawling loops down slightly if needed
    if len(frames) == 1:
        frames[0] = (frames[0][0], 0.0)
    return frames


def _tide_font(px: int) -> ImageFont.FreeTypeFont:
    # pygame bundles freesansbold.ttf on every platform we run on
    return ImageFont.truetype(str(Path(pygame.__file__).parent /
                                  "freesansbold.ttf"), px)


def render_tide_chart(points, events, now, size, bg_color):
    """Draw the tide curve, gallery style, directly at screen size.
    points: [(datetime, feet)] 6-minute predictions, ascending
    events: [(datetime, feet, "H"|"L")] high/low extremes
    Returns a PIL Image.
    """
    w, h = size
    GRID = (34, 38, 44)
    DAYLINE = (56, 62, 70)
    WATER = (13, 24, 38)
    CURVE = (222, 227, 233)
    TEXT = (128, 134, 142)
    AMBER = (216, 174, 90)
    AMBER_DIM = (105, 87, 48)

    img = Image.new("RGB", (w, h), tuple(bg_color))
    d = ImageDraw.Draw(img)
    f_sm = _tide_font(max(13, h // 50))
    f_md = _tide_font(max(15, h // 36))
    line_w = max(2, h // 400)

    x0, x1 = int(w * 0.07), int(w * 0.96)
    y0, y1 = int(h * 0.10), int(h * 0.88)
    t0, t1 = points[0][0], points[-1][0]
    span = (t1 - t0).total_seconds()
    vmin = min(v for _, v in points)
    vmax = max(v for _, v in points)
    lo, hi = math.floor(vmin - 0.4), math.ceil(vmax + 0.4)

    def X(t):
        return x0 + (t - t0).total_seconds() / span * (x1 - x0)

    def Y(v):
        return y1 - (v - lo) / (hi - lo) * (y1 - y0)

    # horizontal grid + feet labels (drawn first; water fill covers them)
    step = 1 if hi - lo <= 9 else 2
    for ft in range(lo, hi + 1, step):
        d.line([(x0, Y(ft)), (x1, Y(ft))], fill=GRID, width=1)
        d.text((x0 - w // 90, Y(ft)), str(ft), font=f_sm, fill=TEXT,
               anchor="rm")
    # top-band labels sit at y0 - h//18, above the reach of high-tide
    # labels (which can climb to ~y0 - h//28 when a high nears the axis
    # ceiling) so the two never overprint
    band_y = y0 - h // 18
    d.text((x0, band_y), "FEET · MLLW", font=f_sm, fill=TEXT, anchor="ls")

    # water: fill under the curve
    curve_xy = [(X(t), Y(v)) for t, v in points]
    d.polygon(curve_xy + [(curve_xy[-1][0], y1), (curve_xy[0][0], y1)],
              fill=WATER)

    # day boundaries with weekday labels
    day = t0.replace(hour=0, minute=0) + timedelta(days=1)
    while day < t1:
        d.line([(X(day), y0), (X(day), y1)], fill=DAYLINE, width=1)
        label = day.strftime("%A").upper()
        # skip the label when the boundary is too close to the right edge
        # for it to fit — better absent than clipped mid-word
        if X(day) + w // 120 + d.textlength(label, font=f_sm) <= x1:
            d.text((X(day) + w // 120, band_y), label,
                   font=f_sm, fill=TEXT, anchor="ls")
        day += timedelta(days=1)

    d.line(curve_xy, fill=CURVE, width=line_w, joint="curve")

    # high/low markers: height + time stacked away from the curve
    r = max(4, h // 160)
    gap = h // 80
    for t, v, kind in events:
        if not (t0 <= t <= t1):
            continue
        cx, cy = X(t), Y(v)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=CURVE)
        ts = t.strftime("%I:%M %p").lstrip("0")
        lx = min(max(cx, x0 + w // 16), x1 - w // 16)
        if kind == "H":
            d.text((lx, cy - r - gap - h // 30), f"{v:.1f} ft",
                   font=f_md, fill=CURVE, anchor="ms")
            d.text((lx, cy - r - gap), ts, font=f_sm, fill=TEXT, anchor="ms")
        else:
            d.text((lx, cy + r + gap + h // 60), f"{v:.1f} ft",
                   font=f_md, fill=CURVE, anchor="ma")
            d.text((lx, cy + r + gap + h // 60 + h // 28), ts,
                   font=f_sm, fill=TEXT, anchor="ma")

    # "now" marker: thin amber line + dot on the curve; interpolate
    # between the bracketing 6-min samples so the dot sits exactly on
    # the drawn polyline even at max tidal slope
    if t0 <= now <= t1:
        xn = X(now)
        d.line([(xn, y0), (xn, y1)], fill=AMBER_DIM, width=1)
        v_now = points[-1][1]
        for (ta, va), (tb, vb) in zip(points, points[1:]):
            if ta <= now <= tb:
                frac = ((now - ta).total_seconds() /
                        max((tb - ta).total_seconds(), 1e-9))
                v_now = va + (vb - va) * frac
                break
        rn = r + max(1, r // 3)
        d.ellipse([xn - rn, Y(v_now) - rn, xn + rn, Y(v_now) + rn],
                  fill=AMBER)
    return img


TIDE_API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"


def station_now(tz_name: str) -> datetime:
    """Wall-clock now in the station's zone (naive, to match CO-OPS
    lst_ldt timestamps). Without this, a machine whose OS timezone
    differs from the station's would silently skew the request window
    and the "now" marker by the offset."""
    if tz_name:
        return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
    return datetime.now()


class Fetcher(threading.Thread):
    """Downloads every slide's image on its own refresh interval and
    swaps decoded frames in atomically. Keeps last good copy on failure.
    Slides with type "tides" are rendered locally from NOAA CO-OPS
    prediction data instead of downloaded."""

    def __init__(self, slides, cache_dir: Path, cfg, screen_size):
        super().__init__(daemon=True)
        self.slides = slides
        self.cache_dir = cache_dir
        self.cfg = cfg
        self.screen_size = screen_size
        self.session = requests.Session()
        self.session.headers["User-Agent"] = cfg["user_agent"]
        self.stop_event = threading.Event()

    def resolve_url(self, slide: Slide) -> str:
        """For latest_in_dir slides, list the directory and return the
        newest .gif (NOAA CDN names are timestamp-sorted)."""
        if not slide.latest_in_dir:
            return slide.url
        import re
        r = self.session.get(slide.url, timeout=30)
        r.raise_for_status()
        gifs = sorted(set(re.findall(r'href="([^"]+\.gif)"', r.text)))
        if not gifs:
            raise RuntimeError("no .gif found in directory listing")
        return slide.url.rstrip("/") + "/" + gifs[-1]

    def tide_predictions(self, slide: Slide, **extra):
        params = {"product": "predictions", "application": "weather-frame",
                  "station": slide.station, "datum": "MLLW",
                  "time_zone": "lst_ldt", "units": "english",
                  "format": "json",
                  "begin_date": (station_now(slide.timezone) -
                                 timedelta(hours=slide.hours_past)
                                 ).strftime("%Y%m%d %H:%M"),
                  "range": str(slide.hours_past + slide.hours_ahead),
                  **extra}
        r = self.session.get(TIDE_API, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()
        if "error" in payload:      # CO-OPS reports errors as HTTP 200
            raise RuntimeError(payload["error"].get("message", "CO-OPS error"))
        return payload["predictions"]

    def render_tides(self, slide: Slide) -> bytes:
        """Fetch CO-OPS predictions and render the chart to PNG bytes."""
        points = [(datetime.strptime(p["t"], "%Y-%m-%d %H:%M"), float(p["v"]))
                  for p in self.tide_predictions(slide)]
        events = [(datetime.strptime(p["t"], "%Y-%m-%d %H:%M"),
                   float(p["v"]), p["type"])
                  for p in self.tide_predictions(slide, interval="hilo")]
        img = render_tide_chart(points, events, station_now(slide.timezone),
                                self.screen_size,
                                self.cfg["background_color"])
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def fetch_sequence(self, slide: Slide) -> bytes:
        """Download numbered frames (NDFD-style loops publish them as
        separate PNGs) and assemble an animated GIF in memory so the
        bytes flow through the normal cache/decode path."""
        imgs = []
        for n in range(slide.frame_start, slide.frame_start + slide.frame_count):
            url = slide.url_template.replace("{n}", str(n))
            try:
                r = self.session.get(url, timeout=60)
                r.raise_for_status()
                imgs.append(Image.open(io.BytesIO(r.content)).convert("RGB"))
            except Exception as e:
                # missing frames happen around forecast-cycle boundaries;
                # keep the loop going with what exists
                log.warning("frame %d failed for %s: %s", n, slide.name, e)
        if not imgs:
            raise RuntimeError("no frames could be fetched")
        ms = int(slide.frame_seconds * 1000)
        durations = [ms] * len(imgs)
        durations[-1] = int(slide.hold_seconds * 1000) or 3 * ms
        buf = io.BytesIO()
        imgs[0].save(buf, "GIF", save_all=True, append_images=imgs[1:],
                     duration=durations, loop=0)
        return buf.getvalue()

    def fetch_one(self, slide: Slide):
        cache_path = self.cache_dir / slide.cache_name
        data, fetched_at = None, time.time()
        try:
            if slide.type == "tides":
                data = self.render_tides(slide)
            elif slide.type == "sequence":
                data = self.fetch_sequence(slide)
            else:
                r = self.session.get(self.resolve_url(slide), timeout=60)
                r.raise_for_status()
                data = r.content
            tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(cache_path)          # atomic swap
            log.info("fetched %s (%d KB)", slide.name, len(data) // 1024)
        except Exception as e:
            log.warning("fetch failed for %s: %s", slide.name, e)
            if cache_path.exists():          # fall back to last good copy
                data = cache_path.read_bytes()
                # caption must reflect the cache's real age, not now —
                # especially for tide charts, whose pixels encode "now"
                fetched_at = cache_path.stat().st_mtime
        if data:
            try:
                frames = decode_to_frames(data, self.screen_size,
                                          self.cfg["background_color"])
                with slide.lock:
                    slide.frames = frames
                    slide.updated_at = fetched_at
            except Exception as e:
                log.warning("decode failed for %s: %s", slide.name, e)

    def run(self):
        # initial load: cached copies first for instant startup, then network
        for s in self.slides:
            cp = self.cache_dir / s.cache_name
            if cp.exists():
                try:
                    frames = decode_to_frames(cp.read_bytes(),
                                              self.screen_size,
                                              self.cfg["background_color"])
                    with s.lock:
                        s.frames, s.updated_at = frames, cp.stat().st_mtime
                except Exception:
                    pass
        next_due = {s.name: 0.0 for s in self.slides}
        while not self.stop_event.is_set():
            now = time.time()
            for s in self.slides:
                if now >= next_due[s.name]:
                    self.fetch_one(s)
                    next_due[s.name] = time.time() + s.refresh_minutes * 60
            self.stop_event.wait(5)


# --------------------------------------------------------------------------
# Quiet hours
# --------------------------------------------------------------------------

def in_quiet_hours(cfg) -> bool:
    q = cfg["quiet_hours"]
    if not q.get("enabled"):
        return False
    now = datetime.now().strftime("%H:%M")
    off, on = q["off"], q["on"]
    if off <= on:                       # e.g. off 01:00 on 06:30
        return off <= now < on
    return now >= off or now < on       # e.g. off 22:00 on 06:30


def set_display_power(on: bool):
    """Best-effort screen power via wlopm (Wayland, Pi OS Bookworm).
    Fails silently if unavailable — the app also blanks to black."""
    try:
        subprocess.run(["wlopm", "--on" if on else "--off", "*"],
                       capture_output=True, timeout=5)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Main display loop
# --------------------------------------------------------------------------

def draw_caption(screen, font, slide: Slide, held: bool = False):
    ts = datetime.fromtimestamp(slide.updated_at).strftime("%I:%M %p").lstrip("0")
    text = f"{slide.name}  ·  updated {ts}"
    if held:
        text += "  ·  held"
    label = font.render(text, True, (200, 200, 200))
    pad = 18
    screen.blit(label, (pad, screen.get_height() - label.get_height() - pad))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path(__file__).parent / "slides.json"))
    ap.add_argument("--windowed", action="store_true",
                    help="1280x800 window for testing instead of fullscreen")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg, slides = load_config(Path(args.config))
    if not slides:
        sys.exit("No enabled slides in config.")

    pygame.init()
    pygame.mouse.set_visible(False)
    if args.windowed:
        screen = pygame.display.set_mode((1280, 800))
    else:
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    size = screen.get_size()
    log.info("display %sx%s, %d slides", *size, len(slides))

    cache_dir = Path(__file__).parent / "cache"
    cache_dir.mkdir(exist_ok=True)
    fetcher = Fetcher(slides, cache_dir, cfg, size)
    fetcher.start()

    font = pygame.font.SysFont("dejavusans", cfg["caption_font_size"])
    clock = pygame.time.Clock()
    bg = tuple(cfg["background_color"])

    idx = 0
    slide_started = time.time()
    frame_i, frame_started = 0, time.time()
    fade_from, fade_started = None, 0.0
    was_quiet = False
    hold = False           # H holds the current slide; P resumes cycling

    def advance(step=1):
        nonlocal idx, slide_started, frame_i, frame_started, fade_from, fade_started
        with slides[idx].lock:
            if slides[idx].frames:
                # copy: set_alpha() during the fade must not mutate the
                # slide's cached surface (it would ghost on the next cycle),
                # and the fetcher may swap frames out mid-transition.
                fade_from = slides[idx].frames[frame_i % len(slides[idx].frames)][0].copy()
                fade_started = time.time()
        for _ in range(len(slides)):
            idx = (idx + step) % len(slides)
            if slides[idx].frames:
                break
        slide_started = time.time()
        frame_i, frame_started = 0, time.time()

    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif ev.key in (pygame.K_RIGHT, pygame.K_SPACE):
                    advance(1)
                elif ev.key == pygame.K_LEFT:
                    advance(-1)
                elif ev.key == pygame.K_h:
                    hold = True
                elif ev.key == pygame.K_p and hold:
                    hold = False
                    slide_started = time.time()   # fresh dwell, no jump

        quiet = in_quiet_hours(cfg)
        if quiet != was_quiet:
            set_display_power(not quiet)
            was_quiet = quiet
        if quiet:
            screen.fill((0, 0, 0))
            pygame.display.flip()
            clock.tick(1)
            continue

        slide = slides[idx]
        with slide.lock:
            frames = slide.frames
        if not frames:                   # nothing loaded yet anywhere?
            screen.fill(bg)
            pygame.display.flip()
            if any(s.frames for s in slides):
                advance(1)
            clock.tick(10)
            continue

        # per-slide dwell time; GIFs animate within it (hold suspends
        # auto-advance but lets the current loop keep playing)
        if not hold and time.time() - slide_started >= cfg["seconds_per_slide"]:
            advance(1)
            slide = slides[idx]
            with slide.lock:
                frames = slide.frames

        # animated frame stepping
        surf, dur = frames[frame_i % len(frames)]
        if dur > 0 and time.time() - frame_started >= dur:
            frame_i = (frame_i + 1) % len(frames)
            frame_started = time.time()
            surf = frames[frame_i][0]

        screen.blit(surf, (0, 0))

        # crossfade over the new slide
        if fade_from is not None:
            t = (time.time() - fade_started) / cfg["crossfade_seconds"]
            if t >= 1.0:
                fade_from = None
            else:
                fade_from.set_alpha(int(255 * (1 - t)))
                screen.blit(fade_from, (0, 0))

        if cfg["show_captions"]:
            draw_caption(screen, font, slide, hold)

        pygame.display.flip()
        clock.tick(30)

    fetcher.stop_event.set()
    pygame.quit()


if __name__ == "__main__":
    main()
