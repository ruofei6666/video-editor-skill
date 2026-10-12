#!/usr/bin/env python3
"""生成本地语义上下文并校验 Agent 写出的口播剪辑计划。"""

from __future__ import annotations

import array
import json
import math
import re
import sys
import wave
from pathlib import Path
from typing import Any

from common import format_timestamp, log

END_PUNCTUATION = re.compile(r"[。！？!?；;]$")
SOFT_PUNCTUATION = re.compile(r"[，,：:]$")


def _word_text(word: dict[str, Any]) -> str:
    return str(word.get("word") or word.get("text") or "").strip()


def transcript_atoms(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split ASR sentences into clause-level atomic utterances on punctuation."""
    atoms: list[dict[str, Any]] = []
    for segment in segments:
        words = [
            w
            for w in (segment.get("words") or [])
            if w.get("start") is not None and w.get("end") is not None and _word_text(w)
        ]
        if not words:
            text = str(segment.get("text") or "").strip()
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))
            if text and end > start:
                atoms.append({"start": start, "end": end, "text": text})
            continue

        current: list[dict[str, Any]] = []
        for index, word in enumerate(words):
            current.append(word)
            text = "".join(_word_text(item) for item in current)
            start = float(current[0]["start"])
            end = float(current[-1]["end"])
            next_start = (
                float(words[index + 1]["start"]) if index + 1 < len(words) else None
            )
            token = _word_text(word)
            hard_boundary = bool(END_PUNCTUATION.search(token))
            soft_boundary = bool(SOFT_PUNCTUATION.search(token))
            gap_boundary = next_start is not None and next_start - end >= 0.45
            duration_boundary = end - start >= 8.0
            if hard_boundary or soft_boundary or gap_boundary or duration_boundary:
                atoms.append({"start": start, "end": end, "text": text})
                current = []
        if current:
            atoms.append({
                "start": float(current[0]["start"]),
                "end": float(current[-1]["end"]),
                "text": "".join(_word_text(item) for item in current),
            })

    atoms.sort(key=lambda item: (item["start"], item["end"]))
    for index, atom in enumerate(atoms, start=1):
        atom["id"] = f"U{index:04d}"
    return atoms


def chunk_transcript_atoms(
    atoms: list[dict[str, Any]],
    target_duration: float = 210.0,
    overlap: float = 35.0,
) -> list[list[dict[str, Any]]]:
    """Split transcript atoms into overlapping windows aligned to natural speech gaps."""
    if not atoms:
        return []
    total_start = float(atoms[0]["start"])
    total_end = float(atoms[-1]["end"])
    if total_end - total_start <= target_duration:
        return [atoms]

    chunks: list[list[dict[str, Any]]] = []
    start_idx = 0
    n = len(atoms)
    while start_idx < n:
        chunk_start_time = float(atoms[start_idx]["start"])
        target_end_time = chunk_start_time + target_duration
        if target_end_time >= total_end:
            chunks.append(atoms[start_idx:])
            break

        best_end_idx = start_idx
        best_score = float("inf")
        for i in range(start_idx, n):
            cur_end = float(atoms[i]["end"])
            next_start = float(atoms[i + 1]["start"]) if i + 1 < n else cur_end
            gap = next_start - cur_end
            dist = abs(cur_end - target_end_time)
            score = dist - (15.0 if gap >= 0.8 else (5.0 if gap >= 0.4 else 0.0))
            if cur_end >= target_end_time - 30.0 and score < best_score:
                best_score = score
                best_end_idx = i
            if cur_end > target_end_time + 30.0:
                break

        chunk = atoms[start_idx : best_end_idx + 1]
        chunks.append(chunk)

        actual_end_time = float(chunk[-1]["end"])
        next_target_start = actual_end_time - overlap
        next_start_idx = best_end_idx
        for j in range(best_end_idx, start_idx, -1):
            if float(atoms[j]["start"]) <= next_target_start:
                next_start_idx = j
                break
        if next_start_idx <= start_idx:
            next_start_idx = best_end_idx + 1
        start_idx = next_start_idx

    return chunks


def build_prompt(atoms: list[dict[str, Any]]) -> str:
    rows = []
    for i, item in enumerate(atoms):
        if i > 0:
            gap = float(item["start"]) - float(atoms[i - 1]["end"])
            if gap >= 1.5:
                rows.append(f"--- [PAUSE {gap:.1f}s] ---")
        rows.append(
            f"[{item['id']} {format_timestamp(item['start'])}-{format_timestamp(item['end'])}] {item['text']}"
        )
    transcript_rows = "\n".join(rows)
    return f"""你正在为一段中文口播视频制作粗剪计划。
下面是带有时间戳和原子片段编号的转录。删掉口误、废话、无意义语气词，以及没有新信息的重复。定义、推导、例题、数字和结论必须保留。

可以标记：
- 口误、结巴、说错后改口的那一遍；后面有完整重讲时，删掉被放弃的前一遍，replacement_ids 指向保留的那一遍。
- 废话：与本段知识点无关的闲聊、玩笑、录制说明，以及没有信息的过渡。类别用 filler，可以不填 replacement_ids。
- 无意义语气词：单独成段的“啊、呃、嗯、哎、是吧、对吧、好吧、懂了吧、看懂没”。整段去掉后前后知识点仍然接得上。类别用 filler。
- 重复的话：同一意思已经讲清，后面又整段再说，且没有新例子、新数字、新条件或新结论。保留更完整的一遍，另一遍用 duplicate_take，replacement_ids 指向保留的那一遍。

不要标记：
- 一句里既有语气词，又有定义、定理、推导、例题或结论。不能为了抠掉一个“啊”把知识点一起剪掉。
- 重复里新出现的例子、反例、警告或计算步骤。
- 删掉后主语、谓语或结论会接不上的半句。
- 超过 2.5 秒的局部 delivery_cleanup 或 self_correction。单段 filler 不要超过 45 秒，更长就拆成多段。

removed_quote 必须逐字覆盖 remove_start_id 到 remove_end_id 的全部内容。

只返回严格 JSON：
{{
  "edits": [
    {{
      "remove_start_id": "U0001",
      "remove_end_id": "U0002",
      "cut_until_id": "U0003 or null",
      "replacement_ids": [
        "U0010"
      ],
      "removed_quote": "verbatim words copied from the removed IDs",
      "replacement_quote": "verbatim words copied from replacement IDs",
      "category": "filler | duplicate_take | explicit_restart | abandoned_take | self_correction | delivery_cleanup | recording_meta",
      "confidence": "high | medium | low",
      "reason": "specific evidence that the range is disposable"
    }}
  ]
}}

FULL TRANSCRIPT:
{transcript_rows}
"""


def build_semantic_context(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """给调用 Skill 的 Agent 准备转录上下文，不发起模型请求。"""
    atoms = transcript_atoms(segments)
    chunks = chunk_transcript_atoms(atoms, target_duration=210.0, overlap=35.0)
    return {
        "schema_version": 1,
        "purpose": "由调用 Skill 的 Agent 根据带时间戳转录判断语义删减，不上传视频或调用外部语义模型。",
        "atoms": atoms,
        "windows": [
            {
                "window_id": index + 1,
                "atom_ids": [atom["id"] for atom in chunk],
                "prompt": build_prompt(chunk),
            }
            for index, chunk in enumerate(chunks)
        ],
    }


def extract_json_from_text(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ValueError("语义分析返回了无效 JSON，未生成剪辑计划") from exc
    if (not isinstance(data, dict) or not isinstance(data.get("edits"), list)
            or any(not isinstance(edit, dict) for edit in data["edits"])):
        raise ValueError("语义分析响应缺少有效 edits 列表")
    return data


def grounding_text(text: str) -> str:
    return re.sub(r"[\s\.,!?;:\"'。！？!?；;，,：：“”'、…—\-_]+", "", text)


def candidates_from_plan(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
    model: str,
    max_local_cleanup_ms: float = 2500.0,
) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in atoms}
    positions = {item["id"]: idx for idx, item in enumerate(atoms)}
    candidates = []

    for raw in plan.get("edits") or []:
        start_id = raw.get("remove_start_id")
        end_id = raw.get("remove_end_id")
        if start_id not in by_id or end_id not in by_id:
            continue
        if positions[start_id] > positions[end_id]:
            continue

        start = float(by_id[start_id]["start"])
        end = float(by_id[end_id]["end"])
        spoken_end = end
        cut_until_id = raw.get("cut_until_id")
        if cut_until_id is None and positions[end_id] + 1 < len(atoms):
            cut_until_id = atoms[positions[end_id] + 1]["id"]
        if cut_until_id and cut_until_id in by_id:
            if positions[cut_until_id] == positions[end_id] + 1:
                end = float(by_id[cut_until_id]["start"])

        category = raw.get("category", "delivery_cleanup")
        confidence = str(raw.get("confidence") or "medium").lower().strip()
        if confidence not in {"high", "medium"}:
            continue

        replacement_ids = [
            str(item)
            for item in (raw.get("replacement_ids") or [])
            if str(item) in by_id
            and not (positions[start_id] <= positions[str(item)] <= positions[end_id])
        ]
        duration_ms = (end - start) * 1000.0
        min_ms = 180.0 if category == "filler" else 500.0
        if duration_ms < min_ms:
            continue
        # One filler span stays reviewable; longer asides are split into multiple edits.
        if category == "filler" and duration_ms > 45000.0:
            continue

        if category in {"delivery_cleanup", "self_correction"} and duration_ms > max_local_cleanup_ms:
            continue

        # Filler and recording asides do not need a replacement sentence.
        if category != "filler" and not replacement_ids and duration_ms > 3500.0:
            continue

        replacementless_local_cleanup = (
            category in {"self_correction", "delivery_cleanup"}
            and confidence in {"high", "medium"}
            and cut_until_id in by_id
            and positions[cut_until_id] == positions[end_id] + 1
            and duration_ms <= 3500.0
        )
        if (
            category not in {"recording_meta", "screen_pause", "filler"}
            and not replacement_ids
            and not replacementless_local_cleanup
        ):
            continue

        removed_atoms = atoms[positions[start_id] : positions[end_id] + 1]
        removed_text = "".join(item["text"] for item in removed_atoms)
        removed_quote_value = str(raw.get("removed_quote") or "").strip()
        gt_removed = grounding_text(removed_text)

        # Require the quote to cover the complete removed range.
        matched = False
        if gt_removed and grounding_text(removed_quote_value) == gt_removed:
            matched = True
        if not matched:
            continue

        replacement_text = "".join(by_id[item]["text"] for item in replacement_ids)
        candidates.append({
            "id": f"cand_{len(candidates) + 1:03d}",
            "start_ms": round(start * 1000.0),
            "end_ms": round(end * 1000.0),
            "duration_ms": round(duration_ms),
            "spoken_start_ms": round(start * 1000.0),
            "spoken_end_ms": round(spoken_end * 1000.0),
            "removed_text": removed_text,
            "kept_text": replacement_text,
            "replacement_ids": replacement_ids,
            "replacement_intervals": [
                [by_id[item]["start"] * 1000, by_id[item]["end"] * 1000]
                for item in replacement_ids
            ],
            "category": category,
            "confidence": confidence,
            "reason": str(raw.get("reason") or "").strip(),
            "model": model,
        })
    return candidates


def plan_video_cuts(
    segments: list[dict[str, Any]],
    semantic_plan: dict[str, Any] | None = None,
    *,
    model: str = "agent",
    max_local_cleanup_ms: float = 2500.0,
) -> list[dict[str, Any]]:
    """Validate an Agent-written semantic plan; never calls an external model."""
    atoms = transcript_atoms(segments)
    if semantic_plan is None:
        raise RuntimeError("语义判断由调用 Skill 的 Agent 完成；请先读取 semantic_context.json 并提交 semantic_plan.json")
    if not isinstance(semantic_plan, dict):
        raise ValueError("semantic_plan 必须是 JSON 对象")
    all_candidates = candidates_from_plan(
        semantic_plan, atoms, model, max_local_cleanup_ms=max_local_cleanup_ms
    )

    # Deduplicate candidates across overlapping windows
    all_candidates.sort(key=lambda c: (c["start_ms"], c["end_ms"]))
    deduped = []
    for c in all_candidates:
        if not deduped:
            deduped.append(c)
            continue
        prev = deduped[-1]
        # IoU check
        ov = max(
            0.0, min(prev["end_ms"], c["end_ms"]) - max(prev["start_ms"], c["start_ms"])
        )
        union = (
            max(prev["end_ms"], c["end_ms"]) - min(prev["start_ms"], c["start_ms"])
        )
        iou = ov / union if union > 0 else 0.0
        if iou >= 0.5:
            # Keep broader/higher confidence
            if c.get("confidence") == "high" and prev.get("confidence") != "high":
                deduped[-1] = c
        else:
            deduped.append(c)

    # Conservative global check: a replacement must survive every proposed cut.
    safe = [c for c in deduped if not any(
        max(rs, other["start_ms"]) < min(re, other["end_ms"])
        for rs, re in c["replacement_intervals"] for other in deduped
    )]
    if len(safe) != len(deduped):
        log(f"保留了 {len(deduped) - len(safe)} 个存在替代冲突的候选片段。")
    log(f"Agent Semantic Planner: found {len(safe)} validated candidate(s).")
    return safe


def refine_cut_boundaries_to_minima(
    cuts: list[dict[str, Any]],
    audio_path: Path,
    search_window_ms: float = 120.0,
) -> list[dict[str, Any]]:
    """Snap cut start and end to nearby waveform minima / zero-crossings."""
    if not cuts or not audio_path.exists():
        return cuts
    try:
        with wave.open(str(audio_path), "rb") as handle:
            sr = handle.getframerate()
            pcm = array.array("h")
            pcm.frombytes(handle.readframes(handle.getnframes()))
    except Exception:
        return cuts

    if sys.byteorder != "little":
        pcm.byteswap()
    refined = []
    half_win = int(sr * search_window_ms / 2000.0)
    for c in cuts:
        start = max(0, math.ceil(c["start_ms"] * sr / 1000.0))
        end = min(len(pcm), math.floor(c["end_ms"] * sr / 1000.0))
        if end <= start:
            raise ValueError("语义删除区间超出音频范围")
        # Only shrink a cut; waveform snapping must never consume retained speech.
        best_s = min(range(start, min(end, start + half_win + 1)),
                     key=lambda i: (abs(pcm[i]), abs(i - start)))
        best_e = min(range(max(start, end - half_win), end),
                     key=lambda i: (abs(pcm[i]), abs(i - end)))
        new_c = dict(c)
        if best_s < best_e:
            new_c["start_ms"] = best_s * 1000.0 / sr
            new_c["end_ms"] = best_e * 1000.0 / sr
        new_c["duration_ms"] = new_c["end_ms"] - new_c["start_ms"]
        refined.append(new_c)
    return refined
