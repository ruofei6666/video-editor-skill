"""One continuous audio worker process and one final stream-copy mux.

Use two temporary media files for a whole course, not one file per cut. The
worker reuses ContinuousAudio's exact timeline/fades and returns actual stats.
"""
from fractions import Fraction
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

from common import log


class AudioProcess:
    def __init__(self, source, output, ranges, origin, segments, tracks, fade_ms, encoder, coder):
        self.temp = tempfile.TemporaryDirectory(prefix='.continuous_audio_', dir=output.parent)
        self.directory = Path(self.temp.name)
        self.video_path = self.directory / ('video' + output.suffix)
        self.audio_path = self.directory / 'audio.mp4'
        self.stats_path = self.directory / 'stats.json'
        self.log_path = self.directory / 'worker.log'
        self.process = None
        self.log_file = None
        job = {
            'source': str(source), 'output': str(self.audio_path), 'stats': str(self.stats_path),
            'ranges': [(str(s), str(e)) for s, e in ranges], 'origin': str(origin),
            'segments': [(str(s.start_time), str(s.end_time)) for s in segments],
            'track_count': tracks, 'fade_ms': fade_ms, 'encoder': encoder, 'coder': coder,
        }
        self.job_path = self.directory / 'job.json'
        self.job_path.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')

    def start(self):
        self.started = time.perf_counter()
        self.log_file = self.log_path.open('wb')
        startup = None
        if os.name == 'nt':
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
        self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(self.job_path)],
                                        stdout=self.log_file, stderr=subprocess.STDOUT, startupinfo=startup,
                                        env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})

    def check(self):
        if self.process.poll() not in (None, 0):
            self.log_file.flush()
            tail = self.log_path.read_bytes()[-3000:].decode('utf-8', errors='replace')
            raise RuntimeError(f'独立音频进程失败；未交付成片：\n{tail}')

    def collect(self):
        started = time.perf_counter()
        next_log = started + 15
        while self.process.poll() is None:
            if time.perf_counter() >= next_log:
                log('视频已完成，等待连续音频编码进程…')
                next_log = time.perf_counter() + 15
            time.sleep(.1)
        self.check()
        result = json.loads(self.stats_path.read_text(encoding='utf-8'))
        result['audio_wait_s'] = time.perf_counter() - started
        return result

    def mux(self, output):
        started = time.perf_counter()
        args = ['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(self.video_path),
                '-i', str(self.audio_path), '-map', '0:v:0', '-map', '1:a', '-c', 'copy',
                '-map_metadata', '0']
        if output.suffix.lower() in ('.mp4', '.mov'):
            args.extend(['-movflags', '+faststart'])
        args.extend(['-y', str(output)])
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError('音视频合并失败：' + result.stderr[-2000:].decode('utf-8', errors='replace'))
        return time.perf_counter() - started

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)
        if self.log_file is not None:
            self.log_file.close()
        self.temp.cleanup()


def run_worker(job):
    import av
    import numpy as np
    from smart_renderer import ContinuousAudio
    started = time.perf_counter()
    generators = []
    processing_s = mux_s = 0.0
    try:
        with av.open(job['output'], 'w') as output:
            ranges = [(Fraction(s), Fraction(e)) for s, e in job['ranges']]
            segments = [SimpleNamespace(start_time=Fraction(s), end_time=Fraction(e))
                        for s, e in job['segments']]
            for track in range(job['track_count']):
                generators.append(ContinuousAudio(av, np, job['source'], track, output, ranges,
                    Fraction(job['origin']), job['fade_ms'], job['coder'], job['encoder']))
            output.start_encoding()
            if any('aac_coder' in g.output.codec_context.options for g in generators):
                raise RuntimeError('AAC 算法参数未被编码器采用')
            for segment in [*segments, None]:
                for generator in generators:
                    iterator = iter(generator.finish() if segment is None else generator.segment(segment))
                    while True:
                        t = time.perf_counter()
                        try:
                            packet = next(iterator)
                        except StopIteration:
                            processing_s += time.perf_counter() - t
                            break
                        processing_s += time.perf_counter() - t
                        t = time.perf_counter()
                        output.mux(packet)
                        mux_s += time.perf_counter() - t
        result = {
            'audio_encoders_used': [g.encoder_name for g in generators],
            'audio_encoder_selection_reasons': [g.encoder_reason for g in generators],
            'audio_encoder_probe_elapsed_s': sum(g.encoder_probe_s for g in generators),
            'audio_encoder_flush_padding_samples': [g.encoder_flush_padding_samples for g in generators],
            'audio_samples_written': [g.samples_written for g in generators],
            'audio_padded_samples': [g.padded_samples for g in generators],
            'audio_stage_detail_s': {key: sum(g.timings[key] for g in generators)
                                     for key in generators[0].timings},
            'audio_process_encode_s': processing_s, 'audio_process_mux_s': mux_s,
            'audio_worker_elapsed_s': time.perf_counter() - started,
        }
        Path(job['stats']).write_text(json.dumps(result), encoding='utf-8')
    finally:
        for generator in generators:
            generator.close()


if __name__ == '__main__':
    run_worker(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')))
