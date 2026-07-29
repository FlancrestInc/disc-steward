from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    from rapidocr_onnxruntime import RapidOCR
except ImportError:  # pragma: no cover - optional runtime dependency for OCR
    RapidOCR = None  # type: ignore[assignment]

from .models import ScannedFile, SubtitleStream

TEXT_SUBTITLE_CODECS = {"subrip", "srt", "webvtt", "mov_text", "ass", "ssa"}
IMAGE_SUBTITLE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
JAPANESE_LANGUAGE_CODES = {"ja", "jpn", "jp", "japanese"}

_SANITIZE_RE = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_SRT_TIMING_RE = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$")


@dataclass
class SubtitleSidecar:
    source_stream_index: int
    source_stream_ordinal: int
    codec: str | None
    language: str | None
    kind: str
    output_path: Path
    output_name: str
    warnings: list[str] = field(default_factory=list)


def build_subtitle_sidecar_name(video_name: str, stream: SubtitleStream, ordinal: int) -> str:
    stem = Path(video_name).stem
    language = _slug(stream.language) or "und"
    codec = _slug(stream.codec) or "subtitle"
    return f"{stem}.sub{ordinal + 1:02d}.{language}.{codec}.srt"


def extract_subtitle_sidecars(
    ffmpeg_path: str,
    ffprobe_path: str,
    source: ScannedFile,
    output_dir: Path,
    video_name: str,
    ffmpeg_runner: Callable[[list[str]], object] | None = None,
    ocr_engine: Any | None = None,
    convert_image_subtitles_to_srt: bool = True,
    ffprobe_runner: Callable[[list[str]], str] | None = None,
    ignored_source_stream_indexes: set[int] | None = None,
    ocr_backend: str = "auto",
    tesseract_path: str = "tesseract",
) -> list[SubtitleSidecar]:
    output_dir.mkdir(parents=True, exist_ok=True)
    streams = list(source.subtitle_streams)
    if not convert_image_subtitles_to_srt and any((stream.codec or "").lower() in IMAGE_SUBTITLE_CODECS for stream in streams):
        raise RuntimeError("image subtitle OCR must be enabled for strict SRT sidecars")
    results: list[SubtitleSidecar] = []
    ignored = ignored_source_stream_indexes or set()
    for ordinal, stream in enumerate(streams):
        if stream.index in ignored:
            continue
        output_path = output_dir / build_subtitle_sidecar_name(video_name, stream, ordinal)
        codec = (stream.codec or "").lower()
        if codec in TEXT_SUBTITLE_CODECS:
            _extract_text_subtitle(
                ffmpeg_path,
                source.path,
                stream.index,
                output_path,
                ffmpeg_runner=ffmpeg_runner,
            )
            results.append(
                SubtitleSidecar(
                    source_stream_index=stream.index,
                    source_stream_ordinal=ordinal,
                    codec=stream.codec,
                    language=stream.language,
                    kind="text",
                    output_path=output_path,
                    output_name=output_path.name,
                )
            )
            continue
        if codec in IMAGE_SUBTITLE_CODECS:
            warnings: list[str] = []
            active_ocr_engine = ocr_engine or _create_ocr_engine(
                stream.language,
                backend=ocr_backend,
                tesseract_path=tesseract_path,
            )
            if _extract_image_subtitle(
                ffmpeg_path,
                ffprobe_path,
                source,
                stream,
                ordinal,
                output_path,
                ocr_engine=active_ocr_engine,
                ffmpeg_runner=ffmpeg_runner,
                ffprobe_runner=ffprobe_runner,
                warnings=warnings,
            ):
                results.append(
                    SubtitleSidecar(
                        source_stream_index=stream.index,
                        source_stream_ordinal=ordinal,
                        codec=stream.codec,
                        language=stream.language,
                        kind="ocr",
                        output_path=output_path,
                        output_name=output_path.name,
                        warnings=warnings,
                    )
                )
            continue
        raise ValueError(f"unsupported subtitle codec for extraction: {stream.codec or 'unknown'}")
    return results


def _create_ocr_engine(language: str | None, *, backend: str, tesseract_path: str) -> Any:
    normalized_backend = (backend or "auto").strip().lower()
    if normalized_backend not in {"auto", "rapidocr", "tesseract"}:
        raise ValueError(f"unsupported subtitle OCR backend: {backend}")
    if normalized_backend == "tesseract" or (
        normalized_backend == "auto" and (language or "").strip().lower() in JAPANESE_LANGUAGE_CODES
    ):
        return TesseractOCR(tesseract_path=tesseract_path, language="jpn+eng")
    if RapidOCR is None:
        raise RuntimeError("rapidocr-onnxruntime is required to OCR image subtitle streams")
    return RapidOCR()


