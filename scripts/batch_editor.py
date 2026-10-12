"""Course batch coordinator: separate Python processes, cached plans and progress."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from common import load_json, log, source_signature, write_json

MEDIA = {'.mp4', '.mov', '.mkv'}
EXPORT_OPTIONS = ('renderer', 'video_encoder', 'video_decoder', 'audio_coder',
                  'audio_encoder', 'audio_pipeline', 'av1_compatibility')


def read_json(path, default=None):
    try:
        return load_json(path)
    except (OSError, ValueError):
        return {} if default is None else default


@contextmanager
def coordinator_lock(root):
    """OS releases this lock after interruption; never steal a live job's lock."""
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / '.batch.lock').open('a+b')
    if os.fstat(handle.fileno()).st_size == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('此输出目录已有批处理进程，不能重复调度') from None
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == 'nt':
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def discover(folder: Path, output: Path, recursive=False):
    if not folder.is_dir() or folder == output:
        raise ValueError('批处理需要源目录和不同的输出目录')
    paths = folder.rglob('*') if recursive else folder.iterdir()
    return sorted((p.resolve() for p in paths if p.is_file() and p.suffix.lower() in MEDIA
                   and not p.is_symlink() and not any(part.startswith('.') for part in p.relative_to(folder).parts)
                   and not p.resolve().is_relative_to(output)
                   and not p.stem.lower().endswith('_edited')), key=lambda p: str(p).casefold())


def job_paths(video, folder, output):
    relative = video.relative_to(folder)
    # Include the source extension, so lesson.mp4 and lesson.mov never collide.
    work = output / '.work' / relative.parent / relative.name
    destination = output / relative.parent / f'{video.stem}_{video.suffix[1:]}_edited.mp4'
    return work, destination


def digest_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def render_fingerprint(source, work, video, options):
    from video_editor import work_paths
    _, plan, _ = work_paths(video, work)
    semantic, context = work / 'semantic_plan.json', work / 'semantic_context.json'
    inputs = ([digest_file(context), digest_file(semantic)] if context.exists() and semantic.exists()
              else [digest_file(plan)])
    return hashlib.sha256(json.dumps([source, inputs, options], sort_keys=True).encode()).hexdigest()


def analyze_ready(source, work, config):
    context = read_json(work / 'semantic_context.json')
    keys = ('asr_backend', 'semantic_max_local_cleanup_ms', 'pause_threshold_ms',
            'sentence_threshold_ms', 'min_pause_ms', 'crossfade_ms')
    return (context.get('source') == source and (work / 'transcript.json').exists()
            and all(context.get('config', {}).get(key) == config[key] for key in keys))


def worker(stage, request_path):
    request = load_json(request_path)
    with coordinator_lock(Path(request['work'])):
        return execute_worker(stage, request)


def execute_worker(stage, request):
    from video_editor import compile_cached_plan, export_plan, review_plan, run_pipeline, work_paths
    from video_assembler import generate_markdown_report
    video, work, output = map(Path, (request['video'], request['work'], request['output']))
    status_path = work / f'{stage}.status.json'
    started = time.time()
    try:
        if stage == 'analyze':
            result = run_pipeline(video, output, work_dir=work, dry_run=True, prepare_only=True,
                                  asr_chunk_workers=request['asr_chunk_workers'])
        else:
            if (work / 'semantic_context.json').exists() and (work / 'semantic_plan.json').exists():
                plan = compile_cached_plan(video, work)
            else:
                plan = review_plan(video, work)
            _, _, report = work_paths(video, work)
            generate_markdown_report(video, output, plan['original_duration_s'], plan['new_duration_s'],
                                     plan['pause_cuts'], plan['semantic_cuts'], report,
                                     config=plan['config'], kept_intervals=plan['kept_intervals'])
            actual = export_plan(plan, video, output, report, overwrite=request['overwrite'],
                                 **request['options'])
            result = {'input_video': str(video), 'output_video': str(output),
                      'original_duration_s': plan['original_duration_s'],
                      'actual_duration_s': actual, 'saved_s': plan['original_duration_s'] - actual,
                      'export_details': plan['export_details'], 'report_md': str(report)}
        status = {'ok': True, 'fingerprint': request['fingerprint'],
                                'started_at': started, 'finished_at': time.time(),
                                'elapsed_s': time.time() - started, 'result': result}
        if stage == 'render':
            stat = output.stat()
            status['output_stat'] = [stat.st_size, stat.st_mtime_ns]
        write_json(status_path, status)
        return 0
    except Exception as exc:
        write_json(status_path, {'ok': False, 'fingerprint': request['fingerprint'],
                                'finished_at': time.time(), 'elapsed_s': time.time() - started,
                                'error': str(exc)})
        log(f'{stage} 失败：{video.name}；{exc}')
        return 1


