"""Build a source-preserving 25-second montage and the opening+overview draft.

Requires ffmpeg/ffprobe and Pillow. All product frames come from OBS recordings.
Only captions, cuts and an explicitly documented reading-hold retime are added.
"""
from pathlib import Path
from array import array
import hashlib
import json
import subprocess

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/video/production/assembly"
CLIPS = ROOT / "artifacts/video/production/clips"
INTRO = ROOT / "artifacts/video/telegram-animated-v2/steward-opening-1080p.mp4"
FPS = 30
SHOTS = [
    dict(id="01-message", source="artifacts/video/obs-trial/steward-recording-trial-1080p.mp4",
         start=10, source_duration=4, duration=5, position=[40, 830],
         label="CAPTURED TELEGRAM REPORT",
         title="Turn messages into cases",
         body=["A resident asks for a shared", "visitor parking rule."],
         evidence="Actual resident source opened in the hosted parking case. The four-second reading hold is retimed to five seconds; this does not represent processing latency."),
    dict(id="02-case", source="artifacts/video/production/clips/08-meeting-sequence-1080p.mp4",
         start=0, source_duration=5, duration=5, position=[40, 830],
         label="RECORDED PRODUCT · DEMO COMMUNITY",
         title="Give each case a next step",
         body=["See what happens next", "and whose response is needed."],
         evidence="Existing parking case: waiting for participant availability. No completed meeting or adopted decision is implied."),
    dict(id="03-maintenance", source="artifacts/video/production/clips/04-maintenance-status-1080p.mp4",
         start=0, source_duration=5, duration=5, position=[40, 830],
         label="DEMO · SIMULATED VENDOR EMAIL",
         title="Start the vendor follow-up",
         body=["Request quotes and keep", "the vendor response in view."],
         evidence="Separate existing maintenance case remains Vendor Contacted and Collect and compare vendor quotes. Hosted vendor mail is simulated, not a recorded real supplier transaction."),
    dict(id="04-meeting", source="artifacts/video/production/clips/08-meeting-sequence-1080p.mp4",
         start=5, source_duration=5, duration=5, position=[40, 830],
         label="MEETING PREPARATION · DISCUSSION DRAFT",
         title="Prepare a community meeting",
         body=["Bring the topic and agenda", "into one shared discussion."],
         evidence="Actual prepared topic and agenda from the parking case. Preparation is not a confirmed decision."),
    dict(id="05-follow-up", source="artifacts/video/production/clips/01-today-1080p.mp4",
         start=0, source_duration=5, duration=5, position=[1050, 155], compact=True,
         label="RECORDED PRODUCT · DEMO COMMUNITY",
         title="Keep the follow-up visible",
         body=["Decisions, ongoing work and pending replies."],
         evidence="Actual manager Today view: 0 needs decision, 3 in Steward's care, 3 waiting for a response. Caption is placed above the follow-up list, not over it."),
]


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True)


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def caption(shot):
    image = Image.new("RGBA", (1920, 1080))
    d = ImageDraw.Draw(image)
    x, y = shot["position"]
    compact = shot.get("compact", False)
    width, height = (690, 148) if compact else (650, 204)
    d.rounded_rectangle((x, y, x + width, y + height), 14, fill=(4, 40, 59, 246))
    d.rounded_rectangle((x, y + 17, x + 4, y + height - 17), 2, fill=(143, 174, 98, 255))
    fonts = {
        "label": ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 19),
        "title": ImageFont.truetype("C:/Windows/Fonts/segoeuisl.ttf", 34 if compact else 37),
        "body": ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 25 if compact else 27),
    }
    texts = [(shot["label"], "label", 18 if compact else 24, (174, 199, 142, 255)),
             (shot["title"], "title", 48 if compact else 60, (255, 255, 255, 255)),
             (shot["body"][0], "body", 99 if compact else 116, (225, 234, 236, 255))]
    if not compact:
        texts.append((shot["body"][1], "body", 151, (225, 234, 236, 255)))
    for text, font, offset, fill in texts:
        assert d.textlength(text, font=fonts[font]) <= width - 52, text
        d.text((x + 26, y + offset), text, font=fonts[font], fill=fill)
    target = OUT / "captions" / f"{shot['id']}.png"
    image.save(target)
    return target


