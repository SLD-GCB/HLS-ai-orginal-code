#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆窗口.py — 主窗：三栏 + 菜单 + 工具条 + 状态栏

```
┌──────────────────────────────────────────────────────────────────┐
│ 文件  角色  接口  视图  帮助       [新建角色][接口配置][换主题][刷新] │
├───────────┬────────────────────────────────┬─────────────────────┤
│ 🔍 搜索    │  (头) 爱丽丝                    │  会话                │
│ ┌───────┐ │       ……你挡着光了。            │  [+ 新对话]          │
│ │头 爱丽丝│ │              ┌──────────────┐ │ ┌─────────────────┐ │
│ │头 鲍勃  │ │              │ 你好呀 (头)   │ │ │ 第一次见面       │ │
│ └───────┘ │              └──────────────┘ │ │ 12 条 · 刚刚     │ │
│ [新建][编辑]│ ┌──────────────────────────┐  │ └─────────────────┘ │
│ [删除]    │  │ 说点什么…        [发送]   │  │                     │
├───────────┴────────────────────────────────┴─────────────────────┤
│ 数据：…\ai酒馆\酒馆数据 │ 5 角色 │ 接口：本地大脑                │
└──────────────────────────────────────────────────────────────────┘
```

**这里是唯一一处把三块面板接起来的地方。** 面板之间不互相认识，全靠这个
文件转接。接的就四根线：

    角色页.选中了  →  对话页.换角色 + 会话栏.换角色 + 读这个角色的头像
    会话栏.选了    →  对话页.开会话
    会话栏.新开了  →  对话页.开会话
    对话页.要存了  →  会话栏.刷新

**开库不在这儿。** 数据落地是本机 JSON 文件（`酒馆数据/`，跟程序同目录），
打开就是建个目录，没有握手、没有端口、没有账号口令，所以**也没有"换库"这条
路了**——库的生死由 `酒馆.py` 统一管（开在起窗之前，关在收尾那一下 `关()`）。
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QLabel, QMainWindow,
                               QMessageBox, QSplitter, QStackedWidget,
                               QStatusBar, QToolBar, QWidget)

import 酒馆上下文页
import 酒馆助手页
import 酒馆本地
import 酒馆对话页
import 酒馆接口
import 酒馆接口页
import 酒馆模型页
import 酒馆角色
import 酒馆角色页
import 酒馆样式
from 酒馆工人 import 工位, 生成线

__all__ = ['主窗']

#: 三栏初始宽度。**得等 `show()` 之后再 `setSizes`**——show 之前窗口还没
#: 有真实几何，`setSizes` 传进去的数会被归一化掉，看着像"没生效"。
栏宽 = (250, 640, 250)


