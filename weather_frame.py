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
import array
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
    "voice": {
        "enabled": False,
        "wake_word": "hey jarvis",        # phrase in the wake grammar
        "command_seconds": 3.0,           # listen window after the wake word
        "help_seconds": 10.0,             # how long the instructions card shows
        "mic_device": None,               # sounddevice input (None = default)
        "vosk_model_dir": "models/vosk-model-small-en-us-0.15",
    },
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
    observed: bool = True         # tides: overlay observed water level (surge)
    url_template: str = ""        # sequence: url with {n} placeholder
    frame_start: int = 1          # sequence: first frame number
    frame_count: int = 0          # sequence: how many frames
    frame_seconds: float = 0.7    # sequence: seconds per frame
    hold_seconds: float = 0.0     # sequence: pause on last frame (0 = 3x frame)
    keyword: str = ""             # voice: "hey jarvis <keyword>" jumps here
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
        # strip any query string: dots in query params (ERDDAP's
        # &.colorBar=...) would otherwise pollute the suffix
        return f"{safe}{Path(self.url.split('?')[0]).suffix or '.img'}"


def load_config(path: Path):
    with open(path) as f:
        raw = json.load(f)
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k != "slides"}}
    # nested merge so a partial voice block keeps the other defaults
    cfg["voice"] = {**DEFAULTS["voice"], **raw.get("voice", {})}
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
    # voice keywords must be unique single words and not collide with
    # the fixed command vocabulary — a collision would shadow a command
    seen_kw = set()
    for s in raw["slides"]:
        kw = s.get("keyword", "")
        if not kw or not s.get("enabled", True):
            continue
        if " " in kw or kw != kw.lower():
            sys.exit(f"slides.json: keyword {kw!r} must be one lowercase word")
        if kw in VOICE_COMMANDS or kw in VOICE_HELP_WORDS:
            sys.exit(f"slides.json: keyword {kw!r} collides with a voice command")
        if kw in seen_kw:
            sys.exit(f"slides.json: duplicate keyword {kw!r}")
        seen_kw.add(kw)
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
              observed=s.get("observed", True),
              url_template=s.get("url_template", ""),
              frame_start=s.get("frame_start", 1),
              frame_count=s.get("frame_count", 0),
              frame_seconds=s.get("frame_seconds", 0.7),
              hold_seconds=s.get("hold_seconds", 0.0),
              keyword=s.get("keyword", ""))
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


