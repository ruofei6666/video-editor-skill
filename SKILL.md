---
name: video-editor
description: "使用 smartcut 智能渲染快速粗剪课程与口播 MP4、MOV、MKV 视频：压缩停顿，删掉口误重讲、废话、无意义语气词和重复的话，导出新视频、剪辑计划和审计报告。用户要求自动粗剪、压缩口播节奏或导出剪辑时间线时使用。不处理原生剪辑工程，不生成字幕。"
---

# Video Editor

压缩停顿、识别口误废案，生成可复核的剪辑计划，再导出新视频。语音转文字由 ASR 完成，语义判断由调用这个 Skill 的 Agent 根据带时间戳的转录亲自完成；两步都可能出错，正式使用前需要检查报告与接缝听感。

本技能不生成字幕；需要字幕时，可另用字幕工具处理成片。

---

## 目标

适合以人声讲解为主的视频；会议、音乐和依赖长停顿的演示需要逐段复核。
1. **压缩停顿**：以能量检测和 VAD 确认无声，统一使用 300ms 的连续无声门限，保留约 180ms 气口。底层探测窗口是 250ms，只负责找候选；只有实际无声超过 300ms 才会剪。ASR 时间戳用于对齐和说明，落在确认无声区里不会自动阻止剪辑；只有纯 ASR 词间距候选才使用文字保护。
2. **删掉口误、废话、无意义语气词和重复**：按子句切分并滑窗分析。口误和重复讲解要保留更完整的那一遍；整段都是废话或语气词时可以没有替代句。引用必须覆盖完整删除话术。
3. **收尾安全检查**：语义删减仍须避开保留词和替代话术；音频已确认的停顿可以覆盖不精确的 ASR 时间戳，不能再被同一条 ASR 保护规则拦回。
4. **减轻接缝杂音**：波形吸附只允许缩小删除区间；音频按样本剪切，在保留区间首尾施加 15ms 微淡化，使用一条连续 AAC 编码器保持累计同步。
5. **快速安全导出**：默认 smartcut，只重编码切点附近的视频，其余视频数据直接复制。全部区间写入一个成片，不生成上千个临时视频或启动上千次 FFmpeg。默认真实探测 NVENC，H.264/HEVC 可用时优先使用 GPU 编码。`--video-encoder nvenc` 明确要求 GPU，探测或运行失败均停止；`--video-encoder cpu` 明确使用 CPU。auto 探测不可用时会在日志与报告中说明 CPU 选择。运行失败不自动回退完整重编码。`--renderer encode` 可手动选择兼容模式；`--fast-copy` 仅用于近似预览。永不覆盖源片。

---

## 外部服务与语义判断边界

只有语音转文字需要服务商 API Key，先读[API Key 配置与业务读取](references/api-key-setup.md)。ASR 支持百炼 `bailian` 和火山豆包语音 `volcengine`；火山使用「录音文件识别 2.0 标准版」（`volc.seedasr.auc`），不要求开通极速版。默认配置使用百炼，配置示例展示如何选择火山。语义判断由当前 Agent 读取本地转录并写出删减计划，不额外调用语义模型。已有系统凭据直接复用，不输出 Key。

---

## 环境与配置

### 1. 基础依赖

- `ffmpeg`、`ffprobe`：安装后放入系统 PATH；macOS 也支持 `/opt/homebrew/bin/`。
- 火山语音 API Key（仅用于 ASR），经凭据入口注入 `VOLCENGINE_ASR_API_KEY`。百炼仍可选：`bl` CLI 的 `fun-asr`，或 SDK 的 `paraformer-realtime-v2`。
- Python 3.10+；渲染依赖固定在 `requirements-render.txt`（smartcut 1.7、PyAV 16.0.1 等）。已有虚拟环境可单独执行 `python -m pip install -r requirements-render.txt` 升级导出器。macOS / Linux 首次运行 `bash setup.sh`，Windows 首次运行 `PowerShell -ExecutionPolicy Bypass -File .\setup.ps1`，两者都会创建虚拟环境并安装依赖。钥匙串配置与读取另需 Node.js 22.18+，详见凭据说明。
- macOS / Linux 使用 `bash <SKILL_DIR>/scripts/run.sh ...`；Windows PowerShell 使用 `<SKILL_DIR>\scripts\run.ps1 ...`。

