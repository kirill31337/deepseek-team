#!/usr/bin/env python3
"""Render the repository workflow illustration.

This script has one optional dependency: Pillow. It writes only
``assets/deepseek-team-demo.gif`` and
``assets/deepseek-team-demo-poster.png`` relative to the repository root.

Run it from any directory with, for example:

    python3 -m venv /tmp/deepseek-team-render
    /tmp/deepseek-team-render/bin/pip install Pillow
    /tmp/deepseek-team-render/bin/python scripts/render_repository_demo.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH, HEIGHT = 1000, 560
FRAME_MS = 400
HOLD_FRAMES = 20
TRANSITION_FRAMES = 3
FRAMES_PER_STAGE = HOLD_FRAMES + TRANSITION_FRAMES
STAGE_COUNT = 4

ROOT = Path(__file__).resolve().parents[1]
GIF_PATH = ROOT / "assets" / "deepseek-team-demo.gif"
POSTER_PATH = ROOT / "assets" / "deepseek-team-demo-poster.png"

BG = "#070b12"
PANEL = "#0d1520"
PANEL_2 = "#101b29"
INK = "#f3f7fb"
MUTED = "#93a4b7"
FAINT = "#506275"
CYAN = "#2ce5e8"
CYAN_SOFT = "#0d8f9e"
PURPLE = "#8e78ff"
PURPLE_SOFT = "#473f8f"
GREEN = "#62e6a8"
BORDER = "#25384a"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        if bold
        else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    names += [
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"
    ]
    for name in names:
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    raise RuntimeError("No supported sans-serif font found")


F12 = font(12, True)
F15 = font(15)
F16 = font(16, True)
F18 = font(18)
F20 = font(20, True)
F24 = font(24, True)
F28 = font(28, True)
F34 = font(34, True)


def rounded(draw: ImageDraw.ImageDraw, box, radius=18, fill=PANEL, outline=BORDER, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def pill(draw: ImageDraw.ImageDraw, xy, text: str, color=CYAN, fill="#0b2027"):
    x, y = xy
    bbox = draw.textbbox((0, 0), text, font=F12)
    w = bbox[2] - bbox[0] + 22
    h = 27
    draw.rounded_rectangle((x, y, x + w, y + h), radius=13, fill=fill, outline=color, width=1)
    draw.text((x + 11, y + 6), text, font=F12, fill=color)
    return w


def wrap(draw: ImageDraw.ImageDraw, text: str, face, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=face) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def arrow(draw: ImageDraw.ImageDraw, start, end, color=CYAN_SOFT, width=3, progress=1.0):
    x1, y1 = start
    x2, y2 = end
    xe = x1 + (x2 - x1) * progress
    ye = y1 + (y2 - y1) * progress
    draw.line((x1, y1, xe, ye), fill=color, width=width)
    if progress > 0.92:
        angle = math.atan2(y2 - y1, x2 - x1)
        for offset in (2.55, -2.55):
            draw.line(
                (x2, y2, x2 + 11 * math.cos(angle + offset), y2 + 11 * math.sin(angle + offset)),
                fill=color,
                width=width,
            )


def base(stage: int, title: str, subtitle: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    # Quiet technical grid and glows, kept subtle for GIF compression.
    for x in range(20, WIDTH, 40):
        draw.line((x, 0, x, HEIGHT), fill="#09111b", width=1)
    for y in range(20, HEIGHT, 40):
        draw.line((0, y, WIDTH, y), fill="#09111b", width=1)
    draw.ellipse((755, -175, 1135, 205), fill="#09131f")
    draw.ellipse((-160, 410, 190, 760), fill="#09151c")

    pill(draw, (44, 34), "WORKFLOW ILLUSTRATION")
    draw.text((44, 79), title, font=F34, fill=INK)
    draw.text((44, 122), subtitle, font=F18, fill=MUTED)

    labels = ["REQUEST", "DELEGATE", "ISOLATE", "REVIEW"]
    x0, gap = 52, 224
    y = 522
    for index, label in enumerate(labels):
        x = x0 + gap * index
        active = index == stage
        draw.ellipse((x, y - 1, x + 12, y + 11), fill=CYAN if active else BG, outline=CYAN if active else FAINT, width=2)
        draw.text((x + 20, y - 3), f"0{index + 1}  {label}", font=F12, fill=INK if active else FAINT)
    return image, draw


def request_stage() -> Image.Image:
    image, draw = base(0, "Start with a coding request", "The coordinator keeps the goal and constraints in view.")
    rounded(draw, (110, 190, 890, 432), radius=24, fill=PANEL, outline=CYAN_SOFT, width=2)
    draw.ellipse((145, 230, 203, 288), fill="#102936", outline=CYAN, width=2)
    draw.ellipse((163, 244, 185, 266), fill=CYAN)
    draw.arc((156, 258, 192, 287), 195, 345, fill=CYAN, width=3)
    draw.text((230, 224), "USER REQUEST", font=F16, fill=CYAN)
    request = "Add a focused feature, verify it, and keep the change easy to review."
    for i, line in enumerate(wrap(draw, request, F28, 620)):
        draw.text((230, 260 + i * 38), line, font=F28, fill=INK)
    pill(draw, (230, 360), "GOAL")
    pill(draw, (320, 360), "SCOPE")
    pill(draw, (420, 360), "CHECKS")
    return image


def coordinator_stage() -> Image.Image:
    image, draw = base(1, "Coordinator maps the work", "Codex or Claude Code assigns bounded, reviewable pieces.")
    rounded(draw, (62, 196, 344, 434), radius=22, fill=PANEL, outline=PURPLE_SOFT, width=2)
    draw.text((91, 225), "CODEX / CLAUDE CODE", font=F16, fill=PURPLE)
    draw.text((91, 259), "Coordinator", font=F28, fill=INK)
    draw.line((91, 306, 315, 306), fill=BORDER, width=2)
    for y, label in [(329, "Keeps architecture"), (363, "Sets acceptance checks"), (397, "Owns final integration")]:
        draw.ellipse((93, y + 4, 101, y + 12), fill=PURPLE)
        draw.text((113, y), label, font=F15, fill=MUTED)

    tasks = [("01", "Implementation"), ("02", "Focused tests"), ("03", "Documentation")]
    for i, (number, label) in enumerate(tasks):
        y = 204 + i * 78
        rounded(draw, (570, y, 913, y + 60), radius=14, fill=PANEL_2, outline=BORDER, width=2)
        draw.text((590, y + 18), number, font=F16, fill=CYAN)
        draw.text((635, y + 17), label, font=F18, fill=INK)
        arrow(draw, (344, 315), (570, y + 30), color=CYAN_SOFT, width=2)
    pill(draw, (570, 414), "BOUNDED ASSIGNMENTS")
    return image


def worker_stage() -> Image.Image:
    image, draw = base(2, "DeepSeek Flash works in parallel", "Each assignment runs independently in an isolated development copy.")
    workers = [
        ("01", "Implement", "Focused code change", CYAN),
        ("02", "Verify", "Declared local checks", PURPLE),
        ("03", "Document", "Clear usage notes", GREEN),
    ]
    for i, (number, role, detail, accent) in enumerate(workers):
        x = 47 + i * 316
        rounded(draw, (x, 207, x + 274, 426), radius=20, fill=PANEL, outline=accent, width=2)
        draw.text((x + 22, 230), number, font=F16, fill=accent)
        draw.text((x + 62, 229), "DEEPSEEK FLASH", font=F16, fill=INK)
        draw.line((x + 22, 267, x + 252, 267), fill=BORDER, width=2)
        draw.text((x + 22, 292), role, font=F24, fill=INK)
        draw.text((x + 22, 329), detail, font=F15, fill=MUTED)
        pill(draw, (x + 22, 371), "ISOLATED COPY", color=accent, fill="#0a171e")
    pill(draw, (402, 454), "FULL-ACCESS EXAMPLE", color=PURPLE, fill="#17142b")
    return image


def review_stage() -> Image.Image:
    image, draw = base(3, "Coordinator reviews before integration", "Accepted changes return to one controlled result.")
    rounded(draw, (54, 202, 435, 430), radius=22, fill=PANEL, outline=PURPLE_SOFT, width=2)
    draw.text((82, 229), "COORDINATOR REVIEW", font=F16, fill=PURPLE)
    checks = ["Inspect the actual diff", "Run final verification", "Accept or request rework"]
    for i, label in enumerate(checks):
        y = 281 + i * 47
        draw.ellipse((83, y, 105, y + 22), fill="#0d2b2a", outline=GREEN, width=2)
        draw.line((89, y + 11, 94, y + 16), fill=GREEN, width=2)
        draw.line((94, y + 16, 101, y + 7), fill=GREEN, width=2)
        draw.text((122, y - 1), label, font=F18, fill=INK)

    arrow(draw, (435, 316), (551, 316), color=CYAN, width=3)
    rounded(draw, (551, 225, 943, 407), radius=22, fill="#0b2027", outline=CYAN, width=2)
    draw.text((582, 253), "REVIEWED RESULT", font=F16, fill=CYAN)
    draw.text((582, 293), "Ready to integrate", font=F28, fill=INK)
    draw.text((582, 342), "The coordinator owns the final decision.", font=F15, fill=MUTED)
    pill(draw, (582, 374), "ACCEPTED", color=GREEN, fill="#0c241f")
    return image


def ease(value: float) -> float:
    return value * value * (3 - 2 * value)


def render_frames() -> list[Image.Image]:
    stages = [request_stage(), coordinator_stage(), worker_stage(), review_stage()]
    frames: list[Image.Image] = []
    # Start with a crisp request frame, then hold each scene before moving forward.
    for index, current in enumerate(stages):
        following = stages[(index + 1) % len(stages)]
        for frame_index in range(FRAMES_PER_STAGE):
            if frame_index >= HOLD_FRAMES:
                transition_frame = frame_index - HOLD_FRAMES + 1
                alpha = ease(transition_frame / TRANSITION_FRAMES)
                frame = Image.blend(current, following, alpha)
            else:
                frame = current.copy()
            frames.append(frame)
    return frames


def save_gif(frames: list[Image.Image]) -> None:
    # A shared compact palette keeps solid brand colors clean and the file small.
    strip = Image.new("RGB", (WIDTH, HEIGHT * STAGE_COUNT))
    for i, stage in enumerate((request_stage(), coordinator_stage(), worker_stage(), review_stage())):
        strip.paste(stage, (0, i * HEIGHT))
    palette = strip.quantize(colors=48, method=Image.Quantize.MEDIANCUT)
    paletted = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
    paletted[0].save(
        GIF_PATH,
        save_all=True,
        append_images=paletted[1:],
        duration=FRAME_MS,
        loop=0,
        optimize=True,
        disposal=2,
    )


def main() -> None:
    GIF_PATH.parent.mkdir(parents=True, exist_ok=True)
    frames = render_frames()
    save_gif(frames)
    review_stage().save(POSTER_PATH, optimize=True)
    print(f"Rendered {len(frames)} frames at {WIDTH}x{HEIGHT} ({len(frames) * FRAME_MS / 1000:.1f}s)")
    print(f"GIF: {GIF_PATH} ({GIF_PATH.stat().st_size:,} bytes)")
    print(f"Poster: {POSTER_PATH} ({POSTER_PATH.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
