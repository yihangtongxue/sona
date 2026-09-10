import webview
from pathlib import Path


def main() -> None:
    web_root = Path(__file__).with_name("web")
    icon_path = Path(__file__).resolve().parents[2] / "assets" / "Sona.icns"
    webview.create_window(
        "Sona",
        str(web_root / "index.html"),
        width=960,
        height=640,
        min_size=(960, 640),
    )
    webview.start(http_server=True, icon=str(icon_path))
