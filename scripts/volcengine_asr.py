"""Doubao Speech 2.0 standard ASR; local audio goes only to the official API."""
from __future__ import annotations

import base64
import json
import hashlib
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import wave

from common import load_json, log, write_json
from transcriber import load_api_key, validate_transcript

SUBMIT_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
QUERY_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
RESOURCE = "volc.seedasr.auc"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the API key to a redirected destination.
        return None


def convert_response(raw: dict) -> list[dict]:
    result = raw.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("utterances"), list):
        raise ValueError("火山 ASR 未返回分句及逐字时间戳，已停止剪辑")
    segments = []
    try:
        for utterance in result["utterances"]:
            text = str(utterance.get("text") or "").strip()
            if not text:
                continue
            words = [{"word": str(w["text"]).strip(),
                      "start": float(w["start_time"]) / 1000,
                      "end": float(w["end_time"]) / 1000}
                     for w in utterance.get("words", []) if str(w.get("text") or "").strip()]
            segment = {"text": text, "start": float(utterance["start_time"]) / 1000,
                       "end": float(utterance["end_time"]) / 1000, "words": words}
            if any(w["start"] < segment["start"] or w["end"] > segment["end"] for w in words):
                raise ValueError("火山 ASR 字时间戳超出分句范围")
            segments.append(segment)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("火山 ASR 时间戳格式无效，已停止剪辑") from exc
    return validate_transcript(sorted(segments, key=lambda s: (s["start"], s["end"])))


def _failure(http_status, code) -> RuntimeError:
    # Do not echo provider response bodies, request headers, or credential values.
    if str(code) == "45000030":
        return RuntimeError(f"火山语音未授予资源权限：请开通并授权录音文件识别 2.0 标准版 {RESOURCE}（45000030）。")
    if http_status in (401, 403):
        return RuntimeError("火山语音鉴权失败，请检查语音控制台 API Key 和资源权限。")
    return RuntimeError(f"火山语音请求失败（HTTP {http_status}，状态码 {code}），未生成成功转录。")


def _post(endpoint: str, payload: dict, headers: dict) -> tuple[str, dict, str]:
    request = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=120) as response:
            code = response.headers.get("X-Api-Status-Code", "missing")
            if str(code) not in ("20000000", "20000001", "20000002"):
                raise _failure(response.status, code)
            return str(code), json.load(response), response.headers.get("X-Tt-Logid", "")
    except urllib.error.HTTPError as exc:
        raise _failure(exc.code, exc.headers.get("X-Api-Status-Code", "missing")) from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("火山语音网络连接失败或超时；未自动重交任务，请检查后重新运行。") from None


def transcribe_audio_volcengine(audio_path: Path, output_json: Path) -> list[dict]:
    if output_json.exists():
        return validate_transcript(load_json(output_json))
    if audio_path.stat().st_size > 100_000_000:
        raise ValueError("当前火山本地直传入口上限 100 MB，请先拆分较长视频后分别剪辑。")
    with wave.open(str(audio_path), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getframerate() != 16000 or audio.getsampwidth() != 2:
            raise ValueError("火山 ASR 入口要求 16 kHz 单声道 16-bit WAV。")
        if audio.getnframes() <= 0:
            raise ValueError("音频为空，不能提交转录。")
    audio_bytes = audio_path.read_bytes()
    audio_hash = hashlib.sha256(audio_bytes).hexdigest()
    job_path = output_json.with_name(f"{output_json.stem}_volcengine_job.json")
    try:
        job = load_json(job_path)
    except (OSError, ValueError):
        job = {}
    matching_job = isinstance(job, dict) and job.get("audio_sha256") == audio_hash and job.get("resource") == RESOURCE
    if not matching_job:
        job = {"request_id": str(uuid.uuid4()), "audio_sha256": audio_hash, "resource": RESOURCE}
    headers = {
        "Content-Type": "application/json", "X-Api-Key": load_api_key("volcengine"),
        "X-Api-Resource-Id": RESOURCE, "X-Api-Request-Id": job["request_id"], "X-Api-Sequence": "-1"}
    if not matching_job:
        upload_bytes = audio_bytes
        upload_format = "wav"
        if len(audio_bytes) > 1_000_000:
            upload_path = audio_path.with_name(f"{audio_path.stem}_asr.mp3")
            encoded = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(audio_path), "-ac", "1", "-ar", "16000",
                "-c:a", "libmp3lame", "-b:a", "48k", str(upload_path)
            ], capture_output=True)
            if encoded.returncode != 0:
                raise RuntimeError("语音上传音频编码失败，未提交云端任务。")
            upload_bytes = upload_path.read_bytes()
            upload_format = "mp3"
        payload = {"user": {"uid": "video-editor"},
                   "audio": {"data": base64.b64encode(upload_bytes).decode("ascii"), "format": upload_format},
                   "request": {"model_name": "bigmodel", "show_utterances": True,
                               "enable_itn": False, "enable_punc": True, "enable_ddc": False}}
        log("提交火山录音文件识别 2.0 标准版；保留口误与重复内容。")
        write_json(job_path, job)  # Keep the ID even if submission response times out.
        code, _, log_id = _post(SUBMIT_ENDPOINT, payload, headers)
        if code != "20000000":
            raise _failure(200, code)
        job["log_id"] = log_id
        write_json(job_path, job)  # No key; resume this task after a poll timeout.
    if job.get("log_id"):
        headers["X-Tt-Logid"] = job["log_id"]
    for attempt in range(120):
        time.sleep(5)
        code, raw, _ = _post(QUERY_ENDPOINT, {}, headers)
        if code == "20000000":
            segments = convert_response(raw)
            write_json(output_json, segments)
            log(f"火山转录已保存：{len(segments)} 个分句。")
            return segments
        if attempt % 6 == 0:
            log("火山转录仍在处理，继续查询同一任务。")
    raise RuntimeError("火山转录等待超过十分钟；任务信息已保存，重新运行会继续查询，不会重复提交。")
