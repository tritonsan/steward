"""Read-only media QA for a completed continuous OBS take; no browser/OBS control."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from array import array
import argparse
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "artifacts/video/coverage-session"


def command(args, text=True):
    return subprocess.run(args, check=True, capture_output=True, text=text)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("filename",help="Completed MP4 filename inside coverage-session/raw")
    parser.add_argument("--times",help="Comma-separated actual elapsed seconds to sample")
    args=parser.parse_args()
    source=(BASE/"raw"/args.filename).resolve()
    assert source.parent==(BASE/"raw").resolve() and source.suffix.lower()==".mp4"
    output=BASE/"qa"/source.stem
    output.mkdir(parents=True,exist_ok=True)
    probe=json.loads(command(["ffprobe","-v","error","-count_frames","-show_streams","-show_format","-of","json",str(source)]).stdout)
    video=next(s for s in probe["streams"] if s["codec_type"]=="video")
    duration=float(probe["format"]["duration"])
    if args.times:
        times=[float(t) for t in args.times.split(",")]
    else:
        times=[float(t) for t in range(0,int(duration),5)]+[max(0,duration-.1)]
    assert all(0<=t<duration for t in times)
    width,height=1920,((len(times)+2)//3)*400
    sheet=Image.new("RGB",(width,height),"#082733")
    draw=ImageDraw.Draw(sheet)
    font=ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf",23)
    sampled=[]
    for i,t in enumerate(times):
        frame=output/f"frame-{t:07.3f}.png"
        command(["ffmpeg","-hide_banner","-loglevel","error","-y","-ss",str(t),"-i",str(source),"-frames:v","1",str(frame)])
        assert frame.exists()
        with Image.open(frame) as im:
            sheet.paste(im.resize((640,360),Image.Resampling.LANCZOS),(i%3*640,i//3*400))
        draw.text((i%3*640+10,i//3*400+365),f"{t:.3f}s",font=font,fill="white")
        sampled.append({"elapsed_seconds":t,"file":str(frame.relative_to(BASE)).replace("\\","/")})
    sheet.save(output/"contact-sheet.jpg",quality=92)
    pages=[]
    for first in range(0,len(sampled),12):
        batch=sampled[first:first+12]
        page=Image.new("RGB",(1920,((len(batch)+2)//3)*400),"#082733")
        painter=ImageDraw.Draw(page)
        for j,record in enumerate(batch):
            x,y=j%3*640,j//3*400
            with Image.open(BASE/record["file"]) as im:
                page.paste(im.resize((640,360),Image.Resampling.LANCZOS),(x,y))
            painter.text((x+10,y+365),f"{record['elapsed_seconds']:.3f}s",font=font,fill="white")
        page_path=output/f"contact-page-{first//12+1:02}.jpg"
        page.save(page_path,quality=92)
        pages.append(str(page_path.relative_to(BASE)).replace("\\","/"))
    decode=command(["ffmpeg","-hide_banner","-i",str(source),"-vf","blackdetect=d=0.1:pix_th=0.02:pic_th=0.99",
                    "-map","0:v:0","-an","-f","null","-"])
    (output/"decode-blackdetect.log").write_text(decode.stderr,encoding="utf-8")
    black=[line for line in decode.stderr.splitlines() if "black_start:" in line]
    audio_streams=[s for s in probe["streams"] if s["codec_type"]=="audio"]
    peak=None
    if audio_streams:
        pcm=command(["ffmpeg","-v","error","-i",str(source),"-map","0:a:0","-ac","1","-ar","48000","-f","f32le","pipe:1"],text=False).stdout
        values=array("f");values.frombytes(pcm)
        peak=max(map(abs,values),default=0)
    digest=hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda:stream.read(8*1024*1024),b""):digest.update(block)
    report={"source":str(source.relative_to(BASE)).replace("\\","/"),"sha256":digest.hexdigest(),
            "bytes":source.stat().st_size,"creation_time_utc":probe["format"].get("tags",{}).get("creation_time"),
            "duration_seconds":duration,"width":video["width"],"height":video["height"],"fps":video["avg_frame_rate"],
            "stored_frames":int(video.get("nb_frames",0)),"decoded_frames":int(video.get("nb_read_frames",0)),
            "full_decode":"passed","black_intervals":black,"audio_streams":len(audio_streams),"decoded_audio_absolute_peak":peak,
            "sampled_frames":sampled,"contact_sheet":str((output/"contact-sheet.jpg").relative_to(BASE)).replace("\\","/"),
            "contact_pages":pages,
            "visual_review":"pending; contact sheet and selected full-size frames must be inspected by the editor"}
    (output/"probe.json").write_text(json.dumps(probe,indent=2)+"\n",encoding="utf-8")
    (output/"technical-qa.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="sampled_frames"},indent=2))


if __name__=="__main__":main()
