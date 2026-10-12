"""Single-file smart video rendering with continuous, sample-accurate AAC audio.

Composes smartcut's video cutter with a continuous audio stream generator. PCM is decoded sequentially in
small frames; neither one file per cut nor a whole-course PCM buffer is needed.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
import math
from pathlib import Path
import time

from common import log
from audio_encoder import choose_audio_encoder, mf_flush_frame
from audio_process import AudioProcess
from fast_index import index_media
from gpu_encoder import (choose_video_encoder, create_nvenc_context, nvenc_name,
                         choose_video_decoder, create_nvdec_context)


def load_backend():
    try:
        import av
        import numpy as np
        from smartcut.cut_video import VideoCutter, VideoSettings, make_cut_segments, make_adjusted_segment_times
        from smartcut.media_utils import VideoExportMode, VideoExportQuality
        from smartcut.media_container import MediaContainer
    except ImportError as exc:
        raise RuntimeError(
            "缺少智能渲染依赖。请用本技能虚拟环境的 Python 执行 "
            "-m pip install -r requirements-render.txt；或手动选择 --renderer encode。"
        ) from exc
    return av, np, VideoCutter, VideoSettings, VideoExportMode, VideoExportQuality, make_cut_segments, make_adjusted_segment_times, MediaContainer


def snap_keep_ranges(media, intervals, np):
    """Expand keeps to frame boundaries so rounding never removes retained speech.

    Very small removed gaps can disappear after snapping. Merge those overlaps
    and disclose the adopted ranges rather than double-writing shared frames.
    """
    frame_times = media.video_frame_times
    if len(frame_times) == 0:
        raise ValueError("输入没有可定位的视频帧")
    ranges = []
    for start_ms, end_ms in intervals:
        start = Fraction(str(start_ms)) / 1000 + media.start_time
        end = Fraction(str(end_ms)) / 1000 + media.start_time
        i = max(0, int(np.searchsorted(frame_times, start, side='right')) - 1)
        j = int(np.searchsorted(frame_times, end, side='left'))
        start = max(Fraction(0), frame_times[i] - media.start_time)
        end = frame_times[j] - media.start_time if j < len(frame_times) else media.duration
        end = min(end, media.duration)
        if start >= end:
            raise ValueError("保留区间没有可输出的视频帧，请调整计划")
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(end, ranges[-1][1]))
        else:
            ranges.append((start, end))
    return ranges


class ContinuousAudio:
    """A smartcut stream generator with one AAC encoder for the entire track."""
    def __init__(self, av, np, source_path, track_index, output, ranges, origin, fade_ms,
                 audio_coder='fast', audio_encoder='auto'):
        self.av, self.np = av, np
        self.input = av.open(str(source_path), metadata_errors='ignore')
        try:
            self.stream = self.input.streams.audio[track_index]
            self.rate = self.stream.codec_context.sample_rate
            if not self.rate:
                raise ValueError("音轨缺少采样率")
            self.layout = self.stream.codec_context.layout.name
            self.channels = len(self.stream.codec_context.layout.channels)
            self.encoder_name, self.encoder_reason, self.encoder_probe_s = choose_audio_encoder(
                av, np, self.stream.codec_context, audio_encoder, audio_coder)
            self.output = output.add_stream(self.encoder_name, rate=self.rate)
            self.output.layout = self.layout
            self.output.bit_rate = 192000
            self.output.options = {'aac_coder': audio_coder} if self.encoder_name == 'aac' else {}
            self.output.time_base = Fraction(1, self.rate)
            self.output.metadata.update(self.stream.metadata)
            self.output.disposition = self.stream.disposition.value
            self.resampler = av.AudioResampler(format='fltp', layout=self.layout, rate=self.rate)
            self.frames = iter(self.input.decode(self.stream))
            self.pending = deque()
            self.buffer = None
            self.buffer_start = 0
            self.eof = False
            self.fade = round(fade_ms * self.rate / 1000)
            self.ranges = [(round((s + origin) * self.rate), round((e + origin) * self.rate))
                           for s, e in ranges]
            self.range_index = 0
            self.samples_written = 0
            self.output_time = Fraction(0)
            self.padded_samples = 0
            self.encoder_flush_padding_samples = 0
            self.timings = dict(audio_decode_resample_s=0.0, audio_pcm_edit_s=0.0,
                                audio_frame_build_s=0.0, audio_encode_s=0.0)
        except Exception:
            self.input.close()
            raise

    def close(self):
        self.input.close()

    def next_buffer(self):
        started = time.perf_counter()
        while not self.pending and not self.eof:
            try:
                frame = next(self.frames)
                self.pending.extend(self.resampler.resample(frame))
            except StopIteration:
                self.pending.extend(self.resampler.resample(None))
                self.eof = True
        if not self.pending:
            self.buffer = None
            self.timings['audio_decode_resample_s'] += time.perf_counter() - started
            return False
        frame = self.pending.popleft()
        if frame.pts is None or frame.time_base is None:
            raise ValueError("音频缺少真实时间戳，不能确认剪辑同步")
        self.buffer_start = round(frame.pts * frame.time_base * self.rate)
        self.buffer = frame.to_ndarray()
        self.timings['audio_decode_resample_s'] += time.perf_counter() - started
        return True

    def pcm_chunks(self, start, count):
        """Read a source sample span, preserving genuine gaps as silence."""
        end = start + count
        while start < end:
            if self.buffer is None:
                self.next_buffer()
            while self.buffer is not None and self.buffer_start + self.buffer.shape[1] <= start:
                self.next_buffer()
            if self.buffer is None or self.buffer_start > start:
                n = min(4096, end - start)
                if self.buffer is not None:
                    n = min(n, self.buffer_start - start)
                self.padded_samples += n
                yield start, self.np.zeros((self.channels, n), dtype=self.np.float32)
            else:
                offset = start - self.buffer_start
                n = min(end - start, self.buffer.shape[1] - offset)
                yield start, self.buffer[:, offset:offset + n].copy()
            start += n

    def segment(self, segment):
        # Round the cumulative output clock, rather than independently rounding
        # every AAC packet or GOP. Error stays under one sample across all cuts.
        stop_time = self.output_time + segment.end_time - segment.start_time
        count = round(stop_time * self.rate) - self.samples_written
        start = round(segment.start_time * self.rate)
        while (self.range_index + 1 < len(self.ranges)
               and start >= self.ranges[self.range_index][1]):
            self.range_index += 1
        range_start, range_end = self.ranges[self.range_index]
        fade = min(self.fade, (range_end - range_start) // 2)
        for position, pcm in self.pcm_chunks(start, count):
            started = time.perf_counter()
            if fade and (position < range_start + fade or position + pcm.shape[1] > range_end - fade):
                x = self.np.arange(position, position + pcm.shape[1])
                gain = self.np.minimum(self.np.clip((x - range_start) / fade, 0, 1),
                                      self.np.clip((range_end - 1 - x) / fade, 0, 1))
                pcm *= gain.astype(self.np.float32)
            self.timings['audio_pcm_edit_s'] += time.perf_counter() - started
            started = time.perf_counter()
            frame = self.av.AudioFrame.from_ndarray(self.np.ascontiguousarray(pcm),
                                                   format='fltp', layout=self.layout)
            frame.sample_rate = self.rate
            frame.time_base = Fraction(1, self.rate)
            frame.pts = self.samples_written
            self.samples_written += frame.samples
            self.timings['audio_frame_build_s'] += time.perf_counter() - started
            started = time.perf_counter()
            packets = self.output.encode(frame)
            self.timings['audio_encode_s'] += time.perf_counter() - started
            for packet in packets:
                if self.encoder_name == 'aac_mf':
                    packet.duration = 1024
                yield packet
        self.output_time = stop_time

    def finish(self):
        started = time.perf_counter()
        packets = []
        if self.encoder_name == 'aac_mf':
            padding = mf_flush_frame(self.av, self.np, self.rate, self.layout,
                                     self.channels, self.samples_written)
            self.encoder_flush_padding_samples = padding.samples
            packets.extend(self.output.encode(padding))
        packets.extend(self.output.encode(None))
        self.timings['audio_encode_s'] += time.perf_counter() - started
        for packet in packets:
            if self.encoder_name == 'aac_mf':
                if packet.pts is None or packet.time_base is None:
                    raise RuntimeError('MF 输出音频包缺少时间戳')
                start = round(packet.pts * packet.time_base * self.rate)
                if start >= self.samples_written:
                    continue
                packet.duration = min(1024, self.samples_written - start)
            yield packet


def render_smart(input_video: Path, intervals, output_video: Path, *, fade_ms=15, stats=None,
                 video_encoder='auto', video_decoder='auto', audio_coder='fast', audio_encoder='auto',
                 audio_pipeline='auto'):
    if not math.isfinite(fade_ms) or fade_ms < 0:
        raise ValueError("微淡化必须是非负有限数值")
    if audio_coder not in ('fast', 'twoloop'):
        raise ValueError('audio-coder 必须为 fast 或 twoloop')
    if audio_pipeline not in ('auto', 'thread', 'process'):
        raise ValueError('audio-pipeline 必须为 auto、thread 或 process')
    av, np, VideoCutter, VideoSettings, VideoExportMode, VideoExportQuality, make_segments, adjust_times, MediaContainer = load_backend()
    started = time.perf_counter()
    media = None
    audio_generators = []
    video_cutter = None
    gpu_decoder = None
    audio_job = None
    process_stats = {}
    try:
        media = index_media(input_video, MediaContainer)
        if media.video_stream is None or not media.audio_tracks:
            raise ValueError("智能渲染需要有效音视频轨道")
        if len(media.av_container.streams.video) != 1:
            raise ValueError("智能渲染目前只支持一个视频轨道")
        codec = media.video_stream.codec_context.name
        if codec not in ('h264', 'hevc', 'vp9', 'av1', 'libdav1d', 'mpeg2video'):
            raise ValueError(f"视频编码 {codec} 未纳入智能渲染支持范围；可手动选择 --renderer encode")
        # Subtitle timing is outside this speech-editing skill. Do not silently
        # retain stale subtitle cues; route subtitle generation after editing.
        if media.subtitle_tracks:
            raise ValueError("源视频包含字幕轨道，请先提取字幕后剪辑，再基于成片生成字幕")
        ranges = snap_keep_ranges(media, intervals, np)
        segments = make_segments(media, adjust_times(ranges, media))
        if not segments:
            raise ValueError("剪辑计划没有可输出的视频片段")
        expected = sum((e - s for s, e in ranges), Fraction(0))
        scheduled = sum((s.end_time - s.start_time for s in segments), Fraction(0))
        use_process = audio_pipeline == 'process' or (audio_pipeline == 'auto' and scheduled >= 60)
        # MF won the standalone benchmark but increased contention in the
        # shared-thread pipeline. Prefer it only in the independent process.
        applied_audio_encoder = 'aac' if audio_encoder == 'auto' and not use_process else audio_encoder
        if abs(float(scheduled - expected)) > .1:
            raise RuntimeError("smartcut 采用的时间线与帧边界计划不一致，已停止导出")
        recode_frames = sum(int(np.searchsorted(media.video_frame_times, s.end_time)
                                - np.searchsorted(media.video_frame_times, s.start_time))
                            for s in segments if s.require_recode)
        index_s = time.perf_counter() - started
        selection_started = time.perf_counter()
        encoder_mode, encoder_reason = choose_video_encoder(av, codec, media.video_stream.codec_context,
                                                            video_encoder)
        encoder_probe_s = time.perf_counter() - selection_started
        selection_started = time.perf_counter()
        decoder_mode, decoder_reason = choose_video_decoder(av, codec, media.video_stream.codec_context,
                                                            input_video, video_decoder, encoder_mode)
        decoder_probe_s = time.perf_counter() - selection_started
        encoders_used = set()
        encoder_pixel_formats = set()
        log(f"smartcut：{len(intervals)} 个保留区间，采用 {len(ranges)} 段；"
            f"计划重编码 {recode_frames} 帧，其余视频直接复制。")
        log(f"边界编码器：{nvenc_name(codec) if encoder_mode == 'nvenc' else 'CPU'}；"
            f"视频解码：{decoder_mode}；连续音频编码在 CPU 上处理。")

        class CourseVideoCutter(VideoCutter):
            def __init__(self, *args):
                super().__init__(*args)
                self.encode_seconds = 0.0
                self.encoder_initializations = 0

            def init_encoder(self):
                super().init_encoder()
                if encoder_mode == 'cpu' and self.codec_name in ('h264', 'hevc'):
                    self.encoding_options['preset'] = 'veryfast'

            def _ensure_enc_codec(self):
                if self.enc_codec is not None:
                    return
                if encoder_mode == 'cpu':
                    super()._ensure_enc_codec()
                else:
                    context = create_nvenc_context(av, self.codec_name, self.out_stream.codec_context,
                                                   self.out_time_base, self.encoding_options.get('profile'),
                                                   input_pix_fmt=({'yuv420p': 'nv12', 'yuv420p10le': 'p010le'}.get(
                                                       self.out_stream.codec_context.pix_fmt, self.out_stream.codec_context.pix_fmt)
                                                       if decoder_mode == 'nvdec' else None))
                    context.open()
                    if context.options:
                        raise RuntimeError(f'NVENC 未采用参数：{sorted(context.options)}')
                    self.enc_codec = context
                    self.enc_last_pts = -1
                encoders_used.add(self.enc_codec.name)
                encoder_pixel_formats.add(self.enc_codec.pix_fmt)
                self.encoder_initializations += 1
                cutter = self
                class TimedEncoder:
                    def __init__(self, context):
                        self.context = context

                    def __getattr__(self, key):
                        return getattr(self.context, key)

                    def encode(self, *args):
                        started = time.perf_counter()
                        result = self.context.encode(*args)
                        cutter.encode_seconds += time.perf_counter() - started
                        return result
                self.enc_codec = TimedEncoder(self.enc_codec)

            def recode_segment(self, segment):
                if self.codec_name not in ('h264', 'hevc'):
                    return super().recode_segment(segment)
                if not self.encoder_inited:
                    self.init_encoder()
                self._ensure_enc_codec()
                priming = None
                if (0 < segment.gop_index < len(self.media_container.gop_has_rasl)
                        and self.media_container.gop_has_rasl[segment.gop_index]):
                    priming = self.media_container.gop_start_times_dts[segment.gop_index - 1]
                packets = []
                clock_cache = {}
                def truncate(numerator, denominator):
                    return numerator // denominator if numerator >= 0 else -((-numerator) // denominator)
                for frame in self.fetch_frame(segment.gop_start_dts, segment.gop_end_dts,
                                               segment.end_time, priming):
                    if frame.pts is None:
                        raise RuntimeError('解码视频帧缺少 PTS')
                    tb = frame.time_base or self.in_time_base
                    if tb not in clock_cache:
                        start = segment.start_time / tb
                        end = segment.end_time / tb
                        scale = tb / self.out_time_base
                        shift = self.segment_start_in_output / self.out_time_base
                        clock_cache[tb] = (start.numerator, start.denominator, end.numerator, end.denominator,
                                           scale.numerator, scale.denominator, shift.numerator, shift.denominator)
                    sn, sd, en, ed, rn, rd, on, od = clock_cache[tb]
                    pts = frame.pts
                    if pts * sd < sn:
                        continue
                    if pts * ed >= en:
                        break
                    # Match the original three int(Fraction) truncations,
                    # including negative timestamps and non-integer scales.
                    pts = truncate(pts * sd - sn, sd)
                    pts = truncate(pts * rn, rd)
                    pts = truncate(pts * od + on, od)
                    frame.pts = max(pts, self.enc_last_pts + 1)
                    self.enc_last_pts = frame.pts
                    frame.time_base = self.out_time_base
                    frame.pict_type = av.video.frame.PictureType.NONE
                    packets.extend(self.enc_codec.encode(frame))
                return packets

            def _fix_packet_timestamps(self, packet):
                super()._fix_packet_timestamps(packet)
                # PyAV's delayed encoder packets can have duration=0. MP4 then
                # marks the final frame discard even though the packet exists.
                # Set the true adopted frame duration, including VFR input.
                if packet.pts is not None and not packet.duration:
                    duration = self.frame_durations.get(packet.pts)
                    if duration is None:
                        raise RuntimeError('编码视频帧无法对齐采用时间线，已停止导出')
                    packet.duration = duration

        written_frames = 0
        if use_process:
            audio_job = AudioProcess(input_video, output_video, ranges, media.start_time, segments,
                                     len(media.audio_tracks), fade_ms, applied_audio_encoder, audio_coder)
            audio_job.start()
            log('音视频独立进程并行：每条音轨保持一条连续样本时间线。')
        with av.open(str(audio_job.video_path if audio_job is not None else output_video), 'w', options={'movflags': '+faststart'}
                     if output_video.suffix.lower() in ('.mp4', '.mov') else {}) as output:
            output.metadata.update(media.av_container.metadata)
            output.metadata['ENCODED_BY'] = 'video-editor / smartcut 1.7'
            video_cutter = CourseVideoCutter(media, output,
                VideoSettings(VideoExportMode.SMARTCUT, VideoExportQuality.NORMAL), 'error')
            class CountedDecoder:
                def __init__(self, context):
                    self.context = context
                    self.frame_count = 0
                    self.seconds = 0.0

                def decode(self, packet):
                    started = time.perf_counter()
                    try:
                        frames = self.context.decode(packet)
                        self.frame_count += len(frames)
                        return frames
                    finally:
                        self.seconds += time.perf_counter() - started

                def flush_buffers(self):
                    started = time.perf_counter()
                    try:
                        self.context.flush_buffers()
                    finally:
                        self.seconds += time.perf_counter() - started

            decoder = CountedDecoder(create_nvdec_context(av, codec, media.video_stream.codec_context)
                                     if decoder_mode == 'nvdec' else video_cutter.decoder)
            video_cutter.decoder = decoder
            if decoder_mode == 'nvdec':
                gpu_decoder = decoder
            video_cutter.frame_durations = {}
            timeline = Fraction(0)
            for start, end in ranges:
                i = int(np.searchsorted(media.video_frame_times, start + media.start_time))
                j = int(np.searchsorted(media.video_frame_times, end + media.start_time))
                for frame_i in range(i, j):
                    t = media.video_frame_times[frame_i] - media.start_time
                    next_t = (media.video_frame_times[frame_i + 1] - media.start_time
                              if frame_i + 1 < len(media.video_frame_times) else media.duration)
                    pts = int((t - start + timeline) / video_cutter.out_time_base)
                    video_cutter.frame_durations[pts] = max(1, round((min(end, next_t) - t) / video_cutter.out_time_base))
                timeline += end - start
            expected_frames = len(video_cutter.frame_durations)
            if audio_job is None:
                for i in range(len(media.audio_tracks)):
                    audio_generators.append(ContinuousAudio(av, np, input_video, i, output,
                                                            ranges, media.start_time, fade_ms, audio_coder, applied_audio_encoder))
                log('连续音频编码器：' + ', '.join(g.encoder_name for g in audio_generators))
            output.start_encoding()
            if any('aac_coder' in generator.output.codec_context.options for generator in audio_generators):
                raise RuntimeError('音频编码器未采用 AAC 算法参数，已停止导出')
            stage_times = {'video_decode_encode_s': 0.0, 'audio_process_encode_s': 0.0,
                           'mux_packets_s': 0.0, 'audio_wait_s': 0.0}

            def mux_packet(packet):
                mux_started = time.perf_counter()
                output.mux(packet)
                stage_times['mux_packets_s'] += time.perf_counter() - mux_started

            def audio_packets(iterator):
                iterator = iter(iterator)
                while True:
                    audio_started = time.perf_counter()
                    try:
                        packet = next(iterator)
                    except StopIteration:
                        stage_times['audio_process_encode_s'] += time.perf_counter() - audio_started
                        return
                    stage_times['audio_process_encode_s'] += time.perf_counter() - audio_started
                    yield packet

            def prepare_audio(segment=None):
                packets = []
                for generator in audio_generators:
                    packets.extend(audio_packets(generator.finish() if segment is None
                                                 else generator.segment(segment)))
                return packets

            processed_time = Fraction(0)
            next_log = time.perf_counter() + 15
            pipeline_started = time.perf_counter()
            # Only the worker owns audio encoders; only the main thread muxes.
            # Prefetch at most the following segment, keeping memory bounded.
            # Native codecs release the GIL; the audio worker can start its next
            # segment before video/mux finishes the current one.
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix='course-audio') as audio_worker:
                audio_future = audio_worker.submit(prepare_audio, segments[0])
                for segment_index, segment in enumerate(segments):
                    if audio_job is not None:
                        audio_job.check()
                    next_audio_future = audio_worker.submit(
                        prepare_audio, segments[segment_index + 1]
                        if segment_index + 1 < len(segments) else None)
                    video_started = time.perf_counter()
                    packets = video_cutter.segment(segment)
                    stage_times['video_decode_encode_s'] += time.perf_counter() - video_started
                    for packet in packets:
                        mux_packet(packet)
                        written_frames += packet.pts is not None
                    wait_started = time.perf_counter()
                    audio_result = audio_future.result()
                    stage_times['audio_wait_s'] += time.perf_counter() - wait_started
                    for packet in audio_result:
                        mux_packet(packet)
                    audio_future = next_audio_future
                    processed_time += segment.end_time - segment.start_time
                    if time.perf_counter() >= next_log:
                        log(f'智能渲染进度：{float(processed_time / scheduled) * 100:.1f}%')
                        next_log = time.perf_counter() + 15
                video_started = time.perf_counter()
                packets = video_cutter.finish()
                stage_times['video_decode_encode_s'] += time.perf_counter() - video_started
                for packet in packets:
                    mux_packet(packet)
                    written_frames += packet.pts is not None
                wait_started = time.perf_counter()
                audio_result = audio_future.result()
                stage_times['audio_wait_s'] += time.perf_counter() - wait_started
                for packet in audio_result:
                    mux_packet(packet)
            pipeline_s = time.perf_counter() - pipeline_started
        if written_frames != expected_frames:
            raise RuntimeError(f'视频帧数偏离计划：应为 {expected_frames}，实际 {written_frames}；未交付成片')
        if audio_job is not None:
            process_stats = audio_job.collect()
            pipeline_s = time.perf_counter() - audio_job.started
            process_stats['final_stream_copy_mux_s'] = audio_job.mux(output_video)
        result = {
            'renderer': 'smartcut', 'smartcut_version': '1.7', 'pyav_version': av.__version__,
            'audio_mode': 'continuous_aac', 'crossfade_ms_applied': fade_ms,
            'requested_interval_count': len(intervals), 'adopted_interval_count': len(ranges),
            'adopted_kept_intervals': [(float(s * 1000), float(e * 1000)) for s, e in ranges],
            'adopted_duration_s': float(scheduled), 'index_elapsed_s': index_s,
            'index_backend': 'c_startcode_scan', 'video_timestamp_math': 'integer_exact',
            'planned_recode_video_frames': recode_frames,
            'source_video_frames': len(media.video_frame_times),
            'written_video_frames': written_frames,
            'requested_video_encoder': video_encoder, 'video_encoders_used': sorted(encoders_used),
            'encoder_input_pixel_formats': sorted(encoder_pixel_formats),
            'gpu_accelerated': any('nvenc' in name for name in encoders_used),
            'gpu_decode_accelerated': gpu_decoder is not None and gpu_decoder.frame_count > 0,
            'gpu_decoded_video_frames': gpu_decoder.frame_count if gpu_decoder is not None else 0,
            'encoder_selection_reason': encoder_reason, 'encoder_probe_elapsed_s': encoder_probe_s,
            'requested_video_decoder': video_decoder, 'decoder_selection_reason': decoder_reason,
            'decoder_probe_elapsed_s': decoder_probe_s, 'audio_coder': audio_coder,
            'requested_audio_encoder': audio_encoder,
            'audio_encoders_used': [g.encoder_name for g in audio_generators],
            'audio_encoder_selection_reasons': [g.encoder_reason for g in audio_generators],
            'audio_encoder_probe_elapsed_s': sum(g.encoder_probe_s for g in audio_generators),
            'audio_encoder_flush_padding_samples': [g.encoder_flush_padding_samples for g in audio_generators],
            'video_preset': ('p4' if encoder_mode == 'nvenc' else 'veryfast') if codec in ('h264', 'hevc') else 'codec_default',
            'video_decode_backend': decoder_mode if encoders_used else 'not_used', **stage_times,
            'video_decode_s': decoder.seconds, 'video_encode_s': video_cutter.encode_seconds,
            'video_encoder_initializations': video_cutter.encoder_initializations,
            'nvenc_surfaces': 32 if any('nvenc' in name for name in encoders_used) else None,
            'export_pipeline': 'process_audio_video' if audio_job is not None else 'parallel_audio_video',
            'requested_audio_pipeline': audio_pipeline, 'pipeline_elapsed_s': pipeline_s,
            'audio_stage_detail_s': {key: sum(g.timings[key] for g in audio_generators)
                                     for key in ('audio_decode_resample_s', 'audio_pcm_edit_s', 'audio_frame_build_s', 'audio_encode_s')},
            'audio_samples_written': [g.samples_written for g in audio_generators],
            'audio_padded_samples': [g.padded_samples for g in audio_generators],
            'render_elapsed_s': time.perf_counter() - started,
            **process_stats,
        }
        if stats is not None:
            stats.update(result)
        return result
    except (ValueError, RuntimeError):
        raise
    except Exception as exc:
        raise RuntimeError(f"smartcut 智能渲染失败：{exc}。未自动改为全片重编码。") from exc
    finally:
        if audio_job is not None:
            audio_job.close()
        for generator in audio_generators:
            generator.close()
        if media is not None:
            media.av_container.close()
        if video_cutter is not None:
            video_cutter.input_av_container.close()