### 2. Agent 语义判断配置

配置文件路径：`~/.config/video-editor/config.json`（可选，不存在时使用默认值）：

```json
{
  "asr_backend": "volcengine",
  "semantic_planner": "calling-agent",
  "semantic_max_local_cleanup_ms": 2500.0,
  "pause_threshold_ms": 300.0,
  "sentence_threshold_ms": 300.0,
  "min_pause_ms": 180.0,
  "crossfade_ms": 15.0,
  "renderer": "smartcut",
  "video_encoder": "auto",
  "video_decoder": "auto",
  "audio_coder": "fast",
  "audio_encoder": "auto",
  "audio_pipeline": "auto"
}
```

- `semantic_planner` 固定为 `calling-agent`，表示语义判断由当前调用 Skill 的 Agent 完成，不读取模型地址、不保存模型 Key。
- `asr_backend` 可选 `volcengine` 或 `bailian`。火山采用标准版 submit/query，上传本地音频的 base64，不上传视频画面；关闭 `enable_ddc` 和 `enable_itn`，保留口误、重复和原始说法。只接受真实逐字时间戳，缺失即停止。当前本地直传入口限制音频 100 MB，较长视频需先分段。任务提交成功后记录无密钥的任务编号，查询超时后可继续同一任务。
- 较大 WAV 会生成 48 kbps MP3 用于 ASR 上传；本地停顿分析仍用原始 WAV。低音量讲解应核对音量门限及字时间戳，可用 `cut --silence-db -45` 为当前视频降低静音门限，实际门限会写入报告。不要把明显有声音的低音量话语当成空白。
- `semantic_max_local_cleanup_ms` 是局部口误的安全上限，超过 2.5 秒的 `delivery_cleanup` 或 `self_correction` 默认拒绝。
- `scripts/run.sh` 和 `scripts/run.ps1` 按当前平台和 `asr_backend` 选择凭据入口。火山使用 `volcengine` 凭据配置，百炼使用 `default`；普通配置 JSON 不保存 Key。

---

## 工作流

### 1. 默认智能剪辑（推荐）

对源视频进行完整分析并导出剪辑后的新视频。语义删减分成两步，避免把一个外部模型偷偷当成“自动裁判”：

```bash
bash scripts/run.sh cut /path/to/video.mp4
```

Windows PowerShell：

```powershell
.\scripts\run.ps1 cut C:\path\to\video.mp4
```

第一次运行会完成 ASR、VAD 和停顿分析，并生成 `semantic_context.json`。调用 Skill 的 Agent 读取这个文件，按其中的 JSON 规则判断口误，写出同目录的 `semantic_plan.json`，然后继续运行：

```bash
bash scripts/run.sh cut /path/to/video.mp4 --semantic-plan /path/to/.video.mp4_work/semantic_plan.json -o /path/to/video_edited.mp4
```