class TesseractOCR:
    """Small callable adapter for Tesseract's UTF-8 text output."""

    def __init__(self, *, tesseract_path: str, language: str) -> None:
        self.tesseract_path = tesseract_path
        self.language = language

    def __call__(self, image_path: Path) -> list[list[tuple[list[list[int]], str, float]]]:
        # Keep the arguments explicit: Tesseract emits UTF-8 text on stdout.
        command = [
            self.tesseract_path,
            str(image_path),
            "stdout",
            "--oem",
            "1",
            "--psm",
            "6",
            "-l",
            self.language,
        ]
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8")
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Tesseract is required for Japanese image subtitles but was not found: {self.tesseract_path}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip()
            raise RuntimeError(f"Tesseract subtitle OCR failed{': ' + detail if detail else ''}") from exc
        text = result.stdout.strip()
        if not text:
            return [None]  # type: ignore[list-item]
        return [[([[0, 0], [1, 0], [1, 1], [0, 1]], line, 1.0) for line in text.splitlines() if line.strip()]]


def _extract_text_subtitle(
    ffmpeg_path: str,
    source_path: str,
    stream_index: int,
    output_path: Path,
    ffmpeg_runner: Callable[[list[str]], object] | None = None,
) -> None:
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-y",
        "-nostdin",
        "-i",
        source_path,
        "-map",
        f"0:{stream_index}",
        "-c:s",
        "srt",
        str(output_path),
    ]
    _run_ffmpeg(command, ffmpeg_runner)
    if not output_path.exists():
        raise RuntimeError(f"ffmpeg did not create expected subtitle file: {output_path}")
    _validate_srt_text(output_path.read_text(encoding="utf-8"), stream_index)


def _extract_image_subtitle(
    ffmpeg_path: str,
    ffprobe_path: str,
    source: ScannedFile,
    stream: SubtitleStream,
    ordinal: int,
    output_path: Path,
    *,
    ocr_engine: Any,
    ffmpeg_runner: Callable[[list[str]], object] | None = None,
    ffprobe_runner: Callable[[list[str]], str] | None = None,
    warnings: list[str] | None = None,
) -> bool:
    if ffprobe_runner is None:
        packets = _subtitle_packets(ffprobe_path, source.path, ordinal)
    else:
        packets = _subtitle_packets(ffprobe_path, source.path, ordinal, ffprobe_runner=ffprobe_runner)
    if not packets:
        raise RuntimeError(f"no packets were found for subtitle stream {stream.index}")
    spans = _subtitle_spans(packets)
    fps = 2
    chunk_size = 40
    width = source.video.width or 720
    height = source.video.height or 480
    cues: list[tuple[float, float, str]] = []
    with tempfile.TemporaryDirectory(dir=output_path.parent) as tmpdir:
        tmp = Path(tmpdir)
        for chunk_start_index in range(0, len(packets), chunk_size):
            chunk_spans = spans[chunk_start_index : chunk_start_index + chunk_size]
            chunk_midpoints = [start + min((end - start) / 2.0, 2.0) for start, end in chunk_spans]
            if not chunk_midpoints:
                continue
            chunk_origin = max(0.0, chunk_spans[0][0] - 1.0)
            chunk_end = chunk_spans[-1][1] + 2.0
            chunk_duration = max(1.0, chunk_end - chunk_origin)
            frame_indices = [max(0, int(round((midpoint - chunk_origin) * fps))) for midpoint in chunk_midpoints]
            image_dir = tmp / f"chunk_{chunk_start_index:05d}"
            image_dir.mkdir(parents=True, exist_ok=True)
            _render_subtitle_frame_sequence(
                ffmpeg_path,
                source.path,
                ordinal,
                width,
                height,
                chunk_duration,
                fps,
                frame_indices,
                image_dir,
                ffmpeg_runner=ffmpeg_runner,
                source_offset=chunk_origin,
            )
            frame_paths = sorted(image_dir.glob("*.png"))
            chunk_texts: list[str] = []
            for frame_path in frame_paths:
                chunk_texts.append(_ocr_frame(frame_path, ocr_engine))
            for (start, end), text in zip(chunk_spans, chunk_texts, strict=False):
                if text:
                    cues.append((start, end, text))
    srt = _cues_to_srt(_merge_cues(cues))
    if not srt:
        raise RuntimeError(f"no recognized text was produced for subtitle stream {stream.index}")
    _validate_srt_text(srt, stream.index)
    output_path.write_text(srt, encoding="utf-8")
    return True


def _render_subtitle_frame(
    ffmpeg_path: str,
    source_path: str,
    subtitle_ordinal: int,
    seek_time: float,
    width: int,
    height: int,
    output_path: Path,
    ffmpeg_runner: Callable[[list[str]], object] | None = None,
) -> None:
    black_duration = max(seek_time + 5.0, 10.0)
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-y",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={width}x{height}:d={black_duration:.3f}",
        "-i",
        source_path,
        "-filter_complex",
        f"[0:v][1:s:{subtitle_ordinal}]overlay",
        "-ss",
        f"{seek_time:.3f}",
        "-frames:v",
        "1",
        str(output_path),
    ]
    _run_ffmpeg(command, ffmpeg_runner)
    if not output_path.exists():
        raise RuntimeError(f"ffmpeg did not render subtitle frame: {output_path}")


