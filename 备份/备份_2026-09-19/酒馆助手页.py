#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
酒馆助手页.py — 编程助手的界面

```
┌──────────────────────────────────────────────────────────────────┐
│ 工作目录：D:\myproj [换…]   模型：Qwen3.5-9B   [设置]              │
│ 上下文 ▓▓▓▓▓░░░░░ 4.2K/8K   步 5/12   上一步 prefill 7.3s        │
├──────────────────────────────────────────────────────────────────┤
│  ▸ 任务  把 main.py 跑起来，报错就修好它                          │
│  ▸ 助手  我先看看目录结构。                                       │
│  ┌ ⚙ 读文件 ─ src/main.py (1-120) ────────────── ✓ 0.3s ┐        │
│  ┌ ▶ 跑命令 ─ python main.py ─────────── ✓ 退出码 1 ────┐        │
│  ┌ ⚠ 要修改文件 ─ src/main.py ─────────────────────────┐         │
│  │  [允许一次]  [本任务总是允许]  [拒绝]               │         │
├──────────────────────────────────────────────────────────────────┤
│ [说个任务…（Ctrl+Enter 开始）]                                    │
│ ⏳ 正在处理提示词（4.2K，预计约 8 秒）…        [停止] [开始]      │
└──────────────────────────────────────────────────────────────────┘
```

════════════════════════════════════════════════════════════════════
三条界面上的硬规矩
════════════════════════════════════════════════════════════════════

**一、工具卡片正文默认折叠。** 读一次 200 行就把转录本淹了，用户再也找不到
审批条 —— 而审批条是**必须被看见**的东西。

**二、prefill 期间必须明说"停不下来"。** 用户在这台机器上实测过 17K 提示要
等 52 秒。不说的话他会以为程序死了，去点停止，然后发现停止按钮也没反应，
最后关窗杀进程。**明说反而不会引发误操作** —— 这是这个项目一贯的诚实风格。

