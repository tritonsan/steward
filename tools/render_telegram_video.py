"""Animate the scripted Telegram opener from masked message layers with FFmpeg."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shutil
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/video/telegram-animated-v1"
LAYERS = OUT / "layers"
FPS, DURATION = 30, 25
ARRIVALS = [1.0, 4.3, 6.2, 8.8, 10.1, 12.5, 16.1, 20.8]
NAMES = ["Maya", "Daniel", "Nora", "Liam", "Olivia", "Maya", "Maya", "Steward"]
BOUNDS = [(498, 639), (655, 757), (774, 914), (929, 1032),
          (1048, 1152), (1168, 1306), (1321, 1427), (1447, 1574)]
AVATARS = [(13, 557, 98, 644), (13, 676, 98, 761),
           (13, 814, 98, 901), (13, 943, 98, 1029),
           (13, 1067, 98, 1153), (13, 1219, 98, 1305),
           (13, 1341, 98, 1427), (13, 1487, 99, 1576)]
MESSAGES = [
    "Visitor parking is full again. Some cars stay for days.",
    "Could we set a 24-hour limit?",
    "I'd prefer 72 hours for weekend guests.",
    "Has anyone seen a blue parcel?",
    "Pool keys are at reception.",
    "Can we discuss the parking options at a residents' meeting?",
    "Is anyone following up on this?",
    "I'm here. Let's turn this into a plan.",
]


def ease(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return 1.0 - (1.0 - value) ** 3


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=255)
    return mask.filter(ImageFilter.GaussianBlur(0.45))


class Renderer:
    def __init__(self) -> None:
        LAYERS.mkdir(parents=True, exist_ok=True)
        self.background = Image.open(LAYERS / "background.png").convert("RGB")
        self.size = self.background.size
        source = Image.open(ROOT / "artifacts/video/telegram-opening-v1/frame-06.png").convert("RGBA")
        assert source.size == self.size == (841, 1870)
        rgb = np.array(source.convert("RGB")).astype(int)
        self.sprites = []
        for index, ((top, bottom), avatar) in enumerate(zip(BOUNDS, AVATARS)):
            mask = Image.new("L", self.size)
            draw = ImageDraw.Draw(mask)
            for y in range(top, bottom):
                row = rgb[y, 102:790]
                white = (row.min(1) > 226) & ((row.max(1) - row.min(1)) < 24)
                xs = np.flatnonzero(white) + 102
                if len(xs) > 20:
                    draw.line((int(xs[0]) - 1, y, int(xs[-1]) + 1, y), fill=255)
            draw.ellipse(avatar, fill=255)
            mask = mask.filter(ImageFilter.GaussianBlur(0.45))
            layer = source.copy()
            layer.putalpha(mask)
            sprite = layer.crop((0, top - 2, 755, max(bottom + 8, avatar[3] + 3)))
            sprite.save(LAYERS / f"message-{index + 1:02d}.png")
            self.sprites.append(sprite)

        self.date = source.crop((309, 429, 535, 480))
        self.date.putalpha(rounded_mask(self.date.size, 26))
        self.date.save(LAYERS / "date-chip.png")
        self.bot_avatar = source.crop((13, 1487, 100, 1577))
        avatar_mask = Image.new("L", self.bot_avatar.size)
        ImageDraw.Draw(avatar_mask).ellipse((0, 0, 86, 89), fill=255)
        self.bot_avatar.putalpha(avatar_mask.filter(ImageFilter.GaussianBlur(0.45)))
        self.endcard = self.make_endcard()

    def make_endcard(self) -> Image.Image:
        endcard = Image.new("RGB", self.size, "white")
        logo = Image.open(ROOT / "web/public/steward-logo.png").convert("RGB")
        logo.thumbnail((760, 760), Image.Resampling.LANCZOS)
        endcard.paste(logo, ((self.size[0] - logo.width) // 2, 440))
        font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 32)
        draw = ImageDraw.Draw(endcard)
        for y, text in [(1240, "A little less to manage."), (1290, "A community that remembers.")]:
            draw.text((self.size[0] // 2, y), text, font=font, fill="#0a3445", anchor="mt")
        return endcard

    def paste(self, canvas: Image.Image, sprite: Image.Image, x: int, y: int, opacity: float = 1.0) -> None:
        if opacity < 0.999:
            sprite = sprite.copy()
            sprite.putalpha(sprite.getchannel("A").point(lambda alpha: round(alpha * opacity)))
        canvas.paste(sprite, (x, y), sprite)

    def typing(self, t: float) -> Image.Image:
        layer = Image.new("RGBA", (400, 100))
        self.paste(layer, self.bot_avatar, 13, 9)
        draw = ImageDraw.Draw(layer)
        draw.rounded_rectangle((114, 5, 377, 93), 28, fill="white")
        font = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 23)
        draw.text((135, 12), "Steward", font=font, fill="#062c40")
        for i in range(3):
            wave = (math.sin((t - 19.45) * 7.0 - i * 0.8) + 1.0) / 2.0
            y = 65 - round(5 * wave)
            color = (round(140 - wave * 35), round(160 - wave * 25), round(148 - wave * 20))
            draw.ellipse((145 + i * 27, y - 6, 157 + i * 27, y + 6), fill=color)
        return layer

    def frame(self, t: float) -> Image.Image:
        canvas = self.background.copy()
        progress = [ease((t - arrival) / 0.43) for arrival in ARRIVALS]
        typing_progress = ease((t - 19.45) / 0.43)
        gap = 14
        allocations = [(sprite.height + gap) * p for sprite, p in zip(self.sprites, progress)]
        typing_space = 114 * typing_progress * (1 - progress[-1])
        stack_top = 1634 - sum(allocations) - typing_space
        if progress[0] > 0:
            self.paste(canvas, self.date, 309, round(stack_top - 65), progress[0])
        cursor = stack_top
        for index, (sprite, p, height) in enumerate(zip(self.sprites, progress, allocations)):
            if index == 7 and typing_space > 0:
                self.paste(canvas, self.typing(t), 0, round(cursor + 12 * (1 - typing_progress)), typing_progress * (1 - p))
                cursor += typing_space
            if p > 0:
                self.paste(canvas, sprite, 0, round(cursor + (1 - p) * 24), p)
            cursor += height
        # Chat is clipped behind the fixed composer and header, as in a real viewport.
        canvas.paste(self.background.crop((0, 1638, 841, 1870)), (0, 1638))
        canvas.paste(self.background.crop((0, 0, 841, 270)), (0, 0))
        end_progress = ease((t - 24.05) / 0.45)
        if end_progress:
            canvas = Image.blend(canvas, self.endcard, end_progress)
        return canvas

    def previews(self) -> None:
        directory = OUT / "previews"
        directory.mkdir(exist_ok=True)
        times = [0.5, 1.3, 5.0, 10.7, 16.8, 20.1, 21.5, 24.7]
        sheet = Image.new("RGB", (4 * 252, 2 * 594), "#0a3445")
        for i, time in enumerate(times):
            frame = self.frame(time)
            frame.save(directory / f"frame-{time:04.1f}s.png")
            frame.thumbnail((252, 560), Image.Resampling.LANCZOS)
            x, y = (i % 4) * 252, (i // 4) * 594
            sheet.paste(frame, (x, y + 28))
            ImageDraw.Draw(sheet).text((x + 8, y + 6), f"{time:.1f}s", fill="white")
        sheet.save(directory / "contact-sheet.jpg", quality=94)

    def render(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise SystemExit("ffmpeg is required on PATH")
        silent = OUT / "telegram-opening-portrait-silent.mp4"
        audio = OUT / "telegram-opening-portrait.mp4"
        wide = OUT / "telegram-opening-landscape.mp4"
        command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", "841x1870", "-r", str(FPS), "-i", "pipe:0", "-an",
                   "-vf", "scale=1080:2400:flags=lanczos", "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(silent)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE)
        try:
            for index in range(FPS * DURATION):
                process.stdin.write(self.frame(index / FPS).tobytes())
                if index % (FPS * 5) == 0:
                    print(f"Rendered {index / FPS:.0f}/{DURATION}s", flush=True)
        finally:
            process.stdin.close()
        if process.wait() != 0:
            raise SystemExit("Video encoder failed")
        subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(silent),
                        "-i", str(OUT / "notification-track.wav"), "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", str(DURATION),
                        "-movflags", "+faststart", str(audio)], check=True)
        subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(audio),
                        "-vf", "scale=486:1080:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0a3445,setsar=1",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "copy",
                        "-movflags", "+faststart", str(wide)], check=True)
        manifest = {"duration_seconds": DURATION, "fps": FPS, "scripted": True,
                    "arrivals": [{"seconds": sec, "sender": name, "message": message}
                                 for sec, name, message in zip(ARRIVALS, NAMES, MESSAGES)],
                    "typing_start_seconds": 19.45, "endcard_start_seconds": 24.05,
                    "outputs": [str(path.relative_to(ROOT)) for path in [audio, silent, wide]]}
        (OUT / "timeline.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print("All three MP4 files rendered.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview-only", action="store_true")
    args = parser.parse_args()
    renderer = Renderer()
    renderer.previews()
    if not args.preview_only:
        renderer.render()


if __name__ == "__main__":
    main()