def hms(seconds):
    sign = '-' if seconds < 0 else ''
    seconds = abs(round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{sign}{hours:02d}:{minutes:02d}:{seconds:02d}'


def make_progress(jobs, started, stage, *, finished=False):
    counts = {}
    originals, actuals = 0.0, 0.0
    entries = []
    for job in jobs:
        state = job['state']
        counts[state] = counts.get(state, 0) + 1
        record = {'video': str(job['video']), 'state': state, 'output': str(job['output']),
                  'work_dir': str(job['work']), 'log': str(job['work'] / f"{job.get('stage', 'render')}.log")}
        if state == 'complete':
            result = read_json(job['work'] / 'render.status.json').get('result', {})
            originals += result.get('original_duration_s', 0)
            actuals += result.get('actual_duration_s', 0)
            record.update(result)
        if job.get('error'):
            record['error'] = job['error']
        if 'active_started' in job:
            record['active_elapsed_s'] = time.time() - job['active_started']
        entries.append(record)
    return {'schema_version': 1, 'stage': stage, 'started_at': started,
            'updated_at': time.time(), 'finished': finished,
            'run_wall_s': time.time() - started, 'total': len(jobs), 'counts': counts,
            'completed_original_s': originals, 'completed_output_s': actuals,
            'completed_saved_s': originals - actuals,
            'completed_saved_percent': (originals - actuals) / originals * 100 if originals else 0,
            'courses': entries}


def report_progress(progress, output):
    write_json(output / 'batch_progress.json', progress)
    counts = progress['counts']
    summary = (f"完成 {counts.get('complete', 0)}/{progress['total']}；"
               f"分析中 {counts.get('analyzing', 0)}，待 Agent 计划 {counts.get('needs_agent_plan', 0)}，"
               f"导出中 {counts.get('rendering', 0)}，失败 {counts.get('failed', 0)}；"
               f"本次墙钟 {hms(progress['run_wall_s'])}；已完成课程缩短 {hms(progress['completed_saved_s'])}")
    log(summary)
    lines = ['# 批处理进度与缩短时长', '', summary, '',
             f"已完成源时长：{hms(progress['completed_original_s'])}；成片：{hms(progress['completed_output_s'])}；"
             f"缩短比例：{progress['completed_saved_percent']:.1f}%。", '',
             '墙钟包含本次等待 Agent 的时间；恢复后重新计时。音视频并行阶段不能相加。', '',
             '| 课程 | 状态 | 原时长 | 成片时长 | 缩短 |', '| --- | --- | --- | --- | --- |']
    for course in progress['courses']:
        name = Path(course['video']).name.replace('|', '\\|')
        lengths = [hms(course[k]) if k in course else '—' for k in
                   ('original_duration_s', 'actual_duration_s', 'saved_s')]
        lines.append(f"| {name} | {course['state']} | {' | '.join(lengths)} |")
    target = output / 'batch_progress.md'
    temp = target.with_suffix('.tmp.md')
    temp.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    temp.replace(target)


def stop_child(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_batch(args):
    from video_editor import DEFAULT_CONFIG, get_config, work_paths
    folder = args.folder.resolve()
    output = (args.output or folder.with_name(folder.name + '_edited')).resolve()
    if args.stage == 'status':
        progress = load_json(output / 'batch_progress.json')
        print(json.dumps(progress, ensure_ascii=False, indent=2))
        return 0
    videos = discover(folder, output, args.recursive)
    if not videos:
        raise ValueError('源目录中未找到 MP4、MOV 或 MKV 视频')
    options = {key: getattr(args, key) for key in EXPORT_OPTIONS if getattr(args, key) is not None}
    config = {**DEFAULT_CONFIG, **get_config()}
    options = {key: options.get(key, config[key]) for key in EXPORT_OPTIONS}
    with coordinator_lock(output):
        started = time.time()
        jobs, running, attempted = [], {}, set()
        for video in videos:
            work, destination = job_paths(video, folder, output)
            source = source_signature(video)
            jobs.append({'video': video, 'work': work, 'output': destination, 'source': source,
                         'state': 'pending'})
        last_report = 0.0
        idle_since = None
        try:
            while True:
                for index, active in list(running.items()):
                    process, logfile = active
                    if process.poll() is not None:
                        logfile.close()
                        job = jobs[index]
                        status = read_json(job['work'] / f"{job['stage']}.status.json")
                        if process.returncode or not status.get('ok') or status.get('fingerprint') != job['fingerprint']:
                            job['state'] = 'failed'
                            job['error'] = status.get('error', f'工作进程退出 {process.returncode}；查看日志')
                        else:
                            job['state'] = 'pending'
                        job.pop('active_started', None)
                        del running[index]
                for index, job in enumerate(jobs):
                    if index in running or job['state'] == 'failed':
                        continue
                    work, video = job['work'], job['video']
                    ready = analyze_ready(job['source'], work, config)
                    _, plan, _ = work_paths(video, work)
                    fingerprint = render_fingerprint(job['source'], work, video, options)
                    previous = read_json(work / 'render.status.json')
                    current_output_stat = ([job['output'].stat().st_size, job['output'].stat().st_mtime_ns]
                                           if job['output'].exists() else None)
                    if (previous.get('ok') and previous.get('fingerprint') == fingerprint
                            and previous.get('output_stat') == current_output_stat and current_output_stat is not None):
                        job['state'] = 'complete'
                        continue
                    can_render = ((ready and (work / 'semantic_plan.json').exists())
                                  or (not (work / 'semantic_plan.json').exists() and plan.exists()))
                    if args.stage == 'analyze':
                        stage = None if ready else 'analyze'
                        job['state'] = 'needs_agent_plan' if ready else 'pending'
                    elif can_render:
                        stage = 'render'
                    elif args.stage == 'render':
                        stage = None
                        job['state'] = 'needs_agent_plan' if ready else 'needs_analysis'
                    else:
                        stage = None if ready else 'analyze'
                        job['state'] = 'needs_agent_plan' if ready else 'pending'
                    if stage is None or (index, stage) in attempted:
                        continue
                    active_count = sum(jobs[i]['stage'] == stage for i in running)
                    limit = args.analysis_workers if stage == 'analyze' else args.render_workers
                    if active_count >= limit:
                        continue
                    attempted.add((index, stage))
                    work.mkdir(parents=True, exist_ok=True)
                    job.update(stage=stage, fingerprint=fingerprint, active_started=time.time(),
                               state='analyzing' if stage == 'analyze' else 'rendering')
                    request_path = work / f'{stage}.request.json'
                    write_json(request_path, {'video': str(video), 'work': str(work), 'output': str(job['output']),
                                              'fingerprint': fingerprint, 'options': options,
                                              'asr_chunk_workers': args.asr_chunk_workers, 'overwrite': args.overwrite})
                    logfile = (work / f'{stage}.log').open('a', encoding='utf-8')
                    env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1',
                           'OMP_NUM_THREADS': '2', 'MKL_NUM_THREADS': '2'}
                    try:
                        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '_worker', stage,
                                                    str(request_path)], stdout=logfile, stderr=logfile, env=env,
                                                   start_new_session=os.name != 'nt')
                    except OSError:
                        logfile.close()
                        raise
                    running[index] = process, logfile
                    log(f'{stage} 开始：{video.name}（PID {process.pid}）')
                if time.time() - last_report >= args.progress_every:
                    report_progress(make_progress(jobs, started, args.stage), output)
                    last_report = time.time()
                if not running:
                    # Keep watching for newly written Agent plans only when requested.
                    waiting = any(j['state'] == 'needs_agent_plan' for j in jobs)
                    if args.stage in ('run', 'resume', 'render') and waiting and args.wait_for_plans > 0:
                        if idle_since is None:
                            idle_since = time.time()
                        if time.time() - idle_since < args.wait_for_plans:
                            time.sleep(1)
                            continue
                    break
                idle_since = None
                time.sleep(1)
        finally:
            for process, logfile in running.values():
                stop_child(process)
                logfile.close()
        progress = make_progress(jobs, started, args.stage, finished=True)
        report_progress(progress, output)
        print(json.dumps({key: value for key, value in progress.items() if key != 'courses'}, ensure_ascii=False, indent=2))
        return 1 if any(j['state'] == 'failed' for j in jobs) else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == '_worker':
        return worker(argv[1], Path(argv[2]))
    parser = argparse.ArgumentParser(description='多课程并行处理：ASR / Agent 计划 / smartcut 导出')
    parser.add_argument('stage', choices=('analyze', 'render', 'run', 'resume', 'status'))
    parser.add_argument('folder', type=Path)
    parser.add_argument('-o', '--output', type=Path)
    parser.add_argument('--recursive', action='store_true')
    parser.add_argument('--analysis-workers', type=int, default=4)
    parser.add_argument('--render-workers', type=int, default=2)
    parser.add_argument('--asr-chunk-workers', type=int, default=1)
    parser.add_argument('--progress-every', type=float, default=10)
    parser.add_argument('--wait-for-plans', type=float, default=0, help='空闲时等待 Agent 计划的秒数；默认返回待办')
    parser.add_argument('--overwrite', action='store_true')
    for name, choices in (('renderer', ('smartcut', 'encode')), ('video_encoder', ('auto', 'cpu', 'nvenc')),
                          ('video_decoder', ('auto', 'cpu', 'nvdec')), ('audio_coder', ('fast', 'twoloop')),
                          ('audio_encoder', ('auto', 'aac', 'mf')), ('audio_pipeline', ('auto', 'thread', 'process')),
                          ('av1_compatibility', ('auto', 'h264', 'off'))):
        parser.add_argument('--' + name.replace('_', '-'), choices=choices)
    args = parser.parse_args(argv)
    if not all(1 <= count <= 16 for count in (args.analysis_workers, args.render_workers)) or not 1 <= args.asr_chunk_workers <= 4:
        parser.error('课程并发数必须为 1 到 16，ASR 分段并发数必须为 1 到 4')
    if not 1 <= args.progress_every <= 300 or not 0 <= args.wait_for_plans <= 86400:
        parser.error('进度间隔必须为 1 到 300 秒，等待时间必须为 0 到 86400 秒')
    try:
        return run_batch(args)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f'批处理未完成：{exc}\n')


if __name__ == '__main__':
    sys.exit(main())