**三、失败不弹 `QMessageBox`。** 弹窗会打断流程、还要多一次点击。直接在
卡片上红框显示 —— 跟 `酒馆对话页._收坏` 一个做法。
"""

import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QMessageBox, QPlainTextEdit,
                               QProgressBar, QScrollArea, QSpinBox,
                               QToolButton, QVBoxLayout, QWidget)

import 酒馆助手
import 酒馆助手线
import 酒馆接口
import 酒馆本地
import 酒馆样式
import 酒馆工具

__all__ = ['助手页']

#: 工具名 → 界面上的中文标签。
标签们 = {
    'read_file': '读文件', 'list_dir': '列目录', 'search': '找内容',
    'write_file': '写文件', 'edit_file': '改文件', 'run_cmd': '跑命令',
}

#: 状态图标。**不用 emoji** —— 这条链上全是中文界面，emoji 在 Windows 上
#: 常缺字变框框，反而更难看（`酒馆样式.做按钮` 那份注释同理）。
态标 = {'跑': '·', '成': '✓', '坏': '×', '拒': '—', '问': '!'}

#: 阶段条多久刷一次"已等 N 秒"。
走秒毫秒 = 200


def _卡头(卡):
    """卡片标题行：干什么、对什么、结果如何。"""
    名 = 标签们.get(卡.get('名'), 卡.get('名') or '?')
    参 = 卡.get('参数') or {}
    对 = ''
    for 键 in ('path', 'cmd', 'pattern'):
        if 参.get(键):
            对 = str(参[键]).replace('\n', ' ')
            break
    if 卡.get('名') == 'read_file' and 参.get('start'):
        对 += ' (第 %s 行起，%s 行)' % (参.get('start'), 参.get('count') or '?')
    return '%s %s%s' % (态标.get(卡.get('态'), '·'), 名,
                      ('  %s' % 对[:70]) if 对 else '')


class 卡片(QFrame):
    """
    一张工具调用卡。**正文默认折叠**（见文件头第一条）。

    审批按钮长在这张卡里，不是弹窗 —— 用户要能一边看前面几步干了什么、
    一边决定批不批。
    """

    def __init__(self, 卡数据, 父=None):
        super().__init__(父)
        self.setObjectName('工具卡')
        self._卡 = dict(卡数据)
        self._展开 = False

        排 = QVBoxLayout(self)
        排.setContentsMargins(8, 6, 8, 6)
        排.setSpacing(4)

        头行 = QHBoxLayout()
        头行.setContentsMargins(0, 0, 0, 0)
        self.箭头 = QToolButton()
        self.箭头.setArrowType(Qt.RightArrow)
        self.箭头.setAutoRaise(True)
        self.箭头.clicked.connect(self.翻)
        self.标题 = QLabel(_卡头(self._卡))
        self.标题.setTextInteractionFlags(Qt.TextSelectableByMouse)
        头行.addWidget(self.箭头)
        头行.addWidget(self.标题, 1)
        排.addLayout(头行)

        self.正文 = QPlainTextEdit()
        self.正文.setReadOnly(True)
        self.正文.setMaximumHeight(220)
        self.正文.setVisible(False)
        排.addWidget(self.正文)

        # 审批那一行（只在"要批"时出现）
        self.批行 = QWidget()
        批排 = QHBoxLayout(self.批行)
        批排.setContentsMargins(0, 0, 0, 0)
        self.准 = 酒馆样式.做按钮('允许一次', None, '只放行这一次', self)
        self.总准 = 酒馆样式.做按钮('本任务总是允许', None,
                                '同一个文件/同一条命令，这个任务里不再问', self)
        self.拒 = 酒馆样式.做按钮('拒绝', None, '这次不执行', self)
        批排.addWidget(self.准)
        批排.addWidget(self.总准)
        批排.addWidget(self.拒)
        批排.addStretch(1)
        self.批行.setVisible(False)
        排.addWidget(self.批行)

        self.刷样式()

    def 设(self, 卡数据):
        self._卡 = dict(卡数据)
        self.标题.setText(_卡头(self._卡))
        if self._卡.get('文'):
            self.正文.setPlainText(self._卡['文'])
        self.刷样式()

    def 翻(self):
        self._展开 = not self._展开
        self.正文.setVisible(self._展开)
        self.箭头.setArrowType(Qt.DownArrow if self._展开 else Qt.RightArrow)

    def 显批(self, 显):
        self.批行.setVisible(显)

    def 刷样式(self):
        """
        按状态给边框上色。用左框线而不是整框 —— 整框太抢眼，一屏几张卡
        会看着很花。
        """
        c = 酒馆样式.配色()
        色 = {'跑': c['primary'], '成': c['border'], '坏': c['danger'],
              '拒': c['text_secondary'], '问': c['active']}.get(
                  self._卡.get('态'), c['border'])
        self.setStyleSheet(
            'QFrame#工具卡 { border-left: 3px solid %s; background: %s; }'
            % (色, c['surface']))


class 气泡(QFrame):
    """任务 / 助手说话。跟工具卡片分开，让人一眼看出哪些是"话"。"""

    def __init__(self, 谁, 文, 父=None):
        super().__init__(父)
        排 = QVBoxLayout(self)
        排.setContentsMargins(8, 6, 8, 6)
        排.setSpacing(2)
        名 = QLabel('任务' if 谁 == '任务' else '助手')
        c = 酒馆样式.配色()
        名.setStyleSheet('color: %s;' % c['text_secondary'])
        排.addWidget(名)
        身 = QLabel(文 or '')
        身.setWordWrap(True)
        身.setTextInteractionFlags(Qt.TextSelectableByMouse)
        排.addWidget(身)


def _整数(值, 默认=0, 下限=0, 上限=None):
    """
    把存过的东西读成一个整数。**读不出来就给默认，绝不抛。**

    ⚠ `助手设置.json` 是**能手打的文件**（`酒馆存储` 那条"坏配置不该让功能
    打不开"同理）。里面写 `"最大步数": "十二"` 完全可能，`int()` 一炸就是
    整页打不开 —— 为了一个格子里的错别字，代价太大。
    """
    try:
        数 = int(值)
    except (TypeError, ValueError):
        return 默认
    if 数 < 下限:
        return 下限
    if 上限 is not None and 数 > 上限:
        return 上限
    return 数


#: 最大步数的上下限。上限 200 是**防手滑**，不是防模型 ——
#: 真跑 200 步光是折叠就够呛，但至少不让用户在设置里写个 99999
#: 然后一按开始就跑到天黑。
步数下限, 步数上限 = 1, 200

#: 云端上下文的**上限**。这道闸只是挡住手滑填个天文数字（填了会把请求
#: 撑爆换一个 400），不是断言对面真有这么大。
云端上下文上限 = 1000000

#: 单步输出的上下限。下限 256 是"再少就连一个函数都写不完"，
#: 上限 65536 是**防手滑**（填了会一次性挤掉整份转录本预算）。
输出下限, 输出上限 = 256, 65536


class 设置框(QDialog):
    """
    编程助手的三个旋钮：**最多跑几步、按多大的上下文算预算、一步能写多长**。

    ⚠ **`单步输出` 是后加进来的，起因是一个实测故障：** 让模型写网页，它读了
    一圈文件、什么都没写，然后报"干完了"。查下来是 1024 的输出上限**根本装不
    下一个网页** —— 而它该多大取决于你让它干什么（改几行 vs 写一整页），只有
    用户自己知道，所以它必须是个能调的数。原来那个"不摆出来"的理由站不住了。

    ⚠ **剩下两项仍然不摆在这儿：**

      · `工具协议` —— 判错会让模型**完全看不见工具**，是整条链上最难查的
        一种坏法，不该放在随手能点的地方；
      · `保留步数` —— 折叠策略的一部分，动它等于动 prefill 的账。

    ⚠ **上下文那格 0 = 「自动」。** 自动的含义**两档不一样**（本地读接口
    配置的 `n_ctx`，云端读不到、按一个保守值估），所以下面那行小字必须
    说清"现在自动算出来是多少、换算成转录本预算是多少" —— 不然用户填完
    根本不知道自己覆盖掉了什么。

    ⚠ **本地那一档的上限就卡在 `n_ctx`。** 提示词一旦越过它，llama.cpp 会
    **从前面截断**，最先丢的正是系统提示和工具协议 —— 模型会毫无提示地
    "忘记规矩"，界面上一点异常都看不出来。宁可在这儿挡住。
    """

    def __init__(self, 设, 自动, 上限, 说明, 父=None):
        super().__init__(父)
        self.setWindowTitle('编程助手设置')
        self.setMinimumWidth(470)

        self.自动 = max(1, _整数(自动, 8192, 1))
        self.上限 = max(0, _整数(上限, self.自动, 0))
        self.说明 = 说明 or ''

        外 = QVBoxLayout(self)
        表 = QFormLayout()

        self.步 = QSpinBox()
        self.步.setRange(步数下限, 步数上限)
        self.步.setValue(_整数(设.get('最大步数'),
                             酒馆助手.默认设置['最大步数'],
                             步数下限, 步数上限))
        self.步.setToolTip(
            '一次任务最多走多少步。\n'
            '一步 = 一次模型输出 + 它引发的所有工具调用。\n\n'
            '⚠ 不是越大越好：步数越多转录本越长，一旦触发折叠，'
            '本轮要重付一次整段 prefill（实测同样 10.6K，折叠一轮 '
            '6.3 秒，追加一轮只要 0.7 秒）。')
        表.addRow('最大步数', self.步)

        self.上下文 = QSpinBox()
        self.上下文.setRange(0, self.上限)
        self.上下文.setSingleStep(1024)
        self.上下文.setSpecialValueText('自动')
        self.上下文.setValue(min(_整数(设.get('上下文'), 0, 0),
                               self.上限))
        self.上下文.setToolTip('按多大的上下文窗口来分预算、什么时候折叠。\n'
                             '自动 = 跟着接口配置走。')
        表.addRow('上下文（token）', self.上下文)

        self.输出 = QSpinBox()
        self.输出.setRange(输出下限, 输出上限)
        self.输出.setSingleStep(512)
        self.输出.setValue(_整数(设.get('单步输出'),
                             酒馆助手.默认设置['单步输出'],
                             输出下限, 输出上限))
        self.输出.setToolTip(
            '一次模型输出最多多少 token（包含它写的代码）。\n\n'
            '⚠ 装不下就是两种**不报错**的坏法：\n'
            '  · 工具调用的参数被截在半路 → 写出一个残缺文件；\n'
            '  · 对面带思维链时思维链也吃这个额度 → 正文和工具调用一个字\n'
            '    都生不出来，看起来像"模型什么都没说"。\n\n'
            '⚠ 它不是白给的：`分预算` 会从这里扣掉一块，留给转录本的就少了。\n'
            '写整个网页/整份文件要大一点；只改几行可以小一点。')
        表.addRow('单步输出（token）', self.输出)

        外.addLayout(表)

        self.提示 = QLabel('')
        self.提示.setWordWrap(True)
        self.提示.setStyleSheet('color: %s;' % 酒馆样式.配色()['text_secondary'])
        外.addWidget(self.提示)

        钮 = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        钮.button(QDialogButtonBox.Ok).setText('保存')
        钮.button(QDialogButtonBox.Cancel).setText('取消')
        钮.accepted.connect(self.accept)
        钮.rejected.connect(self.reject)
        外.addWidget(钮)

        self.上下文.valueChanged.connect(self._刷提示)
        self.输出.valueChanged.connect(self._刷提示)
        self._刷提示()

    def _刷提示(self):
        """
        把那行小字刷成"这次到底按多少算、各档分到多少"。

        ⚠ **要当场换算给用户看。** 光写"自动 / 4096"没用 —— 用户真正想知道
        的是"够我干几步活"，而那由 `分预算` 决定，不是由这个数本身决定。

        ⚠ **三个格子是互相挤的**（`分预算` 里 `转录本 = 总 − 输出 − 系统 −
        余量`），所以动哪一个都得把整份重算一遍 —— 而且**被压住的那一档必须
        说出来**：`分预算` 会把输出压在 `n_ctx // 4` 以内，不说的话用户把
        「单步输出」调到 8192 却没反应，只会以为这个格子坏了。
        """
        填 = int(self.上下文.value())
        n = 填 or self.自动
        要的 = int(self.输出.value())
        预 = 酒馆助手.分预算(n, {'单步输出': 要的})
        出 = int(预['输出'])
        self.提示.setText(
            '%s\n\n本次按%s = %s token 算：\n'
            '单步输出 %s%s，转录本预算约 %.1fK。'
            % (self.说明,
               '你填的' if 填 else '自动', '{:,}'.format(n),
               '{:,}'.format(出),
               '（你要的 %s 被上下文上限压到四分之一）' % '{:,}'.format(要的)
               if 出 < 要的 else '',
               预['转录本'] / 1000.0))


class 助手页(QWidget):
    """
    编程助手整页。**自己走 `助手线`**（线程）+ 自己的审批握手。

    `关掉()` 必须**幂等** —— `酒馆窗口._拆面板` 在关窗路上会被叫两遍
    （那边有注释说明），不幂等就会二次 `wait` 卡住主线程。
    """

    def __init__(self, 工位, 父=None):
        super().__init__(父)
        self._工位 = 工位
        self._根 = ''
        self._配置 = None
        self._套们 = []          # 「接口配置」清单（不含密钥，渲染下拉够用）
        self._编号 = 0           # 助手自己记住的那套编号，见 `_读设置`
        self._步数 = 酒馆助手.默认设置['最大步数']   # 设置框那三格，见 `_改设置`
        self._上下文 = 0         # 0 = 自动（跟着接口配置走）
        self._输出 = 酒馆助手.默认设置['单步输出']
        self._卡们 = {}          # 审批号 → 卡片控件
        self._总准 = set()       # 「本任务总是允许」记下的东西
        self._关过 = False
        self._等秒 = 0
        self._阶段 = ''
        self._预算 = {}

        self._线 = 酒馆助手线.助手线()
        self._线.阶段.connect(self._收阶段)
        self._线.正文.connect(self._收正文)
        self._线.卡.connect(self._收卡)
        self._线.要批.connect(self._收要批)
        self._线.步.connect(self._收步)
        self._线.预算.connect(self._收预算)
        self._线.完.connect(self._收完)
        self._线.start()
        self._预算 = {}

        self._搭()

        self._走秒 = QTimer(self)
        self._走秒.setInterval(走秒毫秒)
        self._走秒.timeout.connect(self._刷秒)

        # 两格下拉的线。**顺序有讲究**：模式变了先重填配置下拉，
        # 下拉的 `currentIndexChanged` 再触发 `_换配置` 去读那一套。
        self.模式.currentIndexChanged.connect(self._按模式变)
        self.配置下拉.currentIndexChanged.connect(self._换配置)

        self._读设置()

    # ── 搭界面 ──

    def _搭(self):
        c = 酒馆样式.配色()
        外 = QVBoxLayout(self)
        外.setContentsMargins(10, 8, 10, 8)
        外.setSpacing(6)

        # ── 顶栏 ──
        #
        # ⚠ **用的是「接口配置」里那一套，不另造配置。** 本地和云端走同一个
        # 表、同一个管理器（`酒馆接口` / `酒馆接口页`），这儿只是**多一个指针**
        # ——助手自己记住用哪一套，跟聊天那套互不干扰。
        #
        # 「模式」那格只干一件事：**把下拉筛一下**。本地和云端混在一个下拉里
        # 最容易出的事是"这栏写着本地、实际跑的是云端"，筛开就没这问题。
        顶 = QHBoxLayout()
        顶.setContentsMargins(0, 0, 0, 0)
        self.目录标 = QLabel('工作目录：（还没选）')
        self.目录标.setTextInteractionFlags(Qt.TextSelectableByMouse)
        顶.addWidget(self.目录标, 1)
        self.换目录 = 酒馆样式.做按钮('换目录…', self._换目录, '选一个目录当沙箱')
        顶.addWidget(self.换目录)
        self.设置钮 = 酒馆样式.做按钮(
            '设置…', self._改设置,
            '最多跑几步、按多大的上下文算预算。\n'
            '只影响下一个任务 —— 开跑之后改不会把正在跑的搅乱')
        顶.addWidget(self.设置钮)
        外.addLayout(顶)

        选 = QHBoxLayout()
        选.setContentsMargins(0, 0, 0, 0)
        self.模式 = QComboBox()
        self.模式.addItem('本地模型', 'local')
        self.模式.addItem('云端 API', 'cloud')
        self.模式.setToolTip('只影响下面那个「配置」下拉里显示哪几套。'
                            '本地和云端用的是同一份「接口配置」，不另存一份')
        选.addWidget(QLabel('模式'))
        选.addWidget(self.模式)
        选.addSpacing(10)
        self.配置下拉 = QComboBox()
        self.配置下拉.setToolTip('助手用哪一套接口。'
                               '跟聊天用的那套是分开记的，互不影响')
        选.addWidget(self.配置下拉, 1)
        选.addWidget(酒馆样式.做按钮('刷新', self._读设置, '重新读「接口配置」'))
        self.模型标 = QLabel('')
        self.模型标.setStyleSheet('color: %s;' % 酒馆样式.配色()['text_secondary'])
        选.addWidget(self.模型标)
        外.addLayout(选)

        # ── 仪表 ──
        仪 = QHBoxLayout()
        仪.setContentsMargins(0, 0, 0, 0)
        self.用量 = QProgressBar()
        self.用量.setRange(0, 100)
        self.用量.setValue(0)
        self.用量.setMaximumWidth(200)
        self.用量.setTextVisible(False)
        仪.addWidget(QLabel('上下文'))
        仪.addWidget(self.用量)
        self.用量字 = QLabel('0 / 8K')
        仪.addWidget(self.用量字)
        self.预算标 = QLabel('')
        self.预算标.setStyleSheet('color: %s;' % c['text_secondary'])
        仪.addWidget(self.预算标)
        self.步字 = QLabel('')
        仪.addWidget(self.步字)
        self.慢字 = QLabel('')
        self.慢字.setStyleSheet('color: %s;' % c['text_secondary'])
        仪.addWidget(self.慢字)
        仪.addStretch(1)
        外.addLayout(仪)

        # ── 转录本 ──
        self.区 = QScrollArea()
        self.区.setObjectName('消息区')
        self.区.setWidgetResizable(True)
        self.区.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.里 = QWidget()
        self.柱 = QVBoxLayout(self.里)
        self.柱.setContentsMargins(4, 4, 4, 4)
        self.柱.setSpacing(8)
        self.柱.addStretch(1)
        self.区.setWidget(self.里)
        外.addWidget(self.区, 1)

        # ── 阶段条 ──
        self.阶 = QLabel('选好工作目录，写个任务，点开始。')
        self.阶.setWordWrap(True)
        self.阶.setStyleSheet('color: %s;' % c['text_secondary'])
        外.addWidget(self.阶)

        # ── 输入 ──
        输 = QHBoxLayout()
        self.任务 = QPlainTextEdit()
        self.任务.setPlaceholderText('说个任务…（Ctrl+Enter 开始，Enter 换行）')
        self.任务.setMaximumHeight(72)
        self.任务.keyPressEvent = self._按键
        输.addWidget(self.任务, 1)
        钮 = QVBoxLayout()
        self.开始 = 酒馆样式.做按钮('开始', self._开工, 'Ctrl+Enter', self)
        self.停 = 酒馆样式.做按钮('停止', self._喊停, '本地模型处理长提示时停不下来', self)
        self.停.setEnabled(False)
        self.清 = 酒馆样式.做按钮('清空', self._清空, '清掉上面的记录', self)
        钮.addWidget(self.开始)
        钮.addWidget(self.停)
        钮.addWidget(self.清)
        钮.addStretch(1)
        输.addLayout(钮)
        外.addLayout(输)

    def _按键(self, 事):
        if 事.key() in (Qt.Key_Return, Qt.Key_Enter) and \
                (事.modifiers() & Qt.ControlModifier):
            self._开工()
            return
        QPlainTextEdit.keyPressEvent(self.任务, 事)

    # ── 设置与目录 ──

    def _读设置(self):
        """
        读「编程助手」的设置，顺便把「接口配置」清单拉出来填下拉。

        ⚠ **助手有自己的配置指针，跟聊天那套分开。** 聊天看的是"会话的
        `接口编号`，没指定就落到默认那套"；助手这个存在自己的 `助手设置` 里。
        两边互不干扰 —— 你在聊天里切了接口，助手不会跟着变，反过来也一样。
        """
        def 活(库):
            return (库.取('助手设置', 1) or {}, 酒馆接口.列(库) or [])

        def 回来(包, 错):
            if 错:
                return
            行, 套们 = 包
            self._根 = (行 or {}).get('工作目录') or ''
            self._编号 = int((行 or {}).get('接口编号') or 0)
            # ⚠ 这几格是**能手打的 JSON**，读坏了要给默认值，别抛（见 `_整数`）
            self._步数 = _整数((行 or {}).get('最大步数'),
                            酒馆助手.默认设置['最大步数'], 步数下限, 步数上限)
            self._上下文 = _整数((行 or {}).get('上下文'), 0, 0)
            self._输出 = _整数((行 or {}).get('单步输出'),
                            酒馆助手.默认设置['单步输出'],
                            输出下限, 输出上限)
            self._套们 = list(套们 or [])
            # 模式按上次选的那套的协议定；它没在（被删了）就看第一套
            上次 = next((套 for 套 in self._套们 if 套.编号 == self._编号), None)
            if 上次 is None and self._套们:
                上次 = self._套们[0]
            本 = (上次.协议 or 'local') == 'local' if 上次 else True
            self.模式.blockSignals(True)
            self.模式.setCurrentIndex(0 if 本 else 1)
            self.模式.blockSignals(False)
            self._填配置下拉()
            self._刷新目录标()

        self._工位.干(活, 回来)

    def _填配置下拉(self):
        """
        按当前模式筛「接口配置」，填进下拉。**不新建任何配置。**

        ⚠ **必须筛。** 本地和云端混在一个下拉里，最容易出的事是"上面那栏
        写着本地、实际跑的却是云端"—— 而这两种的失败样子完全不同（一个是
        0xc000001d 之类，一个是 401），混着看最难查。
        """
        要本地 = (self.模式.currentData() or 'local') == 'local'
        留 = [套 for 套 in self._套们
              if ((套.协议 or 'local') == 'local') == 要本地]
        self.配置下拉.blockSignals(True)
        self.配置下拉.clear()
        for 套 in 留:
            标 = '%s  ·  %s' % (套.名称 or '没名字', 套.模型 or '（没填模型）')
            self.配置下拉.addItem(标[:76], 套.编号)
        位 = self.配置下拉.findData(self._编号)
        self.配置下拉.setCurrentIndex(位 if 位 >= 0 else 0)
        self.配置下拉.blockSignals(False)

        if not 留:
            self._配置 = None
            self.模型标.setText('这一档下没有配置')
            self.开始.setEnabled(False)
            self.预算标.setText('')
            self.用量字.setText('—')
            self.阶.setText(
                '去「接口配置」里建一套%s的（助手用的是同一份配置，不用另建）。'
                % ('本地（协议选 local）' if 要本地
                   else '云端（协议选 openai 兼容的那家）'))
            return
        self._换配置()

    def _换配置(self):
        """下拉换了（或者模式换了导致它重填）→ 重新读那套的完整配置。"""
        编号 = self.配置下拉.currentData()
        if not 编号:
            return
        self._编号 = int(编号)
        self._存设置()                    # 记住，下次打开还是这套
        self._工位.干(lambda 库, n=self._编号: 酒馆接口.取或默认(库, n),
                    self._收配置)

    def _按模式变(self):
        """模式那格动了。**先重填下拉，再让它自己去加载。**"""
        self._填配置下拉()

    def _收配置(self, 套, 错):
        if 错:
            return
        self._配置 = 套
        if 套 is None:
            self.模型标.setText('（取不到这套配置）')
            self.开始.setEnabled(False)
            return
        本 = (套.协议 or 'local') == 'local'
        self.模型标.setText('协议 %s' % (套.协议 or '空'))
        self.开始.setEnabled(not self._线.在跑())

        # 没开跑之前也把仪表画出来 —— 用户一进来就该看到"我这次有多少预算"，
        # 而不是一个写死的数
        #
        # ⚠ **设置里手填过就按手填的画。** 实际用的是 `助手线._干` 现算的那份，
        # 同一套规则（`_上下文档` 和它就是一对）—— 两处算得不一样的话，仪表
        # 画的跟真跑的不是一回事，那比不画还坏。
        # 云端的 `n_ctx` **是我们读不到的**（那在人家的服务端），所以自动那一档
        # 只能按保守值估，并在界面上说清这是估的。
        自动, 上限, _ = self._上下文档()
        填 = min(self._上下文, 上限) if self._上下文 else 0
        n = 填 or 自动
        # 单步输出也要传进去 —— 它跟转录本是抢同一块地方的（`分预算` 里
        # `转录本 = 总 − 输出 − 系统 − 余量`），不传的话仪表画的就是旧数
        预 = 酒馆助手.分预算(n, {'单步输出': int(self._输出)})
        self.用量字.setText('0 / %.1fK' % (n / 1000.0))
        self.预算标.setText('转录本预算 %.1fK%s'
                          % (预['转录本'] / 1000.0,
                             '（设置里填的）' if 填
                             else ('' if 本 else '（按 32K 估）')))
        if 本 and n < 8192:
            self.阶.setText('提示：这套接口的 n_ctx 只有 %d。编程助手能用的'
                          '上下文跟它走 —— 去「接口配置」调大一点'
                          '（8G 显存建议 16384）能干更长的活。' % n)
        elif not 本:
            self.阶.setText('跑在云端接口上。写文件和跑命令**仍然在你本机执行**，'
                          '审批也照旧 —— 云端只负责出主意和写代码。')
        self._刷新目录标()

    def _刷新目录标(self):
        if self._根 and os.path.isdir(self._根):
            self.目录标.setText('工作目录：%s' % self._根)
            self.目录标.setStyleSheet('')
        elif self._根:
            self.目录标.setText('工作目录不存在了：%s（换个目录）' % self._根)
            self.目录标.setStyleSheet('color: %s;' % 酒馆样式.配色()['danger'])
        else:
            self.目录标.setText('工作目录：（还没选）')
            self.目录标.setStyleSheet('')

    def _换目录(self):
        选 = QFileDialog.getExistingDirectory(self, '选一个工作目录')
        if not 选:
            return
        真 = os.path.realpath(选)
        家 = os.path.realpath(os.path.expanduser('~'))
        盘根 = os.path.splitdrive(真)[0] + os.sep
        if 真 in (家, 盘根) or 真.lower().startswith(
                os.path.join(盘根, 'windows').lower()) or \
                真.lower().startswith(os.path.join(盘根, 'program files').lower()):
            if QMessageBox.warning(
                    self, '这个目录太大了',
                    '你选的是 %s。\n\n助手能在这个范围内的**任何地方**读写文件、'
                    '跑命令。范围太大一旦出事收不回来，建议选一个具体项目目录。\n\n'
                    '还是要用它吗？' % 真,
                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
        self._根 = 真
        self._存设置()
        self._刷新目录标()

    def _存设置(self):
        def 活(库):
            行 = 库.取('助手设置', 1) or {'编号': 1}
            行['工作目录'] = self._根
            行['接口编号'] = int(getattr(self._配置, '编号', 0) or 0)
            行['最大步数'] = int(self._步数)
            行['上下文'] = int(self._上下文)
            行['单步输出'] = int(self._输出)
            库.存('助手设置', 行)

        self._工位.干(活)

    # ── 设置框 ──

    def _上下文档(self):
        """
        返回 `(自动算出来的上下文, 上限, 说明)` —— 给设置框用。

        ⚠ **两档的"自动"来源不一样，所以说明必须分开写。** 本地是**读得到**
        的：`n_ctx` 就写在这套接口配置里；云端**读不到**（那在人家的服务端），
        只能按一个保守值估。界面上不说实话的话，用户填完不知道自己覆盖掉了
        什么，也判断不了该填多少。

        ⚠ 本地那档**上限就是 `n_ctx`**，不给填更大 —— 理由见 `设置框` 的注释。
        """
        本 = (getattr(self._配置, '协议', '') or 'local') == 'local'
        n = 0
        try:
            n = int(self._配置.参数().get('n_ctx') or 0)
        except (AttributeError, TypeError, ValueError):
            n = 0

        if 本:
            n = n or 酒馆本地.默认上下文
            return n, n, (
                '自动 = 这套接口配置里的 n_ctx，现在是 %s token。\n'
                '本地模型的上限就是它 —— 填更大没有用，而且危险：'
                '提示词一旦越过 n_ctx，llama.cpp 会从前面截断，'
                '最先丢的正是系统提示和工具清单，模型会毫无提示地"忘记规矩"。'
                % '{:,}'.format(n))

        n = n or 酒馆助手线.云端默认上下文
        return n, 云端上下文上限, (
            '自动 = 按 %s token 估（云端的真实窗口在人家服务端，我们读不到）。\n'
            '知道对面多大就自己填：填小了会过早折叠，填大了会把请求撑爆'
            '换一个 400。' % '{:,}'.format(n))

    def _改设置(self):
        """
        开设置框。

        ⚠ **不看有没有任务在跑。** 这两个数只在 `_开工` 那一刻读进 `设`，
        跑起来之后再改只影响下一个任务 —— 不会把正在跑的搅乱，所以没必要
        禁掉这个按钮（禁掉反而让人以为改了会出事）。
        """
        自动, 上限, 说明 = self._上下文档()
        框 = 设置框({'最大步数': self._步数, '上下文': self._上下文,
                   '单步输出': self._输出},
                   自动, 上限, 说明, self)
        if 框.exec() != QDialog.Accepted:
            return
        self._步数 = int(框.步.value())
        self._上下文 = int(框.上下文.value())
        self._输出 = int(框.输出.value())
        self._存设置()
        # 仪表得跟着变 —— 否则用户改完上下文，上面还画着旧预算
        if self._配置 is not None:
            self._收配置(self._配置, None)

    # ── 开始 / 停止 ──

    def _开工(self):
        if self._线.在跑():
            return
        if not self._根 or not os.path.isdir(self._根):
            QMessageBox.information(self, '先选工作目录',
                                    '助手干活需要一个目录当边界。点「换目录…」选一个项目文件夹。')
            return
        if self._配置 is None:
            QMessageBox.information(self, '先选一套接口',
                                    '上面那个「配置」下拉里挑一套。'
                                    '本地和云端用的是同一份「接口配置」，'
                                    '去「接口配置」里建好就有了。')
            return
        任务 = self.任务.toPlainText().strip()
        if not 任务:
            QMessageBox.information(self, '先说个任务', '比如「跑一下 main.py，报错就修好它」。')
            return

        self._清空()
        self.加气泡('任务', 任务)
        self._总准.clear()
        self.任务.setPlainText('')

        设 = dict(酒馆助手.默认设置)
        # 设置框里那三格。**在这儿灌进去，不在 `_干` 里读全局** —— 这样
        # "改了之后能不能影响正在跑的任务"就只有一个答案：不能。
        设['最大步数'] = int(self._步数)
        设['上下文'] = int(self._上下文)
        设['单步输出'] = int(self._输出)
        # 协议由 `酒馆助手线.默认工具协议` 定（它在 _干 里自己算），
        # 这儿不写 —— 写了两处会打架。用户手选的话走 `设['工具协议']`。
        if not self._线.排(self._根, 任务, self._配置, 设):
            return
        self._锁界面(True)
        self._等秒 = 0
        self._走秒.start()
        self._收阶段('想', '正在准备…')

    def _喊停(self):
        self._线.打断()
        self.阶.setText('正在停…（如果模型正在处理提示词，这一步停不下来，'
                      '要等它处理完；跑命令的话要等命令超时）')
        self.停.setEnabled(False)

    def _锁界面(self, 忙):
        self.开始.setEnabled(not 忙)
        self.停.setEnabled(忙)
        self.任务.setEnabled(not 忙)
        self.换目录.setEnabled(not 忙)

    # ── 收信号 ──

    def _收阶段(self, 类, 文):
        self._阶段 = 文
        if 类 == '想':
            # prefill 期间必须说实话，见文件头第二条
            self.阶.setText('正在处理提示词… 已等 %d 秒'
                          '（本地模型处理长提示时停不下来，属正常，请稍等）'
                          % self._等秒)
        else:
            self.阶.setText(文)
        self.慢字.setText('')

    def _刷秒(self):
        if not self._线.在跑():
            return
        self._等秒 += 走秒毫秒 / 1000.0
        if self._阶段.startswith('正在处理') or '正在处理提示词' in self.阶.text():
            self.阶.setText('正在处理提示词… 已等 %.0f 秒'
                          '（本地模型处理长提示时停不下来，属正常，请稍等）'
                          % self._等秒)

    def _收正文(self, 字):
        """
        模型在写字。**只把最后一段贴成一行灰字** —— 这一屏的主角是工具卡片，
        把模型的每句话都铺开会让卡片淹没在文字里。
        """
        self.慢字.setText(('模型：' + 字.replace('\n', ' '))[-88:])

    def _收步(self, 第, 共, token):
        self.步字.setText('第 %d / %d 步' % (第, 共))
        总 = self._上限()
        比 = 100 if 总 <= 0 else min(100, int(token * 100 / 总))
        self.用量.setValue(比)
        self.用量字.setText('%.1fK / %.1fK' % (token / 1000.0, 总 / 1000.0))
        # 快满的时候变黄，给用户一个"下一步要折叠了"的预告
        c = 酒馆样式.配色()
        色 = c['danger'] if 比 >= 90 else (c['primary'] if 比 >= 75 else c['border'])
        self.用量.setStyleSheet(
            'QProgressBar::chunk { background: %s; }' % 色)

    def _收预算(self, 预):
        """
        预算跟着接口配置里的 `n_ctx` 走。所以仪表的分母是**这次任务真实的
        上下文**，不是写死的数 —— 写死的话用户去把 `n_ctx` 调大了，界面还在
        按老的算，看着像"改了没生效"。
        """
        self._预算 = dict(预 or {})
        总 = self._上限()
        self.用量字.setText('%.1fK / %.1fK' % (0, 总 / 1000.0))
        self.预算标.setText('转录本预算 %.1fK' % (self._转录本() / 1000.0))

    def _转录本(self):
        return int(self._预算.get('转录本') or 0)

    def _上限(self):
        """
        仪表的分母：这次任务真实的 `n_ctx`。

        优先用 `分预算` 那次推出来的总额（跟着接口配置走）；没有就现读配置；
        再没有才退回引擎默认值。**绝不写死** —— 写死的话用户把 `n_ctx` 调大
        了，仪表还在按老数算，看着像"改了没生效"。
        """
        n = int((self._预算 or {}).get('总') or 0)
        if n:
            return n
        try:
            if self._配置 is not None:
                return int(self._配置.参数().get('n_ctx')
                           or 酒馆本地.默认上下文)
        except (TypeError, ValueError):
            pass
        return 酒馆本地.默认上下文

    def _收卡(self, 号, 卡):
        """一张工具卡片。同一号第二次来（跑完了）是更新，不是新加。"""
        现 = self._卡们.get(('卡', 号, 卡.get('第'), 卡.get('号')))
        if 现 is None:
            现 = 卡片(卡, self.里)
            self.柱.insertWidget(self.柱.count() - 1, 现)
            self._卡们[('卡', 号, 卡.get('第'), 卡.get('号'))] = 现
        现.设(卡)
        酒馆样式.滚动到底(self.区)

    def _收要批(self, 号, 卡):
        """
        审批请求。**不弹窗** —— 卡片长在转录本里，用户能一边看前面几步
        干了什么一边决定。

        「本任务总是允许」在这里生效：命中记过的粒度就直接放行，不再问。
        **粒度在 `_授权键` 里定死了** —— 命令只认完全相同的字符串。
        """
        if _授权键(卡) in self._总准:
            self._线.回批(号, True)
            return
        卡 = dict(卡)
        卡['态'] = '问'
        件 = 卡片(卡, self.里)
        件.设(卡)
        件.显批(True)
        件.准.clicked.connect(lambda: self._回批(号, 件, True, False))
        件.总准.clicked.connect(lambda: self._回批(号, 件, True, True))
        件.拒.clicked.connect(lambda: self._回批(号, 件, False, False))
        self.柱.insertWidget(self.柱.count() - 1, 件)
        self._卡们[('批', 号)] = 件
        酒馆样式.滚动到底(self.区)
        self.阶.setText('等你确认：%s（5 分钟不理就当拒绝）' % _卡头(卡))

    def _回批(self, 号, 件, 准, 总):
        if 准 and 总:
            self._总准.add(_授权键(件._卡))
            # 「本任务总是允许」的粒度：写文件按路径，命令按完全相同的字符串。
            # **绝不做"本任务允许所有命令"** —— 那等于把审批彻底关掉。
            件.设(dict(件._卡, 态='成', 文='（已允许，本任务里这个不再问）'))
        elif 准:
            件.设(dict(件._卡, 态='跑', 文='（已允许）'))
        else:
            件.设(dict(件._卡, 态='拒', 文='（已拒绝）'))
        件.显批(False)
        self._线.回批(号, 准)
        self.阶.setText('继续中…')

    def _收完(self, 果):
        self._走秒.stop()
        self._锁界面(False)
        停 = bool(果.get('停'))
        坏 = 果.get('坏')
        if 坏:
            self.加气泡('错', 坏)
            self.阶.setText('停下了。')
        elif 停:
            self.阶.setText('已停止。')
        else:
            答复 = 果.get('答复') or '（没有输出）'
            self.加气泡('助手', 答复)
            self.阶.setText('干完了（%s 步）。' % 果.get('步', '?'))
        self.慢字.setText('')

    # ── 小东西 ──

    def 加气泡(self, 谁, 文):
        泡 = 气泡(谁, 文, self.里)
        self.柱.insertWidget(self.柱.count() - 1, 泡)
        酒馆样式.滚动到底(self.区)

    def _清空(self):
        # 同 `酒馆对话页._清空`：`hide()` + `deleteLater()`，
        # **绝不 `setParent(None)`** —— 那句会把它变成带标题栏的顶层窗口，
        # 弹一下再消失。
        for i in range(self.柱.count() - 1, -1, -1):
            件 = self.柱.itemAt(i).widget()
            if 件 is None:
                continue
            件.hide()
            件.deleteLater()
        self._卡们.clear()
        self._总准.clear()
        self.用量.setValue(0)
        n = self._上限()
        self.用量字.setText('0 / %.1fK' % (n / 1000.0))
        self.步字.setText('')
        self.慢字.setText('')

    def 现在忙吗(self):
        return self._线.在跑()

    def 关掉(self):
        """
        收尾。**必须幂等** —— 关窗路上会被叫两遍，第二次再 `wait` 会卡主线程。
        """
        if self._关过:
            return True
        self._关过 = True
        self._走秒.stop()
        return self._线.停()


def _授权键(卡):
    """
    「本任务总是允许」按什么粒度记住。

    ⚠ **命令只按完全相同的字符串授权。** 文件名按路径前缀给一点余量（同一个
    文件连着改几次很常见），但命令绝不给"这一类"的余量 —— 那等于把审批关掉。
    """
    名 = 卡.get('名')
    参 = 卡.get('参数') or {}
    if 名 == 'run_cmd':
        return ('run_cmd', str(参.get('cmd') or ''))
    return (名, str(参.get('path') or ''))
