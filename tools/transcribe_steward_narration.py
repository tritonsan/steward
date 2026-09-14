"""Local, unprompted ASR and source inventory for Steward narration editing.

Requires the official whisper.cpp 1.9.2 Windows CLI and ggml-base.en model
under .scratch/narration-models. Source MP3s are never modified.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/video/narration-analysis"
CLI = ROOT / ".scratch/narration-models/whisper-cli-1.9.2/Release/whisper-cli.exe"
MODEL = ROOT / ".scratch/narration-models/ggml-base.en.bin"


def norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def run(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(a) for a in args], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    prior_path = OUT / "narration-inventory.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8")) if prior_path.exists() else {}
    cached_hashes = {f["number"]: f["sha256"] for f in prior.get("files", [])}
    scripts = json.loads((ROOT / "artifacts/video/coverage-session/assembly-v2/timeline.json")
                         .read_text(encoding="utf-8-sig"))["narration"]
    files = sorted((ROOT / "artifacts/video").glob("*.mp3"),
                   key=lambda p: int(re.search(r"\d+", p.name)[0]))
    inventory, hashes = [], {}
    for path in files:
        num = int(re.search(r"\d+", path.name)[0])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        probe = json.loads(run("ffprobe", "-v", "error", "-show_format", "-show_streams",
                               "-of", "json", path).stdout)
        prefix = OUT / f"steward-{num:02}"
        if digest in hashes:
            original = hashes[digest]
            asr = json.loads((OUT / f"steward-{original:02}.json").read_text(encoding="utf-8"))
            prefix.with_suffix(".json").write_text(json.dumps(asr, indent=2), encoding="utf-8")
        else:
            original = None
            hashes[digest] = num
            if (args.refresh or not prefix.with_suffix(".json").exists()
                    or cached_hashes.get(num) != digest):
                result = run(CLI, "-m", MODEL, "-f", path, "-l", "en", "-t", "4", "-ng",
                             "-nfa", "-dtw", "base.en", "-ojf", "-osrt", "-otxt", "-of", prefix)
                prefix.with_suffix(".log").write_text(result.stdout + "\n" + result.stderr,
                                                      encoding="utf-8")
            asr = json.loads(prefix.with_suffix(".json").read_text(encoding="utf-8"))
        segments = asr["transcription"]
        transcript = " ".join(seg["text"].strip() for seg in segments)
        candidates = sorted(({"id": s["id"], "similarity": round(difflib.SequenceMatcher(
            None, norm(transcript), norm(s["text"])).ratio(), 4)} for s in scripts),
            key=lambda x: x["similarity"], reverse=True)
        tokens = [t for seg in segments for t in seg.get("tokens", [])
                  if not t["text"].startswith("[_")]
        sentences, pending = [], []
        for token in tokens:
            pending.append(token)
            if re.search(r"[.!?]$", token["text"].strip()):
                sentences.append(pending)
                pending = []
        if pending:
            sentences.append(pending)
        sentence_cues = []
        for phrase in sentences:
            sentence_cues.append({
                "text": "".join(t["text"] for t in phrase).strip(),
                "start": round(phrase[0]["offsets"]["from"] / 1000, 3),
                "end": round(phrase[-1]["offsets"]["to"] / 1000, 3),
                "first_token_dtw": phrase[0].get("t_dtw"),
                "last_token_dtw": phrase[-1].get("t_dtw"),
            })
        signal_log = run("ffmpeg", "-hide_banner", "-i", path, "-af",
                         "silencedetect=noise=-40dB:d=0.12,loudnorm=I=-18:TP=-1.5:LRA=9:print_format=json",
                         "-f", "null", "-").stderr
        (OUT / f"steward-{num:02}-signal.log").write_text(signal_log, encoding="utf-8")
        loudness = json.loads(re.search(r'\{\s*"input_i".*?\}', signal_log, re.S)[0])
        silences = []
        silence_start = None
        for match in re.finditer(r"silence_(start|end): ([0-9.]+)", signal_log):
            value = float(match[2])
            if match[1] == "start":
                silence_start = value
            elif silence_start is not None:
                silences.append({"start": silence_start, "end": value})
                silence_start = None
        item = {"number": num, "file": str(path.relative_to(ROOT)).replace("\\", "/"),
                "sha256": digest, "bytes": path.stat().st_size,
                "duration": float(probe["format"]["duration"]),
                "audio": {k: probe["streams"][0].get(k) for k in
                          ("codec_name", "sample_rate", "channels", "channel_layout")},
                "duplicate_of": original, "signal_loudness": loudness,
                "silences_minus40dB_min120ms": silences,
                "transcript": transcript, "best_script": candidates[0],
                "candidates": candidates[:3], "sentences": sentence_cues,
                "segments": [{"start": s["offsets"]["from"] / 1000,
                              "end": s["offsets"]["to"] / 1000,
                              "text": s["text"].strip()} for s in segments],
                "asr_json": str(prefix.with_suffix(".json").relative_to(ROOT)).replace("\\", "/")}
        inventory.append(item)
        print(f"{num:02} {item['duration']:.3f}s -> {candidates[0]} dup={original}", flush=True)
    mapped = {i["best_script"]["id"] for i in inventory if i["best_script"]["similarity"] > 0.7}
    report = {"method": "Unprompted English ASR, whisper.cpp v1.9.2 base.en; token DTW enabled, CPU 4 threads. Model hypothesis timings are editing estimates, not sample-accurate forced alignment.",
              "asr_sources": ["https://github.com/ggml-org/whisper.cpp/releases/tag/v1.9.2",
                              "https://huggingface.co/ggerganov/whisper.cpp"],
              "missing_scripts": [s for s in scripts if s["id"] not in mapped],
              "files": inventory}
    (OUT / "narration-inventory.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Narration source verification", "", report["method"], ""]
    for item in inventory:
        lines.extend([f"## {item['number']:02} — {item['best_script']['id']} ({item['duration']:.3f}s)",
                      f"Source: {item['file']}; duplicate of: {item['duplicate_of']}", "", item["transcript"], ""])
        lines.extend(f"- {s['start']:.2f}–{s['end']:.2f}: {s['text']}" for s in item["sentences"])
        lines.append("")
    lines.extend(["## Missing script recordings", ""] + [s["id"] + ": " + s["text"] for s in report["missing_scripts"]])
    (OUT / "TRANSCRIPTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
