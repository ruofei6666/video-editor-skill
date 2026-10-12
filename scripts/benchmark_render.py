"""Offline renderer benchmark using clearly labelled synthetic keep intervals."""
import argparse
from pathlib import Path
import time

from audio_extractor import probe_video_info
from common import write_json
from video_assembler import assemble_video_smartcut, assemble_video_encode


def main():
    parser = argparse.ArgumentParser(description='只测试导出，规则切点不代表真实课程剪辑')
    parser.add_argument('video', type=Path)
    parser.add_argument('-o', '--output', type=Path, required=True)
    parser.add_argument('--interval-count', type=int, default=1000)
    parser.add_argument('--gap-ms', type=float, default=350)
    parser.add_argument('--renderer', choices=('smartcut', 'encode'), default='smartcut')
    parser.add_argument('--video-encoder', choices=('auto', 'cpu', 'nvenc'), default='auto')
    parser.add_argument('--video-decoder', choices=('auto', 'cpu', 'nvdec'), default='auto')
    parser.add_argument('--audio-coder', choices=('fast', 'twoloop'), default='fast')
    parser.add_argument('--audio-encoder', choices=('auto', 'aac', 'mf'), default='auto')
    parser.add_argument('--audio-pipeline', choices=('auto', 'thread', 'process'), default='auto')
    args = parser.parse_args()
    duration_ms = float(probe_video_info(args.video)['duration_s']) * 1000
    if args.interval_count <= 0 or not 0 <= args.gap_ms < duration_ms / args.interval_count:
        parser.error('区间数量或删除间隔无效')
    step = duration_ms / args.interval_count
    intervals = [(i * step, (i + 1) * step - (args.gap_ms if i + 1 < args.interval_count else 0))
                 for i in range(args.interval_count)]
    stats = {}
    started = time.perf_counter()
    assemble = assemble_video_smartcut if args.renderer == 'smartcut' else assemble_video_encode
    kwargs = {'stats': stats}
    if args.renderer == 'smartcut':
        kwargs['video_encoder'] = args.video_encoder
        kwargs['video_decoder'] = args.video_decoder
        kwargs['audio_coder'] = args.audio_coder
        kwargs['audio_encoder'] = args.audio_encoder
        kwargs['audio_pipeline'] = args.audio_pipeline
    elif args.video_encoder == 'nvenc':
        parser.error('NVENC 仅适用于 smartcut 渲染器')
    assemble(args.video.resolve(), intervals, args.output.resolve(), **kwargs)
    result = {'source': str(args.video.resolve()), 'output': str(args.output.resolve()),
              'synthetic_intervals': True, 'source_duration_s': duration_ms / 1000,
              'gap_ms': args.gap_ms, 'wall_elapsed_s': time.perf_counter() - started, **stats}
    report = args.output.with_suffix('.benchmark.json')
    write_json(report, result)
    print(f'规则切点测试完成：源片 {duration_ms / 1000:.3f}s，导出 {result["actual_duration_s"]:.3f}s，'
          f'耗时 {result["wall_elapsed_s"]:.3f}s；记录：{report}')


if __name__ == '__main__':
    main()