class 主窗(QMainWindow):
    """
    整台程序的主窗。

    `库` 是外面开好的。**这个类不负责开关库**——开库要口令、要处理失败，
    那是 `酒馆.py` 的事。这儿只管用。
    """

    def __init__(self, 库, 父=None):
        super().__init__(父)
        self._库 = 库
        self._工位 = None
        self._生成线 = None
        self._面板 = None            # 中间那块 QSplitter
        self._角色页 = None
        self._对话页 = None
        self._会话栏 = None
        self._拆过了 = False          # 见 `_拆面板` 那条——**关窗这条路上会被叫两次**
        self._接口编号 = 0            # 状态栏上写着的是哪套接口

        self.setWindowTitle('鸿胪寺')
        self.resize(1180, 760)
        self._搭菜单()
        self._搭工具条()
        self._搭状态栏()
        self._搭面板()

    # ── 面板 ──

    def _搭面板(self):
        """
        把三栏和两条线程搭起来。**开窗时走一次。**

        造的次序有讲究：`工位`（带工人线程）和 `生成线` 先起来，三个面板
        才拿得到它们。反过来不行——面板的构造函数里就要投活读数据。
        """
        self._拆过了 = False          # 拆过又搭起来了，重新允许下一次拆
        self._工位 = 工位(self._库)
        self._生成线 = 生成线()
        self._生成线.start()

        self._角色页 = 酒馆角色页.角色页(self._工位)
        self._对话页 = 酒馆对话页.对话页(self._工位, self._生成线)
        self._会话栏 = 酒馆对话页.会话栏(self._工位)

        # ── 四根线 ──
        self._角色页.选中了.connect(self._换了角色)
        self._角色页.改过了.connect(self._数角色)
        self._会话栏.选了.connect(self._对话页.开会话)
        self._会话栏.新开了.connect(self._对话页.开会话)
        self._对话页.要存了.connect(self._会话栏.刷新)
        self._对话页.会话变了.connect(self._会话变了)

        self._面板 = QSplitter(Qt.Horizontal)
        self._面板.addWidget(self._角色页)
        self._面板.addWidget(self._对话页)
        self._面板.addWidget(self._会话栏)
        self._面板.setStretchFactor(0, 0)
        self._面板.setStretchFactor(1, 1)      # 拉窗口时中间那栏吃掉多的
        self._面板.setStretchFactor(2, 0)

        # ── 页堆：0 = 酒馆三栏，1 = 编程助手 ──
        # ⚠ **用 `QStackedWidget` 把中心控件包起来，而不是给 splitter 加第 4 栏。**
        # 编程助手要整屏宽度放工具卡片和命令输出，挤在 640px 里读不了代码。
        # 包一层的好处是**上面那三栏的代码一个字没动** —— splitter 原样当
        # 第 0 页的孩子，`setStretchFactor` / `setSizes` 全部照旧。切页只是
        # 一次 `setCurrentIndex`，不重建控件、不重连信号，也就没有"切回来
        # 状态丢了"这类问题。
        self._助手页 = 酒馆助手页.助手页(self._工位, self)
        self._页堆 = QStackedWidget()
        self._页堆.addWidget(self._面板)
        self._页堆.addWidget(self._助手页)
        self.setCentralWidget(self._页堆)
        QTimer.singleShot(0, lambda: self._面板.setSizes(list(栏宽)))

        self._数角色()

    def _拆面板(self):
        """
        把面板和它那两条线程拆干净。**关窗必须走这一道。**

        ⚠ **这条路正常关一次窗就会走两遍**，所以必须是幂等的：

          1. 用户点右上角 ×（或 Ctrl+Q）→ `closeEvent` → `关掉()` → 拆干净、
             窗口关掉；
          2. `app.exec()` 这才返回 → `酒馆.py` 的 `main()` 里又调一次
             `窗.关掉()`。

        第一遍里 `self._面板.deleteLater()` 已经把**三个面板连带它们的
        子控件**全销毁了，可 `self._对话页` 这个 Python 名字还指着那个
        已经不存在的控件（`_拆面板` 只把 `_面板` 置了 `None`）。第二遍
        再 `self._对话页.关掉()` 就是 `self.表.stop()` 打在一个死掉的
        `QTimer` 上——`Internal C++ object already deleted`。

        后果比一个报错严重：**异常是在 `酒馆.py` 那句 `现在 = 窗.当前库()`
        （紧接着 `现在.关()`）之前抛的，所以库根本不会关**，连接就那么撂着。

        用旗子而不是 `if self._对话页 is not None`：那个判断在这里永远为真
        ——名字还在，只是它指的东西没了。`_搭面板` 里会把旗子放回去。
        """
        if self._拆过了:
            return True

        # ⚠ **先停工位，后停生成线。** 反过来的话，工位一旦停不干净（`停()`
        # 返回 `False`、线程还活着），生成线已经被杀掉了——这一步退不回去，
        # 窗口就卡在「面板还在、但发不出话」的半死状态上。先停工位的话，
        # 停不干净就原封不动地退出来，界面还是好的。
        #
        # 成功那条路上两种次序没差别：真在飞的 `结束` 信号要等主线程回到
        # 事件循环才投递，那时候旧的面板已经 `deleteLater` 了，接不到。
        if self._工位 is not None:
            if not self._工位.停():
                # 工人没停干净 = 线程还活着、`库` 还被它捏着。这时候去关库
                # 就是让一条活着的线程往关掉的库上写。**必须让用户知道。**
                QMessageBox.warning(
                    self, '工人线程没停下来',
                    '还有一件活没干完。这次就不动数据了——\n'
                    '接着关下去可能把文件写坏。\n\n'
                    '等它写完再关，或者直接结束进程。')
                return False
        if self._对话页 is not None:
            self._对话页.关掉()          # 停生成线（它只被对话页用）
        # ⚠ **助手页必须在 `卸载全部()` 之前停。** 它那条线程会在任务中途
        # 调本地模型，而 `卸载全部` 要拿 `酒馆本地.锁` —— 助手还在跑就去拿
        # 就是把主线程卡在这儿等它把这一轮说完，最长能到几十秒。
        if self._助手页 is not None:
            self._助手页.关掉()
        # 生成线停干净之后才放引擎——`卸载全部` 要拿 `酒馆本地.锁`，
        # 生成没停就去拿就是把主线程卡在这等它说完那句话。
        酒馆本地.卸载全部()
        if self._面板 is not None:
            # ⚠ **不要在这儿 `setParent(None)`。** 它和 `酒馆对话页._清空` 里
            # 那一句是同一条毛病：无父控件就是顶层窗口，而面板这会儿**正显示着**，
            # 一脱父就在 Windows 上真的建出一个带标题栏三键的窗口，随后
            # `deleteLater()` 再把它收掉——关窗那一下屏幕上多闪一个窗。
            # 面板本来就是主窗的子控件（`setCentralWidget`），`deleteLater()`
            # 一样销毁它，父子关系不用动。
            self._页堆.deleteLater()
            self._页堆 = None
            self._面板 = None
        self._助手页 = None
        self._工位 = None
        self._生成线 = None
        self._拆过了 = True
        return True

    # ── 菜单 / 工具条 / 状态栏 ──

    def _动作(self, 菜单, 文字, 槽, 快捷键=''):
        动 = QAction(文字, self)
        动.triggered.connect(槽)
        if 快捷键:
            动.setShortcut(QKeySequence(快捷键))
        菜单.addAction(动)
        return 动

    def _搭菜单(self):
        条 = self.menuBar()

        文件 = 条.addMenu('文件')
        self._动作(文件, '刷新', self.刷新, 'F5')
        文件.addSeparator()
        self._动作(文件, '退出', self.close, 'Ctrl+Q')

        角色 = 条.addMenu('角色')
        self._动作(角色, '新建角色', lambda: self._角色页._新建(), 'Ctrl+N')
        self._动作(角色, '编辑选中的角色', lambda: self._角色页._编辑())
        self._动作(角色, '删掉选中的角色', lambda: self._角色页._删除())

        会话 = 条.addMenu('会话')
        self._动作(会话, '上下文…', self._开上下文, 'Ctrl+K')

        接口 = 条.addMenu('接口')
        self._动作(接口, '接口配置…', self._开接口, 'Ctrl+I')
        self._动作(接口, '本地模型…', self._开模型, 'Ctrl+M')

        视图 = 条.addMenu('视图')
        self._编程动 = self._动作(视图, '编程助手', self._切编程, 'Ctrl+Shift+A')
        self._编程动.setCheckable(True)
        视图.addSeparator()
        self._动作(视图, '换明暗', self._换主题, 'Ctrl+T')

        帮助 = 条.addMenu('帮助')
        self._动作(帮助, '关于', self._关于)

    def _搭工具条(self):
        把 = QToolBar('主')
        把.setMovable(False)
        self.addToolBar(把)
        for 文字, 槽, 提示 in (
                ('新建角色', lambda: self._角色页._新建(), ''),
                ('接口配置', self._开接口, '模型 API 那几套配置'),
                ('上下文', self._开上下文, '这次到底会发给模型什么'
                                        '（「聊天会话」和「编程助手」两个页签）'),
                ('编程助手', self._切编程, '让本地模型改代码、跑命令（Ctrl+Shift+A）'),
                ('换明暗', self._换主题, '深浅色来回切'),
                ('刷新', self.刷新, 'F5')):
            动 = QAction(文字, self)
            动.triggered.connect(槽)
            if 提示:
                动.setToolTip(提示)
            把.addAction(动)

    def _搭状态栏(self):
        self.setStatusBar(QStatusBar())
        条 = self.statusBar()
        self._库标 = QLabel('')
        self._角标 = QLabel('')
        self._口标 = QLabel('')
        for 件, 伸 in ((self._库标, 1), (self._角标, 0), (self._口标, 0)):
            件.setContentsMargins(6, 0, 6, 0)
            条.addWidget(件, 伸)
        self._画库标()

    def _画库标(self):
        """
        状态栏左下角那格写着**数据存在哪**。

        这一格现在比数据库那会儿更有用：落地是本机文件，用户真要备份、
        要拷走、要看看存了些什么，就是去这个目录翻。写全路径，别写简称。
        """
        if self._库 is None:
            self._库标.setText('数据：没开')
            return
        self._库标.setText('数据：%s' % self._库.说明())
        self._库标.setToolTip('角色卡 / 接口配置 / 会话 / 消息都在这底下\n%s'
                            % self._库.说明())

    # ── 接线 ──

    def _换了角色(self, 角色编号):
        """
        左栏选中了一个角色。**三件事一起做**：中栏换角色、右栏换会话清单、
        把头像读出来。

        头像得由这儿统一读一次发给对话页，不能让每个气泡各读一遍——一屏
        几十个气泡就是几十次读库。
        """
        self._对话页.换角色(角色编号)
        self._会话栏.换角色(角色编号)
        self._对话页.头像 = None

        if not 角色编号:
            return

        def 有头像(图, _错):
            if 图:
                self._对话页.头像 = (图[0] if isinstance(图, (tuple, list))
                                   else 图)

        self._工位.干(lambda 库, n=角色编号: 酒馆角色.取头像(库, n), 有头像)

    def _会话变了(self, 会话编号):
        if not 会话编号:
            return
        self._会话栏.选编号(会话编号)

    # ── 状态栏那几个数 ──

    def _数角色(self):
        def 回来(数, 错):
            if 错 is None:
                self._角标.setText('%d 角色' % (数 or 0))
        # 表名得对。`酒馆存储.库.认表` 只是个白名单，拼错当场报错——它挡的
        # 是路径（表名要拼成文件名），顺带也挡住了"写错字悄悄数出个 0"。
        self._工位.干(lambda 库: 库.数('角色卡'), 回来)

    def _数接口(self):
        def 回来(套, 错):
            if 错:
                return
            self._接口编号 = (套.编号 if 套 else 0)
            self._口标.setText('接口：%s'
                              % ((套.名称 or '（没名字）') if 套
                                 else '一套都没配'))
            self._口标.setToolTip((套.模型 or '') if 套 else '去「接口配置」里建一套')

        # 状态栏写的是**聊天这条路**实际会用哪套（`接口编号=0` 落到的那套）。
        # 两条路线分开之后必须指明"对话" —— 不然它可能报出一个只给 agent 用的。
        self._工位.干(lambda 库: 酒馆接口.取或默认(库, 0, '对话'), 回来)

    # ── 菜单干的事 ──

    def _切编程(self):
        """
        酒馆三栏 ↔ 编程助手。切页只是一次 `setCurrentIndex`。

        ⚠ **切回第 0 页时要补一次 `setSizes`。** `QStackedWidget` 的每一页
        在没显示的时候没有真实几何，切回来那一瞬间布局还没算完，`setSizes`
        得等事件循环转一圈（`QTimer.singleShot(0, ...)`）才作数 —— 不补
        的话三栏宽度会回到默认比例，用户看着"我调的宽度没了"。

        助手页在跑任务时也能切走：它那条线程自己活着，切页不影响。
        """
        if self._页堆 is None:
            return
        在编程 = self._页堆.currentIndex() != 1
        self._页堆.setCurrentIndex(1 if 在编程 else 0)
        if hasattr(self, '_编程动') and self._编程动 is not None:
            self._编程动.setChecked(在编程)
        if not 在编程:
            QTimer.singleShot(0, lambda: self._面板.setSizes(list(栏宽)))

    def 刷新(self):
        self._角色页.刷新()
        self._会话栏.刷新()
        # 接口配置可能刚被改过/新加，中间那栏的下拉要跟着重读一遍
        self._对话页.重读接口()
        self._数角色()
        self._数接口()

    def _开接口(self):
        框 = 酒馆接口页.接口页(self._工位, self, self._接口编号)
        框.exec()
        if 框.改过了:
            self._数接口()
            # 刚建/刚改名的那套得立刻能选中，不然用户回去看下拉还是老几套
            self._对话页.重读接口()

    def _开模型(self):
        """「本地模型」管理框。模型文件动过之后接口下拉可能该重灌。"""
        框 = 酒馆模型页.模型页(self._工位, self)
        框.exec()

    def _开上下文(self):
        """
        开「上下文」框 —— **一个窗口两个页签**（聊天会话 / 编程助手）。

        默认落在哪一页**跟着你正在看的那一页走**：在酒馆三栏按 `Ctrl+K` 就是
        「聊天会话」，在编程助手页按就是「编程助手」。不这么做的话，你在助手页
        干完活想看记录，开出来先是一页聊天、还得自己切一下。

        ⚠ **这里不再拦"聊天正在生成"。**（早先的规矩是"在忙就不给开"，因为
        那个框能删消息，而生成线程收尾时会把 `覆盖消息(会话, 序号, …)` 把那一行
        写回来 —— 删了又长出来。）合并之后那条规矩会把「编程助手」那一页也一起
        挡在外面，而那页跟聊天正在生成毫无关系。所以改成把 `在忙` 传进去，
        让**「聊天会话」那一页自己只读**（保存/删/清空都灰着，并写明为什么）——
        出问题的动作被挡住了，看照旧能看。

        ⚠ **也不再拦"先挑一段对话"。** 没有会话时聊天那页自己会说"先在中间
        那栏挑一段"，而助手那页本来就有东西可看 —— 拿一个弹窗把人挡在门外，
        比让他进去看看更糟。
        """
        看哪 = 1 if self._页堆.currentIndex() == 1 else 0
        框 = 酒馆上下文页.上下文框(
            self._工位, self,
            会话编号=self._对话页.现在哪段(), 看哪=看哪,
            忙查=self._对话页.在忙,
            # 项目记忆按工作目录挂，「编程助手」那一页要知道现在在哪个目录。
            # 走助手页的口子，别去掏它的 `_根`。
            目录=self._助手页.现在哪个目录(),
            # 记忆的 token 上限要按同一套接口的窗口算，两处各算一份就会对不上
            窗口=self._助手页.现在窗口())
        框.exec()
        if 框.改过了:
            # 消息被删过 / 预设改过，中间和右边都得重读一遍
            self._对话页.刷新()
            self._会话栏.刷新()

    def _换主题(self):
        """
        深浅色来回切。

        ⚠ `酒馆样式.装` 收的是 **`QApplication`**，不是窗口——传错不报错，
        只是不生效，属于最难查的那一类。所以从 `QApplication.instance()` 拿。
        """
        下个 = 'dark' if 酒馆样式.当前明暗 == 'light' else 'light'
        酒馆样式.装(QApplication.instance(), 下个)

    def 当前库(self):
        """现在用的哪个库。**`酒馆.py` 靠这个在收尾时关它。**"""
        return self._库

    def _关于(self):
        QMessageBox.information(
            self, '关于',
            'AI 酒馆\n\n'
            '角色卡、接口配置、会话消息都存在本机\n'
            '%s 里，一个 JSON 一个文件。\n'
            '界面：PySide6' % (self._库.说明() if self._库 else '酒馆数据'))

    # ── 收尾 ──

    def 关掉(self):
        """**窗口要关之前必须叫这个**，它会停掉两条线程。返回 `False` 是没停干净。"""
        return self._拆面板()

    def closeEvent(self, 事):
        """
        关窗收尾。**没停干净就不关。**

        工人线程停不掉的时候（`wait` 超时）线程还活着、`库` 还被它捏着，
        这时候放行去关库，就是让一条活着的线程往关掉的库上写。宁可留个
        窗口让用户自己结束进程。
        """
        if not self.关掉():
            事.ignore()
            return
        事.accept()
