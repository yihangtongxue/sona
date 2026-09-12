"""Custom Windows caption, with sizing/moving still handled by the OS.

Keep WS_CAPTION/WS_THICKFRAME and the system menu, but remove their drawing
through WM_NCCALCSIZE. The exposed form surface performs native hit testing;
the WebView occupies only the content area, so it cannot swallow resize grips.
All methods and subclass installation run on the WinForms UI thread.
"""

import ctypes
import logging
from ctypes import wintypes


logger = logging.getLogger(__name__)
# comctl32 does not retain Python callbacks. Hold them until WM_NCDESTROY.
_live_frames = {}

WM_GETMINMAXINFO = 0x0024
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCACTIVATE = 0x0086
WM_NCDESTROY = 0x0082
WM_SYSCOMMAND = 0x0112
WM_DPICHANGED = 0x02E0
SC_MINIMIZE, SC_MAXIMIZE, SC_CLOSE, SC_RESTORE = 0xF020, 0xF030, 0xF060, 0xF120


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                ("work", wintypes.RECT), ("flags", wintypes.DWORD)]


class _MinMaxInfo(ctypes.Structure):
    _fields_ = [(name, wintypes.POINT) for name in
                ("reserved", "max_size", "max_position", "min_track", "max_track")]


class _AppBarData(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("hwnd", wintypes.HWND),
                ("callback", wintypes.UINT), ("edge", wintypes.UINT),
                ("rect", wintypes.RECT), ("param", ctypes.c_ssize_t)]


def _function(library, name, result, *arguments):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = list(arguments)
    return function


