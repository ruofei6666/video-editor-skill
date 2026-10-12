#!/usr/bin/env python3
"""Safe smartcut export, explicit compatibility encoding, and editing audit reports."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path
from typing import Any

from common import format_timestamp, log, interval_duration, validate_intervals
from audio_extractor import probe_video_info


def validate_output_path(input_video: Path, output_video: Path, overwrite: bool = False) -> None:
    if (input_video.resolve() == output_video.resolve()
            or (output_video.exists() and input_video.samefile(output_video))):
        raise ValueError("输出不能指向源视频，请选择新文件名")
    if output_video.exists() and not overwrite:
        raise FileExistsError("输出文件已存在；请换文件名，或显式使用 --overwrite")


def safe_export(assemble):
    @wraps(assemble)
    def wrapped(input_video, kept_intervals, output_video, *, overwrite=False, **kwargs):
        input_video, output_video = Path(input_video), Path(output_video)
        validate_output_path(input_video, output_video, overwrite)
        info = probe_video_info(input_video)
        kept_intervals = validate_intervals(kept_intervals, float(info["duration_s"]) * 1000)
        if not kept_intervals or not info["has_audio"] or not info["has_video"]:
            raise ValueError("需要非空保留区间及有效音视频轨道")
        output_video.parent.mkdir(parents=True, exist_ok=True)
        # Keep an existing export intact if encoding or validation fails.
        with tempfile.TemporaryDirectory(prefix=".video_export_", dir=output_video.parent) as d:
            target = Path(d) / ("output" + output_video.suffix)
            started = time.perf_counter()
            stats = kwargs.pop("stats", None)
            if stats is None:
                stats = {}
            assemble(input_video, kept_intervals, target, stats=stats, **kwargs)
            actual = probe_video_info(target)
            expected = stats.get("adopted_duration_s", interval_duration(kept_intervals) / 1000)
            if not actual["has_video"] or not actual["has_audio"] or float(actual["duration_s"]) <= 0:
                raise RuntimeError("导出文件缺少有效音视频轨道")
            delta = float(actual["duration_s"]) - expected
            if assemble.__name__ != "assemble_video_stream_copy":
                tolerance = (max(0.1, 1 / float(info["fps"])) if "adopted_duration_s" in stats
                             else max(0.1, len(kept_intervals) / float(info["fps"]) + 0.05))
                if abs(delta) > tolerance:
                    raise RuntimeError(f"成片时长偏离计划 {delta:.3f}s，未替换输出文件")
            else:
                log(f"流拷贝为近似剪辑：计划 {expected:.3f}s，实测 {actual['duration_s']:.3f}s；可能保留切点附近内容。")
            validate_output_path(input_video, output_video, overwrite)
            if overwrite:
                os.replace(target, output_video)
            else:
                os.link(target, output_video)  # Fails atomically if another run created it.
            stats.update(actual_duration_s=float(actual["duration_s"]),
                         export_elapsed_s=time.perf_counter() - started)
        log(f"已验收导出：{output_video}")
        return output_video
    return wrapped


def detect_video_encoder() -> tuple[str, list[str]]:
    """Select best hardware/software encoder for the current platform."""
    if platform.system() == "Darwin":
        # macOS VideoToolbox hardware acceleration
        return "h264_videotoolbox", ["-b:v", "12M", "-allow_sw", "1"]
    # Fallback to software x264
    return "libx264", ["-crf", "19", "-preset", "fast"]


# One filter graph with a thousand trims barely moves. Encode small groups,
# then join those files without re-encoding.
SLICE_BATCH = 48
BATCH_WORKERS = 4


def _filter_graph(kept_intervals: list[tuple[float, float]], fade_s: float) -> str:
    v_filters = []
    a_filters = []
    concat_inputs = []
    for idx, (start_ms, end_ms) in enumerate(kept_intervals):
        start_s = start_ms / 1000.0
        end_s = end_ms / 1000.0
        dur_s = end_s - start_s

        v_label = f"v{idx}"
        a_label = f"a{idx}"

        v_filters.append(
            f"[0:v]trim=start={start_s:.3f}:end={end_s:.3f},setpts=PTS-STARTPTS[{v_label}]"
        )

        # Audio micro-fade at cut boundaries (15ms fade-in, 15ms fade-out) to eliminate jump-cut clicks
        if fade_s > 0 and dur_s > 2 * fade_s:
            fade_out_start = dur_s - fade_s
            a_filter = (
                f"[0:a]atrim=start={start_s:.3f}:end={end_s:.3f},asetpts=PTS-STARTPTS,"
                f"afade=t=in:ss=0:d={fade_s:.3f},"
                f"afade=t=out:st={fade_out_start:.3f}:d={fade_s:.3f}[{a_label}]"
            )
        else:
            a_filter = f"[0:a]atrim=start={start_s:.3f}:end={end_s:.3f},asetpts=PTS-STARTPTS[{a_label}]"
        a_filters.append(a_filter)
        concat_inputs.append(f"[{v_label}][{a_label}]")

    concat_str = "".join(concat_inputs) + f"concat=n={len(concat_inputs)}:v=1:a=1[outv][outa]"
    return ";".join(v_filters + a_filters + [concat_str])


def _run_ffmpeg(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _encode_interval_group(
    input_video: Path,
    kept_intervals: list[tuple[float, float]],
    output_video: Path,
    *,
    encoder: str,
    enc_opts: list[str],
    fade_s: float,
    threads: int | None = None,
) -> None:
    filter_complex = _filter_graph(kept_intervals, fade_s)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".filter", delete=False, encoding="utf-8") as handle:
        handle.write(filter_complex)
        filter_file = Path(handle.name)
    encode_opts = list(enc_opts)
    if threads:
        encode_opts = [*encode_opts, "-threads", str(threads)]
    try:
        cmd = [
            "ffmpeg", "-y", "-i", str(input_video),
            "-filter_complex_script", str(filter_file),
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", encoder, *encode_opts,
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(output_video),
        ]
        result = _run_ffmpeg(cmd)
        # FFmpeg 9 on Windows removed the long-standing script-file option.
        # Its replacement reads an option value from a file. Keep that path
        # before the inline graph so long videos do not hit Windows command
        # line length limits.
        if result.returncode != 0 and "filter_complex_script" in result.stderr:
            log("当前 FFmpeg 不支持 filter_complex_script，改用文件参数读取滤镜图重试。")
            file_value_cmd = [
                "ffmpeg",
                "-y",
                "-i", str(input_video),
                "-/filter_complex", str(filter_file),
                "-map", "[outv]",
                "-map", "[outa]",
                "-c:v", encoder,
                *encode_opts,
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
                str(output_video),
            ]
            result = _run_ffmpeg(file_value_cmd)
            if result.returncode != 0:
                log("当前 FFmpeg 不支持文件参数读取，改用内联滤镜图重试。")
                inline_cmd = [
                    "ffmpeg",
                    "-y",
                    "-i", str(input_video),
                    "-filter_complex", filter_complex,
                    "-map", "[outv]",
                    "-map", "[outa]",
                    "-c:v", encoder,
                    *encode_opts,
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-movflags", "+faststart",
                    str(output_video),
                ]
                result = _run_ffmpeg(inline_cmd)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg assembly failed:\n{result.stderr[-2000:]}")
    finally:
        filter_file.unlink(missing_ok=True)


def _concat_encoded_parts(parts: list[Path], output_video: Path) -> None:
    list_path = output_video.with_suffix(".concat.txt")
    lines = []
    for part in parts:
        path = part.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{path}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = _run_ffmpeg([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c", "copy", "-movflags", "+faststart",
        str(output_video),
    ])
    if result.returncode != 0 or not output_video.exists():
        raise RuntimeError(f"ffmpeg concat failed:\n{result.stderr[-2000:]}")


def _concat_copy_intervals(
    input_video: Path,
    kept_intervals: list[tuple[float, float]],
    output_video: Path,
) -> None:
    """Remux the kept ranges in one pass. No re-encode; throughput depends on file size and interval count."""
    list_path = output_video.with_suffix(".concat.txt")
    source = input_video.resolve().as_posix().replace("'", "'\\''")
    lines = []
    for start_ms, end_ms in kept_intervals:
        lines.append(f"file '{source}'")
        lines.append(f"inpoint {start_ms / 1000:.3f}")
        lines.append(f"outpoint {end_ms / 1000:.3f}")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = _run_ffmpeg([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c", "copy", "-movflags", "+faststart",
        str(output_video),
    ])
    if result.returncode != 0 or not output_video.exists():
        raise RuntimeError(f"ffmpeg concat copy failed:\n{result.stderr[-2000:]}")


@safe_export
def assemble_video_smartcut(
    input_video: Path, kept_intervals: list[tuple[float, float]], output_video: Path,
    *, crossfade_ms: float = 15.0, stats: dict | None = None, video_encoder: str = 'auto',
    video_decoder: str = 'auto', audio_coder: str = 'fast', audio_encoder: str = 'auto', audio_pipeline: str = 'auto',
) -> Path:
    from smart_renderer import render_smart
    render_smart(input_video, kept_intervals, output_video, fade_ms=crossfade_ms, stats=stats,
                 video_encoder=video_encoder, video_decoder=video_decoder, audio_coder=audio_coder,
                 audio_encoder=audio_encoder, audio_pipeline=audio_pipeline)
    return output_video


@safe_export
def assemble_video_encode(
    input_video: Path,
    kept_intervals: list[tuple[float, float]],
    output_video: Path,
    *,
    crossfade_ms: float = 15.0,
    stats: dict | None = None,
) -> Path:
    """Explicit compatibility mode; never substitute approximate cuts."""
    if not kept_intervals:
        raise RuntimeError("No kept intervals specified — cannot assemble empty video.")

    output_video.parent.mkdir(parents=True, exist_ok=True)
    if stats is not None:
        stats.update(renderer="encode", audio_mode="batch_aac", crossfade_ms_applied=crossfade_ms)
    encoder, enc_opts = detect_video_encoder()
    fade_s = crossfade_ms / 1000.0
    if len(kept_intervals) <= SLICE_BATCH:
        log(f"Assembling {len(kept_intervals)} slice(s) with {encoder} and {crossfade_ms}ms micro-fades...")
        _encode_interval_group(
            input_video, kept_intervals, output_video,
            encoder=encoder, enc_opts=enc_opts, fade_s=fade_s,
        )
        return output_video

    groups = [kept_intervals[index:index + SLICE_BATCH] for index in range(0, len(kept_intervals), SLICE_BATCH)]
    log(f"分批编码 {len(kept_intervals)} 段，每批最多 {SLICE_BATCH}，共 {len(groups)} 批，然后拼接。")
    batch_dir = output_video.parent / "_batches"
    batch_dir.mkdir(parents=True, exist_ok=True)

    def encode_batch(item: tuple[int, list[tuple[float, float]]]) -> Path:
        index, group = item
        part = batch_dir / f"batch_{index:04d}.mp4"
        _encode_interval_group(
            input_video, group, part,
            encoder=encoder, enc_opts=enc_opts, fade_s=fade_s, threads=4,
        )
        log(f"批次 {index + 1}/{len(groups)} 完成")
        return part

    with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as pool:
        parts = list(pool.map(encode_batch, list(enumerate(groups))))
    _concat_encoded_parts(parts, output_video)
    return output_video


@safe_export
def assemble_video_stream_copy(
    input_video: Path,
    kept_intervals: list[tuple[float, float]],
    output_video: Path,
    *, stats: dict | None = None,
) -> Path:
    """One FFmpeg process; approximate preview with keyframe pre-roll."""
    _concat_copy_intervals(input_video, kept_intervals, output_video)
    if stats is not None:
        stats.update(renderer="fast-copy", audio_mode="passthrough", crossfade_ms_applied=0)
    return output_video


def generate_markdown_report(
    input_video: Path,
    output_video: Path,
    original_duration_s: float,
    new_duration_s: float,
    pauses: list[dict[str, Any]],
    semantic_cuts: list[dict[str, Any]],
    report_md_path: Path,
    *,
    config: dict[str, Any] | None = None,
    actual_duration_s: float | None = None,
    fast_copy: bool = False,
    kept_intervals: list[tuple[float, float]] | None = None,
    export_details: dict | None = None,
) -> Path:
    """Write human-readable editing audit report."""
    export_details = export_details or {}
    saved_s = original_duration_s - new_duration_s
    pct = (saved_s / original_duration_s * 100.0) if original_duration_s else 0.0

    lines = [
        f"# 视频智能剪辑报告",
        f"",
        f"- **源视频**：`{input_video.name}`",
        f"- **输出视频**：`{output_video.name}`",
        f"- **原始时长**：{format_timestamp(original_duration_s)} ({original_duration_s:.1f}s)",
        f"- **计划时长**：{format_timestamp(new_duration_s)} ({new_duration_s:.3f}s)",
        f"- **导出状态**：{'已导出' if actual_duration_s is not None else '仅计划，尚未成功导出'}",
        f"- **计划精简时长**：{format_timestamp(saved_s)} ({saved_s:.1f}s, 减少 **{pct:.1f}%**)",
        f"- **切除停顿数**：{len(pauses)} 处",
        f"- **切除口误/废案数**：{len(semantic_cuts)} 处",
        f"",
        f"---",
        f"",
        f"## 1. 语义口误与废案删减清单",
        f"",
    ]
    if actual_duration_s is not None:
        lines.insert(7, f"- **实测时长**：{actual_duration_s:.3f}s，偏离计划 {actual_duration_s - new_duration_s:+.3f}s")
    if fast_copy:
        lines.insert(7, "- **近似流拷贝**：切点受关键帧限制，可能保留额外内容；未应用音频淡化。")
    if actual_duration_s is not None and export_details:
        compatibility = export_details.get('av1_compatibility', {})
        if compatibility.get('selected'):
            lines[7:7] = [f"- **AV1 兼容转换**：H.264；额外耗时 {compatibility['current_transcode_s']:.3f}s；复用缓存：{compatibility['cache_reused']}。重编码有损。",
                          f"- **含兼容转换的导出墙钟**：{export_details['total_export_elapsed_s']:.3f}s。"]
        lines[7:7] = [
            f"- **实际渲染器**：{export_details.get('renderer', 'unknown')}",
            f"- **导出耗时**：{export_details.get('export_elapsed_s', 0):.3f}s（不含 ASR 与语义分析）",
            f"- **音频模式**：{export_details.get('audio_mode', 'unknown')}",
        ]
        if 'video_encoders_used' in export_details:
            lines[7:7] = [f"- **视频编码器**：{', '.join(export_details['video_encoders_used']) or '仅复制'}",
                          f"- **GPU 编码**：{'已实际使用' if export_details['gpu_accelerated'] else '未使用'}；视频解码：{export_details.get('video_decode_backend', 'cpu')}。"]
        if 'audio_encoders_used' in export_details:
            lines[7:7] = [f"- **实际音频编码器**：{', '.join(export_details['audio_encoders_used'])}（CPU）；192k，原采样率与声道。"]
        if export_details.get('nvenc_surfaces'):
            lines[7:7] = [f"- **NVENC 输入缓冲**：{export_details['nvenc_surfaces']} 个 surfaces；编码质量参数保持原设置。"]
        if export_details.get('export_pipeline') in ('parallel_audio_video', 'process_audio_video'):
            lines[7:7] = [
                f"- **并行处理墙钟**：{export_details['pipeline_elapsed_s']:.3f}s；音频与视频阶段重叠，不能相加。",
                f"- **视频细分**：解码 {export_details['video_decode_s']:.3f}s，编码 {export_details['video_encode_s']:.3f}s。",
                f"- **音频 AAC 编码**：{export_details['audio_stage_detail_s']['audio_encode_s']:.3f}s；等待音频 {export_details['audio_wait_s']:.3f}s。",
            ]
            if export_details['export_pipeline'] == 'process_audio_video':
                lines[7:7] = [f"- **独立音频进程**：最后一次流拷贝合并 {export_details['final_stream_copy_mux_s']:.3f}s；全片仅两条临时媒体流。"]
        if 'adopted_duration_s' in export_details:
            lines[7:7] = [
                f"- **帧边界采用时长**：{export_details['adopted_duration_s']:.3f}s",
                f"- **采用时长偏差**：{actual_duration_s - export_details['adopted_duration_s']:+.3f}s",
                f"- **计划重编码视频帧数**：{export_details['planned_recode_video_frames']}（共 {export_details['source_video_frames']} 帧）",
            ]
    if not semantic_cuts:
        lines.append("*未发现明显语义口误或重复废案。*")
    else:
        lines.append("| 序号 | 时间区间 | 类型 | 删减内容 | 替换/保留说明 | 理由 |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for idx, c in enumerate(semantic_cuts, start=1):
            s_ts = format_timestamp(c["start_ms"] / 1000.0)
            e_ts = format_timestamp(c["end_ms"] / 1000.0)
            cat = c.get("category", "cleanup")
            rem = c.get("removed_text", "").replace("\n", " ").replace("|", "\\|")
            kept = c.get("kept_text", "").replace("\n", " ").replace("|", "\\|")
            reason = c.get("reason", "").replace("\n", " ").replace("|", "\\|")
            lines.append(f"| {idx} | `{s_ts} → {e_ts}` | `{cat}` | {rem} | {kept or '*(直接顺接)*'} | {reason} |")

    lines.extend([
        f"",
        f"---",
        f"",
        f"## 2. 停顿与静音切除概览",
        f"",
        f"计划压缩 **{len(pauses)}** 处停顿。参数：`{config or {}}`。",
        "音频微淡化已应用。" if actual_duration_s is not None and not fast_copy and export_details.get("crossfade_ms_applied", 0) > 0 else "本次未确认应用音频微淡化。",
        f"",
    ])
    if export_details.get('adopted_kept_intervals') is not None:
        kept_intervals = export_details['adopted_kept_intervals']
    if kept_intervals is not None:
        interval_heading = ("## 3. 帧边界采用的保留区间" if export_details.get('adopted_kept_intervals') is not None
                            else "## 3. 请求的保留区间")
        lines.extend([interval_heading, "",
                      "本表为源视频坐标。smartcut 将保留区间向外对齐帧边界，优先保留话语，可能少删不足一帧的内容；流拷贝仍仅近似执行。", "",
                      "| 开始 | 结束 |", "| --- | --- |"])
        lines.extend(f"| {format_timestamp(s / 1000)} | {format_timestamp(e / 1000)} |"
                     for s, e in kept_intervals)

    report_md_path.parent.mkdir(parents=True, exist_ok=True)
    report_md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_md_path