def render_tide_chart(points, events, now, size, bg_color, observed=None):
    """Draw the tide curve, gallery style, directly at screen size.
    points:   [(datetime, feet)] 6-minute predictions, ascending
    events:   [(datetime, feet, "H"|"L")] high/low extremes
    observed: [(datetime, feet)] measured water level (optional) — drawn
              in amber over the prediction; the gap is storm surge
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
    observed = [(t, v) for t, v in (observed or []) if t0 <= t <= t1]
    all_v = [v for _, v in points] + [v for _, v in observed]
    vmin, vmax = min(all_v), max(all_v)
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

    # observed water level over the prediction — divergence is surge
    if len(observed) >= 2:
        d.line([(X(t), Y(v)) for t, v in observed],
               fill=AMBER, width=line_w, joint="curve")

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

    # "now" marker: thin amber line + dot. The dot rides the observed
    # curve when we have a fresh measurement (reality beats forecast);
    # otherwise it interpolates the prediction so it sits exactly on
    # the drawn polyline even at max tidal slope
    if t0 <= now <= t1:
        xn = X(now)
        d.line([(xn, y0), (xn, y1)], fill=AMBER_DIM, width=1)
        xd, v_now = xn, points[-1][1]
        if observed and (now - observed[-1][0]) <= timedelta(minutes=45):
            xd, v_now = X(observed[-1][0]), observed[-1][1]
        else:
            for (ta, va), (tb, vb) in zip(points, points[1:]):
                if ta <= now <= tb:
                    frac = ((now - ta).total_seconds() /
                            max((tb - ta).total_seconds(), 1e-9))
                    v_now = va + (vb - va) * frac
                    break
        rn = r + max(1, r // 3)
        d.ellipse([xd - rn, Y(v_now) - rn, xd + rn, Y(v_now) + rn],
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

    def coops_json(self, slide: Slide, **params):
        base = {"application": "weather-frame", "station": slide.station,
                "datum": "MLLW", "time_zone": "lst_ldt",
                "units": "english", "format": "json"}
        r = self.session.get(TIDE_API, params={**base, **params}, timeout=30)
        r.raise_for_status()
        payload = r.json()
        if "error" in payload:      # CO-OPS reports errors as HTTP 200
            raise RuntimeError(payload["error"].get("message", "CO-OPS error"))
        return payload

    def tide_predictions(self, slide: Slide, **extra):
        begin = (station_now(slide.timezone) -
                 timedelta(hours=slide.hours_past)).strftime("%Y%m%d %H:%M")
        return self.coops_json(slide, product="predictions",
                               begin_date=begin,
                               range=str(slide.hours_past + slide.hours_ahead),
                               **extra)["predictions"]

    def render_tides(self, slide: Slide) -> bytes:
        """Fetch CO-OPS predictions (plus observed water level — the gap
        between the curves is storm surge) and render the chart to PNG."""
        ts = "%Y-%m-%d %H:%M"
        points = [(datetime.strptime(p["t"], ts), float(p["v"]))
                  for p in self.tide_predictions(slide)]
        events = [(datetime.strptime(p["t"], ts), float(p["v"]), p["type"])
                  for p in self.tide_predictions(slide, interval="hilo")]
        observed = []
        if slide.observed:
            try:        # non-fatal: chart falls back to predictions-only
                data = self.coops_json(slide, product="water_level",
                                       date="recent")["data"]
                observed = [(datetime.strptime(p["t"], ts), float(p["v"]))
                            for p in data if p.get("v")]
            except Exception as e:
                log.warning("observed water level unavailable for %s: %s",
                            slide.name, e)
        img = render_tide_chart(points, events, station_now(slide.timezone),
                                self.screen_size,
                                self.cfg["background_color"], observed)
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
# Voice control (optional)
# --------------------------------------------------------------------------

VOSK_MODEL_URL = ("https://alphacephei.com/vosk/models/"
                  "vosk-model-small-en-us-0.15.zip")

VOICE_COMMANDS = {
    "next": pygame.K_RIGHT, "forward": pygame.K_RIGHT,
    "back": pygame.K_LEFT, "previous": pygame.K_LEFT,
    "hold": pygame.K_h, "pause": pygame.K_h,
    "play": pygame.K_p, "resume": pygame.K_p,
}

VOICE_EVENT = pygame.event.custom_type()   # mic indicator on/off
VOICE_JUMP = pygame.event.custom_type()    # jump to slide (attr: index)
VOICE_HELP = pygame.event.custom_type()    # show the instructions card

VOICE_HELP_WORDS = ("instructions", "help")


class VoiceControl(threading.Thread):
    """Always-on voice control, fully on-device via Vosk: a recognizer
    constrained to the grammar ["hey jarvis", "[unk]"] listens
    continuously; when the wake phrase appears, a second recognizer
    decodes a few seconds against the tiny VOICE_COMMANDS grammar and
    posts the matching KEYDOWN. (openWakeWord was tried first but its
    embedding pipeline silently returns zero scores on numpy>=2, so one
    Vosk engine does both jobs.)

    Optional feature in the wlopm spirit: if the voice deps, model, or
    a microphone are missing it logs why and stays off — the slideshow
    is never affected."""

    def __init__(self, vcfg, app_dir: Path, keywords=None):
        super().__init__(daemon=True)
        self.cfg = vcfg
        self.app_dir = app_dir
        self.keywords = keywords or {}   # spoken word -> slide index
        self.stop_event = threading.Event()
        # capture rate is set from the mic's native rate in run();
        # tests inject audio directly into loop() at 16 kHz
        self.rate = 16000
        self.chunk = int(self.rate * 0.2)

    def ensure_vosk_model(self) -> Path:
        """Download + unzip the small Vosk model on first run (~40 MB)."""
        target = self.app_dir / self.cfg["vosk_model_dir"]
        if target.exists():
            return target
        import zipfile
        log.info("downloading vosk model (one-time, ~40 MB) ...")
        target.parent.mkdir(parents=True, exist_ok=True)
        zpath = target.parent / "vosk-model.zip"
        with requests.get(VOSK_MODEL_URL, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(zpath, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(target.parent)
        zpath.unlink()
        return target

    def loop(self, read_chunk):
        """Core recognition loop. read_chunk() returns 16-bit mono PCM
        bytes (or None when the source ends — lets tests feed WAV data
        instead of a microphone)."""
        from vosk import KaldiRecognizer
        wake_grammar = json.dumps([self.cfg["wake_word"], "[unk]"])
        cmd_grammar = json.dumps(list(VOICE_COMMANDS) + list(self.keywords) +
                                 list(VOICE_HELP_WORDS) + ["[unk]"])
        wake_rec = KaldiRecognizer(self.vosk, self.rate, wake_grammar)
        while not self.stop_event.is_set():
            data = read_chunk()
            if data is None:
                break
            if wake_rec.AcceptWaveform(data):
                heard = json.loads(wake_rec.Result()).get("text", "")
            else:
                heard = json.loads(wake_rec.PartialResult()).get("partial", "")
            if self.cfg["wake_word"] not in heard:
                continue
            log.info("wake word heard, listening for a command ...")
            pygame.event.post(pygame.event.Event(VOICE_EVENT, listening=True))
            cmd_rec = KaldiRecognizer(self.vosk, self.rate, cmd_grammar)
            deadline = time.time() + self.cfg["command_seconds"]
            while time.time() < deadline and not self.stop_event.is_set():
                data = read_chunk()
                if data is None or cmd_rec.AcceptWaveform(data):
                    break                       # source ended / utterance end
            text = json.loads(cmd_rec.FinalResult()).get("text", "")
            pygame.event.post(pygame.event.Event(VOICE_EVENT, listening=False))
            words = [w for w in text.split()
                     if w in VOICE_COMMANDS or w in self.keywords
                     or w in VOICE_HELP_WORDS]
            if not words:
                log.info("no command recognized (heard %r)", text)
            elif words[-1] in VOICE_HELP_WORDS:
                log.info("voice: showing instructions")
                pygame.event.post(pygame.event.Event(VOICE_HELP))
            elif words[-1] in VOICE_COMMANDS:
                log.info("voice command: %s", words[-1])
                pygame.event.post(pygame.event.Event(
                    pygame.KEYDOWN, key=VOICE_COMMANDS[words[-1]]))
            else:
                log.info("voice jump: %s -> slide %d", words[-1],
                         self.keywords[words[-1]])
                pygame.event.post(pygame.event.Event(
                    VOICE_JUMP, index=self.keywords[words[-1]]))
            wake_rec = KaldiRecognizer(self.vosk, self.rate, wake_grammar)

    SILENT_REOPEN_S = 45   # dead-zero capture for this long = zombie stream

    def make_reader(self, stream):
        """Wrap stream reads with a silence watchdog. A mic opened while
        the audio stack is still settling (boot, or a fast respawn ~5 s
        after the previous instance died) can open successfully but
        capture only zeros forever — a real room never yields sustained
        digital silence, so treat it as a dead stream and force a
        reopen via the caller's retry path."""
        quiet_since = [None]
        heard = [False]

        def read():
            data = bytes(stream.read(self.chunk)[0])
            peak = max(abs(s) for s in array.array("h", data))
            if peak < 3:
                if quiet_since[0] is None:
                    quiet_since[0] = time.time()
                elif time.time() - quiet_since[0] > self.SILENT_REOPEN_S:
                    raise RuntimeError(
                        f"mic captured only silence for "
                        f"{self.SILENT_REOPEN_S}s (zombie stream)")
            else:
                quiet_since[0] = None
                if not heard[0]:
                    heard[0] = True
                    log.info("voice: hearing audio (peak %d)", peak)
            return data

        return read

    def open_mic(self, sd):
        """Open the mic at its NATIVE rate — many USB mics only do
        44.1/48 kHz and ALSA won't resample a raw stream (PaError -9997
        "Invalid sample rate"); Vosk downsamples internally as long as
        the recognizer is told the true rate."""
        dev = sd.query_devices(self.cfg["mic_device"], "input")
        self.rate = int(dev.get("default_samplerate") or 16000)
        self.chunk = int(self.rate * 0.2)
        # NOTE: on macOS the first mic access blocks on the OS
        # permission dialog — grant it once and this proceeds
        log.info("voice: opening %r at %d Hz (first run on macOS pops "
                 "a permission dialog) ...", dev.get("name", "mic"),
                 self.rate)
        stream = sd.RawInputStream(samplerate=self.rate, channels=1,
                                   dtype="int16", blocksize=self.chunk,
                                   device=self.cfg["mic_device"])
        stream.start()
        return stream

    def run(self):
        try:
            import sounddevice as sd
            from vosk import Model as VoskModel, SetLogLevel
        except ImportError as e:
            log.info("voice control off (pip install sounddevice vosk): %s", e)
            return
        try:
            SetLogLevel(-1)
            self.vosk = VoskModel(str(self.ensure_vosk_model()))
        except Exception as e:
            log.warning("voice control off: %s", e)
            return
        # Keep trying forever: at boot the app can start before USB
        # audio / PipeWire are up (autostart races them), and a mic can
        # be unplugged and replugged — voice should come back on its
        # own in all cases, appliance-style.
        while not self.stop_event.is_set():
            try:
                stream = self.open_mic(sd)
            except Exception as e:
                log.warning("voice: mic unavailable (%s) — retrying in 30 s", e)
                try:      # rescan devices; PortAudio caches enumeration,
                          # so a late-arriving USB mic is invisible without this
                    sd._terminate()
                    sd._initialize()
                except Exception:
                    pass
                if self.stop_event.wait(30):
                    return
                continue
            log.info("voice control on — say %r then one of: %s",
                     self.cfg["wake_word"],
                     ", ".join(sorted(set(VOICE_COMMANDS))))
            try:
                self.loop(self.make_reader(stream))
            except Exception as e:
                log.warning("voice: audio stream failed (%s) — reopening "
                            "in 10 s", e)
                try:      # full device rescan before the reopen: the
                          # zombie-capture case needs fresh enumeration
                    stream.stop()
                    stream.close()
                    sd._terminate()
                    sd._initialize()
                except Exception:
                    pass
                self.stop_event.wait(10)
            finally:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass


