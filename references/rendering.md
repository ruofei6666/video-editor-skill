# 智能渲染与速度验收

默认 `smartcut` 1.7（MIT），它固定使用 PyAV 16.0.1。依赖见 `requirements-render.txt`；本技能组合其 VideoCutter 与连续音频生成器，不修改依赖源码。H.264/HEVC 默认真实探测 NVENC 并优先使用 GPU，CPU 路径仍使用 veryfast 预设；包装层为零时长编码包填写实际帧时长，防止 MP4 丢掉最后一帧。

## 模式

- 默认 `--renderer smartcut`：一次传入全部保留区间，只重编码切点附近的视频，其余视频直接复制。支持 H.264、HEVC、VP9、AV1、MPEG-2；实际输入与封装仍需本机验证。当前只接收一个视频轨道；音频各轨道独立连续编码为 192k AAC，保留采样率、声道与轨道元数据。PCM 按帧读取，不将一小时解码音频全部装进内存。smartcut 自身会索引视频并缓存压缩音频包。
- `--renderer encode`：明确选择原来的完整重编码兼容模式。使用平台编码器和分批滤镜图，速度较慢。智能渲染失败不会自动进入此模式。
- `--fast-copy`：一次 FFmpeg concat demuxer 流拷贝，不创建上千个临时文件；仅近似预览，可能残留被删内容。没有音频微淡化，不用于确认口误准确删除。

现有 `schema_version=1` 计划可继续使用：未设置 renderer 的旧计划采用 smartcut。`review`、`render` 不调用 ASR，也不读取 Key；不需重新分析即可测试导出。

## GPU 模式

`--video-encoder auto` 为默认值。H.264/HEVC 使用与实际输入尺寸、像素格式对应的 NVENC 上下文做一帧真实编码探测；成功后使用 `h264_nvenc` 或 `hevc_nvenc`。探测失败时 auto 会记录原因并选 CPU。`--video-encoder nvenc` 是严格 GPU 模式，探测或执行失败即停止，不自动切换 CPU；`--video-encoder cpu` 是显式 CPU 模式。旧计划未设置此字段时也采用 auto。

GPU 编码参数为 P4/HQ、VBR CQ 23、bitrate 0、无 B 帧。默认 `--video-decoder auto` 使用 CPU 解码；当前 `h264_cuvid`/`hevc_cuvid` 路径先下载为 NV12/P010，再上传编码器，本机课程实测慢于 CPU 解码。可显式使用 `--video-decoder nvdec` 重新测试其他输入和设备，该严格请求不可回退 CPU。`--video-decoder cpu` 明确使用 CPU。此路径不是零拷贝管线；源片索引、音频和文件写入仍在 CPU/磁盘。显式 `--renderer encode` 为原兼容模式，不接受严格 nvenc 请求。

NVENC 使用 `surfaces=32` 增加输入帧并发缓冲，减少逐帧编码调用的等待。未改变 B 帧、lookahead、预设或质量值。较深缓冲会延迟输出包，保留 smartcut 在复制段前及 EOF 的 flush，之后才调整包时间戳与封装；实际编码包须与旧缓冲路径对照。报告 `nvenc_surfaces` 仅在确实使用 NVENC 时记录 32，否则为 null。

NVENC 独立编码上下文保留 IDR 内的 SPS/PPS/VPS，复制段通过原 smartcut 位流滤镜恢复原参数集；混合段必须通过实际解码和画面对应测试。当前 FFmpeg 8/PyAV 16 NVENC 并未采用 `sps-id` 参数，不能用一个未生效的参数声称解决了参数集衔接。最后一帧时长修正继续保留。

实际报告字段：`video_encoders_used`、`gpu_accelerated`、`encoder_selection_reason`、`encoder_probe_elapsed_s`、`gpu_decode_accelerated`、`gpu_decoded_video_frames`、`video_decode_backend`、`video_decode_encode_s`、`video_decode_s`、`video_encode_s`、`audio_process_encode_s`、`audio_stage_detail_s`、`audio_wait_s`、`pipeline_elapsed_s`、`mux_packets_s`。视频总阶段计时包含解码和编码，细分计时直接包围实际 codec 调用。`audio_stage_detail_s` 分为解码/重采样、淡化、构建音频帧、AAC 编码；切片拷贝和 Python 调度等余量包含在音频总阶段。NVENC CQ 23 与 CPU CRF 23 并非同画质保证，因此性能对比还要说明编码器发生变化。