def encode_shot(shot):
    source = ROOT / shot["source"]
    assert source.is_file() and source.stat().st_size > 0, source
    overlay = caption(shot)
    target = OUT / "parts" / f"{shot['id']}.mp4"
    factor = shot["duration"] / shot["source_duration"]
    graph = (
        f"[0:v]setpts={factor:.8f}*(PTS-STARTPTS),fps={FPS},"
        f"tpad=stop_mode=clone:stop_duration=0.1,trim=end_frame=150,setsar=1[base];"
        "[1:v]format=rgba,fade=t=in:st=0:d=0.2:alpha=1,"
        "fade=t=out:st=4.8:d=0.2:alpha=1[caption];"
        "[base][caption]overlay=0:0:shortest=1[out]"
    )
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-ss", str(shot["start"]), "-t", str(shot["source_duration"]), "-i", str(source),
         "-loop", "1", "-framerate", "30", "-i", str(overlay),
         "-filter_complex", graph, "-map", "[out]", "-frames:v", "150", "-an",
         "-c:v", "libx264", "-crf", "17", "-preset", "medium", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", str(target)])
    return target


def concatenate(parts):
    listing = OUT / "parts" / "concat.txt"
    listing.write_text("".join(f"file '{path.name}'\n" for path in parts), encoding="utf-8")
    target = OUT / "steward-workflow-overview-1080p.mp4"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c:v", "copy", "-an", "-movflags", "+faststart", str(target)])
    return target


def opening_and_overview(overview):
    target = OUT / "steward-opening-and-overview-1080p.mp4"
    graph = (
        "[0:v]trim=duration=30,setpts=PTS-STARTPTS,setsar=1,fps=30[v0];"
        "[1:v]trim=duration=25,setpts=PTS-STARTPTS,setsar=1,fps=30[v1];"
        "[v0][v1]concat=n=2:v=1:a=0[v];"
        "[0:a]atrim=duration=30,asetpts=PTS-STARTPTS,apad=whole_dur=55[a]"
    )
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(INTRO),
         "-i", str(overview), "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
         "-t", "55", "-c:v", "libx264", "-crf", "17", "-preset", "medium",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
         "-metadata", "title=Steward — opening and workflow overview draft",
         "-metadata", "comment=Approved fictional Telegram opening first; then actual OBS product footage with labelled simulated vendor email. Overview awaits narration.",
         str(target)])
    return target


def qa(file, expected_duration, audio):
    info = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(file)]).stdout)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert video["codec_name"] == "h264"
    assert (video["width"], video["height"], video["r_frame_rate"]) == (1920, 1080, "30/1")
    assert int(video["nb_frames"]) == expected_duration * FPS
    assert abs(float(info["format"]["duration"]) - expected_duration) < 0.05
    assert len([s for s in info["streams"] if s["codec_type"] == "audio"]) == audio
    run(["ffmpeg", "-v", "error", "-i", str(file), "-f", "null", "-"])
    result = dict(file=file.name, duration_seconds=expected_duration, video_frames=int(video["nb_frames"]),
                  width=1920, height=1080, fps=30, audio_streams=audio, full_decode="passed", sha256=sha(file))
    if audio:
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(file), "-vn",
                              "-af", "atrim=start=30:end=55", "-ac", "1", "-ar", "48000",
                              "-f", "f32le", "pipe:1"], check=True, capture_output=True).stdout
        samples = array("f")
        samples.frombytes(pcm)
        assert len(samples) == 25 * 48000
        peak = max(abs(s) for s in samples)
        assert peak < 1e-6
        result["overview_audio"] = {"decoded_samples": len(samples), "absolute_peak": peak,
                                    "result": "silent from 30 to 55 seconds; user narration pending"}
    return result


