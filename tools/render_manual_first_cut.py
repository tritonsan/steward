"""Edit only the user's new manual takes; never reuse rejected automated footage."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from array import array
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/video/manual-production"
INTRO = ROOT / "artifacts/video/telegram-animated-v2/steward-opening-1080p.mp4"
SHOTS = [
    dict(id="01-hosted-meeting-preparation", source="2026-09-14 02-26-36.mp4", start=8, end=19,
         context="Hosted demo · Meeting preparation", label_position=[365, 1014],
         action="Today → Cases → open parking case → scroll through topic, agenda and 24/72-hour options.",
         result="The existing Discussion Draft and proposed alternatives are visible. No newly opened case, confirmed meeting or adopted rule is claimed."),
    dict(id="02-hosted-activity-navigation", source="2026-09-14 02-26-36.mp4", start=26, end=32,
         context="Hosted demo · Activity navigation", label_position=[365, 1014],
         action="Expand and scroll activity history, then close the case panel.",
         result="Existing activity entries are shown. Individual source contents were not opened; an earlier unsaved date selection is visible briefly."),
    dict(id="03-local-resident-availability", source="2026-09-14 02-29-33.mp4", start=8, end=20.5,
         context="Separate local simulation · Resident view", label_position=[1230, 1014],
         action="Community cases → My meetings → choose the 17 September option → Share availability.",
         result="Your response is recorded and Your availability has been shared. The meeting is not yet shown as confirmed."),
]


def run(args):
    return subprocess.run(args, capture_output=True, text=True, check=True)


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def encode_shot(shot):
    target = OUT / "clips" / f"{shot['id']}-1080p.mp4"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(shot["start"]),
         "-i", str(OUT / "raw" / shot["source"]), "-t", str(shot["end"]-shot["start"]),
         "-map", "0:v:0", "-vf", "setpts=PTS-STARTPTS,fps=30,setsar=1", "-an",
         "-c:v", "libx264", "-crf", "17", "-preset", "medium", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", str(target)])
    return target


def label(shot):
    image = Image.new("RGBA", (1920, 1080))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 22)
    x, y = shot["label_position"]
    width = round(draw.textlength(shot["context"], font=font)) + 36
    assert x+width <= 1900
    draw.rounded_rectangle((x,y,x+width,y+42), 8, fill=(4, 40, 59, 238))
    draw.text((x+18,y+6),shot["context"],font=font,fill=(242,247,237,255))
    path = OUT / "qa" / f"{shot['id']}-label.png"
    image.save(path)
    return path


def assembly():
    # Deliberately omit the activity-only take and any unfinished overview slot.
    # These are separate hosted/local examples, explicitly labelled throughout.
    a, b = SHOTS[0], SHOTS[2]
    la, lb = label(a), label(b)
    target = OUT / "steward-manual-first-cut-1080p.mp4"
    graph = (
        "[0:v]trim=end_frame=900,setpts=PTS-STARTPTS,setsar=1,fps=30[v0];"
        "[1:v]setpts=PTS-STARTPTS,setsar=1[v1];"
        "[2:v]setpts=PTS-STARTPTS,setsar=1[v2];"
        "[v1][3:v]overlay=0:0:shortest=1[l1];"
        "[v2][4:v]overlay=0:0:shortest=1[l2];"
        "[v0][l1][l2]concat=n=3:v=1:a=0[v];"
        "[0:a]atrim=end=30,asetpts=PTS-STARTPTS,apad=whole_dur=53.5[a]"
    )
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(INTRO),
         "-i", str(OUT/"clips"/f"{a['id']}-1080p.mp4"),
         "-i", str(OUT/"clips"/f"{b['id']}-1080p.mp4"),
         "-loop", "1", "-framerate", "30", "-i", str(la),
         "-loop", "1", "-framerate", "30", "-i", str(lb),
         "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-t", "53.5",
         "-c:v", "libx264", "-crf", "17", "-preset", "medium", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
         "-metadata", "title=Steward — first cut from manually recorded footage",
         "-metadata", "comment=Approved illustrative opening, then separate hosted preparation and local simulated resident availability. Partial rough cut; narration pending.",
         str(target)])
    return target


def check(path, duration, audio):
    result = json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]).stdout)
    v = next(s for s in result["streams"] if s["codec_type"]=="video")
    assert v["width"]==1920 and v["height"]==1080 and v["r_frame_rate"]=="30/1"
    assert v["codec_name"]=="h264" and int(v["nb_frames"])==round(duration*30)
    assert abs(float(result["format"]["duration"])-duration)<0.03
    assert sum(s["codec_type"]=="audio" for s in result["streams"])==audio
    run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"])
    record = dict(file=str(path.relative_to(OUT)),duration_seconds=duration,frames=int(v["nb_frames"]),
                  width=1920,height=1080,fps=30,audio_streams=audio,full_decode="passed",sha256=hash_file(path))
    if audio:
        pcm=subprocess.run(["ffmpeg","-v","error","-i",str(path),"-vn","-af",f"atrim=start=30:end={duration}",
                            "-ac","1","-ar","48000","-f","f32le","pipe:1"],capture_output=True,check=True).stdout
        samples=array("f");samples.frombytes(pcm)
        peak=max(abs(s) for s in samples)
        assert peak<1e-6
        record["post_opening_audio"]={"absolute_peak":peak,"result":"silent pending narration"}
    return record


def preview():
    frames=[]
    for shot in SHOTS:
        for time in [0,(shot["end"]-shot["start"])/2,shot["end"]-shot["start"]-1/30]:
            p=OUT/"qa"/f"{shot['id']}-{time:.2f}.png"
            run(["ffmpeg","-hide_banner","-loglevel","error","-y","-ss",str(time),
                 "-i",str(OUT/"clips"/f"{shot['id']}-1080p.mp4"),"-frames:v","1",str(p)])
            frames.append((p,f"{shot['id']} · {time:.2f}s"))
    sheet=Image.new("RGB",(1920,1200),"#092b38"); d=ImageDraw.Draw(sheet)
    font=ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf",20)
    for i,(path,text) in enumerate(frames):
        x,y=i%3*640,i//3*400
        with Image.open(path) as im:sheet.paste(im.resize((640,360),Image.Resampling.LANCZOS),(x,y))
        d.text((x+10,y+365),text,font=font,fill="white")
    sheet.save(OUT/"qa"/"manual-clips-contact-sheet.jpg",quality=92)
    for time in [29.9,30,35,40.9,41,46,53.4]:
        run(["ffmpeg","-hide_banner","-loglevel","error","-y","-ss",str(time),
             "-i",str(OUT/"steward-manual-first-cut-1080p.mp4"),"-frames:v","1",
             str(OUT/"qa"/f"assembly-{time:.1f}.png")])


def manifest(checks):
    sources=[]
    for name in dict.fromkeys(s["source"] for s in SHOTS):
        original=Path("C:/Users/tekes/Videos")/name
        copy=OUT/"raw"/name
        assert hash_file(original)==hash_file(copy)
        sources.append(dict(original=str(original),working_copy=str(copy.relative_to(ROOT)),
                            bytes=copy.stat().st_size,sha256=hash_file(copy),original_preserved=True))
    data=dict(
        created_date="2026-09-14",status="Partial first cut; additional user recordings and narration needed",
        render_script="tools/render_manual_first_cut.py",fps=30,width=1920,height=1080,
        sources=sources,approved_opening=str(INTRO.relative_to(ROOT)),
        exclusions=[
            "No rejected automated OBS clips, trial product footage, technical cards or former overview are used.",
            "First raw: only 8–19s and26–32s are selected. OBS windows, browser menu, authentication screens and unrelated idle portions are excluded.",
            "Second raw: only 8–20.5s is selected. Authentication, the password-save prompt, ending OBS window and desktop are excluded.",
            "Steward2.mp3 has not been mixed: transcript and timing relevance remain unverified."],
        clips=[dict(s,source_start_frame_60fps=round(s["start"]*60),source_end_frame_exclusive_60fps=round(s["end"]*60),
                    output_frames_30fps=round((s["end"]-s["start"])*30)) for s in SHOTS],
        rough_cut=[dict(start=0,end=30,source="approved opening",context="Illustrative Telegram conversation; existing approved audio"),
                   dict(start=30,end=41,clip=SHOTS[0]["id"],context=SHOTS[0]["context"]),
                   dict(start=41,end=53.5,clip=SHOTS[2]["id"],context=SHOTS[2]["context"])],
        edit_policy=["Normal speed, chronological within each selected source range. Actual clicks, scrolls and resulting state are retained.",
                     "Full frame; no invented UI, crop, zoom, freeze-frame, reading-hold retime, reconstructed transition or technical card.",
                     "Raw60fps is sampled to30fps for delivery. Clean component clips have no overlays or audio.",
                     "Combined cut adds only small persistent context labels to distinguish hosted footage from the separate local simulation.",
                     "The 25s overview target and later final-video gaps have not been filled or padded."],
        missing_coverage=["Original parking source opened and earlier community source inspected.",
                          "Local resident Refresh leading to Meeting confirmed and expanded agenda.",
                          "Demo minutes extraction, selected decision AND action, authorized confirmation, owner/due date.",
                          "Community action completion evidence, outcome verification and resulting memory.",
                          "Elevator history, quote sources/completeness, recommendation and alternatives.",
                          "Approval action, awaiting appointment, labelled simulated proposal, confirmation and repair verification.",
                          "User-recorded real code/recorded model trace and narrow restart evidence.",
                          "User-recorded channel evidence showing separate hostedTelegram and controlledSES contexts.",
                          "Footage-derived workflow overview, matched narration, closing and final assembly."],
        qa=checks)
    (OUT/"edit-manifest.json").write_text(json.dumps(data,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")


if __name__=="__main__":
    for directory in [OUT,OUT/"clips",OUT/"qa"]:directory.mkdir(parents=True,exist_ok=True)
    paths=[encode_shot(s) for s in SHOTS]
    combined=assembly()
    checks=[check(p,s["end"]-s["start"],0) for p,s in zip(paths,SHOTS)]
    checks.append(check(combined,53.5,1))
    preview()
    manifest(checks)
    print(json.dumps(checks,indent=2))