def _subtitle_packets(
    ffprobe_path: str,
    source_path: str,
    subtitle_ordinal: int,
    *,
    ffprobe_runner: Callable[[list[str]], str] | None = None,
) -> list[dict]:
    command = [
        ffprobe_path,
        "-v",
        "quiet",
        "-select_streams",
        f"s:{subtitle_ordinal}",
        "-show_packets",
        "-show_entries",
        "packet=pts_time,duration_time",
        "-of",
        "json",
        source_path,
    ]
    if ffprobe_runner is None:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        output = result.stdout
    else:
        output = ffprobe_runner(command)
    data = json.loads(output or "{}")
    return data.get("packets") or []


def _subtitle_spans(packets: list[dict]) -> list[tuple[float, float]]:
    spans: list[tuple[float, float]] = []
    for position, packet in enumerate(packets):
        start = float(packet["pts_time"])
        duration = float(packet.get("duration_time") or 0.0)
        if duration <= 0 and position + 1 < len(packets):
            next_start = float(packets[position + 1]["pts_time"])
            duration = max(next_start - start, 0.1)
        if duration <= 0:
            duration = 2.0
        spans.append((start, start + duration))
    return spans


def _subtitle_midpoints(packets: list[dict]) -> list[float]:
    return [start + min((end - start) / 2.0, 2.0) for start, end in _subtitle_spans(packets)]


def _render_subtitle_frame_sequence(
    ffmpeg_path: str,
    source_path: str,
    subtitle_ordinal: int,
    width: int,
    height: int,
    duration_seconds: float,
    fps: int,
    frame_indices: list[int],
    output_dir: Path,
    ffmpeg_runner: Callable[[list[str]], object] | None = None,
    source_offset: float = 0.0,
) -> None:
    select_expr = "+".join(f"eq(n\\,{index})" for index in sorted(set(frame_indices)))
    source_seek = max(0.0, source_offset)
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-y",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={width}x{height}:r={fps}:d={duration_seconds:.3f}",
        "-ss",
        f"{source_seek:.3f}",
        "-i",
        source_path,
        "-filter_complex",
        f"[0:v][1:s:{subtitle_ordinal}]overlay=shortest=0:eof_action=pass,select='{select_expr}',setpts=N/FRAME_RATE/TB",
        "-vsync",
        "vfr",
        str(output_dir / "frame_%05d.png"),
    ]
    _run_ffmpeg(command, ffmpeg_runner)
    if not list(output_dir.glob("*.png")):
        raise RuntimeError("ffmpeg did not render any subtitle frames")


def _ocr_frame(image_path: Path, ocr_engine: Any) -> str:
    result = ocr_engine(image_path)
    if not result or not result[0]:
        return ""
    items = []
    for entry in result[0]:
        box, text, confidence = entry
        if confidence is not None and confidence < 0.3:
            continue
        if not text:
            continue
        ys = [point[1] for point in box]
        xs = [point[0] for point in box]
        items.append((min(ys), min(xs), str(text).strip()))
    items.sort()
    lines = [text for _y, _x, text in items if text]
    return "\n".join(lines).strip()


def _merge_cues(cues: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    merged: list[tuple[float, float, str]] = []
    for start, end, text in cues:
        text = _normalize_text(text)
        if not text:
            continue
        if merged and merged[-1][2] == text and start <= merged[-1][1] + 0.5:
            previous_start, previous_end, previous_text = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end), previous_text)
            continue
        merged.append((start, end, text))
    return merged


def _cues_to_srt(cues: list[tuple[float, float, str]]) -> str:
    lines: list[str] = []
    for index, (start, end, text) in enumerate(cues, start=1):
        lines.append(str(index))
        lines.append(f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}")
        lines.extend(text.splitlines())
        lines.append("")
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def _format_srt_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    whole_seconds = int(secs)
    millis = int(round((secs - whole_seconds) * 1000))
    if millis == 1000:
        millis = 0
        whole_seconds += 1
    if whole_seconds == 60:
        whole_seconds = 0
        minutes += 1
    if minutes == 60:
        minutes = 0
        hours += 1
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def _run_ffmpeg(command: list[str], ffmpeg_runner: Callable[[list[str]], object] | None = None) -> None:
    if ffmpeg_runner is None:
        subprocess.run(command, check=True)
        return
    ffmpeg_runner(command)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\u00ad", "")).strip()


def _validate_srt_text(contents: str, source_stream_index: int) -> None:
    blocks = [block.strip() for block in re.split(r"\r?\n\s*\r?\n", contents.strip()) if block.strip()]
    if not blocks:
        raise RuntimeError(f"subtitle stream {source_stream_index} produced an empty SRT file")
    for expected_number, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        if len(lines) < 3 or lines[0].strip() != str(expected_number) or not _SRT_TIMING_RE.fullmatch(lines[1].strip()):
            raise RuntimeError(f"subtitle stream {source_stream_index} produced invalid SRT")
        if not any(line.strip() for line in lines[2:]):
            raise RuntimeError(f"subtitle stream {source_stream_index} produced an empty SRT cue")


def _slug(value: str | None) -> str:
    cleaned = _SANITIZE_RE.sub(" ", value or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.replace(" ", "_").lower()
