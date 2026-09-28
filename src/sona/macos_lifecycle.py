"""Cocoa window hiding, Dock reopening and orderly application termination.

Imported only after pywebview has initialized its Cocoa backend. Subclass its
delegates so resize, appearance and WebView callbacks keep their behavior.
"""

import logging
from threading import Thread

import AppKit
import Foundation
import objc
from PyObjCTools import AppHelper
from webview.platforms.cocoa import BrowserView


logger = logging.getLogger(__name__)


class SonaApplicationDelegate(BrowserView.AppDelegate):
    def applicationShouldHandleReopen_hasVisibleWindows_(self, application, visible):
        self.owner.restore()
        return False

    def applicationShouldTerminate_(self, application):
        return AppKit.NSTerminateLater if self.owner.begin_termination() else AppKit.NSTerminateCancel

    @objc.typedSelector(b"v@:@")
    def finishTermination_(self, unused):
        BrowserView.app.replyToApplicationShouldTerminate_(True)


class SonaWindowDelegate(BrowserView.WindowDelegate):
    def windowShouldClose_(self, window):
        self.owner.hide()
        return False

    def windowDidExitFullScreen_(self, notification):
        objc.super(SonaWindowDelegate, self).windowDidExitFullScreen_(notification)
        self.owner.finish_hiding()

    def windowDidFailToExitFullScreen_(self, window):
        # Keep the window accessible if the system rejects the transition.
        self.owner.hide_after_fullscreen = False


class MacOSLifecycle:
    def __init__(self, window, shutdown):
        self.window = window
        self.shutdown = shutdown
        self.native = window.native
        self.quitting = False
        self.hide_after_fullscreen = False
        self.browser = BrowserView.instances[window.uid]
        self.application_delegate = SonaApplicationDelegate.alloc().init()
        self.application_delegate.owner = self
        self.window_delegate = SonaWindowDelegate.alloc().init()
        self.window_delegate.owner = self
        # Cocoa does not own these delegates; retain them in both controllers.
        old_app_delegate = BrowserView._shared_app_delegate
        old_window_delegate = self.browser._windowDelegate
        try:
            BrowserView.app.setDelegate_(self.application_delegate)
            self.native.setDelegate_(self.window_delegate)
        except Exception:
            BrowserView.app.setDelegate_(old_app_delegate)
            self.native.setDelegate_(old_window_delegate)
            raise
        BrowserView._shared_app_delegate = self.application_delegate
        self.browser._windowDelegate = self.window_delegate
        # pywebview constructs its menus after before_show and shown. Queue the
        # explicit Cmd+W menu item for the first iteration of the Cocoa loop.
        try:
            AppHelper.callAfter(self.install_close_menu)
        except Exception:
            logger.exception("无法安排关闭窗口菜单初始化")

    def install_close_menu(self):
        if self.quitting:
            return
        try:
            menu = BrowserView.app.mainMenu()
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("文件", None, "")
            submenu = AppKit.NSMenu.alloc().initWithTitle_("文件")
            submenu.addItemWithTitle_action_keyEquivalent_("关闭窗口", "performClose:", "w")
            item.setSubmenu_(submenu)
            menu.insertItem_atIndex_(item, 1)
        except Exception:
            # The backend also handles Cmd+W in its WebView keyDown fallback.
            logger.exception("无法添加关闭窗口菜单")

    def hide(self):
        if self.quitting or self.hide_after_fullscreen:
            return
        if self.native.styleMask() & AppKit.NSWindowStyleMaskFullScreen:
            self.hide_after_fullscreen = True
            self.native.toggleFullScreen_(None)
        else:
            self.native.orderOut_(None)

    def finish_hiding(self):
        self.browser.is_fullscreen = False
        if self.hide_after_fullscreen:
            self.hide_after_fullscreen = False
            if not self.quitting:
                self.native.orderOut_(None)

    def restore(self):
        if self.quitting:
            return
        self.hide_after_fullscreen = False
        if self.native.isMiniaturized():
            self.native.deminiaturize_(None)
        BrowserView.app.unhide_(None)
        self.native.makeKeyAndOrderFront_(None)
        BrowserView.app.activateIgnoringOtherApps_(True)

    def request_quit(self):
        # Updates call from a Python worker; all Cocoa operations stay on UI.
        AppHelper.callAfter(BrowserView.app.terminate_, None)

    def begin_termination(self):
        if self.quitting:
            return True
        self.quitting = True
        self.hide_after_fullscreen = False
        # Native terminate: can exit without unwinding webview.start(). Clean
        # services first, off the UI thread, including for logout/shutdown.
        try:
            Thread(target=self._stop_services, name="application-shutdown", daemon=False).start()
        except Exception:
            self.quitting = False
            logger.exception("无法启动退出清理，应用继续运行")
            return False
        try:
            self.native.orderOut_(None)
        except Exception:
            logger.exception("退出时无法隐藏窗口，继续清理后台任务")
        return True

    def _stop_services(self):
        try:
            self.shutdown()
        finally:
            # NSTerminateLater uses a modal run loop. A default-mode callAfter
            # can stall there, so explicitly include NSModalPanelRunLoopMode.
            pool = Foundation.NSAutoreleasePool.alloc().init()
            try:
                self.application_delegate.performSelectorOnMainThread_withObject_waitUntilDone_modes_(
                    "finishTermination:", None, False,
                    [Foundation.NSRunLoopCommonModes, AppKit.NSModalPanelRunLoopMode],
                )
            finally:
                del pool
