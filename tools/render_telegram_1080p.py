"""Render a native 1080p opener with full-frame branding and supplied narration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageFont

from render_telegram_video import Renderer, ROOT, ease

OUT = ROOT / "artifacts/video/telegram-animated-v2"
FPS = 30
SIZE = (1920, 1080)
VOICE = ROOT / "artifacts/video/Steward 1.mp3"
NOTIFICATIONS = ROOT / "artifacts/video/telegram-animated-v1/notification-track.wav"


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


class FullHDRenderer:
    def __init__(self, voice_start: float = 20.8) -> None:
        self.chat = Renderer()
        self.voice_start = voice_start
        self.logo_start = 24.25
        self.duration = 30 if voice_start > 10 else 28
        self.logo_card = self.make_card()

    def make_card(self) -> Image.Image:
        card = Image.new("RGB", SIZE, "white")
        logo = Image.open(ROOT / "web/public/steward-logo.png").convert("RGB")
        logo.thumbnail((950, 950), Image.Resampling.LANCZOS)
        card.paste(logo, ((SIZE[0] - logo.width) // 2, -22))
        return card

    def frame(self, t: float) -> Image.Image:
        canvas = Image.new("RGB", SIZE, "#0a3445")
        # Preserve the approved chat framing and avoid the old portrait end card.
        chat = self.chat.frame(min(t, 24.04)).resize((486, 1080), Image.Resampling.LANCZOS)
        canvas.paste(chat, ((SIZE[0] - chat.width) // 2, 0))
        p = ease((t - self.logo_start) / 0.65)
        if p:
            card = self.logo_card.copy()
            tagline = ease((t - self.logo_start - 0.6) / 0.5)
            if tagline:
                font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 37)
                color = tuple(round(255 + (c - 255) * tagline) for c in (10, 52, 69))
                draw = ImageDraw.Draw(card)
                draw.text((960, 862), "A little less to manage.", font=font, fill=color, anchor="mt")
                draw.text((960, 919), "A community that remembers.", font=font, fill=color, anchor="mt")
            canvas = Image.blend(canvas, card, p)
        return canvas

    def previews(self) -> None:
        directory = OUT / "previews"
        directory.mkdir(parents=True, exist_ok=True)
        for time in (16.8, 21.5, 24.55, 26.0, 29.0):
            self.frame(time).save(directory / f"frame-{time:04.1f}s.png")

    def render(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise SystemExit("ffmpeg is required on PATH")
        if not VOICE.is_file():
            raise SystemExit(f"Missing user voice recording: {VOICE}")
        silent = OUT / "steward-opening-1080p-silent.mp4"
        final = OUT / "steward-opening-1080p.mp4"
        mix = OUT / "opening-audio-mix.wav"
        command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", "1920x1080", "-r", str(FPS), "-i", "pipe:0", "-an", "-vf", "setsar=1",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                   "-movflags", "+faststart", str(silent)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE)
        try:
            for index in range(FPS * self.duration):
                process.stdin.write(self.frame(index / FPS).tobytes())
                if index % (FPS * 5) == 0:
                    print(f"Rendered {index / FPS:.0f}/{self.duration}s", flush=True)
        finally:
            process.stdin.close()
        if process.wait() != 0:
            raise SystemExit("Video encoder failed")
        delay = round(self.voice_start * 1000)
        # Original recording measured -25.6 LUFS / -7.4 dBTP. +6 dB leaves headroom.
        # Notification attenuation under speech prevents the last chime masking it.
        audio_filter = (
            f"[0:a]aresample=48000,aformat=channel_layouts=stereo,volume=6dB,"
            f"adelay={delay}:all=1,apad,atrim=duration={self.duration}[voice];"
            f"[1:a]volume='if(gte(t,{self.voice_start}),0.32,0.8)':eval=frame,"
            f"apad,atrim=duration={self.duration}[fx];"
            f"[voice][fx]amix=inputs=2:duration=longest:normalize=0,"
            f"alimiter=limit=0.95:level=false:latency=true,atrim=duration={self.duration}[mix]"
        )
        run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(VOICE), "-i", str(NOTIFICATIONS),
             "-filter_complex", audio_filter, "-map", "[mix]", "-c:a", "pcm_s16le", str(mix)])
        run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(silent), "-i", str(mix),
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-t", str(self.duration), "-movflags", "+faststart", str(final)])
        manifest = {
            "duration_seconds": self.duration, "resolution": list(SIZE), "fps": FPS,
            "voice_source": str(VOICE.relative_to(ROOT)), "voice_start_seconds": self.voice_start,
            "voice_gain_db": 6, "voice_speed": 1.0, "logo_transition_seconds": [24.25, 24.9],
            "full_frame_endcard": True, "scripted_conversation": True,
            "source_animation": "artifacts/video/telegram-animated-v1",
            "outputs": [p.name for p in (final, silent, mix)],
        }
        (OUT / "timeline.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print("1080p video and audio mix complete.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-start", type=float, default=20.8)
    parser.add_argument("--preview-only", action="store_true")
    args = parser.parse_args()
    renderer = FullHDRenderer(args.voice_start)
    renderer.previews()
    if not args.preview_only:
        renderer.render()


if __name__ == "__main__":
    main()
