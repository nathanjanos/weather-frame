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
Keys: ESC/Q quit, RIGHT/SPACE next slide, LEFT previous slide
"""

import argparse
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pygame
import requests
from PIL import Image, ImageSequence

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
    url: str
    refresh_minutes: int = 10
    enabled: bool = True
    latest_in_dir: bool = False   # url is a directory listing; fetch newest .gif
    # runtime state
    frames: list = field(default_factory=list)      # [(Surface, duration_s)]
    updated_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def cache_name(self) -> str:
        safe = "".join(c if c.isalnum() else "_" for c in self.name.lower())
        return f"{safe}{Path(self.url).suffix or '.img'}"


def load_config(path: Path):
    with open(path) as f:
        raw = json.load(f)
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k != "slides"}}
    slides = [
        Slide(name=s["name"], url=s["url"],
              refresh_minutes=s.get("refresh_minutes", 10),
              enabled=s.get("enabled", True),
              latest_in_dir=s.get("latest_in_dir", False))
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


class Fetcher(threading.Thread):
    """Downloads every slide's image on its own refresh interval and
    swaps decoded frames in atomically. Keeps last good copy on failure."""

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

    def fetch_one(self, slide: Slide):
        cache_path = self.cache_dir / slide.cache_name
        data = None
        try:
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
        if data:
            try:
                frames = decode_to_frames(data, self.screen_size,
                                          self.cfg["background_color"])
                with slide.lock:
                    slide.frames = frames
                    slide.updated_at = time.time()
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

def draw_caption(screen, font, slide: Slide):
    ts = datetime.fromtimestamp(slide.updated_at).strftime("%I:%M %p").lstrip("0")
    text = f"{slide.name}  ·  updated {ts}"
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

        # per-slide dwell time; GIFs animate within it
        if time.time() - slide_started >= cfg["seconds_per_slide"]:
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
            draw_caption(screen, font, slide)

        pygame.display.flip()
        clock.tick(30)

    fetcher.stop_event.set()
    pygame.quit()


if __name__ == "__main__":
    main()
