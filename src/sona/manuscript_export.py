"""Plain-text manuscript exports using a destination chosen by the user."""

from pathlib import Path
import re
import tempfile


def manuscript_filename(title: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', title).strip(' .')[:80].rstrip(' .')
    if not name:
        name = '文稿'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', name, re.I):
        name = '_' + name
    return name + '.txt'


def export_txt(manuscript: dict, destination: Path) -> None:
    if destination.suffix.lower() != '.txt':
        raise ValueError('请使用 .txt 作为文稿文件的后缀。')
    temporary = None
    try:
        # Finish writing before replacing an existing file selected in the
        # native save dialog, so a failed write preserves the previous export.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=destination.parent, prefix='.sona-manuscript-',
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(f"{manuscript['title']}\n\n{manuscript['body']}\n")
        temporary.replace(destination)
    except OSError:
        raise ValueError('文稿保存失败，请选择可写的位置并检查剩余空间。') from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