线程管线让音频与视频采用两个工作线程并行：提交当前片段的连续音频编码，至多预取下一个片段；主线程同时处理视频，再收取结果统一 mux。缓存仅覆盖当前与下一段的音频包，不创建上千个临时文件，不缓存全课程 PCM，不允许多个线程同时操作同一编码器或 muxer。音频线程错误会传回主线程，失败仍保留已有成片。`video_decode_encode_s` 与 `audio_process_encode_s` 存在重叠，不能相加；总时间应读取 `export_elapsed_s`，纯处理墙钟时间读取 `pipeline_elapsed_s`。

新版默认 `--audio-pipeline auto`：采用时长至少 60 秒时，音频在一个独立 Python 进程中编码，视频保持 smartcut 路径，整片仅生成音频和视频两条临时媒体流，最后用一次 FFmpeg 流拷贝合并。短片继续用上述线程管线，避免进程启动和额外封装开销。`--audio-pipeline process` / `thread` 可明确选择；进程运行失败会停止导出并保留已有成片。`final_stream_copy_mux_s` 记录额外合并耗时，`audio_worker_elapsed_s` 记录音频进程实际处理耗时。

NVENC 支持原像素格式时直接接收它，8-bit CPU 解码帧无需先转为 NV12；NVDEC 仍以 NV12/P010 传入，10-bit 输入按编码器支持情况转为 P010。`encoder_input_pixel_formats` 记录实际编码格式。H.264/HEVC 重编码的 PTS 用整数分子和分母进行原有三次截断运算，保持时间戳行为。H.264 索引把逐字节 Python 循环换成 C 的 bytes.find，保持 smartcut 1.7 的 NAL 分类，临时替换仅包围索引构建并始终恢复；不修改依赖文件。

依赖原有 NVIDIA 驱动与 PyAV 自带 FFmpeg，无需为此安装 CUDA Toolkit。本机已真实探测 RTX 3050 Laptop GPU 的 H.264/HEVC NVENC；其他设备仍以运行探测为准。

