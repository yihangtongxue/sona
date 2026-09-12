"""Coordinate the custom Windows frame and WebView2 page theme."""

import json
import logging
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .windows_frame import WindowsFrame


logger = logging.getLogger(__name__)


def configure_windows_chrome(window, appearance) -> None:
    controller = _WindowsChrome(window, appearance)
    # before_show runs on the WinForms UI thread after native controls exist.
    window.events.before_show += controller.attach


class _WindowsChrome:
    def __init__(self, window, appearance):
        self.window = window
        self.appearance = appearance
        self.native = None
        self.webview = None
        self.core = None
        self.profile_theme = None
        self.closed = False
        self.frame = None
        self.bootstrap_url = None
        self.bootstrap_source = None

    def attach(self):
        from Microsoft.Win32 import SystemEvents

        self.native = self.window.native
        self.webview = self.native.webview
        self.frame = WindowsFrame(self.native)
        # real_url is resolved by pywebview when it starts the local HTTP server.
        self.bootstrap_url = urlsplit(urljoin(self.window.real_url, "js/theme-bootstrap.js"))
        try:
            self.bootstrap_source = (Path(__file__).with_name("web") / "js" / "theme-bootstrap.js").read_text(encoding="utf-8")
        except OSError:
            logger.exception("无法读取主题预加载脚本，将在页面连接后应用设置")

        SystemEvents.UserPreferenceChanged += self.schedule
        # The backend's system-only handler must not override our theme.
        SystemEvents.UserPreferenceChanged -= self.native.on_system_theme_changed
        self.native.FormClosed += self.close
        self.native.HandleCreated += self.schedule
        self.native.Activated += self.schedule
        self.native.Deactivate += self.schedule
        self.appearance.on_change = self.schedule

        self.webview.CoreWebView2InitializationCompleted += self.on_webview_ready
        if self.webview.CoreWebView2 is not None:
            self.configure_core()
        self.apply()

    def schedule(self, *_):
        # SystemEvents and Python API calls may arrive from background threads.
        # Queue work; a synchronous Invoke here can deadlock with SystemEvents.
        if self.closed or self.window.events.closed.is_set():
            return
        try:
            from System import Action

            self.native.BeginInvoke(Action(self.apply))
        except Exception:
            if not self.closed and not self.window.events.closed.is_set():
                logger.exception("无法安排 Windows 外观更新")

    def is_dark(self, theme):
        if theme != "system":
            return theme == "dark"
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        except OSError:
            return False

    def apply(self):
        if self.closed or self.window.events.closed.is_set() or self.native.IsDisposed:
            return
        from System.Drawing import Color

        theme = self.appearance.get_theme()
        dark = self.is_dark(theme)
        try:
            # Match --chrome / --canvas in app.css on both Windows 10 and 11.
            self.frame.apply(dark)
            canvas = 24 if dark else 255
            self.webview.DefaultBackgroundColor = Color.FromArgb(canvas, canvas, canvas)
        except Exception:
            logger.exception("无法同步 Windows 窗口外观")

        if self.core is not None and theme != self.profile_theme:
            try:
                from Microsoft.Web.WebView2.Core import CoreWebView2PreferredColorScheme

                schemes = {"system": CoreWebView2PreferredColorScheme.Auto,
                           "light": CoreWebView2PreferredColorScheme.Light,
                           "dark": CoreWebView2PreferredColorScheme.Dark}
                # Auto restores OS tracking for media queries and native web UI.
                self.core.Profile.PreferredColorScheme = schemes[theme]
            except Exception:
                logger.exception("无法同步 WebView2 外观，页面仍使用已选择的主题")
            finally:
                # Activation events only need to repaint the native title text.
                # Avoid rewriting the profile or logging unsupported SDK features
                # every time the window gains focus. A new theme retries it.
                self.profile_theme = theme

    def on_webview_ready(self, sender, args):
        if self.closed or not args.IsSuccess:
            return
        # Initialization handlers run synchronously on the UI thread. pywebview
        # queues the first navigation in its handler; install our resource handler
        # before that navigation can request the page's blocking theme script.
        self.configure_core()
        self.apply()

    def configure_core(self):
        if self.core is not None:
            return
        self.core = self.webview.CoreWebView2
        if self.bootstrap_source is None:
            return
        try:
            from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceContext

            self.core.AddWebResourceRequestedFilter(self.bootstrap_url.geturl() + "*", CoreWebView2WebResourceContext.Script)
            self.core.WebResourceRequested += self.serve_bootstrap
        except Exception:
            logger.exception("无法预加载 Windows 主题，将在页面连接后应用")

    def serve_bootstrap(self, sender, args):
        if self.closed:
            return
        requested = urlsplit(str(args.Request.Uri))
        if requested._replace(query="", fragment="") != self.bootstrap_url._replace(query="", fragment=""):
            return
        try:
            from System.IO import MemoryStream
            from System.Text import Encoding

            # Serve only our local bootstrap script, with the current preference.
            # This also covers reloads, without accumulating document scripts or
            # relying on storage that WebView2 clears between private sessions.
            source = "window.__sonaThemePreference = " + json.dumps(self.appearance.get_theme()) + ";\n" + self.bootstrap_source
            stream = MemoryStream(Encoding.UTF8.GetBytes(source))
            # WebView2 retains and reads the response stream asynchronously.
            args.Response = self.core.Environment.CreateWebResourceResponse(
                stream, 200, "OK", "Content-Type: application/javascript; charset=utf-8\r\nCache-Control: no-store\r\n",
            )
        except Exception:
            logger.exception("无法提供 Windows 主题脚本，回退到静态脚本")

    def close(self, *_):
        if self.closed:
            return
        self.closed = True
        self.appearance.on_change = None
        from Microsoft.Win32 import SystemEvents

        # Static SystemEvents subscriptions must not retain a closed window.
        SystemEvents.UserPreferenceChanged -= self.schedule