工作流程：
1. **音频提取与声学校准**：提取 16kHz WAV 音频，自适应计算能量分布分位数与底噪；自动静音阈值设有 `-30.5 dB` 下限，避免低电平空白片段因少量孤立峰值被误当成有声。
2. **高精度 ASR 字级识别**：调用已选择的 ASR，检查转录格式与字时间戳；缺失时停止。
3. **自适应停顿检测与证据标注**：结合 Silero VAD 与句尾标点感知切分死寂停顿；ASR 负责时间对齐和复核提示，不会否决已经由音频确认的无声区。
4. **Agent 语义口误规划**：按子句标点切分为原子片段，生成带时间戳的本地上下文；由当前 Agent 阅读上下文，依据“宁可漏删，不要误删”的规则写出 `semantic_plan.json`，程序只负责校验编号、原话覆盖、替代冲突和安全时长。
5. **切点波形极小值吸附**：只在删除区间内微调，随后重新检查保留文字。
6. **智能渲染与连续音频**：一次传入全部保留区间，视频向外对齐实际帧边界，优先保留话语；重叠区间合并。smartcut 只重编码受切点影响的视频，音频按样本剪切、微淡化并连续编码。默认 CPU 解码与可用的 NVENC 编码，NVENC 支持时直接接收原像素格式；索引使用等价的 C 字节查找，H.264/HEVC 重编码时间戳用精确整数运算。`--audio-pipeline auto` 对成片至少 60 秒的计划采用独立音频进程，整片仅两条临时媒体流，最后一次流拷贝合并；短片保留原线程方案。Windows 长片的 `--audio-encoder auto` 在 44.1/48kHz 单/双声道上真实探测 MF AAC 的起始和尾部同步，未通过则选原生 AAC。MF 采用 16-bit PCM，保留 192k、采样率、声道及 15ms 微淡化，编码数据与原 AAC 不同。`--audio-encoder aac --audio-pipeline thread` 保留原音频处理路径；`--audio-coder twoloop` 选择原生 AAC 的原算法。`--video-decoder nvdec` 与 `--audio-encoder mf` 是严格请求，不静默回退。报告记录实际编解码、进程、阶段与墙钟耗时；并行阶段不能相加。详细边界与验收见 [references/rendering.md](references/rendering.md)。
7. **生成 Markdown 审计报告**：记录实际渲染器、音频模式、采用的源视频区间、计划与实测时长、导出耗时。分析与导出分开计时；没有实测证据，不宣称一小时视频十秒完成。

当前 NVENC 使用 32 个输入 surfaces 提高在途帧并发，保留 P4/HQ/CQ23 等质量设置；进入复制段前及 EOF 必须完整 flush，保持最后一帧。实际报告记录 `nvenc_surfaces`，性能以导出墙钟与成片验收为准。

### Agent 写 `semantic_plan.json` 的规则

Agent 只根据 `semantic_context.json` 里的文字和时间戳判断，不重新听猜。上下文里的窗口提示如果还写着“宁可漏删”，以本节规则为准。

要删：
- 口误、结巴、说错后立刻改口的那一遍；后面有完整重讲时，删掉被放弃的前一遍。
- 废话：与本段知识点无关的闲聊、玩笑、录制说明、催进度，以及“好，我们继续”“这个大家知道啊”这类没有信息的过渡。
- 无意义语气词：单独成段的“啊、呃、嗯、哎、是吧、对吧、好吧、懂了吧、看懂没”。整段去掉后，前后的知识点仍然接得上。
- 重复的话：同一意思已经讲清楚，后面又整段再说一遍，而且没有新例子、新数字、新条件或新结论。保留更完整的一遍，删掉另一遍。

不要删：
- 定义、定理、推导、例题、数字、条件和结论。一句里既有语气词又有这些内容时，整句保留，不能为了抠掉一个“啊”把知识点一起剪掉。
- 重复里多出来的例子、反例、警告或计算步骤。
- 删掉后主语、谓语或结论会接不上的半句。

计划文件最小格式如下：

```json
{
  "source": "复制 semantic_context.json 里的 source",
  "edits": [
    {
      "remove_start_id": "U0007",
      "remove_end_id": "U0007",
      "cut_until_id": "U0008",
      "replacement_ids": [],
      "removed_quote": "是吧。",
      "replacement_quote": "",
      "category": "filler",
      "confidence": "high",
      "reason": "整段只有语气词，删掉后前后的知识点仍然接得上。"
    }
  ]
}
```

`removed_quote` 必须覆盖完整的待删原话。口误和重复讲解的 `replacement_ids` 只能指向保留下来的那一遍。废话和语气词用 `filler`，可以不填替代句。程序会拒绝低置信度候选、替代片段被同时删除的候选、超过 2.5 秒的局部口误，以及短于 180 毫秒或长于 45 秒的单段废话。超过 45 秒的废话要拆成多段 `filler`。

---

### 2. 预览分析与试运行（Dry-run）

若只想生成上下文、先让 Agent 判断而不实际渲染大视频：

```bash
bash scripts/run.sh cut /path/to/video.mp4
```

