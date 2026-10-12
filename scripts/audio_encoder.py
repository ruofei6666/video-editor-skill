"""Select continuous AAC implementations by actual encoding and sync probes."""
from fractions import Fraction
import os
import time

from common import log


def mf_flush_frame(av, np, rate, layout, channels, pts):
    # Media Foundation needs look-ahead input to emit its final real block.
    # Two AAC blocks also cover an incomplete final input block. This is codec
    # padding, not source silence; discard packets beyond the real timeline.
    frame = av.AudioFrame.from_ndarray(np.zeros((channels, 2048), dtype=np.float32),
                                      format='fltp', layout=layout)
    frame.sample_rate = rate
    frame.time_base = Fraction(1, rate)
    frame.pts = pts
    return frame


def probe_mf(av, np, reference):
    rate, layout = reference.sample_rate, reference.layout.name
    channels = len(reference.layout.channels)
    if channels not in (1, 2) or rate not in (44100, 48000):
        raise ValueError('当前 MF 验收范围为 44.1/48kHz 单声道或双声道')
    context = av.CodecContext.create('aac_mf', 'w')
    context.sample_rate = rate
    context.layout = layout
    context.format = 's16'
    context.bit_rate = 192000
    context.time_base = Fraction(1, rate)
    context.open()
    rng = np.random.default_rng(43)
    noise = rng.uniform(-.15, .15, (channels, 4609))
    # Use a speech-band probe: AAC implementations have different bandwidth
    # limits on full-band white noise. This probe checks time/tail correctness,
    # not equivalence of perceptual quality or encoder frequency response.
    x = np.arange(-32, 33)
    kernel = np.sinc(2 * min(10000 / rate, .25) * x) * np.hanning(65)
    kernel /= kernel.sum()
    values = np.stack([np.convolve(ch, kernel, mode='same') for ch in noise]).astype(np.float32)
    frame = av.AudioFrame.from_ndarray(values, format='fltp', layout=layout)
    frame.sample_rate = rate
    frame.time_base = Fraction(1, rate)
    frame.pts = 0
    packets = list(context.encode(frame))
    packets.extend(context.encode(mf_flush_frame(av, np, rate, layout, channels, values.shape[1])))
    packets.extend(context.encode(None))
    decoder = av.CodecContext.create('aac', 'r')
    decoder.extradata = context.extradata
    decoded = []
    for packet in packets:
        decoded.extend(decoder.decode(packet))
    decoded.extend(decoder.decode(None))
    if not decoded:
        raise RuntimeError('MF 真实探测未产生可解码音频')
    actual = np.concatenate([f.to_ndarray() for f in decoded], axis=1)
    if actual.shape[1] < values.shape[1]:
        raise RuntimeError('MF EOF 未保留全部真实样本')
    # Check both the beginning and the incomplete final AAC block. MF's
    # packets have no priming skip metadata, so do not assume encoder delay.
    for channel in range(channels):
        for start in (512, 3500):
            reference_values = values[channel, start:start + 1024]
            candidates = [float(np.dot(reference_values, actual[channel, start + lag:start + lag + 1024]))
                          for lag in range(-8, 9)]
            if abs(int(np.argmax(candidates)) - 8) > 2:
                raise RuntimeError('MF 编码探测有音频时间偏移')
            if float(np.corrcoef(reference_values, actual[channel, start:start + 1024])[0, 1]) < .9:
                raise RuntimeError('MF 编码探测的音频内容不符')


def choose_audio_encoder(av, np, reference, requested, audio_coder='fast'):
    if requested not in ('auto', 'aac', 'mf'):
        raise ValueError('audio-encoder 必须为 auto、aac 或 mf')
    if requested == 'aac' or (requested == 'auto' and (os.name != 'nt' or audio_coder == 'twoloop')):
        return 'aac', '选择 FFmpeg 原生 AAC', 0.0
    if requested == 'mf' and audio_coder == 'twoloop':
        raise ValueError('MF 不支持 aac_coder twoloop；请选择 --audio-encoder aac')
    started = time.perf_counter()
    try:
        if 'aac_mf' not in av.codecs_available:
            raise RuntimeError('当前 PyAV 没有 aac_mf')
        probe_mf(av, np, reference)
        return 'aac_mf', 'MF 真实编码、解码、起始及尾部同步探测成功', time.perf_counter() - started
    except Exception as exc:
        if requested == 'mf':
            raise RuntimeError(f'请求的 MF AAC 不可用：{exc}；未切换原生 AAC') from exc
        reason = f'MF 探测未通过，使用原生 AAC：{exc}'
        log(reason)
        return 'aac', reason, time.perf_counter() - started