def mic_test(cfg):
    """Interactive mic diagnostic (--mic-test): prints the device list,
    then 20 seconds of live level meter + free-vocabulary recognition,
    and ends with a plain-language verdict. Run `frame.sh stop` first if
    the app is running (it may hold the mic)."""
    try:
        import sounddevice as sd
        from vosk import KaldiRecognizer, Model as VoskModel, SetLogLevel
    except ImportError as e:
        print(f"voice deps missing ({e.name}): pip install sounddevice vosk")
        return
    import array
    print("== audio devices (> marks default input) ==")
    print(sd.query_devices())
    print(f'\n== configured mic_device: {cfg["voice"]["mic_device"]!r} '
          "(null = default) ==")
    vc = VoiceControl(cfg["voice"], Path(__file__).parent)
    try:
        stream = vc.open_mic(sd)
    except Exception as e:
        print(f"\nVERDICT: cannot open the mic: {e}")
        print("  - is the USB mic plugged in and listed above?")
        print('  - if the wrong device is default, set "mic_device" in '
              "slides.json to the right name substring or index")
        print("  - if the app is running it may hold the mic: frame.sh stop")
        return
    SetLogLevel(-1)
    print(f"\nlistening for 20 s at {vc.rate} Hz — TALK NOW "
          "(any words; try 'hey jarvis next'):")
    vosk = VoskModel(str(vc.ensure_vosk_model()))
    rec = KaldiRecognizer(vosk, vc.rate)      # no grammar: any word counts
    peak, heard = 0, set()
    t_end = time.time() + 20
    while time.time() < t_end:
        data = bytes(stream.read(vc.chunk)[0])
        samples = array.array("h", data)
        level = max(abs(s) for s in samples)
        peak = max(peak, level)
        if rec.AcceptWaveform(data):
            words = json.loads(rec.Result()).get("text", "")
            if words:
                heard.update(words.split())
        partial = json.loads(rec.PartialResult()).get("partial", "")
        bar = "#" * min(40, level // 800)
        print(f"\r  level {level:5d} |{bar:<40s}| {partial[:30]:<30s}",
              end="", flush=True)
    heard.update(json.loads(rec.FinalResult()).get("text", "").split())
    stream.stop(); stream.close()
    print(f"\n\npeak level: {peak}   words recognized: "
          f"{' '.join(sorted(heard)) or '(none)'}")
    if peak < 300:
        print("VERDICT: mic opens but captures silence — raise the capture "
              "gain:\n  alsamixer -> F6 pick the USB device -> F5 -> find "
              "the Capture/Mic bar -> arrow-up to ~80% -> Esc\n  then: "
              "sudo alsactl store   (persists the gain across reboots)")
    elif not heard:
        print("VERDICT: audio arrives but no speech recognized — speak "
              "louder/closer, or the mic is picking a noisy channel")
    else:
        print("VERDICT: mic and recognition work. If the app still doesn't "
              "respond to 'hey jarvis', make sure it was restarted since "
              "the last git pull (frame.sh stop && frame.sh start), then "
              "check: grep voice /tmp/weather-frame.log")


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

def draw_help(screen, slides, title_font, body_font):
    """The 'hey jarvis, instructions' card: a dark veil over the current
    slide with the command vocabulary and every slide keyword, in the
    gallery typographic style (light text, amber accents)."""
    w, h = screen.get_size()
    AMBER, TEXT, DIM = (216, 174, 90), (206, 210, 216), (128, 134, 142)
    veil = pygame.Surface((w, h))
    veil.set_alpha(235)
    veil.fill((0, 0, 0))
    screen.blit(veil, (0, 0))

    x0, y = w // 14, h // 16
    title = title_font.render('SAY  "HEY JARVIS"  THEN …', True, AMBER)
    screen.blit(title, (x0, y))
    y += int(title.get_height() * 1.8)
    for line in ("next / forward        →  next slide",
                 "back / previous       →  previous slide",
                 "hold / pause          →  stay on this slide",
                 "play / resume         →  resume cycling",
                 "instructions / help   →  this screen",
                 "… or a slide name:"):
        screen.blit(body_font.render(line, True, TEXT), (x0, y))
        y += int(body_font.get_height() * 1.4)
    y += body_font.get_height() // 2

    tagged = [s for s in slides if s.keyword]
    rows = (len(tagged) + 1) // 2
    col_w = (w - 2 * x0) // 2
    kw_w = max(body_font.size(s.keyword)[0] for s in tagged) + w // 48
    row_h = int(body_font.get_height() * 1.32)
    for i, s in enumerate(tagged):
        cx = x0 + (i // rows) * col_w
        cy = y + (i % rows) * row_h
        screen.blit(body_font.render(s.keyword, True, AMBER), (cx, cy))
        name = s.name
        while body_font.size(name)[0] > col_w - kw_w - w // 40 and len(name) > 4:
            name = name[:-2]
        screen.blit(body_font.render(name, True, DIM), (cx + kw_w, cy))


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
    ap.add_argument("--mic-test", action="store_true",
                    help="diagnose the microphone + speech path, then exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg, slides = load_config(Path(args.config))
    if args.mic_test:
        mic_test(cfg)
        return
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

    voice = None
    if cfg["voice"]["enabled"]:
        keywords = {s.keyword: i for i, s in enumerate(slides) if s.keyword}
        voice = VoiceControl(cfg["voice"], Path(__file__).parent, keywords)
        voice.start()

    font = pygame.font.SysFont("dejavusans", cfg["caption_font_size"])
    help_title_font = pygame.font.SysFont("dejavusans", max(20, size[1] // 32))
    help_body_font = pygame.font.SysFont("dejavusans", max(13, size[1] // 50))
    clock = pygame.time.Clock()
    bg = tuple(cfg["background_color"])

    idx = 0
    slide_started = time.time()
    frame_i, frame_started = 0, time.time()
    fade_from, fade_started = None, 0.0
    was_quiet = False
    hold = False           # H holds the current slide; P resumes cycling
    voice_listening = False
    help_until = 0.0       # instructions card visible until this time

    def goto(target):
        nonlocal idx, slide_started, frame_i, frame_started, fade_from, fade_started
        with slides[idx].lock:
            if slides[idx].frames:
                # copy: set_alpha() during the fade must not mutate the
                # slide's cached surface (it would ghost on the next cycle),
                # and the fetcher may swap frames out mid-transition.
                fade_from = slides[idx].frames[frame_i % len(slides[idx].frames)][0].copy()
                fade_started = time.time()
        idx = target % len(slides)
        slide_started = time.time()
        frame_i, frame_started = 0, time.time()

    def advance(step=1):
        target = idx
        for _ in range(len(slides)):
            target = (target + step) % len(slides)
            if slides[target].frames:
                break
        goto(target)

    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == VOICE_EVENT:
                voice_listening = ev.listening
            elif ev.type == VOICE_JUMP:
                goto(ev.index)
            elif ev.type == VOICE_HELP:
                help_until = time.time() + cfg["voice"]["help_seconds"]
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
        # auto-advance but lets the current loop keep playing; the
        # instructions card also freezes the dwell so it can be read)
        help_showing = time.time() < help_until
        if help_showing:
            slide_started = time.time()   # fresh dwell after the card
        elif not hold and time.time() - slide_started >= cfg["seconds_per_slide"]:
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

        if help_showing:
            draw_help(screen, slides, help_title_font, help_body_font)

        if voice_listening:    # amber dot: wake word heard, capturing
            pygame.draw.circle(screen, (216, 174, 90),
                               (screen.get_width() - 34,
                                screen.get_height() - 34), 9)

        pygame.display.flip()
        clock.tick(30)

    fetcher.stop_event.set()
    if voice:
        voice.stop_event.set()
    pygame.quit()


if __name__ == "__main__":
    main()
