"""Synthesize a quiet, original notification track for the Steward opening.

No sampled audio, speech, music, network access, or third-party libraries.
Existing output files are never overwritten.
"""

from __future__ import annotations

import argparse
from array import array
import json
import math
from pathlib import Path
import sys
import wave


SAMPLE_RATE = 48_000
DURATION_SECONDS = 25
EVENT_SECONDS = [1.0, 4.3, 6.2, 8.8, 10.1, 12.5, 16.1, 20.8]


def add_tone(
    left: array,
    right: array,
    *,
    start: float,
    frequency: float,
    duration: float,
    gain: float,
    pan: float,
) -> None:
    """A sine pluck with a subtle overtone and click-free amplitude envelope."""
    first = round(start * SAMPLE_RATE)
    length = round(duration * SAMPLE_RATE)
    left_gain = math.sqrt((1.0 - pan) / 2.0)
    right_gain = math.sqrt((1.0 + pan) / 2.0)
    for index in range(length):
        sample_index = first + index
        if sample_index >= len(left):
            break
        t = index / SAMPLE_RATE
        attack = min(1.0, t / 0.012)
        attack = math.sin(attack * math.pi / 2.0) ** 2
        release = min(1.0, max(0.0, (duration - t) / 0.050))
        release = math.sin(release * math.pi / 2.0) ** 2
        envelope = attack * math.exp(-t / 0.105) * release
        phase = 2.0 * math.pi * frequency * t
        tone = (math.sin(phase) + 0.045 * math.sin(2.0 * phase)) / 1.045
        sample = gain * envelope * tone
        left[sample_index] += sample * left_gain
        right[sample_index] += sample * right_gain


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "artifacts/video/telegram-animated-v1",
    )
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    wav_path = output_dir / "notification-track.wav"
    metadata_path = output_dir / "notification-track.json"
    for path in (wav_path, metadata_path):
        if path.exists():
            raise SystemExit(f"Refusing to overwrite existing file: {path}")

    sample_count = SAMPLE_RATE * DURATION_SECONDS
    left = array("d", [0.0]) * sample_count
    right = array("d", [0.0]) * sample_count
    events = []
    for number, start in enumerate(EVENT_SECONDS):
        steward = number == len(EVENT_SECONDS) - 1
        frequencies = (440.0, 659.255114) if steward else (659.255114, 880.0)
        pan = 0.0 if steward else (-0.045 if number % 2 == 0 else 0.045)
        for offset, frequency, length, gain in (
            (0.0, frequencies[0], 0.30, 0.15),
            (0.12, frequencies[1], 0.38, 0.115),
        ):
            add_tone(
                left,
                right,
                start=start + offset,
                frequency=frequency,
                duration=length,
                gain=gain,
                pan=pan,
            )
        events.append(
            {
                "start_seconds": start,
                "duration_seconds": 0.50,
                "role": "steward" if steward else "resident",
                "frequencies_hz": list(frequencies),
                "second_tone_offset_seconds": 0.12,
            }
        )

    peak_before = max(max(abs(value) for value in left), max(abs(value) for value in right))
    # Preserve the intended quiet level; limit only if a future edit raises it.
    scale = min(1.0, 0.18 / peak_before) if peak_before else 1.0
    pcm = array("h")
    peak_int = 0
    square_sum = 0.0
    for left_sample, right_sample in zip(left, right):
        for sample in (left_sample, right_sample):
            value = round(sample * scale * 32_767)
            pcm.append(value)
            peak_int = max(peak_int, abs(value))
            square_sum += (value / 32_768) ** 2
    if sys.byteorder != "little":
        pcm.byteswap()

    output_dir.mkdir(parents=True, exist_ok=True)
    with wav_path.open("xb") as raw:
        with wave.open(raw, "wb") as destination:
            destination.setnchannels(2)
            destination.setsampwidth(2)
            destination.setframerate(SAMPLE_RATE)
            destination.writeframes(pcm.tobytes())

    metadata = {
        "file": wav_path.name,
        "duration_seconds": DURATION_SECONDS,
        "sample_rate_hz": SAMPLE_RATE,
        "channels": 2,
        "sample_format": "PCM signed 16-bit little-endian",
        "frames": sample_count,
        "peak_amplitude": peak_int / 32_768,
        "rms_amplitude": math.sqrt(square_sum / (sample_count * 2)),
        "events": events,
        "provenance": "Original mathematical sine-pluck synthesis; no external audio.",
        "purpose": "Illustrative Telegram conversation; gentle arrival cues, no music or speech.",
    }
    with metadata_path.open("x", encoding="utf-8") as destination:
        json.dump(metadata, destination, ensure_ascii=False, indent=2)
        destination.write("\n")
    with wave.open(str(wav_path), "rb") as check:
        assert check.getnchannels() == 2
        assert check.getsampwidth() == 2
        assert check.getframerate() == SAMPLE_RATE
        assert check.getnframes() == sample_count
    assert metadata["peak_amplitude"] <= 0.2
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
