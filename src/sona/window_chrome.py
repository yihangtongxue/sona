"""Match the native titlebar background without replacing its interactions."""

import json
import logging
import sys


logger = logging.getLogger(__name__)


def configure_window_chrome(window, appearance) -> None:
    if sys.platform == "win32":
        from .windows_chrome import configure_windows_chrome
        configure_windows_chrome(window, appearance)
        return
    if sys.platform != "darwin":
        return

    bootstrap = None

    def apply():
        nonlocal bootstrap
        if window.events.closed.is_set():
            return
        import AppKit
        import WebKit
        from webview.platforms.cocoa import BrowserView

        theme = appearance.get_theme()
        native = window.native
        try:
            names = {"light": AppKit.NSAppearanceNameAqua, "dark": AppKit.NSAppearanceNameDarkAqua}
            # None restores system inheritance, including future system changes.
            native.setAppearance_(None if theme == "system" else AppKit.NSAppearance.appearanceNamed_(names[theme]))

            def chrome(current_appearance):
                match = current_appearance.bestMatchFromAppearancesWithNames_(list(names.values()))
                # Keep these values aligned with --chrome in app.css.
                channel = (20 if match == names["dark"] else 249) / 255
                return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(channel, channel, channel, 1)

            color = AppKit.NSColor.colorWithName_dynamicProvider_(None, chrome)
            native.setBackgroundColor_(color)
            # Stop AppKit's standard titlebar material from painting over our
            # color. Transparency affects drawing only: keep the normal content
            # bounds and native titlebar hit testing (no fullSizeContentView).
            native.setTitlebarAppearsTransparent_(True)
            # Match the container colored by pywebview's Cocoa backend. Do not
            # change the style mask, title, controls, geometry or hit testing.
            titlebar = native.contentView().superview().subviews().lastObject()
            titlebar.setBackgroundColor_(color)
            titlebar.setNeedsDisplay_(True)
        except Exception:
            logger.exception("无法同步 macOS 标题栏外观")

        try:
            # WKWebView is not attached to NSWindow yet during before_show.
            # Isolate the backend lookup here; seed the theme before first paint.
            browser = BrowserView.instances[window.uid].webview
            controller = browser.configuration().userContentController()
            script = WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
                "window.__sonaThemePreference = " + json.dumps(theme) + ";",
                WebKit.WKUserScriptInjectionTimeAtDocumentStart, True,
            )
            if bootstrap is not None:
                # Refresh our bootstrap for reloads while preserving other scripts.
                scripts = [item for item in controller.userScripts() if item != bootstrap]
                controller.removeAllUserScripts()
                for item in scripts:
                    controller.addUserScript_(item)
            controller.addUserScript_(script)
            bootstrap = script
        except Exception:
            logger.exception("无法预加载外观设置，将在页面连接后应用")

    def schedule():
        # API methods run on worker threads; AppKit changes belong to its UI thread.
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(apply)
        except Exception:
            # The preference is already saved; do not tell the page it failed.
            logger.exception("无法安排 macOS 外观更新")

    appearance.on_change = schedule
    # Synchronous Cocoa callback before the first navigation starts.
    window.events.before_show += apply
