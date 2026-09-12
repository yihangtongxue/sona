"""Chinese native UI, matching the current Chinese-only application interface."""

import sys


WEBVIEW_ZH = {
    'global.quitConfirmation': '确定要退出吗？',
    'global.ok': '确定',
    'global.quit': '退出',
    'global.cancel': '取消',
    'global.saveFile': '保存文件',
    'cocoa.menu.about': '关于',
    'cocoa.menu.services': '服务',
    'cocoa.menu.view': '显示',
    'cocoa.menu.edit': '编辑',
    'cocoa.menu.hide': '隐藏',
    'cocoa.menu.hideOthers': '隐藏其他',
    'cocoa.menu.showAll': '显示全部',
    'cocoa.menu.quit': '退出',
    'cocoa.menu.fullscreen': '进入全屏幕',
    'cocoa.menu.cut': '剪切',
    'cocoa.menu.copy': '复制',
    'cocoa.menu.paste': '粘贴',
    'cocoa.menu.selectAll': '全选',
    'windows.fileFilter.allFiles': '所有文件',
    'windows.fileFilter.otherFiles': '其他文件类型',
    'linux.openFile': '选择文件',
    'linux.openFiles': '选择文件',
    'linux.openFolder': '选择文件夹',
}


def configure_native_language():
    """Run before importing AppKit/WebKit; never persist system/user preferences."""
    if sys.platform != 'darwin':
        return
    from Foundation import NSArgumentDomain, NSBundle, NSUserDefaults

    defaults = NSUserDefaults.standardUserDefaults()
    arguments = dict(defaults.volatileDomainForName_(NSArgumentDomain) or {})
    arguments['AppleLanguages'] = ['zh-Hans', 'zh']
    defaults.setVolatileDomain_forName_(arguments, NSArgumentDomain)

    # Source launches use Python's bundle, which may only declare English.
    # Mirror the packaged app's declaration in memory, before Cocoa initializes.
    info = NSBundle.mainBundle().infoDictionary()
    if info is not None:
        info['CFBundleDevelopmentRegion'] = 'zh-Hans'
        info['CFBundleLocalizations'] = ['zh-Hans']
        info['CFBundleAllowMixedLocalizations'] = True
