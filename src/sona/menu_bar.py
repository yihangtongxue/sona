"""macOS menu-bar item: MCP status, active tasks, open/copy config/quit.

Imported only on macOS after pywebview initialized Cocoa; all methods run on
the AppKit main thread.
"""

from __future__ import annotations

import logging
import time

import AppKit
import Foundation
import objc

logger = logging.getLogger(__name__)
REFRESH_SECONDS = 5.0


def _format_time(timestamp) -> str:
    if not timestamp:
        return "尚无客户端请求"
    delta = time.time() - timestamp
    if delta < 60:
        return "最近请求：刚刚"
    if delta < 3600:
        return f"最近请求：{int(delta // 60)} 分钟前"
    return "最近请求：" + time.strftime("%m-%d %H:%M", time.localtime(timestamp))


class SonaStatusMenuDelegate(AppKit.NSObject):
    def menuNeedsUpdate_(self, menu):
        self.owner.rebuild(menu)

    @objc.typedSelector(b"v@:@")
    def openSona_(self, sender):
        self.owner.open_window()

    @objc.typedSelector(b"v@:@")
    def copyCodexConfig_(self, sender):
        self.owner.copy_config()

    @objc.typedSelector(b"v@:@")
    def quitSona_(self, sender):
        self.owner.quit()

    @objc.typedSelector(b"v@:@")
    def refresh_(self, timer):
        self.owner.refresh_title()


class MenuBar:
    def __init__(self, *, mcp, tools, codex_config, open_window, quit_app):
        self._mcp = mcp
        self._tools = tools
        self._codex_config = codex_config
        self._open_window = open_window
        self._quit_app = quit_app
        self._delegate = SonaStatusMenuDelegate.alloc().init()
        self._delegate.owner = self
        bar = AppKit.NSStatusBar.systemStatusBar()
        self.item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        button = self.item.button()
        image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_("waveform", "Sona")
        if image is not None:
            image.setTemplate_(True)
            button.setImage_(image)
            button.setImagePosition_(AppKit.NSImageLeft)
        else:
            button.setTitle_("Sona")
        button.setToolTip_("Sona")
        self.menu = AppKit.NSMenu.alloc().initWithTitle_("Sona")
        self.menu.setAutoenablesItems_(False)
        self.menu.setDelegate_(self._delegate)
        self.item.setMenu_(self.menu)
        self._timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            REFRESH_SECONDS, self._delegate, "refresh:", None, True)
        self.refresh_title()

    def _active(self):
        try:
            return self._tools.active_tasks(limit=5)
        except Exception:
            logger.exception("菜单栏无法读取任务")
            return []

    def refresh_title(self):
        try:
            count = len(self._active())
            self.item.button().setTitle_(f" {count}" if count else "")
        except Exception:
            logger.exception("菜单栏刷新失败")

    def _add(self, menu, title, action=None, enabled=False, indent=0):
        item = menu.addItemWithTitle_action_keyEquivalent_(title, action, "")
        if action:
            item.setTarget_(self._delegate)
        item.setEnabled_(enabled)
        item.setIndentationLevel_(indent)
        return item

    def rebuild(self, menu):
        menu.removeAllItems()
        status = self._mcp.status() if self._mcp is not None else {"running": False}
        if status.get("running"):
            self._add(menu, f"MCP：运行中（127.0.0.1:{status['port']}）")
            self._add(menu, _format_time(status.get("last_request_at")), indent=1)
        else:
            self._add(menu, "MCP：未运行" + (f"（{status['error'][:60]}）" if status.get("error") else ""))
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        active = self._active()
        self._add(menu, f"进行中的任务：{len(active)}" if active else "没有进行中的任务")
        for task in active:
            name = task["name"] if len(task["name"]) <= 32 else task["name"][:31] + "…"
            self._add(menu, f"{name} · {task['state_label']}", indent=1)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._add(menu, "打开 Sona", "openSona:", True)
        self._add(menu, "复制 Codex 配置", "copyCodexConfig:", True)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self._add(menu, "退出 Sona", "quitSona:", True)

    def open_window(self):
        try:
            self._open_window()
        except Exception:
            logger.exception("无法从菜单栏打开窗口")

    def copy_config(self):
        try:
            pasteboard = AppKit.NSPasteboard.generalPasteboard()
            pasteboard.clearContents()
            pasteboard.setString_forType_(self._codex_config()["snippet"], AppKit.NSPasteboardTypeString)
        except Exception:
            logger.exception("无法复制 Codex 配置")

    def quit(self):
        self._quit_app()

    def close(self):
        try:
            if self._timer is not None:
                self._timer.invalidate()
            AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(self.item)
        except Exception:
            logger.exception("无法移除菜单栏图标")
