"""浏览器启动辅助：优先 playwright 自带 chromium，回退系统 Chrome。

背景：更新的 playwright（1.63）与本机老版系统 Chrome（111）搭配时，
headless 下 WebGL 不可用（`WebGL not supported`）。playwright 自带
chromium（153）headless 下 WebGL 2.0 正常。若本机未装 playwright
浏览器（旧环境），回退 channel='chrome'。
"""

CHROME_ARGS = ['--enable-unsafe-swiftshader']


def launch_browser(p, headless=True):
    """启动 chromium：优先 playwright 自带版本，失败回退系统 Chrome。"""
    try:
        return p.chromium.launch(headless=headless, args=CHROME_ARGS)
    except Exception as err:
        print('[browser] playwright chromium 不可用'
              f'（{type(err).__name__}: {str(err)[:100]}），回退系统 Chrome')
        return p.chromium.launch(channel='chrome', headless=headless,
                                 args=CHROME_ARGS)
