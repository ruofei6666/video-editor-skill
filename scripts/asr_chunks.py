"""Sample-exact, resumable ASR chunks; never split the source video."""
from __future__ import annotations

import array
from concurrent.futures import ThreadPoolExecutor
import hashlib
import math
from pathlib import Path
import sys
import wave

from common import load_json, log, write_json
from transcriber import validate_transcript

MAX_UPLOAD_BYTES = 100_000_000


def quiet_split(handle, desired: int, rate: int, total: int) -> int:
    start, stop = max(0, desired - 20 * rate), min(total, desired + 20 * rate)
    handle.setpos(start)
    pcm = array.array('h', handle.readframes(stop - start))
    if sys.byteorder != 'little':
        pcm.byteswap()
    window, step = int(rate * .3), int(rate * .05)
    best = min(range(0, max(1, len(pcm) - window + 1), step),
               key=lambda i: sum(int(x) ** 2 for x in pcm[i:i + window]))
    return start + best + window // 2


def transcribe_long_audio(audio_path: Path, output_json: Path, transcribe,
                          *, backend: str, chunk_seconds=2400, workers=2) -> list[dict]:
    """Chunk only uploads over the local 100 MB limit, preserving absolute times."""
    if output_json.exists():
        return validate_transcript(load_json(output_json))
    if audio_path.stat().st_size <= MAX_UPLOAD_BYTES:
        return validate_transcript(transcribe(audio_path, output_json))
    if not math.isfinite(float(chunk_seconds)) or not 60 <= float(chunk_seconds) <= 2800:
        raise ValueError('ASR 分段时长必须为 60 到 2800 秒')
    if not isinstance(workers, int) or not 1 <= workers <= 4:
        raise ValueError('ASR 分段并发数必须为 1 到 4')
    digest = hashlib.sha256()
    with audio_path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    identity = {'version': 1, 'audio_sha256': digest.hexdigest(),
                'backend': backend, 'chunk_seconds': float(chunk_seconds)}
    key = hashlib.sha256(str(sorted(identity.items())).encode()).hexdigest()[:24]
    root = output_json.parent / 'asr_chunks' / key
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / 'manifest.json'
    with wave.open(str(audio_path), 'rb') as original:
        rate, total = original.getframerate(), original.getnframes()
        if (rate, original.getnchannels(), original.getsampwidth()) != (16000, 1, 2) or total <= 0:
            raise ValueError('长课 ASR 要求非空 16 kHz 单声道 16-bit WAV')
        try:
            manifest = load_json(manifest_path)
        except (OSError, ValueError):
            manifest = {}
        if manifest.get('identity') != identity:
            chunks, start = [], 0
            while start < total:
                desired = start + round(float(chunk_seconds) * rate)
                stop = quiet_split(original, desired, rate, total) if total - start > (float(chunk_seconds) + 40) * rate else total
                chunks.append({'file': f'{len(chunks):04d}.wav', 'start_sample': start,
                               'end_sample': stop, 'offset_s': start / rate})
                start = stop
            manifest = {'identity': identity, 'rate': rate, 'chunks': chunks}
            write_json(manifest_path, manifest)
        chunks = manifest['chunks']
        for item in chunks:
            chunk = root / item['file']
            if chunk.exists():
                continue
            temp = chunk.with_suffix('.tmp.wav')
            try:
                original.setpos(item['start_sample'])
                with wave.open(str(temp), 'wb') as target:
                    target.setnchannels(1)
                    target.setsampwidth(2)
                    target.setframerate(rate)
                    remaining = item['end_sample'] - item['start_sample']
                    while remaining:
                        count = min(remaining, rate * 60)
                        data = original.readframes(count)
                        if len(data) != count * 2:
                            raise ValueError('分段音频读取不完整')
                        target.writeframesraw(data)
                        remaining -= count
                temp.replace(chunk)
            finally:
                temp.unlink(missing_ok=True)
    log(f'长课 ASR：{len(chunks)} 段，段内并发 {workers}；复用已完成分段与任务编号')

    def recognize(item):
        chunk = root / item['file']
        segments = validate_transcript(transcribe(chunk, chunk.with_suffix('.json')))
        length = (item['end_sample'] - item['start_sample']) / rate
        if any(s['end'] > length + .1 for s in segments):
            raise ValueError('ASR 分段时间戳超出音频范围')
        log(f"ASR 分段完成：{item['file']}")
        return item, segments

    merged = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for item, segments in pool.map(recognize, chunks):
            offset = item['offset_s']
            for segment in segments:
                merged.append({**segment, 'start': segment['start'] + offset,
                               'end': segment['end'] + offset,
                               'words': [{**w, 'start': w['start'] + offset, 'end': w['end'] + offset}
                                         for w in segment['words']]})
    merged = validate_transcript(merged)
    write_json(output_json.parent / 'asr_chunk_manifest.json', manifest)
    write_json(output_json, merged)
    return merged
