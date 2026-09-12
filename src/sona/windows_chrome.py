"""Theme the existing WinForms titlebar and preload the WebView2 page theme."""

import ctypes
import json
import logging
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit


logger = logging.getLogger(__name__)
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY = 19
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_COLOR_DEFAULT = 0xFFFFFFFF


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
        build = sys.getwindowsversion().build
        self.native_dark_mode = build >= 17763  # Windows 10 1809+
        self.exact_colors = build >= 22000
        self.dark_attribute = DWMWA_USE_IMMERSIVE_DARK_MODE
        self.titlebar_state = None
        self.failed_attributes = set()
        self.set_attribute = None
        self.set_window_pos = None
        self.bootstrap_url = None
        self.bootstrap_source = None

    def attach(self):
        from Microsoft.Win32 import SystemEvents

        self.native = self.window.native
        self.webview = self.native.webview
        # real_url is resolved by pywebview when it starts the local HTTP server.
        self.bootstrap_url = urlsplit(urljoin(self.window.real_url, "js/theme-bootstrap.js"))
        try:
            self.bootstrap_source = (Path(__file__).with_name("web") / "js" / "theme-bootstrap.js").read_text(encoding="utf-8")
        except OSError:
            logger.exception("无法读取主题预加载脚本，将在页面连接后应用设置")

        if self.native_dark_mode:
            from ctypes import wintypes

            self.set_attribute = ctypes.WinDLL("dwmapi").DwmSetWindowAttribute
            self.set_attribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            self.set_attribute.restype = ctypes.c_long  # HRESULT
            self.set_window_pos = ctypes.WinDLL("user32", use_last_error=True).SetWindowPos
            self.set_window_pos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            self.set_window_pos.restype = wintypes.BOOL
        else:
            logger.warning("当前 Windows 版本不支持原生深色标题栏，需要 Windows 10 1809 或更新版本")

        SystemEvents.UserPreferenceChanged += self.schedule
        if self.native_dark_mode:
            # Its system-only handler would override a manually selected theme.
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

    def set_dwm(self, attribute, value, *, warn=True):
        value = ctypes.c_uint32(value)
        # HWND is pointer-sized; ToInt32 would truncate it in a 64-bit process.
        result = self.set_attribute(self.native.Handle.ToInt64(), attribute,
                                    ctypes.byref(value), ctypes.sizeof(value))
        if result < 0 and warn and attribute not in self.failed_attributes:
            self.failed_attributes.add(attribute)
            logger.warning("系统未接受标题栏属性 %s（HRESULT=%#x），保留系统外观", attribute, result & 0xFFFFFFFF)
        return result >= 0

    def apply_titlebar_mode(self, dark):
        if not self.native_dark_mode:
            return
        hwnd = self.native.Handle.ToInt64()
        state = (hwnd, dark)
        can_fallback = not self.exact_colors and self.dark_attribute == DWMWA_USE_IMMERSIVE_DARK_MODE
        success = self.set_dwm(self.dark_attribute, int(dark), warn=not can_fallback)
        if not success and can_fallback:
            # Older Windows 10 uses attribute 19. Probe 20 first, as in
            # Microsoft's PowerToys/ZoomIt implementation; never use 19 on Win11.
            self.dark_attribute = DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY
            success = self.set_dwm(self.dark_attribute, int(dark))
        if success and state != self.titlebar_state:
            # Repaint after a theme change without moving, resizing, activating
            # or changing the window's z-order. Retain all native frame behavior.
            self.titlebar_state = state
            # SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
            if not self.set_window_pos(hwnd, None, 0, 0, 0, 0, 0x0037):
                logger.warning("无法刷新 Windows 标题栏（错误码=%s）", ctypes.get_last_error())

    def apply(self):
        if self.closed or self.window.events.closed.is_set() or self.native.IsDisposed:
            return
        from System.Drawing import Color
        from System.Windows.Forms import Form, SystemInformation

        theme = self.appearance.get_theme()
        dark = self.is_dark(theme)
        try:
            # Match --chrome / --canvas in app.css. Only paint; do not change
            # FormBorderStyle, WndProc, native buttons, resize or snap behavior.
            chrome = 20 if dark else 249
            canvas = 24 if dark else 255
            self.native.BackColor = Color.FromArgb(chrome, chrome, chrome)
            self.webview.DefaultBackgroundColor = Color.FromArgb(canvas, canvas, canvas)
            self.apply_titlebar_mode(dark and not SystemInformation.HighContrast)
            if self.exact_colors:
                if SystemInformation.HighContrast:
                    self.set_dwm(DWMWA_CAPTION_COLOR, DWMWA_COLOR_DEFAULT)
                    self.set_dwm(DWMWA_TEXT_COLOR, DWMWA_COLOR_DEFAULT)
                else:
                    active = Form.ActiveForm == self.native
                    text = (237 if active else 173) if dark else (32 if active else 102)
                    self.set_dwm(DWMWA_CAPTION_COLOR, chrome * 0x010101)
                    self.set_dwm(DWMWA_TEXT_COLOR, text * 0x010101)
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
        if not self.native_dark_mode:
            SystemEvents.UserPreferenceChanged -= self.native.on_system_theme_changed