来源：[NVIDIA FFmpeg 参数文档](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/ffmpeg-with-nvidia-gpu/index.html)、[FFmpeg NVENC 参数集写入](https://ffmpeg.org/doxygen/8.0/nvenc_8c_source.html)。

## 音频性能

默认 `--audio-coder fast`，保持 192k AAC、原采样率与声道、连续样本时间戳及 15ms 淡化。原 twoloop 编码的课程样本音频阶段实测 109.348s，因此音频也需要单独优化。fast 是 FFmpeg 原生 AAC 的另一种算法，不是 GPU 音频编码；改变算法后不能宣称音频位流或听感与原编码完全相同。多接缝相关性、微淡化和同步测试仍须通过。`--audio-coder twoloop` 可恢复原算法。

新版 `--audio-encoder auto` 在独立进程的 Windows 路径上探测 Media Foundation `aac_mf`，当前验收范围为 44.1/48kHz 单/双声道。其他平台、格式或探测未通过时说明原因并选择原生 `aac`。短片的 auto 线程路径保留原生 AAC，因为 MF 在共享线程的完整课程测试中更慢。`--audio-encoder mf` 为严格请求，失败即停止；`--audio-encoder aac` 保留原生编码器。`--audio-coder twoloop` 在 auto 下选择原生 AAC，与严格 mf 同时指定会报错。

MF 接受 16-bit PCM，保留 192k AAC、采样率和声道，编码实现和频率响应与原生 AAC 不同；不能据相同码率宣称同位流或同听感。探测使用语音频带随机信号检查起始、末尾和时间偏移；实际媒体验收仍检查真实素材与多接缝，不以探测替代质量检查。MF 的 EOF 需要额外两块 AAC 静音输入来吐出最后真实样本，包装层丢弃采用时间线之外的包并裁定最后包时长。该内部 look-ahead padding 单独记录为 `audio_encoder_flush_padding_samples`，不计入源缺口补静音或真实输出样本数。

恢复上一轮的音频处理：`--audio-encoder aac --audio-pipeline thread --audio-coder fast`。新增报告字段包括 `audio_encoders_used`、`audio_encoder_selection_reasons`、`audio_encoder_probe_elapsed_s`、`audio_encoder_flush_padding_samples` 和 `requested_audio_pipeline`。

复现原 CPU 编解码与音频算法使用 `--video-encoder cpu --video-decoder cpu --audio-encoder aac --audio-pipeline thread --audio-coder twoloop`；比较各轮组合时，明确区分视频解码、视频编码、音频算法与并行方式的变化。当前默认为 CPU 解码加可用的 NVENC 编码。

来源：[FFmpeg AAC 编码算法](https://ffmpeg.org/ffmpeg-codecs.html#aac)、[微软 AAC Encoder](https://learn.microsoft.com/en-us/windows/win32/medfound/aac-encoder)、[FFmpeg Media Foundation 编码器源码](https://ffmpeg.org/doxygen/8.0/mfenc_8c_source.html)。

## 切点与同步

视频按实际帧时间定位：保留区间的开始向前、结束向后对齐帧边界，以免量化吞掉保留话语。重叠区间合并，避免重复写同一帧。因此不足一帧的删除可能不执行，报告同时保留原计划时长和采用的源视频区间。

音频随采用的视频时间线按样本剪切。每条音轨仅一个连续 AAC 编码器，按累计输出时间设置 PTS；不逐片段舍入 AAC 包。15ms 微淡化只施加在保留区间首尾，不施加在中间未剪开的 GOP 边界。短片段的淡化自动缩短；真实音频缺口补静音并记录样本数。含字幕轨道的源片先提取字幕，再在成片阶段处理字幕，避免原字幕时间线误用。

## 性能验收

“一小时视频十秒导出”是目标，不是保证。索引、视频边界重编码、连续 AAC 编码和磁盘写入均有耗时；关键帧组很长、切点很密时，大部分保留视频可能需要重编码。报告中的 `planned_recode_video_frames` 是计划统计，HEVC CRA 等路径可能还需要额外处理。

使用已有真实计划做验收；单纯测试导出不重新调用收费 ASR：

```powershell
& .\scripts\run.ps1 render 'D:\课程\视频.mp4' -o 'D:\课程\视频_smartcut.mp4'
```

CLI JSON 与审计报告记录 `export_elapsed_s`，该数值不含转录与语义判断。对长视频和上千切点，复核开始、中间、结尾的内容对应与音画同步；不能只看总时长。性能测试生成的规则区间须标成测试素材，不作为真正完成课程语义剪辑的证据。

维护时用虚拟环境 Python 运行 `-m unittest discover -s tests -v`。新的实际媒体测试覆盖非关键帧切点、短区间、帧边界合并、多音轨、音频多次拼接的累计偏移、微淡化和失败保护。快速语法或配置验证不能代替这些测试。

来源：[smartcut 源码](https://github.com/skeskinen/smartcut)、[FFmpeg concat 切点限制](https://ffmpeg.org/ffmpeg-formats.html#concat)。

规则切点的性能测试（不调用 ASR，不代表语义剪辑完成）：

```powershell
& .\.venv\Scripts\python.exe .\scripts\benchmark_render.py 'D:\课程\视频.mp4' --interval-count 1000 --gap-ms 350 -o 'D:\测试\课程_规则切点测试.mp4'
```

记录保存为同名 `.benchmark.json`，包含原始时长、采用的区间、导出墙钟耗时与计划重编码帧数。

## 发布版本的性能记录（2026-10-12）

Windows、i7-13700H、RTX 3050 Laptop GPU 上，720p/20fps 课程源片 1611.650s，1000 个规则保留区间，成片 1311.950s。输出 26234 帧，计划重编码 26217 帧（99.94%），每音轨写入 57857007 个样本。

NVENC 32 surfaces 版本两次完整导出为 39.037s、39.633s；该轮修改前为 64.688s、47.291s。按第二组相邻对照，耗时减少 16.2%。机器状态未锁定，音频阶段也有波动，不能将全部墙钟差异归给视频缓冲或外推为通用性能。

验证包括 76 项 Python 回归、真实课程整片严格解码、采用区间与样本/帧数、源画面和有声窗口的同步抽查。该轮 NVENC 缓冲优化前后，最终两次成片的音频、视频编码包与时间戳逐包完全相同；MF 与更早的原生 AAC 版本位流不同，不能混用这两个结论。

脱敏记录：[benchmarks/windows-rtx3050-2026-10-12.json](../benchmarks/windows-rtx3050-2026-10-12.json)。本仓库不包含课程素材、个人路径或本机凭据。规则切点不是语义口误识别的结果，仍未达到一小时成片十秒的目标。
