"""Render an original, speech-friendly Steward ambient bed from equations only.

No samples, external recordings, or pre-existing melody are used. Output is
deterministic, stereo PCM at 48 kHz. Chunks keep memory bounded for desktop use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import wave

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
RATE = 48_000
DURATION = 300.0
CHUNK_SECONDS = 5
TARGET_RMS_DBFS = -22.5
PEAK_CEILING_DBFS = -7.0

# A newly chosen, slowly evolving voicing sequence, not a quoted melody.
# MIDI pitch numbers: low D / B / G / E foundations with spacious upper notes.
CHORDS = [
    (0.0, 39.0, (50, 57, 64, 66)),
    (32.0, 78.0, (47, 54, 62, 69)),
    (71.0, 113.0, (43, 55, 62, 69)),
    (106.0, 149.0, (52, 59, 66, 69)),
    (142.0, 187.0, (50, 57, 62, 64)),
    (180.0, 229.0, (47, 54, 62, 66)),
    (222.0, 268.0, (43, 55, 62, 66)),
    (261.0, 306.0, (50, 57, 64, 69)),
]

# Irregular placements avoid a metronomic pulse beneath narration.
# Each tuple is onset, pitch, relative level, panorama position.
PLUCKS = [
    (8.4, 74, .66, -.31), (21.3, 69, .54, .27),
    (40.7, 66, .60, .20), (55.9, 74, .48, -.23),
    (74.2, 71, .56, -.25), (91.5, 69, .48, .29),
    (111.6, 66, .61, .25), (129.8, 71, .47, -.24),
    (149.7, 69, .54, -.28), (168.2, 76, .42, .23),
    (188.4, 74, .57, .27), (210.1, 66, .46, -.22),
    (232.8, 71, .56, -.25), (251.7, 74, .44, .24),
    (272.9, 69, .58, .24), (286.4, 74, .41, -.20),
]


def hz(midi: int) -> float:
    return 440.0 * 2.0 ** ((midi - 69) / 12.0)


def smooth01(value: np.ndarray) -> np.ndarray:
    x = np.clip(value, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def section_envelope(t: np.ndarray, start: float, end: float) -> np.ndarray:
    # Seven-second overlap: neighbouring smoothstep envelopes add to unity.
    fade_in = np.ones_like(t) if start == 0 else smooth01((t - start) / 7.0)
    return fade_in * smooth01((end - t) / 7.0)


def global_envelope(t: np.ndarray) -> np.ndarray:
    return smooth01(t / 5.0) * smooth01((DURATION - t) / 10.0)


def render_chunk(first_sample: int, count: int) -> tuple[np.ndarray, np.ndarray]:
    t = (first_sample + np.arange(count, dtype=np.float64)) / RATE
    pads = np.zeros((count, 2), dtype=np.float64)
    plucks = np.zeros_like(pads)
    for chord_index, (start, end, pitches) in enumerate(CHORDS):
        if end <= t[0] or start >= t[-1] + 1 / RATE:
            continue
        env = section_envelope(t, start, end)
        # Slow amplitude breathing without an audible rhythm.
        breath = .93 + .07 * np.sin(2 * np.pi * t / (23.0 + chord_index) + .7)
        for voice_index, midi in enumerate(pitches):
            frequency = hz(midi)
            phase = .41 * chord_index + .73 * voice_index
            amplitude = (.029, .018, .011, .008)[voice_index]
            pan = (-.16, .13, -.24, .25)[voice_index]
            for channel, side in enumerate((-1.0, 1.0)):
                # Tiny detuning and equal-power panning yield restrained width.
                detune = side * (.018 + voice_index * .005)
                f = frequency + detune
                signal = np.sin(2 * np.pi * f * t + phase)
                signal += .12 * np.sin(2 * np.pi * 2 * f * t + phase * .6)
                signal += .025 * np.sin(2 * np.pi * 3 * f * t + phase * .3)
                pan_gain = math.sqrt((1.0 + side * pan) / 2.0)
                pads[:, channel] += signal * env * breath * amplitude * pan_gain

    for event_index, (start, midi, level, pan) in enumerate(PLUCKS):
        if start + 12.0 <= t[0] or start >= t[-1] + 1 / RATE:
            continue
        tau = np.maximum(t - start, 0.0)
        active = ((t >= start) & (tau < 12.0)).astype(float)
        # Soft onset, exponential tail, final smooth fade avoid discontinuities.
        env = smooth01(tau / .065) * np.exp(-tau / 2.2)
        env *= smooth01((12.0 - tau) / 2.0) * active
        frequency = hz(midi)
        for channel, side in enumerate((-1.0, 1.0)):
            phase = .09 * side
            signal = np.sin(2 * np.pi * frequency * tau + phase)
            signal += .105 * np.sin(2 * np.pi * 2.001 * frequency * tau + phase)
            signal += .018 * np.sin(2 * np.pi * 3.003 * frequency * tau + phase)
            pan_gain = math.sqrt((1.0 + side * pan) / 2.0)
            plucks[:, channel] += signal * env * level * .023 * pan_gain

    fade = global_envelope(t)[:, None]
    return pads * fade, plucks * fade


def db(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-15))


def writer(path: Path) -> wave.Wave_write:
    stream = wave.open(str(path), 'wb')
    stream.setnchannels(2)
    stream.setsampwidth(2)
    stream.setframerate(RATE)
    return stream


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/video/music-original')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    total_samples = round(DURATION * RATE)
    chunk_samples = CHUNK_SECONDS * RATE
    first_pass_squares = 0.0
    first_pass_peak = 0.0
    for first in range(0, total_samples, chunk_samples):
        pads, plucks = render_chunk(first, min(chunk_samples, total_samples - first))
        mix = pads + plucks
        first_pass_squares += float(np.square(mix).sum())
        first_pass_peak = max(first_pass_peak, float(np.abs(mix).max()))
    raw_rms = math.sqrt(first_pass_squares / (2 * total_samples))
    rms_gain = 10 ** (TARGET_RMS_DBFS / 20) / raw_rms
    peak_gain = 10 ** (PEAK_CEILING_DBFS / 20) / first_pass_peak
    gain = min(rms_gain, peak_gain)
    filenames = {
        'pads': 'steward-original-pads-300s.wav',
        'plucks': 'steward-original-soft-plucks-300s.wav',
        'master': 'steward-original-ambient-300s.wav',
    }
    streams = {key: writer(output / name) for key, name in filenames.items()}
    peaks = dict.fromkeys(filenames, 0.0)
    squares = dict.fromkeys(filenames, 0.0)
    clipped_samples = dict.fromkeys(filenames, 0)
    jumps = dict.fromkeys(filenames, 0.0)
    last_frames = {}
    window_rms = []
    try:
        for first in range(0, total_samples, chunk_samples):
            pads, plucks = render_chunk(first, min(chunk_samples, total_samples - first))
            chunks = {'pads': pads * gain, 'plucks': plucks * gain,
                      'master': (pads + plucks) * gain}
            for key, data in chunks.items():
                peaks[key] = max(peaks[key], float(np.abs(data).max()))
                squares[key] += float(np.square(data).sum())
                clipped_samples[key] += int((np.abs(data) >= 1.0).sum())
                if key in last_frames:
                    jumps[key] = max(jumps[key], float(np.abs(data[0] - last_frames[key]).max()))
                last_frames[key] = data[-1].copy()
                pcm = np.rint(np.clip(data, -1, 1) * 32767.0).astype('<i2')
                streams[key].writeframes(pcm.tobytes())
            window_rms.append({'start': first / RATE,
                               'rms_dbfs': round(db(float(np.sqrt(np.square(chunks['master']).mean()))), 3)})
    finally:
        for stream in streams.values():
            stream.close()

    stats = {
        'title': 'Steward — Quiet Continuity',
        'composition': 'Original deterministic mathematical synthesis for Steward demo narration.',
        'duration_seconds': DURATION, 'sample_rate': RATE, 'channels': 2,
        'format': 'PCM signed 16-bit little endian WAV',
        'render_gain': gain, 'normalization': 'One linear gain; no dynamics limiter or compression.',
        'sources': 'No external audio samples, recordings, melody transcription, or sample libraries.',
        'files': {}, 'five_second_master_windows': window_rms,
        'suggested_final_mix': {
            'master_gain_db': -12,
            'under_voice_additional_duck_db': -3,
            'target_final_music_lufs_approx': [-36, -32],
            'note': 'Start here, then measure against actual narration. Root editor trims/fades to cut duration.',
        },
    }
    for key, filename in filenames.items():
        path = output / filename
        stats['files'][key] = {
            'path': str(path), 'bytes': path.stat().st_size,
            'sha256': hashlib.file_digest(path.open('rb'), 'sha256').hexdigest(),
            'peak_dbfs': round(db(peaks[key]), 4),
            'rms_dbfs': round(db(math.sqrt(squares[key] / (2 * total_samples))), 4),
            'clipped_samples': clipped_samples[key],
            'max_chunk_boundary_sample_delta': jumps[key],
        }
    (output / 'composition-stats.json').write_text(json.dumps(stats, indent=2), encoding='utf-8')
    (output / 'PROVENANCE.md').write_text('''# Steward — Quiet Continuity

Bu fon müziği Steward videosu için sıfırdan, matematiksel ses senteziyle oluşturuldu.
Dışarıdan alınmış ses, sample, loop, kayıt, örnek kütüphanesi veya kopyalanmış melodi
kullanılmadı. Sesler sinüs dalgaları, düşük düzeyde harmonikler, yumuşak zarflar,
küçük stereo detune ve seyrek yumuşak pluck notalarından oluşur. Nota dizisi ve
zamanlamaları bu çalışma için seçildi. Üçüncü taraf müzik lisansı veya atıf şartı
bulunmuyor; müzik bu projede ve proje videosunda kullanılmak üzere üretildi.
Otomatik platform/Content ID sonuçlarına dair mutlak garanti verilmez.

This bed was composed specifically for Steward using mathematical synthesis only.
No third-party audio, recordings, samples, loops, sample libraries, melody copying,
or external music service were used. The source script contains the complete
voicing/event score and signal-generation equations. It uses sine oscillators,
restrained partials, smooth envelopes, slight stereo detuning, and sparse soft
plucks. There are no third-party music attribution or licence requirements from
source material. It was created for use in this project and its demonstration
video. Automated platform/Content ID outcomes cannot be guaranteed.

## Reproducibility and mix

- Source: `tools/compose_steward_music.py`.
- Render: Python + NumPy, deterministic, 300 seconds, stereo, 48 kHz PCM WAV.
- The master is the exact sum of the two stems before 16-bit quantization.
- All three files use the same linear normalization gain. No limiter is applied.
- Master has a 5-second opening fade and a 10-second ending fade. Fade again at
  the actual final edit duration if it is shorter than 300 seconds.
- Begin around -12 dB gain on this master, with another 3 dB of gentle ducking
  during speech. Actual narration loudness should determine the final balance.
- Technical statistics and SHA-256 hashes: `composition-stats.json`.
- Verification is numerical and spectral; no claim of subjective listening is made.
''', encoding='utf-8')
    print(json.dumps({key: value for key, value in stats.items() if key != 'five_second_master_windows'}, indent=2))


if __name__ == '__main__':
    main()
