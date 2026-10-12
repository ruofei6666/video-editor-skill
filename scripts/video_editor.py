#!/usr/bin/env python3
"""Unified CLI entrypoint for video-editor."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

from audio_extractor import (
    extract_audio_wav,
    measure_audio_levels,
    measure_energy_percentiles,
    probe_video_info,
)
from common import (
    complement_intervals,
    format_timestamp,
    interval_duration,
    load_json,
    log,
    merge_intervals,
    write_json,
    source_signature,
    validate_intervals,
)
from semantic_planner import (
    build_semantic_context,
    plan_video_cuts,
    refine_cut_boundaries_to_minima,
)
from silence_detector import (
    detect_adaptive_pauses,
    detect_nonspeech_regions_vad,
    detect_silence_regions,
    flatten_words,
    resolve_silence_db,
)
from transcriber import load_api_key, transcribe_audio_bailian, validate_transcript
from volcengine_asr import transcribe_audio_volcengine
from video_assembler import (
    assemble_video_smartcut,
    assemble_video_encode,
    assemble_video_stream_copy,
    generate_markdown_report,
    validate_output_path,
)

CONFIG_PATH = Path.home() / ".config" / "video-editor" / "config.json"
DEFAULT_CONFIG = {
    "asr_backend": "bailian",
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
    "audio_pipeline": "auto",
}


def get_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user_cfg = load_json(CONFIG_PATH)
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
                # 旧版的远程语义模型配置不再参与流程，避免残留地址或模型名产生误导。
                for legacy_key in ("model", "api_base", "enable_thinking", "concurrency"):
                    cfg.pop(legacy_key, None)
        except (OSError, ValueError) as exc:
            raise ValueError("无法读取 video-editor 配置，请检查 JSON 格式") from exc
    return cfg


def work_paths(video_path: Path, work_dir: Path | None = None):
    directory = (work_dir or video_path.parent / f".{video_path.name}_work").resolve()
    return directory, directory / f"{video_path.stem}_edit_plan.json", directory / f"{video_path.stem}_edit_report.md"


def prepare_cache(video_path: Path, directory: Path, asr_backend: str = "bailian") -> dict[str, Any]:
    source = source_signature(video_path)
    # Bump this version whenever extraction or ASR normalization changes.
    identity = {"cache_version": 4, "source": source, "asr_backend": asr_backend}
    manifest = directory / "source.json"
    try:
        matches = load_json(manifest) == identity
    except (OSError, ValueError):
        matches = False
    directory.mkdir(parents=True, exist_ok=True)
    if not matches:
        for name in ("audio_16k.wav", "transcript.json", "transcript_raw.json", "transcript_volcengine_job.json"):
            (directory / name).unlink(missing_ok=True)
        write_json(manifest, identity)
    return source


def validate_plan(plan: dict[str, Any], video_path: Path) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("schema_version") != 1 or plan.get("source") != source_signature(video_path):
        raise ValueError("计划版本或源视频不匹配，请重新运行 cut 并重新提交 semantic_plan.json")
    try:
        duration_ms = float(plan["original_duration_s"]) * 1000
        kept = validate_intervals(plan["kept_intervals"], duration_ms)
        fade = float(plan["config"]["crossfade_ms"])
        if not math.isfinite(fade) or fade < 0:
            raise ValueError("淡化参数无效")
        if not isinstance(plan["pause_cuts"], list) or not isinstance(plan["semantic_cuts"], list):
            raise ValueError("删减记录无效")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("剪辑计划格式或时间区间无效，请检查 JSON 或重新分析") from exc
    if not kept:
        raise ValueError("计划没有保留内容，已停止导出")
    plan = dict(plan)
    renderer = plan["config"].get("renderer", "smartcut")
    if renderer not in ("smartcut", "encode"):
        raise ValueError("渲染器必须为 smartcut 或 encode")
    plan["config"] = {**plan["config"], "crossfade_ms": fade, "renderer": renderer}
    video_encoder = plan['config'].get('video_encoder', 'auto')
    if video_encoder not in ('auto', 'cpu', 'nvenc'):
        raise ValueError('video-encoder 必须为 auto、cpu 或 nvenc')
    plan['config']['video_encoder'] = video_encoder
    for key, choices, default in (('video_decoder', ('auto', 'cpu', 'nvdec'), 'auto'),
                                  ('audio_coder', ('fast', 'twoloop'), 'fast'),
                                  ('audio_encoder', ('auto', 'aac', 'mf'), 'auto'),
                                  ('audio_pipeline', ('auto', 'thread', 'process'), 'auto')):
        value = plan['config'].get(key, default)
        if value not in choices:
            raise ValueError(f'{key} 配置无效')
        plan['config'][key] = value
    plan["kept_intervals"] = kept
    plan["new_duration_s"] = interval_duration(kept) / 1000
    plan["merged_cut_intervals"] = complement_intervals(kept, duration_ms)
    return plan


def review_plan(video_path: Path, work_dir: Path | None = None) -> dict[str, Any]:
    video_path = video_path.resolve()
    _, plan_path, _ = work_paths(video_path, work_dir)
    if not plan_path.exists():
        raise FileNotFoundError("未找到已有计划，请先运行 cut 并提交 semantic_plan.json")
    return validate_plan(load_json(plan_path), video_path)


def export_plan(plan: dict[str, Any], video_path: Path, output_video: Path,
                report_path: Path, *, fast_copy=False, overwrite=False, renderer=None, video_encoder=None,
                video_decoder=None, audio_coder=None, audio_encoder=None, audio_pipeline=None) -> float:
    stats = {}
    kwargs = {"overwrite": overwrite, "stats": stats}
    renderer = renderer or plan["config"].get("renderer", "smartcut")
    video_encoder = video_encoder or plan['config'].get('video_encoder', 'auto')
    if renderer not in ("smartcut", "encode"):
        raise ValueError("渲染器必须为 smartcut 或 encode")
    if fast_copy:
        assemble_video_stream_copy(video_path, plan["kept_intervals"], output_video, **kwargs)
    else:
        assemble = assemble_video_smartcut if renderer == "smartcut" else assemble_video_encode
        if renderer == 'smartcut':
            kwargs['video_encoder'] = video_encoder
            kwargs['video_decoder'] = video_decoder or plan['config'].get('video_decoder', 'auto')
            kwargs['audio_coder'] = audio_coder or plan['config'].get('audio_coder', 'fast')
            kwargs['audio_encoder'] = audio_encoder or plan['config'].get('audio_encoder', 'auto')
            kwargs['audio_pipeline'] = audio_pipeline or plan['config'].get('audio_pipeline', 'auto')
        elif video_encoder == 'nvenc':
            raise ValueError('NVENC 仅适用于 smartcut 渲染器')
        assemble(video_path, plan["kept_intervals"], output_video,
                 crossfade_ms=plan["config"]["crossfade_ms"], **kwargs)
    actual = float(probe_video_info(output_video)["duration_s"])
    generate_markdown_report(
        video_path, output_video, plan["original_duration_s"], plan["new_duration_s"],
        plan["pause_cuts"], plan["semantic_cuts"], report_path,
        config=plan["config"], actual_duration_s=actual, fast_copy=fast_copy,
        kept_intervals=plan["kept_intervals"],
        export_details=stats,
    )
    plan["export_details"] = stats
    return actual


def run_pipeline(
    video_path: Path,
    output_video: Path | None = None,
    *,
    dry_run: bool = False,
    fast_copy: bool = False,
    pause_threshold_ms: float | None = None,
    silence_db_override: float | None = None,
    semantic_plan: Path | None = None,
    work_dir: Path | None = None,
    overwrite: bool = False,
    renderer: str | None = None,
    video_encoder: str | None = None,
    video_decoder: str | None = None,
    audio_coder: str | None = None,
    audio_encoder: str | None = None,
    audio_pipeline: str | None = None,
) -> dict[str, Any]:
    video_path = video_path.resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"找不到源视频：{video_path}")
    cfg = get_config()
    if renderer is not None:
        cfg["renderer"] = renderer
    if video_encoder is not None:
        cfg['video_encoder'] = video_encoder
    if video_decoder is not None:
        cfg['video_decoder'] = video_decoder
    if audio_coder is not None:
        cfg['audio_coder'] = audio_coder
    if audio_encoder is not None:
        cfg['audio_encoder'] = audio_encoder
    if audio_pipeline is not None:
        cfg['audio_pipeline'] = audio_pipeline
    if cfg['audio_pipeline'] not in ('auto', 'thread', 'process'):
        raise ValueError('audio-pipeline 必须为 auto、thread 或 process')
    if cfg['audio_encoder'] not in ('auto', 'aac', 'mf'):
        raise ValueError('audio-encoder 必须为 auto、aac 或 mf')
    if cfg['video_decoder'] not in ('auto', 'cpu', 'nvdec') or cfg['audio_coder'] not in ('fast', 'twoloop'):
        raise ValueError('视频解码器或音频编码参数无效')
    if cfg['video_encoder'] not in ('auto', 'cpu', 'nvenc'):
        raise ValueError('video-encoder 必须为 auto、cpu 或 nvenc')
    if cfg["renderer"] not in ("smartcut", "encode"):
        raise ValueError("渲染器必须为 smartcut 或 encode")
    cfg["asr_backend"] = str(cfg.get("asr_backend", "bailian")).strip().lower()
    if cfg["asr_backend"] not in ("bailian", "volcengine"):
        raise ValueError("video-editor 的 ASR 后端必须为 bailian 或 volcengine")
    if pause_threshold_ms is not None:
        cfg["pause_threshold_ms"] = pause_threshold_ms
    for key in ("pause_threshold_ms", "sentence_threshold_ms", "min_pause_ms", "crossfade_ms"):
        cfg[key] = float(cfg[key])
        if not math.isfinite(cfg[key]) or cfg[key] < 0:
            raise ValueError(f"配置 {key} 必须为非负有限数值")
    if cfg["min_pause_ms"] >= min(cfg["pause_threshold_ms"], cfg["sentence_threshold_ms"]):
        raise ValueError("保留停顿必须短于停顿检测门限")
    # Persist only non-secret settings used by the report and renderer.
    report_config = {key: cfg[key] for key in DEFAULT_CONFIG}
    cfg["semantic_max_local_cleanup_ms"] = float(cfg["semantic_max_local_cleanup_ms"])
    if cfg["semantic_max_local_cleanup_ms"] < 500 or not math.isfinite(cfg["semantic_max_local_cleanup_ms"]):
        raise ValueError("semantic_max_local_cleanup_ms 必须是不小于 500 的有限数值")
    report_config["semantic_max_local_cleanup_ms"] = cfg["semantic_max_local_cleanup_ms"]
    output_video = (output_video or video_path.with_name(f"{video_path.stem}_edited{video_path.suffix}")).resolve()
    validate_output_path(video_path, output_video, overwrite=overwrite or dry_run)
    load_api_key(cfg["asr_backend"])  # Only ASR uses this credential.
    directory, plan_path, report_path = work_paths(video_path, work_dir)
    source = prepare_cache(video_path, directory, cfg["asr_backend"])
    start_time = time.time()
    log(f"开始分析：{video_path.name}")
    info = probe_video_info(video_path)
    duration_s = float(info["duration_s"])
    if not info["has_video"] or not info["has_audio"] or not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError("输入必须包含有效的视频、音轨和时长")

    audio_wav = directory / "audio_16k.wav"
    if not audio_wav.exists():
        extract_audio_wav(video_path, audio_wav)
    mean_db, _ = measure_audio_levels(audio_wav)
    noise_floor, speech_level = measure_energy_percentiles(audio_wav)
    if silence_db_override is not None and (not math.isfinite(silence_db_override) or not -100 <= silence_db_override <= 0):
        raise ValueError("silence-db 必须在 -100 到 0 dBFS 之间")
    silence_db = resolve_silence_db(str(silence_db_override) if silence_db_override is not None else "auto", mean_db, noise_floor, speech_level)
    report_config["silence_db"] = silence_db
    transcribe = transcribe_audio_volcengine if cfg["asr_backend"] == "volcengine" else transcribe_audio_bailian
    segments = validate_transcript(transcribe(audio_wav, directory / "transcript.json"))
    words = flatten_words(segments)
    if any(w["end"] > duration_s + 0.1 for w in words):
        raise ValueError("转录时间戳超出源视频，请检查转录结果")
    raw = detect_silence_regions(audio_wav, noise_db=silence_db, min_dur=0.25)
    vad = detect_nonspeech_regions_vad(audio_wav, min_dur=0.25)
    # An explicitly chosen acoustic threshold must not bridge louder samples.
    silence = merge_intervals([(s * 1000, e * 1000) for s, e in raw + vad],
                              gap_ms=0 if silence_db_override is not None else 40)
    pause_cuts = detect_adaptive_pauses(
        [(s / 1000, e / 1000) for s, e in silence], words,
        threshold_ms=cfg["pause_threshold_ms"], sentence_threshold_ms=cfg["sentence_threshold_ms"],
        min_pause_ms=cfg["min_pause_ms"],
    )
    semantic_context_path = directory / "semantic_context.json"
    default_semantic_plan = directory / "semantic_plan.json"
    semantic_plan_path = (semantic_plan or default_semantic_plan).resolve()
    if not semantic_plan_path.exists():
        context = {
            "source": source,
            "input_video": str(video_path),
            "original_duration_s": duration_s,
            "config": report_config,
            "pause_cuts": pause_cuts,
            **build_semantic_context(segments),
            "next_step": "由调用 Skill 的 Agent 阅读本文件，写出同目录 semantic_plan.json，再重新运行 cut --semantic-plan semantic_plan.json。",
        }
        write_json(semantic_context_path, context)
        log(f"已生成语义判断上下文：{semantic_context_path}")
        return {
            "input_video": str(video_path),
            "semantic_context": str(semantic_context_path),
            "semantic_plan": str(semantic_plan_path),
            "pause_cuts": pause_cuts,
            "needs_agent_plan": True,
            "dry_run": True,
        }
    semantic_plan_data = load_json(semantic_plan_path)
    if not isinstance(semantic_plan_data, dict) or semantic_plan_data.get("source") != source:
        raise ValueError("semantic_plan.json 的 source 与当前视频不一致，请基于最新 semantic_context.json 重写")
    semantic_cuts = plan_video_cuts(
        segments, semantic_plan_data, model="agent",
        max_local_cleanup_ms=cfg["semantic_max_local_cleanup_ms"],
    )
    semantic_cuts = refine_cut_boundaries_to_minima(semantic_cuts, audio_wav)
    cuts = merge_intervals([(c["start_ms"], c["end_ms"]) for c in pause_cuts + semantic_cuts])
    cuts = validate_intervals(cuts, duration_s * 1000)
    # Final guard after semantic boundary operations. Confirmed audio silence is
    # allowed to overlap an imprecise ASR timestamp; only semantic cuts must
    # prove that every removed word belongs to the Agent-approved range.
    for word in words:
        ws, we = word["start"] * 1000, word["end"] * 1000
        removed = any(c["spoken_start_ms"] - 1 <= ws and we <= c["spoken_end_ms"] + 1
                      for c in semantic_cuts)
        if not removed and any(max(ws, s) < min(we, e)
                               for s, e in ((c["start_ms"], c["end_ms"])
                                            for c in semantic_cuts)):
            raise ValueError("最终剪辑区间触及保留文字，已停止出片")
    for c in semantic_cuts:
        if any(max(rs, c["start_ms"]) < min(re, c["end_ms"])
               for rs, re in c["replacement_intervals"]):
            raise ValueError("最终剪辑区间触及替代话术，已停止出片")
    kept = complement_intervals(cuts, duration_s * 1000)
    if not kept:
        raise ValueError("剪辑计划未保留内容，已停止出片")
    plan = {
        "schema_version": 1, "source": source, "config": report_config,
        "input_video": str(video_path), "original_duration_s": duration_s,
        "new_duration_s": interval_duration(kept) / 1000,
        "pause_cuts": pause_cuts, "semantic_cuts": semantic_cuts,
        "merged_cut_intervals": cuts, "kept_intervals": kept,
    }
    write_json(plan_path, plan)
    generate_markdown_report(
        video_path, output_video, duration_s, plan["new_duration_s"], pause_cuts, semantic_cuts,
        report_path, config=report_config, fast_copy=fast_copy, kept_intervals=kept,
    )
    actual = None
    if not dry_run:
        actual = export_plan(plan, video_path, output_video, report_path,
                             fast_copy=fast_copy, overwrite=overwrite)
    return {
        **plan, "output_video": str(output_video) if not dry_run else None,
        "actual_duration_s": actual,
        "saved_s": duration_s - (actual if actual is not None else plan["new_duration_s"]),
        "report_md": str(report_path), "plan_json": str(plan_path),
        "dry_run": dry_run, "elapsed_s": time.time() - start_time,
    }


def main():
    parser = argparse.ArgumentParser(description="video-editor：口播粗剪、离线复核与导出")
    commands = parser.add_subparsers(dest="command", required=True)
    cut = commands.add_parser("cut", help="准备语义上下文，或按 Agent 计划分析并剪辑")
    review = commands.add_parser("review", help="离线读取已有计划，不重新分析")
    render = commands.add_parser("render", help="离线按已有计划导出")
    for command in (cut, review, render):
        command.add_argument("video", type=Path)
        command.add_argument("--work-dir", type=Path)
    for command in (cut, render):
        command.add_argument("-o", "--output", type=Path)
        command.add_argument("--fast-copy", action="store_true", help="近似流拷贝预览，可能保留额外内容；无微淡化")
        command.add_argument("--overwrite", action="store_true", help="允许覆盖已有成片，永不覆盖源片")
        command.add_argument("--renderer", choices=("smartcut", "encode"),
                             help="默认 smartcut；encode 为手动选择的完整重编码兼容模式")
        command.add_argument('--video-encoder', choices=('auto', 'cpu', 'nvenc'),
                             help='smartcut 默认自动优先使用 NVENC；显式 nvenc 不回退 CPU')
        command.add_argument('--video-decoder', choices=('auto', 'cpu', 'nvdec'),
                             help='auto 默认 CPU 解码以避免显存往返拷贝；显式 nvdec 不回退 CPU')
        command.add_argument('--audio-coder', choices=('fast', 'twoloop'),
                             help='默认 AAC fast；twoloop 为原音频编码算法')
        command.add_argument('--audio-encoder', choices=('auto', 'aac', 'mf'),
                             help='auto 在 Windows 探测 MF AAC；aac 为原生 AAC；mf 严格要求 Media Foundation')
        command.add_argument('--audio-pipeline', choices=('auto', 'thread', 'process'),
                             help='auto 为长片采用独立音频进程；thread 为原线程管线；process 强制独立进程')
    cut.add_argument("--dry-run", action="store_true")
    cut.add_argument("--semantic-plan", type=Path, help="Agent 写出的 semantic_plan.json")
    cut.add_argument("--pause-threshold", type=float)
    cut.add_argument("--silence-db", type=float, help="本次视频的静音门限 dBFS；省略时自动估计")
    args = parser.parse_args()
    try:
        if args.command == "cut":
            result = run_pipeline(
                args.video, args.output, dry_run=args.dry_run, fast_copy=args.fast_copy,
                pause_threshold_ms=args.pause_threshold, semantic_plan=args.semantic_plan,
                silence_db_override=args.silence_db,
                work_dir=args.work_dir, overwrite=args.overwrite,
                renderer=args.renderer,
                video_encoder=args.video_encoder,
                video_decoder=args.video_decoder, audio_coder=args.audio_coder,
                audio_encoder=args.audio_encoder,
                audio_pipeline=args.audio_pipeline,
            )
        else:
            result = review_plan(args.video, args.work_dir)
            if args.command == "render":
                video = args.video.resolve()
                output = (args.output or video.with_name(f"{video.stem}_edited{video.suffix}")).resolve()
                _, _, report = work_paths(video, args.work_dir)
                # Replace a stale success report before attempting a new export.
                generate_markdown_report(
                    video, output, result["original_duration_s"], result["new_duration_s"],
                    result["pause_cuts"], result["semantic_cuts"], report,
                    config=result["config"], fast_copy=args.fast_copy, kept_intervals=result["kept_intervals"],
                )
                result["actual_duration_s"] = export_plan(
                    result, video, output, report, fast_copy=args.fast_copy, overwrite=args.overwrite,
                    renderer=args.renderer,
                    video_encoder=args.video_encoder,
                    video_decoder=args.video_decoder, audio_coder=args.audio_coder,
                    audio_encoder=args.audio_encoder,
                    audio_pipeline=args.audio_pipeline,
                )
                result["output_video"] = str(output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"剪辑未完成：{exc}\n")


if __name__ == "__main__":
    main()
