"""NVENC context creation and real encode probes, without changing drivers."""
from fractions import Fraction

from common import log


def nvenc_name(codec):
    return {'h264': 'h264_nvenc', 'hevc': 'hevc_nvenc'}.get(codec)


def nvenc_options(profile=None):
    # More input surfaces keep NVENC occupied while the next CPU frame is
    # decoded. No B-frame/lookahead/preset/quality change; flush still returns
    # every delayed packet before a copy segment or end of output.
    options = {'preset': 'p4', 'tune': 'hq', 'rc': 'vbr', 'cq': '23',
               'b': '0', 'forced-idr': '1', 'surfaces': '32'}
    if profile:
        options['profile'] = 'high444p' if profile == 'high444' else profile
    return options


def create_nvenc_context(av, codec, reference, time_base, profile=None, input_pix_fmt=None):
    name = nvenc_name(codec)
    if name is None:
        raise ValueError(f'NVENC 尚未适配视频编码 {codec}')
    context = av.CodecContext.create(name, 'w')
    context.width = reference.width
    context.height = reference.height
    # NVENC accepts planar 8-bit YUV directly. Keep the decoder's format when
    # supported instead of reformatting every CPU frame into NV12. 10-bit HEVC
    # still needs P010 when the native planar format is not accepted.
    input_pix_fmt = input_pix_fmt or reference.pix_fmt
    supported = {fmt.name for fmt in av.Codec(name, 'w').video_formats}
    context.pix_fmt = (input_pix_fmt if input_pix_fmt in supported else
                       {'yuv420p10le': 'p010le'}.get(input_pix_fmt, input_pix_fmt))
    context.time_base = time_base
    context.framerate = reference.rate or Fraction(30)
    context.bit_rate = 0
    context.max_b_frames = 0
    context.gop_size = max(1, round(context.framerate * 5))
    if reference.sample_aspect_ratio is not None:
        context.sample_aspect_ratio = reference.sample_aspect_ratio
    for key in ('color_range', 'color_primaries', 'color_trc', 'colorspace'):
        setattr(context, key, getattr(reference, key))
    # Smart rendering switches between original and newly encoded SPS/PPS.
    # Keep headers in each encoded IDR, rather than suppressing them for MP4.
    context.flags &= ~av.codec.context.Flags.global_header.value
    context.options = nvenc_options(profile)
    return context


def probe_nvenc(av, codec, reference):
    context = create_nvenc_context(av, codec, reference, Fraction(1, 30))
    context.open()
    frame = av.VideoFrame(reference.width, reference.height, context.pix_fmt)
    for plane in frame.planes:
        plane.update(bytes(plane.buffer_size))
    frame.pts = 0
    frame.time_base = context.time_base
    packets = list(context.encode(frame)) + list(context.encode(None))
    if not packets:
        raise RuntimeError('NVENC 探测未产生视频包')
    if context.options:
        raise RuntimeError(f'NVENC 未采用参数：{sorted(context.options)}')


def choose_video_encoder(av, codec, reference, requested):
    if requested not in ('auto', 'cpu', 'nvenc'):
        raise ValueError('video-encoder 必须为 auto、cpu 或 nvenc')
    if requested == 'cpu':
        return 'cpu', '显式选择 CPU'
    name = nvenc_name(codec)
    if name is None:
        if requested == 'nvenc':
            raise ValueError(f'NVENC 尚未适配视频编码 {codec}，请选择 --video-encoder cpu')
        return 'cpu', f'{codec} 未适配 NVENC'
    try:
        if name not in av.codecs_available:
            raise RuntimeError(f'当前 PyAV 未提供 {name}')
        probe_nvenc(av, codec, reference)
        return 'nvenc', 'NVENC 真实编码探测成功'
    except Exception as exc:
        if requested == 'nvenc':
            raise RuntimeError(f'请求的 GPU 编码不可用：{exc}；未切换到 CPU') from exc
        reason = f'NVENC 探测失败，使用 CPU：{exc}'
        log(reason)
        return 'cpu', reason


def create_nvdec_context(av, codec, reference):
    name = {'h264': 'h264_cuvid', 'hevc': 'hevc_cuvid'}.get(codec)
    if name is None:
        raise ValueError(f'NVDEC 尚未适配视频编码 {codec}')
    context = av.CodecContext.create(name, 'r')
    context.extradata = reference.extradata
    context.width = reference.width
    context.height = reference.height
    context.open()
    return context


def choose_video_decoder(av, codec, reference, source_path, requested, encoder_mode):
    if requested not in ('auto', 'cpu', 'nvdec'):
        raise ValueError('video-decoder 必须为 auto、cpu 或 nvdec')
    if requested in ('cpu', 'auto'):
        return 'cpu', ('显式选择 CPU 解码' if requested == 'cpu' else
                       'auto 使用 CPU 解码，避免当前 NVDEC 下载到内存再上传 NVENC 的往返开销')
    try:
        decoder = create_nvdec_context(av, codec, reference)
        decoded = False
        with av.open(str(source_path), metadata_errors='ignore') as source:
            for i, packet in enumerate(source.demux(video=0)):
                if decoder.decode(packet):
                    decoded = True
                    break
                if i >= 127:
                    break
        if not decoded:
            raise RuntimeError('NVDEC 探测未产生视频帧')
        return 'nvdec', 'NVDEC 真实解码探测成功'
    except Exception as exc:
        if requested == 'nvdec':
            raise RuntimeError(f'请求的 GPU 解码不可用：{exc}；未切换到 CPU') from exc
        reason = f'NVDEC 探测失败，使用 CPU 解码：{exc}'
        log(reason)
        return 'cpu', reason
