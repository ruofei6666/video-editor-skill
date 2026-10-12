# 多课程并行、恢复与实时进度

适用于整个课程目录。每份课程由独立 Python 进程执行，分析池与导出池分别限流；smartcut 仍一次接收该课程的全部保留区间。默认同时分析 4 份、导出 2 份，每份长课的 ASR 分段并发为 1。GPU 内存、编码器会话或服务商并发不足时降低对应 worker 数。

## 两步处理

Windows 在 skill 目录执行：

```powershell
.\scripts\run.ps1 batch analyze 'D:\courses' -o 'D:\courses_edited' --analysis-workers 4
```

macOS / Linux 把入口换为 `bash scripts/run.sh`，路径换为对应平台路径。

Agent 逐份读取输出目录 `.work/<源文件名含扩展名>/semantic_context.json`，按 SKILL.md 的规则写出同目录 `semantic_plan.json`。只生成候选或用正则匹配口头禅不能当作语义审查完成；定义、推导、例题与新信息仍须阅读上下文。没有删减也需明确写出绑定当前 `source` 的 `{"edits": []}` 计划。没有计划时只报告 `needs_agent_plan`，不会自动假定全保留并导出。

```powershell
.\scripts\run.ps1 batch render 'D:\courses' -o 'D:\courses_edited' --render-workers 2
```

`render`、`status` 及单片 `compile` 均离线，不读取 Key。批量 render 复用已经完成的 ASR、停顿分析与语义上下文，编译 Agent 计划，不再重复运行 VAD。单片也可用：

```powershell
.\scripts\run.ps1 compile 'D:\courses\lesson.mp4' --work-dir 'D:\courses_edited\.work\lesson.mp4'
```

## 边分析边导出与断点继续

```powershell
.\scripts\run.ps1 batch run 'D:\courses' -o 'D:\courses_edited' --analysis-workers 4 --render-workers 2 --wait-for-plans 120
.\scripts\run.ps1 batch resume 'D:\courses' -o 'D:\courses_edited'
```

`run` 和 `resume` 使用同一调度逻辑：分析缺少上下文的课程，同时导出已具有有效计划的课程。默认队列空闲时返回待办；`--wait-for-plans` 表示队列空闲后继续等待 Agent 写入新计划的秒数，有任务开始后重新计算空闲时间。不要在等待语义计划的阶段声称已完成全自动剪辑。

已完成成片仅在源文件绑定、语义输入、导出设置和成片文件状态一致时跳过。改动计划或设置会重新调度；已有目标文件默认保护，确认需要替换时用 `--overwrite`。失败课程下次执行会重试，有效音频、转录与 AV1 中间文件会复用。每个课程有独立日志和状态，失败不会中止其他课程。目录级和课程级 OS 锁防止重复处理；正常中断会停止当前子进程树。强制结束协调器后若旧工作进程还活着，课程锁会拒绝重复导出，待旧进程结束再恢复。

默认只枚举当前目录的 MP4、MOV、MKV。`--recursive` 保留子目录结构；跳过输出目录、隐藏工作目录、符号链接及以 `_edited` 结尾的成片。输出格式为 MP4，文件名 `<stem>_<原扩展名>_edited.mp4`，避免同名 MP4/MOV 冲突。

## 进度和时间口径

默认每 10 秒更新终端及输出目录的：

- `batch_progress.json`：课程状态、当前阶段耗时、成功成片的实际时长、累计缩短时长。
- `batch_progress.md`：可读进度表和缩短比例。
- `.work/<文件名>/<analyze|render>.log` 与对应 `.status.json`：阶段日志、完成时间和实际耗时；不保存 Key。

```powershell
.\scripts\run.ps1 batch status 'D:\courses' -o 'D:\courses_edited'
```

`--progress-every 5` 可调整写入间隔。`run_wall_s` 是本次协调器的墙钟，包含等候 Agent 的时间，恢复后重新计时；阶段 `elapsed_s` 是每份课程本次分析或导出的耗时。`completed_saved_s` 只累计已成功导出课程的原时长减去实际成片时长，不把计划预测当作已经缩短。并行阶段耗时不能相加得出墙钟。

用户要求实时汇报时，Agent 每次读取这些实际状态后报告完成数、分析/待计划/导出/失败数、本次墙钟和已完成课程缩短时长。工具进度文件会持续更新，但不等于用户已看到聊天汇报；长任务至少每 60 秒发一次有事实依据的进度消息。

## 长课音频与火山任务恢复

单片和批量分析共用 `asr_chunks.py`。提取的 16 kHz 单声道 16-bit WAV 大于 100 MB 时，自动以约 40 分钟为目标，在附近 ±20 秒的低能量位置切分上传音频，连续、不重叠且不漏样本。每段识别的分句与逐字时间戳按起始样本位置还原到源视频坐标。视频不分段，停顿分析继续使用整份 WAV。

支持百炼、火山两个后端。配置 `asr_chunk_seconds` 可选 60–2800 秒，单片 `asr_chunk_workers` 默认 2；批量参数 `--asr-chunk-workers` 默认 1，可选 1–4。总 ASR 并发约为分析课程数乘段内并发数。分段缓存按音频内容、服务商和切分参数隔离；已完成段及已提交任务可继续使用。

火山 submit 前保存无密钥的任务编号；网络超时或普通查询失败保留编号，恢复时继续 query。只有未得到提交确认、且服务商明确返回 `45000092`（当前应用找不到任务）时，才归档旧编号并重交一次。已确认提交的任务和不确定的网络错误不能因此重交；重复出现该错误会停止，需要处理服务商或账号配置。遵守当前代理配置，不擅自更改系统代理或驱动。

## AV1 兼容导出

`--av1-compatibility auto` 为默认：8-bit YUV420 AV1 在 H.264 NVENC 真实探测通过后，先生成 H.264 中间文件，再由 smartcut 精确剪辑。使用固定 PyAV 16.0.1 的 FFmpeg 运行时，避免系统 FFmpeg 与驱动的 NVENC API 不匹配。转换保留逐帧 PTS/时间基准、分辨率、色彩参数及原音轨；中间视频为 P4/HQ/CQ18、无 B 帧、32 秒 GOP、32 surfaces。较长 GOP 减少密集切点下复制/重编码切换导致的编码器反复初始化；它不是适用于所有视频的速度保证。

- `--av1-compatibility off` 保留原 AV1 渲染路径。
- `--av1-compatibility h264` 显式转换；配 `--video-encoder cpu` 时中间文件为 libx264/veryfast/CRF18。
- 默认 auto 配显式 CPU 时保留原 AV1；NVENC 探测不可用时也保留原路径并说明原因，后续编码失败不会偷偷改换路径。
- 10-bit 或其他色度 AV1 不进入此 8-bit 兼容路径；严格转换请求会停止，避免丢失精度。

这会完整重编码一次视频，切点附近随后可能再编码，存在有损转换且成片可能更大。报告单列当前兼容转换耗时、缓存是否复用及包含转换的导出墙钟。缓存绑定源片、转换参数和中间文件状态，源片永不被替换。它不包含额外的批量全片解码、逐包时间戳比对或源片哈希审计工具。

兼容转换阶段使用 CPU 解码和 NVENC 编码；`--video-decoder nvdec` 作用于后续 smartcut 阶段。系统 FFmpeg 仍用于音频提取与最终流拷贝合并，不用于此 NVENC 转换。
