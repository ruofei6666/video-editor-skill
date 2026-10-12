# video-editor

用于 AI 编程助手的课程与口播粗剪 skill：压缩停顿，识别口误、重复和废话，生成可复核剪辑计划，再用 [smartcut](https://github.com/skeskinen/smartcut) 导出视频。

- 语音识别支持百炼和火山豆包语音；语义删减由调用 skill 的 Agent 根据转录规划。
- 一次处理全部保留区间，重编码切点附近的视频，复制其余完整段。
- 可用时自动选择 NVIDIA NVENC；默认 CPU 解码、P4/HQ/CQ23 和 32 个输入缓冲。
- 连续 AAC 音轨按样本剪切，保留采样率与声道，并在剪辑接缝施加 15ms 微淡化。
- 长片音频在独立进程编码；Windows 在真实探测通过后可使用 MF AAC。
- 输出视频、JSON 剪辑计划和 Markdown 审计报告，保护源文件与已有成片。

完整使用规则见 [SKILL.md](SKILL.md)，编解码和同步边界见 [references/rendering.md](references/rendering.md)。

## 安装

将仓库安装为 `video-editor` skill：

```bash
git clone https://github.com/ruofei6666/video-editor-skill.git ~/.codex/skills/video-editor
```

也可以将仓库复制到支持 `SKILL.md` 的助手的 skills 目录，目录名使用 `video-editor`。如果目标目录已存在，先检查已有修改。

依赖：Python 3.10+、系统 PATH 中的 FFmpeg 和 FFprobe。凭据配置页另需 Node.js 22.18+。渲染固定使用 smartcut 1.7，其 PyAV 版本为 16.0.1；没有 NVIDIA GPU 时可用 CPU 路径。仓库不附带 FFmpeg、驱动或模型权重。

Windows PowerShell：

```powershell
Set-Location "$env:USERPROFILE\.codex\skills\video-editor"
.\setup.ps1
```

macOS / Linux：

```bash
cd ~/.codex/skills/video-editor
bash setup.sh
```

## 配置语音识别

默认 ASR 为百炼 `bailian`。选择火山时，在 `~/.config/video-editor/config.json` 设置：

```json
{
  "asr_backend": "volcengine"
}
```

百炼需要 `DASHSCOPE_API_KEY`，火山需要 `VOLCENGINE_ASR_API_KEY`，也可通过随附配置页保存到系统凭据库。只在首次分析时调用 ASR；已有计划的离线复核与导出不读取 Key。配置步骤见 [API Key 配置](references/api-key-setup.md)。

## 使用

Windows 示例：

```powershell
.\scripts\run.ps1 cut 'D:\课程\视频.mp4'
```

首次生成带时间戳的 `semantic_context.json`。Agent 按 [SKILL.md](SKILL.md) 规则审查转录并写出 `semantic_plan.json` 后继续：

```powershell
.\scripts\run.ps1 cut 'D:\课程\视频.mp4' --semantic-plan 'D:\课程\.视频.mp4_work\semantic_plan.json' -o 'D:\课程\视频_edited.mp4'
```

已有计划可直接复核和重新导出：

```powershell
.\scripts\run.ps1 review 'D:\课程\视频.mp4'
.\scripts\run.ps1 render 'D:\课程\视频.mp4' -o 'D:\课程\视频_smartcut.mp4'
```

macOS / Linux 使用 `bash scripts/run.sh` 代替 PowerShell 入口。

| 参数 | 用途 |
| --- | --- |
| `--video-encoder cpu` | 明确选择 CPU 编码 |
| `--video-encoder nvenc` | 严格要求 NVENC，失败即停止 |
| `--video-decoder nvdec` | 显式测试 GPU 解码；本机当前下载/上传路径未显示速度优势 |
| `--audio-encoder aac --audio-pipeline thread` | 选择原生 AAC 与线程音频管线 |
| `--audio-encoder mf` | 严格要求 Media Foundation AAC |
| `--renderer encode` | 手动选择完整重编码兼容模式 |
| `--fast-copy` | 关键帧附近近似预览，可能残留被删内容 |
| `--overwrite` | 允许替换已有成片，仍不能覆盖源片 |

## 性能与验收

Windows、i7-13700H、RTX 3050 Laptop GPU 上，一份 720p/20fps、26 分 52 秒的课程采用 1000 个规则保留区间，成片约 21 分 52 秒，优化后两次导出为 **39.0 秒、39.6 秒**。这是本机规则切点测试，不代表语义剪辑完成或其他硬件性能，也不含 ASR。

该样本 99.94% 的保留帧需要重编码。切点较少、完整关键帧组较多时，smartcut 能复制更多视频；切点密集时可能接近完整重编码成本。**不保证一小时成片十秒导出。**

发布时的验证包括 76 项 Python 回归测试、真实课程的整片解码与同步检查。NVENC 缓冲优化前后，验证成片的音视频编码包和时间戳逐包一致。脱敏记录见 [benchmarks/windows-rtx3050-2026-10-12.json](benchmarks/windows-rtx3050-2026-10-12.json)。

维护时执行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

语音时间戳和 Agent 判断可能出错，正式使用前应复核删减报告、保留话语与接缝听感。重编码部分有损；MF 与原生 AAC 的音频位流不同，相同码率不代表相同听感。

## 许可证

本仓库保留原始 [MIT LICENSE](LICENSE) 及其版权声明。smartcut 等依赖及自行安装的 FFmpeg 分别遵循各自许可证；本仓库不分发其二进制文件。