- 在源文件旁的 `.<name>.<ext>_work/` 生成 `semantic_context.json`；Agent 写完 `semantic_plan.json` 并再次运行带 `--semantic-plan` 的命令后，才会生成 `<name>_edit_plan.json` 和 `<name>_edit_report.md`。
- `semantic_context.json` 包含原子片段、停顿标记和逐窗口提示；它只供 Agent 阅读，不上传视频。
- 报告中包含：原始时长、预计时长、压缩比例、每一处口误删减区间、原话、订正保留话术与删减理由。

---

### 3. 近似流拷贝预览（Fast-copy）

仅用于接受关键帧边界偏差的快速预览。报告记录实测时长与计划偏差；需要准确删除口误时使用默认 smartcut：

```bash
bash scripts/run.sh cut /path/to/video.mp4 --semantic-plan /path/to/.video.mp4_work/semantic_plan.json --fast-copy -o /path/to/video_fast.mp4
```

---

### 4. 离线复核与重新渲染（Review / Re-render）

读取已有计划，不调用模型或读取 Key。需要调整时，编辑计划中的 `kept_intervals`（源视频毫秒坐标），再 render；报告的实际保留区间会同步更新，原话与理由仍来自分析阶段：

```bash
bash scripts/run.sh review /path/to/video.mp4
bash scripts/run.sh render /path/to/video.mp4 -o /path/to/video_edited.mp4
```

更换源视频后必须重新分析。已有成片默认不覆盖，明确需要替换时加 `--overwrite`；该选项也不能覆盖源片。分析失败可直接重试，相同源文件的有效音频和转录会复用，原有成功计划不会被失败结果替换。

---

## 输出规范

1. **剪辑后视频**：
   - 命名：默认在源文件同目录下生成 `<name>_edited.<ext>`，或由 `-o` 指定。
   - 编码：默认保留视频编码与分辨率，只重编码切点附近；音频连续编码为 192k AAC。重编码部分有损，视频切点向外量化到帧边界，报告记录采用的区间；不足一帧的删除可能不执行。临时导出经轨道与采用时长检查后才写入目标文件。
2. **审计报告**：
   - 保存在源文件旁的 `.<name>.<ext>_work/<name>_edit_report.md`。
   - 提供 Markdown 表格，清晰记录各处删减的前后文与删减理由。

---

## 资源导航

- `references/api-key-setup.md`: API Key 安全配置、环境绑定与凭据读取规范。
- `scripts/run.sh` / `scripts/run.ps1`: 按平台选择虚拟环境与凭据入口。
- `scripts/video_editor.py`: 分析、离线复核与按计划渲染。
- `scripts/audio_extractor.py`: 音频提取与声学能量分析。
- `scripts/transcriber.py`: 凭据读取、时间戳校验和百炼 ASR。
- `scripts/volcengine_asr.py`: 火山录音文件识别 2.0 标准版、任务续查及逐字时间戳转换。
- `scripts/silence_detector.py`: 自适应静音检测、VAD 与字保护算法。
- `scripts/semantic_planner.py`: 子句原子化切分、Agent 提示构造和语义计划校验；不发起任何模型请求。
- `scripts/video_assembler.py`: 安全智能渲染入口、手动兼容编码、近似预览与审计报告。
- `scripts/smart_renderer.py`: smartcut 单文件视频导出、按样本剪切的连续音频与微淡化。
- `scripts/gpu_encoder.py`: NVIDIA NVENC/NVDEC 真实编解码探测与上下文创建；不修改驱动或系统设置。
- `scripts/audio_encoder.py`: MF/原生 AAC 选择、实际编解码同步探测与 EOF 保护。
- `scripts/audio_process.py`: 一条连续音频进程、两条临时媒体流和最后一次流拷贝合并。
- `scripts/fast_index.py`: 与固定版本相同的 H.264 NAL 分类，使用 C 字节查找缩短索引。
- `references/rendering.md`: 渲染模式、精度、性能边界与媒体验收。
- `scripts/benchmark_render.py`: 用规则切点离线测试渲染吞吐；不当作真实语义剪辑成果。
- `tests/`: 基础算法与业务回归测试。维护时运行 `.venv/bin/python3 -m unittest discover -s tests -v`，覆盖源片保护、缓存失效、ASR 格式、缺少 Agent 计划、替代冲突和实际渲染；不得以基础函数测试通过代替完整流程验证。
