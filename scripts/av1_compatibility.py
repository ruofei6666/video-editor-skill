"""AV1 -> H.264 compatibility stage using the pinned PyAV encoder runtime."""
from __future__ import annotations

from pathlib import Path
import time
from types import SimpleNamespace

from common import load_json, log, write_json
from gpu_encoder import choose_video_encoder, nvenc_options


def prepare_compatible_source(source: Path, work: Path, identity: dict,
                              *, mode='auto', video_encoder='auto') -> tuple[Path, dict]:
    if mode not in ('auto', 'h264', 'off'):
        raise ValueError('av1-compatibility 必须为 auto、h264 或 off')
    if mode == 'off' or (mode == 'auto' and video_encoder == 'cpu'):
        return source, {}
    import av
    with av.open(str(source), metadata_errors='ignore') as original:
        video = original.streams.video[0]
        context = video.codec_context
        if context.name not in ('av1', 'libdav1d', 'libaom-av1'):
            return source, {}
        if context.pix_fmt != 'yuv420p':
            if mode == 'auto' and video_encoder == 'auto':
                log('AV1 兼容路径仅支持 8-bit YUV420；保留原编码，使用原渲染路径')
                return source, {}
            raise ValueError('AV1 H.264 兼容路径要求 8-bit YUV420，不能丢弃高位深或色度')
        reference = SimpleNamespace(width=context.width, height=context.height,
                                    pix_fmt=context.pix_fmt, rate=video.average_rate,
                                    sample_aspect_ratio=context.sample_aspect_ratio,
                                    **{key: getattr(context, key) for key in
                                       ('color_range', 'colorspace', 'color_trc', 'color_primaries')})
        encoder, reason = choose_video_encoder(av, 'h264', reference, video_encoder)
        if encoder == 'cpu' and mode == 'auto':
            return source, {'selected': False, 'reason': reason}
        rate = video.average_rate
        if rate is None or rate <= 0 or video.time_base is None:
            raise ValueError('AV1 源片缺少有效帧率或时间基准')
        gop = max(1, round(32 * rate))
    name = 'h264_nvenc' if encoder == 'nvenc' else 'libx264'
    settings = {'version': 1, 'source': identity, 'encoder': name,
                'quality': 18, 'gop_frames': gop, 'pixel_format': 'yuv420p'}
    work.mkdir(parents=True, exist_ok=True)
    target, marker = work / 'source_h264_compatible.mp4', work / 'compatibility.json'
    try:
        previous = load_json(marker)
    except (OSError, ValueError):
        previous = {}
    if target.exists() and previous.get('settings') == settings:
        stat = target.stat()
        if previous.get('file_stat') == [stat.st_size, stat.st_mtime_ns]:
            return target, {**previous, 'cache_reused': True, 'current_transcode_s': 0.0}
    temp = work / 'source_h264_compatible.tmp.mp4'
    started = time.perf_counter()
    log(f'AV1 兼容转换：{name}，质量 18，GOP 32 秒；复制原音轨，保留原视频')
    try:
        with av.open(str(source), metadata_errors='ignore') as original, av.open(
                str(temp), 'w', options={'movflags': '+faststart'}) as output:
            video = original.streams.video[0]
            video.codec_context.thread_count = 4
            video.codec_context.thread_type = 'AUTO'
            options = {**nvenc_options(), 'cq': '18'} if encoder == 'nvenc' else {'preset': 'veryfast', 'crf': '18'}
            destination = output.add_stream(name, rate=video.average_rate, options=options)
            destination.width, destination.height = video.width, video.height
            destination.pix_fmt = 'yuv420p'
            destination.time_base = video.time_base
            destination.codec_context.time_base = video.time_base
            destination.codec_context.gop_size = gop
            destination.codec_context.max_b_frames = 0
            if video.codec_context.sample_aspect_ratio is not None:
                destination.codec_context.sample_aspect_ratio = video.codec_context.sample_aspect_ratio
            for key in ('color_range', 'colorspace', 'color_trc', 'color_primaries'):
                setattr(destination.codec_context, key, getattr(video.codec_context, key))
            output.metadata.update(original.metadata)
            destination.metadata.update(video.metadata)
            audio = {s.index: output.add_stream_from_template(s) for s in original.streams.audio}
            written = 0
            for packet in original.demux():
                if packet.stream.index == video.index:
                    for frame in packet.decode():
                        if frame.pts is None:
                            raise ValueError('AV1 解码帧缺少时间戳，已停止兼容转换')
                        for encoded in destination.encode(frame):
                            output.mux(encoded)
                        written += 1
                        if written % 12000 == 0:
                            log(f'AV1 兼容转换：已编码 {written} 帧')
                elif packet.stream.index in audio and packet.pts is not None:
                    packet.stream = audio[packet.stream.index]
                    output.mux(packet)
            for packet in destination.encode(None):
                output.mux(packet)
            if written == 0:
                raise ValueError('AV1 兼容转换未产生视频帧')
        temp.replace(target)
        stat = target.stat()
        elapsed = time.perf_counter() - started
        details = {'selected': True, 'settings': settings, 'input_codec': 'av1',
                   'intermediate_codec': 'h264', 'file_stat': [stat.st_size, stat.st_mtime_ns],
                   'transcode_elapsed_s': elapsed, 'current_transcode_s': elapsed,
                   'cache_reused': False, 'frames_encoded': written, 'reason': reason}
        write_json(marker, details)
        return target, details
    finally:
        temp.unlink(missing_ok=True)