class WindowsFrame:
    def __init__(self, native):
        from System.Drawing import Color
        from System.Windows.Forms import Button, FlatStyle, ToolTip

        self.native = native
        self.closed = False
        self.laying_out = False
        self.scale = 1.0
        self.edge = 5
        self.caption_height = 37
        self.font = None
        self.buttons = []
        self.dark = False
        self.tooltip = ToolTip()
        self.load_api()
        for name, action in (("最小化", "minimize"), ("最大化", "maximize"), ("关闭", "close")):
            button = Button()
            button.AccessibleName = name
            button.Tag = action
            button.FlatStyle = FlatStyle.Flat
            button.FlatAppearance.BorderSize = 0
            button.UseVisualStyleBackColor = False
            button.BackColor = Color.FromArgb(249, 249, 249)
            button.Text = ""
            button.TabStop = True
            button.Click += lambda sender, args, action=action: self.command(action)
            button.Paint += self.paint_button
            button.MouseEnter += self.repaint_button
            button.MouseLeave += self.repaint_button
            self.tooltip.SetToolTip(button, name)
            self.buttons.append(button)
            native.Controls.Add(button)
            button.BringToFront()
        native.Paint += self.paint
        native.Resize += self.layout
        native.HandleCreated += self.install
        native.FormClosed += self.close
        self.layout()
        # Build the replacement controls before removing the original caption.
        # SetWindowSubclass requires the native handle's owning thread.
        self.install()

    def load_api(self):
        user = ctypes.WinDLL("user32", use_last_error=True)
        common = ctypes.WinDLL("comctl32", use_last_error=True)
        shell = ctypes.WinDLL("shell32")
        hwnd, uint, ptr = wintypes.HWND, wintypes.UINT, ctypes.c_void_p
        word, result = ctypes.c_size_t, ctypes.c_ssize_t
        callback_type = ctypes.WINFUNCTYPE(result, hwnd, uint, word, result, word, word)
        self.callback = callback_type(self.wndproc)
        self.set_subclass = _function(common, "SetWindowSubclass", wintypes.BOOL,
                                      hwnd, callback_type, word, word)
        self.remove_subclass = _function(common, "RemoveWindowSubclass", wintypes.BOOL,
                                         hwnd, callback_type, word)
        self.default = _function(common, "DefSubclassProc", result, hwnd, uint, word, result)
        self.window_rect = _function(user, "GetWindowRect", wintypes.BOOL, hwnd, ptr)
        self.is_zoomed = _function(user, "IsZoomed", wintypes.BOOL, hwnd)
        self.monitor_from_window = _function(user, "MonitorFromWindow", hwnd, hwnd, wintypes.DWORD)
        self.monitor_info = _function(user, "GetMonitorInfoW", wintypes.BOOL, hwnd, ptr)
        self.get_dpi = _function(user, "GetDpiForWindow", uint, hwnd)
        self.set_position = _function(user, "SetWindowPos", wintypes.BOOL, hwnd, hwnd,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, uint)
        self.post_message = _function(user, "PostMessageW", wintypes.BOOL, hwnd, uint, word, result)
        self.appbar_message = _function(shell, "SHAppBarMessage", word, wintypes.DWORD, ptr)

    def install(self, *_):
        if self.closed:
            return
        hwnd = self.native.Handle.ToInt64()
        if not self.set_subclass(hwnd, self.callback, 1, 0):
            raise RuntimeError("无法安装 Windows 自定义窗口边框")
        _live_frames[hwnd] = self
        # Recalculate the client area without changing placement or activation.
        if not self.set_position(hwnd, None, 0, 0, 0, 0, 0x0037):
            logger.warning("无法刷新自定义窗口边框（错误码=%s）", ctypes.get_last_error())

    def work_area(self, hwnd):
        monitor = self.monitor_from_window(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        info = _MonitorInfo()
        info.size = ctypes.sizeof(info)
        if not self.monitor_info(monitor, ctypes.byref(info)):
            return None
        # Leave an activation strip for auto-hidden taskbars on any monitor edge.
        for edge in range(4):
            bar = _AppBarData()
            bar.size = ctypes.sizeof(bar)
            bar.edge = edge
            bar.rect = info.monitor
            if self.appbar_message(0x000B, ctypes.byref(bar)):  # ABM_GETAUTOHIDEBAREX
                if edge == 0:
                    info.work.left += 2
                elif edge == 1:
                    info.work.top += 2
                elif edge == 2:
                    info.work.right -= 2
                else:
                    info.work.bottom -= 2
        return info

    def wndproc(self, hwnd, message, wparam, lparam, subclass_id, data):
        default_result = None
        try:
            if message == WM_NCDESTROY:
                self.remove_subclass(hwnd, self.callback, subclass_id)
                _live_frames.pop(hwnd, None)
            elif message == WM_NCCALCSIZE:
                # Both RECT and NCCALCSIZE_PARAMS start with the proposed RECT.
                # The full window becomes client area, including our caption.
                if lparam and self.is_zoomed(hwnd):
                    info = self.work_area(hwnd)
                    if info is not None:
                        rect = wintypes.RECT.from_address(lparam)
                        rect.left, rect.top = info.work.left, info.work.top
                        rect.right, rect.bottom = info.work.right, info.work.bottom
                return 0
            elif message == WM_GETMINMAXINFO:
                default_result = self.default(hwnd, message, wparam, lparam)
                info = self.work_area(hwnd)
                if info is not None:
                    limits = _MinMaxInfo.from_address(lparam)
                    limits.max_position.x = info.work.left - info.monitor.left
                    limits.max_position.y = info.work.top - info.monitor.top
                    limits.max_size.x = info.work.right - info.work.left
                    limits.max_size.y = info.work.bottom - info.work.top
                    # Preserve WinForms' minimum size and virtual-screen limits.
                return default_result
            elif message == WM_NCHITTEST:
                rect = wintypes.RECT()
                if self.window_rect(hwnd, ctypes.byref(rect)):
                    # Screen coordinates can be negative on secondary monitors.
                    x = ctypes.c_short(lparam & 0xFFFF).value - rect.left
                    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value - rect.top
                    width, height = rect.right - rect.left, rect.bottom - rect.top
                    edge = self.edge if not self.is_zoomed(hwnd) else 0
                    left, right = x < edge, x >= width - edge
                    top, bottom = y < edge, y >= height - edge
                    if top:
                        return 13 if left else 14 if right else 12
                    if bottom:
                        return 16 if left else 17 if right else 15
                    if left:
                        return 10
                    if right:
                        return 11
                    if y < self.caption_height:
                        if self.buttons and x >= self.buttons[0].Left:
                            return 1  # Let the custom Button children handle input.
                        # Real Button children receive their own mouse messages.
                        # Empty caption returns HTCAPTION: OS move loop, double
                        # click, Aero Snap, drag-to-restore and system menu.
                        return 2
                    return 1  # HTCLIENT
            elif message == WM_NCACTIVATE:
                # Keep activation semantics, suppress the old non-client paint.
                return self.default(hwnd, message, wparam, -1)
            elif message == WM_DPICHANGED:
                default_result = self.default(hwnd, message, wparam, lparam)
                self.layout()
                return default_result
        except Exception:
            # Never allow an exception to escape a ctypes window callback.
            logger.exception("处理 Windows 自定义边框消息失败：%#x", message)
        if default_result is not None:
            return default_result
        return self.default(hwnd, message, wparam, lparam)

    def layout(self, *_):
        if self.closed or self.native.IsDisposed or self.laying_out:
            return
        from System.Drawing import Font, FontStyle, GraphicsUnit, Rectangle
        from System.Windows.Forms import Padding

        self.laying_out = True
        self.native.SuspendLayout()
        try:
            self.scale = (self.get_dpi(self.native.Handle.ToInt64()) or 96) / 96
            maximized = bool(self.is_zoomed(self.native.Handle.ToInt64()))
            self.edge = 0 if maximized else max(4, round(5 * self.scale))
            height, width = round(32 * self.scale), round(46 * self.scale)
            self.caption_height = self.edge + height
            self.native.Padding = Padding(self.edge, self.caption_height, self.edge, self.edge)
            right = self.native.ClientSize.Width - self.edge
            for index, button in enumerate(self.buttons):
                button.Bounds = Rectangle(right - (3 - index) * width, self.edge, width, height)
            if self.buttons:
                label = "还原" if maximized else "最大化"
                self.buttons[1].AccessibleName = label
                self.tooltip.SetToolTip(self.buttons[1], label)
            size = float(12 * self.scale)
            if self.font is None or abs(float(self.font.Size) - size) > .01:
                previous = self.font
                self.font = Font("Segoe UI", size, FontStyle.Regular, GraphicsUnit.Pixel)
                if previous is not None:
                    previous.Dispose()
        finally:
            self.native.ResumeLayout(True)
            self.laying_out = False
        self.native.Invalidate()
        for button in self.buttons:
            button.Invalidate()

    def apply(self, dark):
        if self.closed or self.native.IsDisposed:
            return
        from System.Drawing import Color, SystemColors
        from System.Windows.Forms import Form, SystemInformation

        self.dark = dark
        active = Form.ActiveForm == self.native
        channel = 20 if dark else 249
        text = (237 if active else 173) if dark else (32 if active else 102)
        background = Color.FromArgb(channel, channel, channel)
        foreground = Color.FromArgb(text, text, text)
        hover = Color.FromArgb(45, 45, 45) if dark else Color.FromArgb(230, 230, 230)
        if SystemInformation.HighContrast:
            background, foreground, hover = SystemColors.Window, SystemColors.WindowText, SystemColors.Highlight
        self.native.BackColor = background
        self.native.ForeColor = foreground
        for button in self.buttons:
            button.BackColor = background
            button.ForeColor = foreground
            close = str(button.Tag) == "close"
            button.FlatAppearance.MouseOverBackColor = Color.FromArgb(196, 43, 28) if close else hover
            button.FlatAppearance.MouseDownBackColor = Color.FromArgb(170, 30, 20) if close else hover
            button.Invalidate()
        self.native.Invalidate()

    def paint(self, sender, args):
        if self.closed or self.font is None:
            return
        from System.Drawing import Color, Pen, Rectangle
        from System.Windows.Forms import TextFormatFlags, TextRenderer

        text_rect = Rectangle(round(12 * self.scale), self.edge,
                              max(0, self.native.ClientSize.Width - round(160 * self.scale)),
                              self.caption_height - self.edge)
        TextRenderer.DrawText(args.Graphics, self.native.Text, self.font, text_rect,
                              self.native.ForeColor, TextFormatFlags.VerticalCenter |
                              TextFormatFlags.SingleLine | TextFormatFlags.EndEllipsis |
                              TextFormatFlags.NoPrefix)
        if self.edge:
            pen = Pen(Color.FromArgb(69, 69, 69) if self.dark else Color.FromArgb(213, 213, 213))
            try:
                args.Graphics.DrawRectangle(pen, 0, 0, max(0, self.native.ClientSize.Width - 1),
                                             max(0, self.native.ClientSize.Height - 1))
            finally:
                pen.Dispose()

    def paint_button(self, button, args):
        if self.closed or button.IsDisposed:
            return
        from System.Drawing import Color, Pen
        from System.Windows.Forms import ControlPaint, Cursor

        action = str(button.Tag)
        hovered = button.ClientRectangle.Contains(button.PointToClient(Cursor.Position))
        color = Color.White if action == "close" and hovered else button.ForeColor
        pen = Pen(color, float(max(1, round(self.scale))))
        size = round(10 * self.scale)
        x, y = (button.Width - size) // 2, (button.Height - size) // 2
        try:
            if action == "minimize":
                args.Graphics.DrawLine(pen, x, y + size // 2, x + size, y + size // 2)
            elif action == "close":
                args.Graphics.DrawLine(pen, x, y, x + size, y + size)
                args.Graphics.DrawLine(pen, x + size, y, x, y + size)
            elif self.is_zoomed(self.native.Handle.ToInt64()):
                offset = max(2, round(2 * self.scale))
                args.Graphics.DrawRectangle(pen, x, y + offset, size - offset, size - offset)
                args.Graphics.DrawLine(pen, x + offset, y + offset, x + offset, y)
                args.Graphics.DrawLine(pen, x + offset, y, x + size, y)
                args.Graphics.DrawLine(pen, x + size, y, x + size, y + size - offset)
                args.Graphics.DrawLine(pen, x + size, y + size - offset, x + size - offset, y + size - offset)
            else:
                args.Graphics.DrawRectangle(pen, x, y, size, size)
            if button.Focused:
                rect = button.ClientRectangle
                rect.Inflate(-3, -3)
                ControlPaint.DrawFocusRectangle(args.Graphics, rect, color, button.BackColor)
        finally:
            pen.Dispose()

    def repaint_button(self, button, args):
        button.Invalidate()

    def command(self, action):
        if self.closed:
            return
        hwnd = self.native.Handle.ToInt64()
        command = {"minimize": SC_MINIMIZE, "close": SC_CLOSE,
                   "maximize": SC_RESTORE if self.is_zoomed(hwnd) else SC_MAXIMIZE}[action]
        # Follow normal Windows commands, including pywebview's close lifecycle.
        if not self.post_message(hwnd, WM_SYSCOMMAND, command, 0):
            logger.warning("无法执行窗口操作 %s（错误码=%s）", action, ctypes.get_last_error())

    def close(self, *_):
        if self.closed:
            return
        self.closed = True
        self.tooltip.Dispose()
        if self.font is not None:
            self.font.Dispose()
            self.font = None
        # The subclass must remain alive until the HWND receives WM_NCDESTROY.