def frames_and_sheet(overview, assembly):
    paths = []
    for i, shot in enumerate(SHOTS):
        path = OUT / "qa" / f"{shot['id']}-encoded.png"
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(i * 5 + 2.5),
             "-i", str(overview), "-frames:v", "1", str(path)])
        paths.append(path)
    for time in [0, 24.9, 29.9, 30, 30.5, 54.9]:
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(time),
             "-i", str(assembly), "-frames:v", "1", str(OUT / "qa" / f"assembly-{time:04.1f}.png")])
    sheet = Image.new("RGB", (1920, 3 * 592), "#062333")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 25)
    for i, path in enumerate(paths):
        x, y = (i % 2) * 960, (i // 2) * 592
        with Image.open(path) as im:
            sheet.paste(im.resize((960, 540), Image.Resampling.LANCZOS).convert("RGB"), (x, y))
        draw.text((x + 18, y + 549), f"{i*5:02d}–{i*5+5:02d}s  ·  {SHOTS[i]['title']}", font=font, fill="#edf2e9")
    sheet.save(OUT / "qa" / "overview-contact-sheet.jpg", quality=92)


def manifest(checks):
    source_paths = [INTRO] + list(dict.fromkeys(ROOT / s["source"] for s in SHOTS))
    data = {
        "created_date": "2026-09-14", "render_script": "tools/render_production_overview.py",
        "duration_seconds": 55, "fps": FPS, "width": 1920, "height": 1080,
        "order": [{"start": 0, "end": 30, "content": "Approved Telegram opening, unchanged sequence and existing user audio"},
                  {"start": 30, "end": 55, "content": "Five-shot recorded product overview; silent pending user narration"}],
        "cuts": [dict(s, overview_start=i*5, overview_end=i*5+5, assembly_start=30+i*5,
                      assembly_end=35+i*5, source_end=s["start"]+s["source_duration"],
                      speed_multiplier=s["source_duration"]/s["duration"]) for i, s in enumerate(SHOTS)],
        "sources": [{"file": str(p.relative_to(ROOT)).replace("\\", "/"), "sha256": sha(p)} for p in source_paths],
        "upstream_manifests": ["artifacts/video/production/clips/hosted-clips-manifest.json", "artifacts/video/obs-trial/edit-timeline.json"],
        "edits": ["Hard cuts between five 5-second reading holds; no fabricated UI states or text.",
                  "First shot stretches an existing 4-second static source hold to 5 seconds; timing is editorial, not model or message latency.",
                  "Other shot timing remains 1x. Existing crop/enlargement is documented by the upstream clip manifests.",
                  "English caption panels fade in/out over 0.2 seconds and sit outside the relevant source cards.",
                  "Original opening video is re-encoded for the combined H.264 draft; original AAC audio is retained perceptually via AAC re-encode, then 25 seconds of silence."],
        "claim_boundaries": ["The opening is a fictional Telegram reenactment. The overview uses real recorded hosted application views.",
                             "Parking message/preparation and maintenance vendor follow-up are separate examples, not one continuous causal transaction.",
                             "Hosted vendor messages are simulated. Real controlled SES evidence is a separate test.",
                             "Community data is synthetic. No actual repair, held meeting, adopted decision, payment or measured time saving is claimed here."],
        "narration_edit": {"files": ["artifacts/video/voiceover/07-technical.txt", "artifacts/video/voiceover/VOICEOVER_EN.md", "artifacts/video/voiceover/timeline.json"],
                           "replaced": "The worker resumes pending jobs after a restart, and the outbox tracks delivery.",
                           "replacement": "Cases and delivery records remain in PostgreSQL when the worker restarts.",
                           "reason": "Recorded cloud evidence establishes preserved cases and outbox records with worker replacement; it does not establish a particular leased job resumed.",
                           "technical_words": 71, "seconds_at_130_wpm": 32.8},
        "qa": checks,
    }
    (OUT / "edit-manifest.json").write_text(json.dumps(data, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")


def main():
    for directory in [OUT, OUT/"captions", OUT/"parts", OUT/"qa"]:
        directory.mkdir(parents=True, exist_ok=True)
    parts = [encode_shot(shot) for shot in SHOTS]
    overview = concatenate(parts)
    assembly = opening_and_overview(overview)
    checks = [qa(overview, 25, 0), qa(assembly, 55, 1)]
    frames_and_sheet(overview, assembly)
    manifest(checks)
    print(json.dumps(checks, indent=2))


if __name__ == "__main__":
    main()
