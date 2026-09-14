"""Edit the actual OBS trial; never alter product content or case state."""
from pathlib import Path
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/video/obs-trial"
RAW = OUT / "raw/2026-09-14 01-07-20.mp4"
# Retain successful navigation and settled reading holds; remove idle time.
SHOTS = [
    {"name": "Cases to parking case", "start": 6, "duration": 4, "crop": None},
    {"name": "Meeting preparation", "start": 22.7, "duration": 3, "crop": [1280, 720, 640, 0]},
    {"name": "Options and questions", "start": 35.8, "duration": 3, "crop": [1280, 720, 640, 210]},
    {"name": "Original resident message", "start": 131, "duration": 4, "crop": [1280, 720, 640, 350]},
]


def render(focused: bool):
    filters = []
    for index, shot in enumerate(SHOTS):
        chain = f"[0:v]trim=start={shot['start']}:duration={shot['duration']},setpts=PTS-STARTPTS"
        if focused and shot["crop"]:
            chain += ",crop=" + ":".join(map(str, shot["crop"]))
            chain += ",scale=1920:1080:flags=lanczos"
        filters.append(chain + f",setsar=1,fps=30[v{index}]")
    filters.append("".join(f"[v{i}]" for i in range(len(SHOTS))) + f"concat=n={len(SHOTS)}:v=1:a=0[out]")
    name = "steward-recording-trial-1080p.mp4" if focused else "steward-recording-trial-full-frame.mp4"
    target = OUT / name
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(RAW),
        "-filter_complex", ";".join(filters), "-map", "[out]", "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-metadata", "title=Steward - recorded product navigation trial",
        str(target),
    ], check=True)
    print(target)


if __name__ == "__main__":
    render(False)
    render(True)
    (OUT / "edit-timeline.json").write_text(json.dumps({
        "raw_recording": str(RAW.relative_to(ROOT)).replace("\\", "/"),
        "duration_seconds": sum(s["duration"] for s in SHOTS),
        "fps": 30, "width": 1920, "height": 1080,
        "audio": "omitted: framing trial",
        "edits": "Cuts remove idle/loading time; focused version crops and enlarges recorded pixels only.",
        "shots": SHOTS,
    }, indent=2) + "\n", encoding="utf-8")
