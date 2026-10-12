"""Manuscript and transcript exports (TXT, SRT, Markdown) to a user-chosen file."""

from pathlib import Path
import re
import tempfile

FORMATS = {"txt": ".txt", "srt": ".srt", "md": ".md"}
FORMAT_LABELS = {"txt": "文本文件", "srt": "SRT 字幕", "md": "Markdown"}


def normalize_format(value) -> str:
    value = (value or "txt").lower() if isinstance(value, str) else value
    if value == "markdown":
        value = "md"
    if value not in FORMATS:
        raise ValueError("导出格式只支持 TXT、SRT 或 Markdown。")
    return value


def manuscript_filename(title: str, export_format: str = "txt") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', title).strip(' .')[:80].rstrip(' .')
    if not name:
        name = '文稿'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', name, re.I):
        name = '_' + name
    return name + FORMATS[normalize_format(export_format)]


def _clock(seconds: float, separator: str = ",") -> str:
    milliseconds = max(0, int(round(float(seconds) * 1000)))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def short_clock(seconds: float) -> str:
    total = max(0, int(float(seconds)))
    hours, rest = divmod(total, 3600)
    return f"{hours:02d}:{rest // 60:02d}:{rest % 60:02d}"


def render_srt(document: dict) -> str:
    """SRT from stored segment timestamps; a transcript without segments becomes one cue."""
    segments = [segment for segment in document.get("segments") or () if str(segment.get("text", "")).strip()]
    if not segments:
        text = (document.get("text") or document.get("body") or "").strip()
        if not text:
            raise ValueError("没有可导出的字幕内容。")
        duration = float(document.get("duration") or 0) or 1.0
        segments = [{"start": 0.0, "end": duration, "text": text}]
    cues = []
    for index, segment in enumerate(segments, 1):
        start = float(segment.get("start") or 0)
        end = max(float(segment.get("end") or start), start + 0.001)
        text = re.sub(r"\n{2,}", "\n", str(segment["text"]).strip())
        cues.append(f"{index}\n{_clock(start)} --> {_clock(end)}\n{text}\n")
    return "\n".join(cues)


def render_markdown(document: dict) -> str:
    title = (document.get("title") or document.get("name") or "文稿").replace("\n", " ").strip()
    lines = [f"# {title}", ""]
    details = []
    if document.get("source_url"):
        details.append(f"- 来源：{document['source_url']}")
    if document.get("language"):
        details.append(f"- 语言：{document['language']}")
    if document.get("duration"):
        details.append(f"- 时长：{short_clock(document['duration'])}")
    if details:
        lines += [*details, ""]
    body = document.get("body")
    if body is None:
        segments = [segment for segment in document.get("segments") or () if str(segment.get("text", "")).strip()]
        if segments:
            body = "\n".join(f"**[{short_clock(segment['start'])}]** {str(segment['text']).strip()}  "
                             for segment in segments)
        else:
            body = document.get("text") or ""
    lines.append(body.strip())
    return "\n".join(lines).rstrip() + "\n"


def render_txt(document: dict) -> str:
    title = document.get("title") or document.get("name") or ""
    body = document.get("body") if document.get("body") is not None else document.get("text", "")
    return f"{title}\n\n{body}\n"


def render(document: dict, export_format: str) -> str:
    export_format = normalize_format(export_format)
    return {"txt": render_txt, "srt": render_srt, "md": render_markdown}[export_format](document)


def write_export(content: str, destination: Path, export_format: str = "txt") -> None:
    suffix = FORMATS[normalize_format(export_format)]
    if destination.suffix.lower() != suffix:
        raise ValueError(f'请使用 {suffix} 作为文件后缀。')
    temporary = None
    try:
        # Finish writing before replacing an existing file selected in the
        # native save dialog, so a failed write preserves the previous export.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=destination.parent, prefix='.sona-export-',
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.replace(destination)
    except OSError:
        raise ValueError('文稿保存失败，请选择可写的位置并检查剩余空间。') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def export_txt(manuscript: dict, destination: Path) -> None:
    if destination.suffix.lower() != '.txt':
        raise ValueError('请使用 .txt 作为文稿文件的后缀。')
    write_export(f"{manuscript['title']}\n\n{manuscript['body']}\n", destination, "txt")
