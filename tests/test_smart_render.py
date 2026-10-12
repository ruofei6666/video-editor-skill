"""Real media checks for content, many seams, continuous audio and safe failures."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import video_assembler as assembler
import smart_renderer
import gpu_encoder
import audio_encoder


READY = (shutil.which('ffmpeg') and shutil.which('ffprobe')
         and importlib.util.find_spec('smartcut') and importlib.util.find_spec('av'))


@unittest.skipUnless(READY, '需要 FFmpeg 和已安装的 smartcut')
class SmartRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import av
        import numpy as np
        cls.av, cls.np = av, np
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.source = cls.root / '中文 source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'testsrc2=size=160x90:rate=30:duration=40', '-f', 'lavfi', '-i',
            'anoisesrc=sample_rate=48000:duration=40:amplitude=0.15:seed=42',
            '-f', 'lavfi', '-i', 'sine=frequency=997:sample_rate=48000:duration=40',
            '-map', '0:v', '-map', '1:a', '-map', '2:a', '-c:v', 'libx264',
            '-g', '60', '-keyint_min', '60', '-sc_threshold', '0', '-c:a', 'aac',
            '-b:a', '192k', str(cls.source)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def audio(cls, path, track=0):
        with cls.av.open(str(path)) as source:
            chunks = [f.to_ndarray()[0] for f in source.decode(audio=track)]
        return cls.np.concatenate(chunks)

    def render(self, name, intervals, fade=15, encoder='auto', decoder='auto', audio_coder='fast',
               audio_backend='auto', audio_pipeline='auto'):
        out = self.root / name
        stats = {}
        assembler.assemble_video_smartcut(self.source, intervals, out,
                                         crossfade_ms=fade, stats=stats, video_encoder=encoder,
                                         video_decoder=decoder, audio_coder=audio_coder,
                                         audio_encoder=audio_backend, audio_pipeline=audio_pipeline)
        subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(out),
                        '-f', 'null', '-'], check=True, capture_output=True)
        return out, stats

    def test_many_seams_have_no_cumulative_audio_shift(self):
        intervals = [(i * 500 + 70, i * 500 + 440) for i in range(75)]
        out, stats = self.render('many.mp4', intervals, fade=0)
        original = self.audio(self.source)
        actual = self.audio(out)
        pieces = [original[round(s * 48):round(e * 48)]
                  for s, e in stats['adopted_kept_intervals']]
        expected = self.np.concatenate(pieces)
        self.assertLessEqual(abs(len(actual) - len(expected)), 1024)
        self.assertEqual(stats['audio_samples_written'], [len(expected), len(expected)])
        cursor = 0
        # Cross-correlate independently after early, middle and final seams.
        # Random source sound makes a missing/duplicated AAC packet detectable.
        for i, piece in enumerate(pieces):
            if i in (0, 1, 20, 40, 73, 74):
                offset = cursor + 4000
                reference = expected[offset:offset + 2048]
                correlations = [self.np.dot(reference, actual[offset + lag:offset + lag + 2048])
                                for lag in range(-128, 129)]
                lag = int(self.np.argmax(correlations)) - 128
                self.assertLessEqual(abs(lag), 2, f'第 {i} 个接缝偏移 {lag} 样本')
                coefficient = float(self.np.corrcoef(reference, actual[offset:offset + 2048])[0, 1])
                self.assertGreater(coefficient, .9)
            cursor += len(piece)
        with self.av.open(str(out)) as media:
            self.assertEqual(len(media.streams.audio), 2)

    def test_microfade_at_edits_but_not_internal_gop_boundaries(self):
        out, stats = self.render('fade.mp4', [(0, 8000), (10110, 12590)])
        audio = self.audio(out, 1)
        source = self.audio(self.source, 1)
        np = self.np
        rms = lambda x: float(np.sqrt(np.mean(x * x)))
        seam = round(stats['adopted_kept_intervals'][0][1] * 48)
        # 5 ms next to an edit must fade; an untouched GOP join remains loud.
        self.assertLess(rms(audio[seam - 240:seam]), rms(source[seam - 240:seam]) * .65)
        self.assertGreater(rms(audio[96000 - 240:96000 + 240]), rms(source[96000 - 240:96000 + 240]) * .85)
        self.assertEqual(stats['crossfade_ms_applied'], 15)

    def test_short_keep_is_preserved_and_overlapping_frames_merge(self):
        out, stats = self.render('short.mp4', [(105, 110), (115, 120), (4111, 4333)])
        self.assertEqual(stats['adopted_interval_count'], 2)
        self.assertLessEqual(stats['adopted_kept_intervals'][0][0], 105)
        self.assertGreaterEqual(stats['adopted_kept_intervals'][0][1], 120)
        self.assertAlmostEqual(stats['actual_duration_s'], stats['adopted_duration_s'], delta=.04)

    def test_copied_video_packets_are_unchanged(self):
        out, stats = self.render('copy-gop.mp4', [(0, 4000)], fade=0)
        def packets(path, end=None):
            with self.av.open(str(path)) as media:
                result = []
                for p in media.demux(video=0):
                    if p.pts is None or (end is not None and float(p.pts * p.time_base) >= end):
                        continue
                    data = bytes(p)
                    pos = 0
                    slices = []
                    while pos < len(data):
                        n = int.from_bytes(data[pos:pos + 4], 'big')
                        nal = data[pos + 4:pos + 4 + n]
                        if nal and nal[0] & 31 in (1, 5):
                            slices.append(nal)
                        pos += 4 + n
                    result.append(slices)
                return result
        self.assertEqual(packets(self.source, 4), packets(out))
        self.assertEqual(stats['planned_recode_video_frames'], 0)

    def test_non_keyframe_video_content_matches_source(self):
        out, stats = self.render('content.mp4', [(1550, 2550), (9150, 9550)])
        def images(path):
            with self.av.open(str(path)) as media:
                return [(float(f.pts * f.time_base), f.to_ndarray(format='rgb24').astype('float32'))
                        for f in media.decode(video=0)]
        selected = [image for t, image in images(self.source)
                    if any(s / 1000 - 1e-6 <= t < e / 1000 - 1e-6
                           for s, e in stats['adopted_kept_intervals'])]
        exported = [image for t, image in images(out)]
        self.assertEqual(len(selected), len(exported))
        for before, after in zip(selected, exported):
            self.assertLess(float(self.np.mean(self.np.abs(before - after))), 8)

    def test_backend_failure_preserves_existing_output_without_fallback(self):
        out = self.root / 'previous.mp4'
        out.write_bytes(b'previous successful export')
        with patch.object(smart_renderer, 'load_backend', side_effect=RuntimeError('缺少依赖')), \
             patch.object(assembler, 'assemble_video_encode', side_effect=AssertionError('不允许自动重编码')):
            with self.assertRaisesRegex(RuntimeError, '缺少依赖'):
                assembler.assemble_video_smartcut(self.source, [(0, 2000)], out, overwrite=True)
        self.assertEqual(out.read_bytes(), b'previous successful export')

    def test_variable_frame_rate_preserves_selected_frames_and_last_frame(self):
        source = self.root / 'variable.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.source), '-t', '16',
            '-map', '0:v', '-map', '0:a', '-vf', "select='if(lt(t,10),1,not(mod(n,3)))'",
            '-fps_mode', 'vfr', '-c:v', 'libx264', '-c:a', 'copy', str(source)],
            check=True, capture_output=True)
        out = self.root / 'variable-cut.mp4'
        stats = {}
        assembler.assemble_video_smartcut(source, [(9210, 11130), (12710, 13240)], out,
                                         stats=stats, audio_pipeline='process')
        def selected_images(path, ranges=None):
            with self.av.open(str(path)) as media:
                result = []
                for frame in media.decode(video=0):
                    t = float(frame.pts * frame.time_base)
                    if ranges is None or any(s / 1000 - 1e-6 <= t < e / 1000 - 1e-6 for s, e in ranges):
                        result.append(frame.to_ndarray(format='rgb24').astype('float32'))
                return result
        before = selected_images(source, stats['adopted_kept_intervals'])
        after = selected_images(out)
        self.assertEqual(len(before), len(after))
        for a, b in zip(before, after):
            self.assertLess(float(self.np.mean(self.np.abs(a - b))), 8)

    def test_explicit_nvenc_mixes_original_gops_with_gpu_encoded_edges(self):
        with self.av.open(str(self.source)) as source:
            try:
                gpu_encoder.probe_nvenc(self.av, 'h264', source.streams.video[0].codec_context)
            except Exception as exc:
                self.skipTest(f'本机 NVENC 不可用：{exc}')
        out, stats = self.render('gpu-mixed.mp4', [(170, 7700), (10170, 17700)], encoder='nvenc')
        self.assertTrue(stats['gpu_accelerated'])
        self.assertEqual(stats['video_encoders_used'], ['h264_nvenc'])
        if stats['video_decode_backend'] == 'nvdec':
            self.assertTrue(stats['gpu_decode_accelerated'])
            self.assertGreater(stats['gpu_decoded_video_frames'], 0)
        self.assertGreater(stats['written_video_frames'], stats['planned_recode_video_frames'])
        with self.av.open(str(self.source)) as source:
            before = [frame.to_ndarray(format='rgb24').astype('float32')
                      for frame in source.decode(video=0)
                      if any(s / 1000 - 1e-6 <= float(frame.pts * frame.time_base) < e / 1000 - 1e-6
                             for s, e in stats['adopted_kept_intervals'])]
        with self.av.open(str(out)) as output:
            after = [frame.to_ndarray(format='rgb24').astype('float32') for frame in output.decode(video=0)]
        self.assertEqual(len(before), len(after))
        for original, edited in zip(before, after):
            self.assertLess(float(self.np.mean(self.np.abs(original - edited))), 8)

    def test_explicit_gpu_failure_preserves_output_and_auto_explains_cpu_selection(self):
        out = self.root / 'gpu-unavailable.mp4'
        out.write_bytes(b'previous GPU export')
        with patch.object(gpu_encoder, 'probe_nvenc', side_effect=RuntimeError('测试用 GPU 不可用')):
            with self.av.open(str(self.source)) as source:
                mode, reason = gpu_encoder.choose_video_encoder(self.av, 'h264',
                    source.streams.video[0].codec_context, 'auto')
                self.assertEqual(mode, 'cpu')
                self.assertIn('测试用 GPU 不可用', reason)
            with self.assertRaisesRegex(RuntimeError, '未切换到 CPU'):
                assembler.assemble_video_smartcut(self.source, [(0, 1500)], out,
                    overwrite=True, video_encoder='nvenc')
        self.assertEqual(out.read_bytes(), b'previous GPU export')

    def test_nvenc_buffering_preserves_video_packets_and_flushes_before_copy(self):
        with self.av.open(str(self.source)) as media:
            try:
                gpu_encoder.probe_nvenc(self.av, 'h264', media.streams.video[0].codec_context)
            except Exception as exc:
                self.skipTest(f'本机 NVENC 不可用：{exc}')
        # Short cuts delay packets inside NVENC; an untouched GOP must be muxed
        # only after those packets, and EOF must retain the final frame.
        intervals = [(i * 500 + 70, i * 500 + 440) for i in range(20)] + [(12000, 18000)]
        original_create = smart_renderer.create_nvenc_context
        def previous_buffer(*args, **kwargs):
            context = original_create(*args, **kwargs)
            context.options = {k:v for k,v in context.options.items() if k != 'surfaces'}
            return context
        with patch.object(smart_renderer, 'create_nvenc_context', previous_buffer):
            old_out, old = self.render('previous-nvenc-buffer.mp4', intervals, encoder='nvenc')
        new_out, new = self.render('larger-nvenc-buffer.mp4', intervals, encoder='nvenc')
        def packets(path):
            with self.av.open(str(path)) as media:
                return [(p.pts, p.dts, p.duration, bytes(p)) for p in media.demux(video=0) if p.pts is not None]
        self.assertEqual(packets(old_out), packets(new_out))
        self.assertEqual(old['adopted_kept_intervals'], new['adopted_kept_intervals'])
        self.assertEqual(old['written_video_frames'], new['written_video_frames'])
        self.assertEqual(new['nvenc_surfaces'], 32)

    def test_explicit_cpu_mode_is_still_available(self):
        out, stats = self.render('cpu.mp4', [(170, 2770)], encoder='cpu')
        self.assertFalse(stats['gpu_accelerated'])
        self.assertEqual(stats['video_encoders_used'], ['libx264'])

    def test_strict_gpu_decode_failure_preserves_existing_export(self):
        out = self.root / 'nvdec-unavailable.mp4'
        out.write_bytes(b'previous valid export')
        with patch.object(gpu_encoder, 'create_nvdec_context', side_effect=RuntimeError('测试用 NVDEC 不可用')):
            with self.assertRaisesRegex(RuntimeError, '未切换到 CPU'):
                assembler.assemble_video_smartcut(self.source, [(0, 1500)], out,
                    overwrite=True, video_encoder='cpu', video_decoder='nvdec')
        self.assertEqual(out.read_bytes(), b'previous valid export')

    def test_audio_worker_failure_preserves_previous_output(self):
        out = self.root / 'audio-worker-failed.mp4'
        out.write_bytes(b'previous valid export')
        with patch.object(smart_renderer.ContinuousAudio, 'segment',
                          side_effect=RuntimeError('测试用音频工作线程错误')):
            with self.assertRaisesRegex(RuntimeError, '音频工作线程错误'):
                assembler.assemble_video_smartcut(self.source, [(170, 1500)], out,
                                                 overwrite=True, video_encoder='cpu')
        self.assertEqual(out.read_bytes(), b'previous valid export')

    def test_explicit_nvdec_still_decodes_real_frames(self):
        with self.av.open(str(self.source)) as source:
            try:
                gpu_encoder.create_nvdec_context(self.av, 'h264', source.streams.video[0].codec_context)
            except Exception as exc:
                self.skipTest(f'本机 NVDEC 不可用：{exc}')
        out, stats = self.render('explicit-nvdec.mp4', [(170, 1770)],
                                 encoder='cpu', decoder='nvdec')
        self.assertTrue(stats['gpu_decode_accelerated'])
        self.assertGreater(stats['gpu_decoded_video_frames'], 0)
        self.assertFalse(stats['gpu_accelerated'])

    def test_original_audio_algorithm_can_be_selected(self):
        out, stats = self.render('original-audio.mp4', [(170, 2770)],
                                 encoder='cpu', decoder='cpu', audio_coder='twoloop')
        self.assertEqual(stats['audio_coder'], 'twoloop')
        self.assertFalse(stats['gpu_accelerated'])

    def test_mf_preserves_incomplete_audio_tail(self):
        with self.av.open(str(self.source)) as media:
            try:
                audio_encoder.probe_mf(self.av, self.np, media.streams.audio[0].codec_context)
            except Exception as exc:
                self.skipTest(f'本机 MF 不可用：{exc}')
        out, stats = self.render('mf-tail.mp4', [(170, 3101)], fade=0, audio_backend='mf')
        self.assertEqual(stats['audio_encoders_used'], ['aac_mf', 'aac_mf'])
        self.assertEqual(stats['audio_encoder_flush_padding_samples'], [2048, 2048])
        s, e = stats['adopted_kept_intervals'][0]
        expected = self.audio(self.source)[round(s * 48):round(e * 48)]
        actual = self.audio(out)
        self.assertGreaterEqual(len(actual), len(expected))
        self.assertLessEqual(len(actual) - len(expected), 1024)
        # Tail energy and correlation expose a silently dropped final AAC block.
        offset = len(expected) - 2048
        self.assertGreater(float(self.np.corrcoef(expected[offset:], actual[offset:len(expected)])[0, 1]), .9)
        self.assertEqual(stats['audio_samples_written'][0], len(expected))

    def test_mf_stereo_preserves_both_channels_and_sync(self):
        stereo = self.root / 'stereo.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.source), '-t', '5',
            '-filter_complex', '[0:a:0][0:a:1]amerge=inputs=2[a]', '-map', '0:v', '-map', '[a]',
            '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', str(stereo)], check=True, capture_output=True)
        with self.av.open(str(stereo)) as media:
            try:
                audio_encoder.probe_mf(self.av, self.np, media.streams.audio[0].codec_context)
            except Exception as exc:
                self.skipTest(f'本机立体声 MF 不可用：{exc}')
        out = self.root / 'stereo-mf.mp4'
        stats = {}
        assembler.assemble_video_smartcut(stereo, [(1170, 2217)], out, crossfade_ms=0,
                                         audio_encoder='mf', stats=stats)
        def channels(path):
            with self.av.open(str(path)) as media:
                return self.np.concatenate([f.to_ndarray() for f in media.decode(audio=0)], axis=1)
        s, e = stats['adopted_kept_intervals'][0]
        expected = channels(stereo)[:, round(s * 48):round(e * 48)]
        actual = channels(out)
        self.assertEqual(actual.shape[0], 2)
        self.assertGreaterEqual(actual.shape[1], expected.shape[1])
        for ch in (0, 1):
            for offset in (4096, expected.shape[1] - 2048):
                self.assertGreater(float(self.np.corrcoef(expected[ch, offset:offset + 2048],
                    actual[ch, offset:offset + 2048])[0, 1]), .9)

    def test_mf_failure_is_explicit_and_auto_uses_native_aac(self):
        out = self.root / 'mf-unavailable.mp4'
        out.write_bytes(b'previous successful export')
        with patch.object(audio_encoder, 'probe_mf', side_effect=RuntimeError('测试用 MF 不可用')):
            with self.assertRaisesRegex(RuntimeError, '未切换原生 AAC'):
                assembler.assemble_video_smartcut(self.source, [(170, 1500)], out,
                    overwrite=True, audio_encoder='mf', video_encoder='cpu')
            with self.av.open(str(self.source)) as media:
                name, reason, elapsed = audio_encoder.choose_audio_encoder(self.av, self.np,
                    media.streams.audio[0].codec_context, 'auto')
            self.assertEqual(name, 'aac')
            self.assertIn('测试用 MF 不可用', reason)
        self.assertEqual(out.read_bytes(), b'previous successful export')

    def test_explicit_native_aac_keeps_previous_path_available(self):
        out, stats = self.render('native-aac.mp4', [(170, 1500)], audio_backend='aac')
        self.assertEqual(stats['audio_encoders_used'], ['aac', 'aac'])
        self.assertEqual(stats['audio_encoder_flush_padding_samples'], [0, 0])

    def test_process_pipeline_preserves_multi_seam_sync(self):
        intervals = [(i * 500 + 70, i * 500 + 440) for i in range(75)]
        out, stats = self.render('process-many.mp4', intervals, fade=0,
                                 audio_pipeline='process')
        self.assertEqual(stats['export_pipeline'], 'process_audio_video')
        original = self.audio(self.source)
        expected = self.np.concatenate([original[round(s * 48):round(e * 48)]
                                        for s, e in stats['adopted_kept_intervals']])
        actual = self.audio(out)
        self.assertEqual(stats['audio_samples_written'], [len(expected), len(expected)])
        self.assertGreaterEqual(len(actual), len(expected))
        self.assertLessEqual(len(actual) - len(expected), 1024)
        for offset in (4000, len(expected) // 2, len(expected) - 4096):
            reference = expected[offset:offset + 2048]
            dots = [self.np.dot(reference, actual[offset + lag:offset + lag + 2048])
                    for lag in range(-128, 129)]
            self.assertLessEqual(abs(int(self.np.argmax(dots)) - 128), 2)
            self.assertGreater(float(self.np.corrcoef(reference, actual[offset:offset + 2048])[0, 1]), .9)

    def test_process_failure_preserves_previous_export(self):
        out = self.root / 'audio-process-failed.mp4'
        out.write_bytes(b'previous successful export')
        with self.assertRaisesRegex(RuntimeError, '独立音频进程失败'):
            assembler.assemble_video_smartcut(self.source, [(170, 1500)], out, overwrite=True,
                audio_pipeline='process', audio_encoder='mf', audio_coder='twoloop', video_encoder='cpu')
        self.assertEqual(out.read_bytes(), b'previous successful export')

    def test_native_aac_packets_survive_process_mux_unchanged(self):
        intervals = [(170, 1570), (3170, 4470)]
        threaded, _ = self.render('aac-thread.mp4', intervals, audio_backend='aac', audio_pipeline='thread')
        processed, _ = self.render('aac-process.mp4', intervals, audio_backend='aac', audio_pipeline='process')
        def packets(path):
            with self.av.open(str(path)) as media:
                return [(p.pts, p.duration, bytes(p)) for p in media.demux(audio=0) if p.pts is not None]
        self.assertEqual(packets(threaded), packets(processed))

    def test_hevc_nvenc_mixes_copy_and_encoded_gops(self):
        source = self.root / 'hevc.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'testsrc2=size=160x96:rate=20:duration=8', '-f', 'lavfi', '-i',
            'sine=frequency=701:sample_rate=48000:duration=8', '-c:v', 'libx265',
            '-x265-params', 'keyint=40:min-keyint=40:scenecut=0:log-level=error',
            '-c:a', 'aac', str(source)], check=True, capture_output=True)
        with self.av.open(str(source)) as media:
            try:
                gpu_encoder.probe_nvenc(self.av, 'hevc', media.streams.video[0].codec_context)
            except Exception as exc:
                self.skipTest(f'本机 HEVC NVENC 不可用：{exc}')
        out = self.root / 'hevc-gpu.mp4'
        stats = {}
        assembler.assemble_video_smartcut(source, [(170, 7700)], out,
                                         stats=stats, video_encoder='nvenc')
        subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(out),
                        '-f', 'null', '-'], check=True, capture_output=True)
        self.assertTrue(stats['gpu_accelerated'])
        self.assertEqual(stats['video_encoders_used'], ['hevc_nvenc'])
        self.assertGreater(stats['written_video_frames'], stats['planned_recode_video_frames'])
        with self.av.open(str(source)) as media:
            before = [frame.to_ndarray(format='rgb24').astype('float32')
                      for frame in media.decode(video=0)
                      if any(s / 1000 - 1e-6 <= float(frame.pts * frame.time_base) < e / 1000 - 1e-6
                             for s, e in stats['adopted_kept_intervals'])]
        with self.av.open(str(out)) as media:
            after = [frame.to_ndarray(format='rgb24').astype('float32') for frame in media.decode(video=0)]
        self.assertEqual(len(before), len(after))
        for original, edited in zip(before, after):
            self.assertLess(float(self.np.mean(self.np.abs(original - edited))), 8)


if __name__ == '__main__':
    unittest.main()
