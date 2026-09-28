"""WinForms tray integration, using pywebview's existing .NET runtime."""

import logging


logger = logging.getLogger(__name__)


class WindowsLifecycle:
    def __init__(self, window, settings):
        self.window = window
        self.settings = settings
        self.native = window.native
        self.quitting = False
        self.tray = None
        self.menu = None
        self.icon = None
        self.ready = False
        self.notice_shown = False
        self.restore_state = None
        try:
            self.create_tray()
        except Exception:
            self.dispose_tray()
            logger.exception("无法创建系统托盘，关闭窗口时将退出应用")
            return
        # Replace only the close handler so we can distinguish UserClosing
        # from Windows shutdown before forwarding to pywebview's checks.
        try:
            self.native.FormClosing -= self.native.on_closing
            self.native.FormClosing += self.on_closing
            self.native.FormClosed += self.on_closed
        except Exception:
            self.native.FormClosing -= self.on_closing
            self.native.FormClosing -= self.native.on_closing
            self.native.FormClosing += self.native.on_closing
            self.dispose_tray()
            raise

    def create_tray(self):
        from System.Drawing import SystemIcons
        from System.Windows.Forms import ContextMenuStrip, NotifyIcon, ToolStripMenuItem, ToolStripSeparator

        self.icon = (self.native.Icon or SystemIcons.Application).Clone()
        self.menu = ContextMenuStrip()
        open_item = ToolStripMenuItem("打开 Sona")
        open_item.Click += self.restore
        quit_item = ToolStripMenuItem("退出 Sona")
        quit_item.Click += self.quit_on_ui
        self.menu.Items.Add(open_item)
        self.menu.Items.Add(ToolStripSeparator())
        self.menu.Items.Add(quit_item)
        self.tray = NotifyIcon()
        self.tray.Icon = self.icon
        self.tray.Text = "Sona"
        self.tray.ContextMenuStrip = self.menu
        self.tray.MouseClick += self.on_tray_click
        self.tray.BalloonTipClicked += self.restore
        self.tray.Visible = True
        self.ready = True

    def on_tray_click(self, sender, args):
        from System.Windows.Forms import MouseButtons

        if args.Button == MouseButtons.Left:
            self.restore()

    def on_closing(self, sender, args):
        from System.Windows.Forms import CloseReason

        if (not self.quitting and args.CloseReason == CloseReason.UserClosing
                and self.ready and self.tray.Visible and self.settings.get_close_action() == "tray"):
            try:
                self.restore_state = self.native.WindowState
                self.native.Hide()
            except Exception:
                logger.exception("无法隐藏主窗口，将正常退出")
            else:
                args.Cancel = True
                self.show_background_notice()
                return
        self.native.on_closing(sender, args)
        if not args.Cancel:
            self.quitting = True

    def show_background_notice(self):
        if self.notice_shown or not self.settings.notice_required():
            return
        self.notice_shown = True
        try:
            from System.Windows.Forms import ToolTipIcon

            self.tray.ShowBalloonTip(5000, "Sona 正在后台运行",
                                    "转录和下载会继续。点击托盘图标打开，右键选择退出；可在设置中更改关闭行为。",
                                    ToolTipIcon.Info)
            self.settings.acknowledge_notice()
        except Exception:
            logger.exception("无法显示或保存后台运行提示")

    def restore(self, *_):
        if self.quitting or self.native.IsDisposed:
            return
        from System.Windows.Forms import FormWindowState

        self.native.Show()
        if self.native.WindowState == FormWindowState.Minimized:
            self.native.WindowState = (self.restore_state if self.restore_state == FormWindowState.Maximized
                                       else FormWindowState.Normal)
        self.native.Activate()
        self.native.BringToFront()

    def request_quit(self):
        from System import Action

        if not self.native.IsDisposed:
            self.native.BeginInvoke(Action(self.quit_on_ui))

    def quit_on_ui(self, *_):
        if self.quitting or self.native.IsDisposed:
            return
        self.quitting = True
        self.native.Close()
        if not self.native.IsDisposed:
            # Preserve recovery if another close handler vetoed termination.
            self.quitting = False

    def on_closed(self, *_):
        self.quitting = True
        self.dispose_tray()

    def dispose_tray(self):
        self.ready = False
        for name in ("tray", "menu", "icon"):
            resource = getattr(self, name)
            if resource is not None:
                try:
                    if name == "tray":
                        resource.Visible = False
                    resource.Dispose()
                except Exception:
                    logger.exception("无法释放托盘资源 resource=%s", name)
                finally:
                    setattr(self, name, None)
