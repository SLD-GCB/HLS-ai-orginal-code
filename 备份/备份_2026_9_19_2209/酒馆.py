#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆.py — 入口

    python 酒馆.py

**没有连接这一步了。** 存储落地是本机 JSON 文件（`酒馆数据/`，跟本程序同目录），
打开就是 `os.makedirs` 一下，没有握手、没有端口、没有账号口令，也就没有
"连不上"这回事。

这一层只干四件事，别的全在下面那些模块里：

  1. 把控制台弄成 UTF-8（不去修的话 Windows 上第一条中文输出就崩）
  2. 起 `QApplication`、上样式
  3. 开库（`酒馆存储.打开`，连带把数据目录立起来）
  4. **保证退出时两条线程先停、库后关**——见 `main` 里那个顺序
"""

import sys

__all__ = ['main']


def 修控制台():
    """
    把标准输出掰成 UTF-8。

    ⚠ 不调的话，Windows 上第一条中文或 `✓` 输出就会崩：

        UnicodeEncodeError: 'gbk' codec can't encode character ...

    Windows 控制台默认代码页是 GBK(cp936)。**这不是"把标记换成 ASCII 就完了"**
    ——角色名、模型回的话、异常里的原文，任何一处非 GBK 字符都会照样崩，而且
    是在 `print` 里崩，真正的结果被一堆 traceback 顶掉。根子在输出流的编码，
    就在这儿修。

    幂等，修不动就静默跳过。
    """
    try:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for 名 in ('stdout', 'stderr'):
        流 = getattr(sys, 名, None)
        设 = getattr(流, 'reconfigure', None)
        if 设 is None:                      # 被换成了别的东西，不硬来
            continue
        现在 = (getattr(流, 'encoding', '') or '').lower().replace('-', '')
        if 现在 == 'utf8':
            continue
        try:
            设(encoding='utf-8', errors='replace')
        except Exception:
            pass


def main(参数=None):
    修控制台()

    from PySide6.QtWidgets import QApplication, QMessageBox

    import 酒馆窗口
    import 酒馆存储
    import 酒馆样式

    # ⚠ `QApplication(sys.argv[:1])` 只传 `argv[0]`。传整个 `argv` 的话 Qt
    # 会去解我们的命令行参数，碰上看不懂的当场退出。
    app = QApplication(sys.argv[:1])
    酒馆样式.装(app, 'light')

    try:
        库 = 酒馆存储.打开()
    except Exception as 错:
        # 建不出数据目录（目录只读、盘满了之类）。**不能只往终端打一行就退**
        # ——双击启动的人根本看不见终端，只会觉得"点了没反应"。
        QMessageBox.critical(None, '打不开',
                             '数据目录建不起来。\n\n%s：%s\n\n%s'
                             % (type(错).__name__, 错, 酒馆存储.默认数据目录()))
        return 2

    窗 = 酒馆窗口.主窗(库)
    窗.show()

    码 = app.exec()

    # ⚠ 顺序不能反。**线程先停干净，库最后关。**
    # 反过来的话，一条还活着的工人线程会往一个已经关掉的库上写——那是全项目
    # 一直在防的那种坏法，而且是静默的。
    if not 窗.关掉():
        print('警告：工人线程没停下来，库这次不关了。', file=sys.stderr)
        return 3
    现在 = 窗.当前库()
    if 现在 is not None:
        try:
            现在.关()
        except Exception as 错:
            print('库没关干净：%s：%s' % (type(错).__name__, 错), file=sys.stderr)
    return 码


if __name__ == '__main__':
    sys.exit(main())
