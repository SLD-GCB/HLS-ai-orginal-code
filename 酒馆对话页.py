#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆对话页.py — 中间的消息区 + 输入区，右边那栏会话列表

```
                              ┌────────────┐
┌────────────────────────────┐│ 会话        │
│ (头) 爱丽丝                 ││┌──────────┐│
│      ……你挡着光了。         │││第一次见面 ││
│              ┌────────────┐│││12 条·刚刚││
│              │ 你好呀 (头) │││└──────────┘│
│              └────────────┘││[+ 新对话]   │
│ ┌────────────────────────┐ │└────────────┘
│ │ 说点什么…      [发送]   │ │
│ └────────────────────────┘ │
└────────────────────────────┘
```

三样东西都在这个文件里：

  · `气泡`   —— 一条消息。主题里没有气泡这种东西，从零画。
  · `对话页` —— 消息区 + 输入区，负责流式渲染和"发出去"的全过程。
  · `会话栏` —— 右栏。跟 `对话页` 分开放，由主窗口拼进三栏 splitter。

**存储一个字节都不在这儿碰。** 读写全投给 `工位`，生成交给 `生成线`。
这个文件里出现 `库` 的地方，一处都不该有——除了把它转手交给 `工位`。

⚠ **流式不是每来一块就重绘。** 网络那头一秒能来几十块，每块都
`setText` 一次会让长回复肉眼可见地卡。所以块先堆进缓冲，界面一个 80ms 的
`QTimer` 定期把缓冲一次性贴上去。见 `对话页._刷流式`。
"""

import re
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog,
                               QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

import 酒馆大脑
import 酒馆对话
import 酒馆接口
import 酒馆记忆
import 酒馆角色
import 酒馆样式
from 酒馆存储 import 酒馆错误, 毫秒

__all__ = ['气泡', '对话页', '会话栏', '正文框']

头像边长 = 44
#: 界面多久看一次缓冲。见文件开头那条——不是每来一块就重绘。
刷流毫秒 = 80

#: **一次至少攒这么多字才动界面。** 这是"一串一串出来"的那个数。
#:
#: ⚠ 光有定时器不够。模型是一个字一个字吐的（中文 token 常常就是一个字），
#: 每 80ms 就把缓冲贴上去的话，屏幕上就是一个字一个字往外蹦——观感上就是
#: "卡"。攒成一串再贴，看着才像在写字。
成串字数 = 8

#: 但也不能无限憋。模型慢的时候攒够 8 个字可能要等好几秒，那屏幕就像死了。
#: 从上次贴完算起最多憋这么久，到点有多少贴多少。
憋不住毫秒 = 900

#: 长期记忆最多能占多少 token（云端读不到窗口时用这个数）。见 `酒馆记忆.预算`。
记忆上限 = 1200

#: 一轮自动回收最多收几条。**分批收，不要一次收光** —— 一段几百条的旧会话
#: 第一次跑起来，一次收进去就是几百条候选，界面上根本没法看。
每次回收上限 = 20


class _省略下拉(QComboBox):
    """
    一个 QComboBox，但**闭合时那格文字放不下就显示省略号**，不把窗口撑开。

    名字是「接口名 · 模型文件名」，动不动几十个字。Qt 默认会把这个框撑到装下
    最长的那条 —— 而那个宽度会成为**整个窗口的最小宽度**，窗口就被顶开了。
    这里按当前宽度把显示文字**中间省略**（保住头尾，看得出是哪套），完整内容
    进 tooltip。**弹出的那张列表不受影响**——它是另一个窗口，照样显示全。
    """

    def paintEvent(self, _事):
        from PySide6.QtWidgets import QStyleOptionComboBox, QStylePainter, QStyle
        画 = QStylePainter(self)
        选 = QStyleOptionComboBox()
        self.initStyleOption(选)
        空 = max(20, self.width() - 34)      # 左右内边距 + 右边那个下拉箭头
        选.currentText = self.fontMetrics().elidedText(选.currentText, Qt.ElideMiddle, 空)
        画.drawComplexControl(QStyle.CC_ComboBox, 选)
        画.drawControl(QStyle.CE_ComboBoxLabel, 选)


def _记忆预算(套):
    """这段记忆最多能占多少 token。**算法在 `酒馆记忆.预算` 里**（三个地方共用
    同一份，见那个函数）—— 这儿只负责把"这套接口的窗口"取出来。"""
    return 酒馆记忆.预算(酒馆大脑.本机窗口(套), 记忆上限)


def _该收的(全部, 留下, 水位, 会话编号):
    """
    这次把哪几条收成"待定记忆"，以及水位该推到多少。回 `(候选们, 新水位)`。

    ⚠ **边界取"幸存者里最小的序号"**，不是"候选里被丢掉的那几条"：比
    「条数」更老的消息根本没进候选，按候选算的话它们永远收不到 —— 而那些
    恰恰是最该被记住的（"她三十轮前说过她怕黑"）。

    ⚠ **`留下` 为空时一条都不收。** 那时候没有边界可言（当成 +∞ 的话会把
    整段历史一次收光）。

    ⚠ **只收序号大于水位的。** 见 `酒馆记忆.取水位`：不收的话，用户嫌烦删掉的
    候选下一轮会原样长回来，永远删不掉。
    """
    if not 留下:
        return [], 0
    边界 = min(int(m.序号 or 0) for m in 留下)
    出 = []
    到 = 0
    for m in 全部:
        号 = int(m.序号 or 0)
        if 号 >= 边界:
            # `全部` 是按序号升序的，后面只会更大
            break
        if 号 <= int(水位 or 0) or (m.说话人 or '') not in ('用户', '角色', '系统'):
            continue
        文 = (m.内容 or '').strip()
        if not 文:
            continue
        出.append({'文': '%s：%s' % (m.说话人, 文), '开': False,
                  '来源': '自动', '会话': int(会话编号), '序号': 号,
                  '指纹': 酒馆记忆.指纹(文)})
        if len(出) >= 每次回收上限:
            break
    if 出:
        到 = int(出[-1]['序号'])
    return 出, 到


def _时(毫秒数):
    """epoch 毫秒 → 「刚刚 / 12 分钟前 / 3 天前 / 2026-09-01」。"""
    if not 毫秒数:
        return ''
    差 = time.time() - 毫秒数 / 1000.0
    if 差 < 60:
        return '刚刚'
    if 差 < 3600:
        return '%d 分钟前' % int(差 // 60)
    if 差 < 86400:
        return '%d 小时前' % int(差 // 3600)
    if 差 < 86400 * 7:
        return '%d 天前' % int(差 // 86400)
    return time.strftime('%Y-%m-%d', time.localtime(毫秒数 / 1000.0))


# ── think 过滤 ─────────────────────────────────────────────────────
#
# **铁律：数据库永远存原文，过滤只做在显示层。** 推理模型（R1 系）输出里的
# `<think>…</think>` 是模型的内心戏，不该出现在聊天气泡里；但它是上下文
# 的一部分——`_编辑` 拿 `气泡._原始` 覆盖回库、后续轮次把历史消息原样喂
# 回去，都指着原文还在。所以 `气泡` 存两份：`_原始`（原文）和 `_显示`
# （滤过的），下面这两样就是干"滤"的活。

class 滤思考(object):
    """
    把 `<think>…</think>` 从流式文本里滤掉的**增量状态机**。

    为什么非得有状态：标签会被 token 切散——这一块来 `<thi`、下一块才
    来 `nk>`，简单 `replace` 必漏。所以每次喂进来的字先和上一回**没敢
    吐出去的疑似尾巴**（`_挂`，最长就是一个标签的长度）拼上再判：

        正文态   见 `<think>`（含被切散的）→ 切思考态，标签本身吞掉
                 见别的 `<`（`a < b`、`<br>`）→ 原样吐，不误伤
        思考态   见 `</think>` → 切回正文态，标签吞掉
                 其余全吞
        疑似尾巴 当前块末尾正好是某个标签的前缀（`<`、`<th`、…`</thin`）
                 → 挂起来等下一块，不吐也不吞

    流正常结束时 `_挂` 里可能还有没等到的尾巴，`收尾()` 把它吐出来
    （正文态）或扔掉（思考态——没闭合的 think 就当整段都是思考）。
    """

    开 = '<think>'
    关 = '</think>'

    def __init__(self):
        self._态 = '正文'               # '正文' | '思考'
        self._挂 = ''                   # 疑似标签前缀的尾巴
        self._吐过 = False              # 吐出过可见内容没（开头的 think 滤完
                                        # 会留下前导空行，没吐过之前白字符不吐）
        self.思考过 = False             # 进没进过思考态（界面贴「思考中…」用）

    @property
    def 在思考(self):
        return self._态 == '思考'

    def _啃(self, 文, i, 标签):
        """
        `文[i]` 是 `'<'`。看它开头是不是 `标签`：

            '中'  完整命中 → 返回标签之后的位置（标签本身吞掉）
            '挂'  是标签的前缀但长度不够（块到这儿没了）→ 挂到末尾
            '否'  不是这个标签 → 只前进一格，这个 `<' 由调用方处置
        """
        剩 = 文[i:]
        if 剩.startswith(标签):
            return '中', i + len(标签)
        if len(剩) < len(标签) and 标签.startswith(剩):
            return '挂', len(文)
        return '否', i + 1

    def 喂(self, 字):
        """喂一块流式文本，返回**这块里能显示的部分**（可能是空串）。"""
        文 = self._挂 + (字 or '')
        self._挂 = ''
        出 = []
        i = 0
        while i < len(文):
            if 文[i] != '<':
                j = 文.find('<', i)
                if j < 0:
                    j = len(文)
                if self._态 == '正文':
                    段 = 文[i:j]
                    if not self._吐过:
                        段 = 段.lstrip()
                    if 段:
                        self._吐过 = True
                        出.append(段)
                i = j
                continue
            状, 新 = self._啃(文, i, self.开 if self._态 == '正文' else self.关)
            if 状 == '中':
                if self._态 == '正文':
                    self._态 = '思考'
                    self.思考过 = True
                else:
                    self._态 = '正文'
                i = 新
            elif 状 == '挂':
                self._挂 = 文[i:]
                i = len(文)
            else:                                   # 不是标签的 '<'
                if self._态 == '正文':
                    self._吐过 = True
                    出.append('<')
                i = 新
        return ''.join(出)

    def 收尾(self):
        """
        流结束时把挂着的尾巴了结掉。正文态吐出来（它就是普通文字），
        思考态扔掉（没闭合的 think 当整段都是思考）。
        """
        尾, self._挂 = self._挂, ''
        return 尾 if self._态 == '正文' else ''


def 去思考(文):
    """
    整份文本的去 think 版。**定稿、历史气泡都用它兜底。**

    两条正则：闭合的整段去掉；没闭合的（生成被打断在思考中途）从
    `<think>` 起到末尾全去。最后 `strip`——思考段通常贴在开头，滤完
    会留下前导空行。
    """
    文 = re.sub(r'<think>.*?</think>', '', 文 or '', flags=re.S)
    文 = re.sub(r'<think>.*$', '', 文, flags=re.S)
    return 文.strip()


class 正文框(QPlainTextEdit):
    """
    输入框。**Enter 换行，Ctrl+Enter 发送。**

    为什么不做成"Enter 发送"（大多数聊天软件那样）：**中文输入法**。
    拼音还没上屏的时候敲 Enter 是"选词/上屏"，不是"发送"。想区分得去看
    `inputMethodEvent` 的合成状态，各家输入法行为还不一致，很容易变成
    "打某个词打一半就把半截话发出去了"。Ctrl+Enter 没有这个歧义，旁边
    还有发送按钮，代价只是习惯不一样。
    """

    要发 = Signal()

    def keyPressEvent(self, 事):
        if (事.key() in (Qt.Key_Return, Qt.Key_Enter)
                and 事.modifiers() & Qt.ControlModifier):
            self.要发.emit()
            return
        super().keyPressEvent(事)


class 气泡(QFrame):
    """
    一条消息。角色在左（带头像），用户在右（不带头像）。

    **分两层**：控件自己是「一整行」（撑满消息区宽、透明、只管左右对齐），
    有底色有圆角的那块气泡是里面的 `self._框`。分开是因为底色必须画在
    **跟着字走的那个框**上——画在外层就成了一根通条色带。见 `限宽`。

    ⚠ **`QLabel` 开了 `setWordWrap` 之后必须把 `heightForWidth` 打开**
    （见 `_开换行`）。不打开的话放进 `QScrollArea` 里高度会算错——要么被
    截掉半截，要么留一大片空白，而且**只在特定宽度下才出现**，是最难查的
    那种布局问题。
    """

    #: 这三个都带 `self`，让外面知道说的是哪一条
    要编辑 = Signal(object)
    要重说 = Signal(object)
    要删 = Signal(object)

    def __init__(self, 父, 说话人='角色', 名字='', 头像=None, 正文='',
                 坏了=False):
        super().__init__(父)
        self.说话人 = 说话人
        self.序号 = 0
        self._原始 = 正文 or ''          # 原文。**编辑/复制/落库全用它，永不被滤**
        self._死了 = False               # 生成失败的气泡：红边
        # 显示用的那一份。角色气泡要滤掉 `<think>…</think>`（见上面「think
        # 过滤」那段铁律）：历史正文先整个过一遍 `去思考`，流式来的块过 `_滤`。
        # 其余说话人没有这回事，显示就是原文。
        if 说话人 == '角色':
            self._显示 = 去思考(self._原始)
            self._滤 = 滤思考()
        else:
            self._显示 = self._原始
            self._滤 = None

        # ⚠ **这一层是「一整行」，不是那个气泡。** 它撑满消息区宽、自己不画任何
        # 底色（objectName 见附加 QSS），只负责把气泡推到左边还是右边。
        # 真正的那块底色在下头的 `_框` 上——理由见 `限宽`。
        self.setObjectName('气泡行')

        外 = QHBoxLayout(self)
        外.setContentsMargins(10, 7, 10, 7)
        外.setSpacing(8)

        自己说的 = (说话人 == '用户')
        self._头标 = None

        # ── 真正的气泡：底色 / 圆角 / 红边都挂在它身上 ──
        # 横向策略 `Maximum`：它只长到内容那么宽，多出来的地方全归旁边的
        # stretch。不这么设的话它会一路撑满整行，**用户那条就变成一根通条
        # 蓝杠**，几句短话摞在一起像几条色带，完全不是聊天的样子。
        #
        # ⚠ **底下这一堆控件，建的时候全部要带上父。** 不带父的控件在 Qt 里
        # 就是**顶层窗口**：在 `addWidget` 认爹之前只要碰一下 `show()` /
        # `setVisible(True)`，屏幕上就真的会弹出一个带标题栏三键的小窗
        # （最小化/最大化/关闭），挂上父之后又消失——看着就是"一打开就不停
        # 弹小窗、然后又没了"，一屏几句就弹几个。照片见工程根目录 `报错图.jpg`，
        # 那一次就是下面那行名字标闯的祸。
        self._框 = QFrame(self)
        self._框.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        酒馆样式.气泡样式(self._框, '坏' if 坏了 else 说话人)

        柱 = QVBoxLayout(self._框)
        柱.setContentsMargins(10, 6, 10, 6)
        柱.setSpacing(3)
        self.名标 = 酒馆样式.灰色小字(名字 or '', self._框)
        self.名标.setVisible(bool(名字))
        柱.addWidget(self.名标)

        self.内容 = QLabel(self._显示 or 酒馆大脑.开场占位, self._框)
        self._开换行(self.内容)
        self.内容.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.内容.setOpenExternalLinks(True)
        柱.addWidget(self.内容)

        self.脚标 = 酒馆样式.灰色小字('', self._框)
        self.脚标.setVisible(False)
        柱.addWidget(self.脚标)

        # ── 悬停才出来的操作 ──
        self.操作 = QWidget(self._框)
        操 = QHBoxLayout(self.操作)
        操.setContentsMargins(0, 0, 0, 0)
        操.setSpacing(4)
        for 字, 槽, 提示 in (('复制', self._复制, '把这条复制到剪贴板'),
                           ('编辑', lambda: self.要编辑.emit(self),
                            '改这条的内容'),
                           ('重说', lambda: self.要重说.emit(self),
                            '重新生成这一条（覆盖原来那条）'),
                           ('删', lambda: self.要删.emit(self),
                            '删掉这一条')):
            钮 = 酒馆样式.做按钮(字, 槽, 提示, self.操作)
            钮.setFixedHeight(20)
            操.addWidget(钮)
        self.操作.hide()
        柱.addWidget(self.操作)

        if 说话人 in ('角色', '系统'):
            头 = QLabel(self)
            头.setFixedSize(头像边长, 头像边长)
            self._头标 = 头
            self.贴头像(头像)
            # 头像顶对齐：气泡长起来的时候头像留在上边，不跟着飘到中间去
            外.addWidget(头, 0, Qt.AlignTop)
        if 自己说的:
            外.addStretch(1)
        # 柱不给 stretch：气泡按内容宽，多余的全归两旁那两个 stretch
        外.addWidget(self._框)
        if not 自己说的:
            外.addStretch(1)

    # ── 布局 ──

    @staticmethod
    def _开换行(标):
        """
        开自动换行，**并且把 `heightForWidth` 打开**。

        ⚠ 只 `setWordWrap(True)` 是不够的：`QLabel` 默认的 sizePolicy 不声明
        `heightForWidth`，布局就按"一行的高度"给它算位置——长文本会被下面的
        气泡盖住或者被裁掉。Qt + `QScrollArea` 的老毛病。
        """
        标.setWordWrap(True)
        策 = 标.sizePolicy()
        策.setHeightForWidth(True)
        策.setVerticalPolicy(QSizePolicy.MinimumExpanding)
        标.setSizePolicy(策)

    def 贴头像(self, 数据):
        """给这个气泡塞头像。**头像后到（先建气泡、头像还在读）时也能补。**"""
        标 = self._头标
        if 标 is None:
            return
        图 = 酒馆样式.圆角头像(数据, 头像边长)
        if 图.isNull():
            标.setText('·')
        else:
            标.setText('')
            标.setPixmap(图)

    def 限宽(self, 宽):
        """
        气泡最大宽度。消息区每次改大小都重新叫一遍。

        **只锁 `QLabel` 的宽度，不锁 `QFrame` 自己**：锁 frame 的话用户那条
        气泡会整条拉满到 70% 宽，短消息（"嗯"）看起来像一个空的长条。

        ⚠ **光给 `maximumWidth` 还不够，最小值也得给。** 开了自动换行的
        `QLabel` 报出来的 `sizeHint` 是 Qt 自己折中的一个正方块——实测 900 宽的
        消息区里它只报 144——而这一栏是"按 sizeHint 摆、多余的全归 stretch"。
        于是**每句话都被挤成一小竖条**，一句话上下叠成七八行，旁边一大片空的。
        给它一个最小值：**短消息一行放得下就一行，长消息才按上限换行。**
        """
        宽 = max(120, 宽)
        自 = min(self._自然宽(), 宽)
        self.内容.setMinimumWidth(自)
        self.内容.setMaximumWidth(宽)
        self.名标.setMaximumWidth(宽)

    def _自然宽(self):
        """这条文字**不换行**时要多宽。多行的按最长那行算。"""
        米 = self.内容.fontMetrics()
        行们 = (self.内容.text() or '').split('\n')
        return max([米.horizontalAdvance(行) for 行 in 行们] or [0]) + 2

    # ── 内容 ──

    def 追加(self, 字):
        if not 字:
            return
        self._原始 += 字                            # 原文，进编辑框/落库/复制
        if self._滤 is not None:
            self._显示 += self._滤.喂(字)
        else:
            self._显示 += 字
        self.内容.setText(self._显示 or 酒馆大脑.开场占位)

    def 定稿(self, 全文=None):
        """
        生成完了：定内容，**切 Markdown**。

        ⚠ 切 Markdown 会让高度变一次（`**粗**` 这类标记渲染后长度不一样），
        所以调用方得**先把滚动位置记下来、切完再放回去**——不然用户看着
        看着，界面自己跳一下。见 `对话页._定稿`。
        """
        if 全文 is not None:
            self._原始 = 全文
        if self._滤 is not None:
            # 定稿对全文再滤一遍兜底——think 没闭合（被打断在思考中途）
            # 这种边界流式过滤器管不到，`去思考` 两条正则管得到。
            self._显示 = 去思考(self._原始)
        else:
            self._显示 = self._原始
        self.内容.setTextFormat(Qt.MarkdownText)
        self.内容.setText(self._显示 or 酒馆大脑.开场占位)

    def 标坏(self, 文字):
        """
        这条生成失败了。**红边留着、位置留着**，让人看得见是哪条出的问题。

        文字切回 `PlainText`：没吐完的东西里可能有没闭合的 `**` 或反引号，
        走 Markdown 会把后面整段染成斜体，看着像"内容坏了"而不是"生成坏了"。
        """
        self._死了 = True
        self.内容.setTextFormat(Qt.PlainText)
        self.内容.setText(self._显示 or 酒馆大脑.开场占位)
        酒馆样式.气泡样式(self._框, '坏')
        self.脚标.setText(文字 or '生成失败')
        self.脚标.setVisible(True)

    def 脚(self, 文字):
        self.脚标.setText(文字 or '')
        self.脚标.setVisible(bool(文字))

    def 有选中(self):
        """这会儿有没有被选中的文字。**有的话就别重绘**——见 `对话页._刷流式`。"""
        return self.内容.hasSelectedText()

    def enterEvent(self, 事):
        if not self._死了:
            self.操作.show()
        super().enterEvent(事)

    def leaveEvent(self, 事):
        self.操作.hide()
        super().leaveEvent(事)

    def _复制(self):
        QApplication.clipboard().setText(self._原始 or '')


class 会话栏(QWidget):
    """右栏：这个角色有哪些会话。按 `更新于` 降序，最近说话的排前面。"""

    选了 = Signal(int)
    新开了 = Signal(int)          # 新开的会话编号（外面该切过去）

    def __init__(self, 工位, 父=None):
        super().__init__(父)
        self._工位 = 工位
        self._角色编号 = 0
        self._当前 = 0

        外 = QVBoxLayout(self)
        外.setContentsMargins(8, 8, 8, 8)
        外.setSpacing(8)
        外.addWidget(QLabel('会话'))

        self.列表 = QListWidget()
        self.列表.setObjectName('会话列表')
        self.列表.currentItemChanged.connect(self._选变了)
        self.列表.itemDoubleClicked.connect(lambda _: self.删除())
        外.addWidget(self.列表, 1)

        钮 = QHBoxLayout()
        钮.addWidget(酒馆样式.做按钮('新对话', self.新建, '跟这个角色开一段新的'))
        钮.addWidget(酒馆样式.做按钮('删', self.删除, '连同里面的消息一起删'))
        外.addLayout(钮)

    def 换角色(self, 角色编号):
        self._角色编号 = 角色编号 or 0
        self._当前 = 0
        self.刷新()

    def 刷新(self):
        """重读这个角色的会话清单。"""
        if not self._角色编号:
            self.列表.clear()
            return

        def 活(库):
            # ⚠ **条数必须跟清单在同一次活里一起算出来。**
            # 原来是"先画条目、条数再异步回来补上去"——那条回调写在 `项`
            # 这个 `QListWidgetItem` 上，而列表可能已经被下一次刷新清掉了，
            # 那时 `项` 的 C++ 对象早没了，回调一访问就是
            # `Internal C++ object already deleted`。一次活算完就没这个
            # 悬空的写，顺带还少投 N 次活。
            return [(段, 酒馆对话.消息数(库, 段.编号))
                    for 段 in 酒馆对话.列会话(库, self._角色编号)]

        def 回来(行们, 错):
            if 错:
                return
            self.列表.blockSignals(True)      # 重画的时候别乱发「选了」
            self.列表.clear()
            for 段, 数 in (行们 or []):
                项 = QListWidgetItem()
                项.setData(Qt.UserRole, 段.编号)
                项.setText('%s\n%d 条 · %s'
                          % ((段.标题 or '').strip() or '（还没起标题）',
                             数 or 0, _时(段.更新于)))
                self.列表.addItem(项)
            self.列表.blockSignals(False)
            if not self._当前 and self.列表.count():
                # **刚换到某个角色（或者原来选的那段被删了）：非空就先落在
                # 第一段上，也就是最近说过话的那段**（`列会话` 按 `更新于`
                # 降序）。
                #
                # ⚠ 不这么做的话有个很难看的后果：用户在左栏点一个角色，
                # 中栏是空的，他直接打字发送 —— `对话页._发` 会当"一段都没有"
                # 又开一段新的，**把已经聊了几十轮的那段绕过去了**。落在这儿
                # 选中，`选了` 一发就接上原来那段。
                self.列表.setCurrentRow(0)
            elif self._当前 and not self.选编号(self._当前):
                # 原来选的那段没了（比如刚删了），把选中让出去
                self._当前 = 0
                self.选了.emit(0)

        self._工位.干(活, 回来)

    def _选变了(self, 现在, _前):
        if 现在 is None:
            return
        self._当前 = 现在.data(Qt.UserRole)
        self.选了.emit(self._当前)

    def 选编号(self, 编号):
        for i in range(self.列表.count()):
            if self.列表.item(i).data(Qt.UserRole) == 编号:
                self.列表.setCurrentRow(i)
                return True
        return False

    def 新建(self):
        """
        开一段新对话。**角色有开场白的话，顺手落成第一条消息。**

        这两步必须在**同一件活**里做完（建会话 + 落开场白）。分两次投的话，
        中间那一小会儿这段会话是空的——用户要是正好点了它，看到的就是个
        空框，再刷新一次开场白才冒出来。
        """
        if not self._角色编号:
            QMessageBox.information(self, '先选个角色', '左栏里先点一个角色。')
            return
        角色 = self._角色编号

        def 活(库):
            卡片 = 酒馆角色.取(库, 角色)
            段 = 酒馆对话.建会话(库, 角色)
            头 = (卡片.开场白 or '').strip() if 卡片 else ''
            if 头:
                酒馆对话.追加消息(库, 段.编号, '角色', 头)
            return 段

        def 回来(段, 错):
            if 错:
                QMessageBox.warning(self, '开新对话失败', 错)
                return
            self._当前 = 段.编号
            self.刷新()
            self.新开了.emit(段.编号)

        self._工位.干(活, 回来)

    def 删除(self):
        项 = self.列表.currentItem()
        if 项 is None:
            QMessageBox.information(self, '先选一段', '上面列表里先点一段对话。')
            return
        编号 = 项.data(Qt.UserRole)
        标题 = 项.text().splitlines()[0]
        if QMessageBox.question(
                self, '删掉这段对话？',
                '「%s」和里面的消息会一起删掉，删了找不回来。' % 标题,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return

        def 回来(_值, 错):
            if 错:
                QMessageBox.warning(self, '删对话失败', 错)
                return
            self._当前 = 0
            # ⚠ **得把中栏也一起松开。** 光把右栏的选中清掉的话，`对话页`
            # 那边 `_会话编号` 还指着刚删掉的那一段；用户接着打字发送，
            # `_发到` 就会往一个**不存在的会话**上追加消息——盘上多出一个
            # `消息/<死号>.json`，界面上永远看不到它，也删不掉。
            #
            # 列表里还有别的会话的话，`刷新` 的回调会紧接着自动选中第一段，
            # 所以这一步只是"中间空一下"，不会把用户晾在空框上。
            self.选了.emit(0)
            self.刷新()

        self._工位.干(lambda 库, n=编号: 酒馆对话.删会话(库, n), 回来)


class 对话页(QWidget):
    """
    中间那栏。**对外只有 `换角色` / `开会话` / `刷新` 几个入口。**

    整个"发一条消息"的流程都在这儿的 `_发` 里，顺序一步都不能少——理由见
    那个方法的 docstring。
    """

    会话变了 = Signal(int)        # 当前会话编号变了（状态栏要跟着动）
    要存了 = Signal()             # 有消息落盘了，会话栏该刷新条数

    def __init__(self, 工位, 生成线, 父=None):
        super().__init__(父)
        self._工位 = 工位
        self._生成线 = 生成线
        self._角色编号 = 0
        self._角色 = None            # 整条角色卡（拼提示要用人设 / 开场白）
        self._会话编号 = 0
        self._口子 = {}              # {序号: 气泡}，块按序号路由回来
        self._流 = None              # 当前这次生成，见 `_刷流式`
        #: 从「按了发送」到「这次生成整个收摊」之间为真。**比 `_流` 宽**——
        #: 它涵盖落盘、拼提示、占号、存回复这几段异步路，`_流` 只盖着真正
        #: 在收块的那一段。`_发` 看的是这个，见那边的「同一时刻只许一次」。
        self._忙 = False
        self._这套接口 = None        # 这一次用的是哪套配置（脚注里写模型名用）
        #: 这一段会话**存的**接口编号。**0 = 没指定过**，实际生效的是
        #: `酒馆接口.默认()`（最早建的那套）—— 这两个数分得开，是因为
        #: "没指定"和"指定成第一套"在盘上是两回事，见 `_填配置下拉`。
        self._接口编号 = 0
        self._套们 = []              # 「接口配置」清单（不含密钥），填下拉用
        self.头像 = None             # 当前角色的头像 bytes，取一次给所有气泡用

        外 = QVBoxLayout(self)
        外.setContentsMargins(0, 0, 0, 0)
        外.setSpacing(0)

        self.区 = QScrollArea()
        self.区.setObjectName('消息区')
        self.区.setWidgetResizable(True)
        self.区.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.里 = QWidget()
        self.柱 = QVBoxLayout(self.里)
        self.柱.setContentsMargins(12, 12, 12, 12)
        self.柱.setSpacing(10)
        self.柱.addStretch(1)        # 消息少的时候顶到上面，不居中也不拉伸
        self.区.setWidget(self.里)
        外.addWidget(self.区, 1)

        外.addWidget(self._做输入区())

        # 流式：块先堆进缓冲，这个定时器定期一次性贴上去
        self.表 = QTimer(self)
        self.表.setInterval(刷流毫秒)
        self.表.timeout.connect(self._刷流式)

        self._生成线.分块.connect(self._收块)
        self._生成线.结束.connect(self._收结束)
        self._生成线.坏了.connect(self._收坏)

        self.重读接口()

    # ── 输入区 ──

    def _做输入区(self):
        框 = QFrame()
        排 = QVBoxLayout(框)
        排.setContentsMargins(12, 8, 12, 10)
        排.setSpacing(6)

        self.输入 = 正文框()
        self.输入.setPlaceholderText('说点什么…  （Ctrl+Enter 发送，Enter 换行）')
        self.输入.setMaximumHeight(120)
        self.输入.要发.connect(self._发)
        排.addWidget(self.输入)

        钮排 = QHBoxLayout()
        # ⚠ **这一段对话用哪套接口，在这儿换。** 放在输入区这一行是有意的：
        # 它跟"这一条要发给谁"是同一个决定，而消息区那边一个字都不用动。
        钮排.addWidget(QLabel('接口'))
        self.配置 = _省略下拉()
        self.配置.setMinimumWidth(210)
        # ⚠ **别用 `AdjustToContents`。** 那会让框去装下最长那条，而那个宽度会成为
        # **整个窗口的最小宽度** —— 名字长（名称 · 模型文件名）时窗口被顶开。改用
        # "按 N 个字量"，配上 `_省略下拉` 的省略号；完整名字进 tooltip。
        self.配置.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.配置.setMinimumContentsLength(16)
        self._配置说明 = (
            '这一段对话用哪套接口配置。列的是「接口配置」里的**全部**几套，'
            '想用哪套点哪套。\n\n'
            '**只影响这一段对话** —— 别的会话各用各的，新开的对话仍然是'
            '"没指定"（落到最早建的那套）。')
        self.配置.setToolTip(self._配置说明)
        self.配置.currentIndexChanged.connect(self._换接口)
        钮排.addWidget(self.配置)
        # ⚠ **紧跟下拉的一格小字，专门说"这会儿为什么不能换"。**
        # 不能拿 `提示` 兼职：那一格被"角色：xxx / 生成中… / 这一段现在用：xxx"
        # 轮着占用，灰着的理由会被下一句话顶掉 —— 用户最后只看到一个点不动、
        # 也不解释的灰框，跟"这功能没做"分不出来。
        self.接口说明 = 酒馆样式.灰色小字('')
        钮排.addWidget(self.接口说明)
        钮排.addSpacing(8)
        self.提示 = 酒馆样式.灰色小字('')
        钮排.addWidget(self.提示, 1)
        self.停 = 酒馆样式.做按钮('停止', self._停, '让这次生成停下来')
        self.停.setEnabled(False)
        钮排.addWidget(self.停)
        self.发 = 酒馆样式.做按钮('发送', self._发, 'Ctrl+Enter')
        钮排.addWidget(self.发)
        排.addLayout(钮排)
        return 框

    # ── 接口配置（这一段用哪套）──────────────────────────────────────

    def 重读接口(self):
        """
        重读「接口配置」清单填回下拉。**刚在接口配置页增删改过之后要叫一下**
        （`酒馆窗口.刷新` 和关掉接口页时都走这儿）—— 不重读的话，新加的那套
        要等到下次切会话才出现在下拉里，看着像"没保存上"。
        """
        def 回来(套们, 错):
            if 错:
                return
            self._套们 = list(套们 or [])
            self._填配置下拉(self._接口编号)

        self._工位.干(lambda 库: 酒馆接口.列(库, '对话'), 回来)

    def _填配置下拉(self, 当前编号=0):
        """
        按 `当前编号` 把下拉拨到对的那一项。**列的是全部几套，没有"默认"这一项。**

        ⚠ **`当前编号 = 0`（没指定过）或者指向一套已经删掉的配置时，拨到
        "实际生效的那套"上，但不回写库。** 实际生效的那套就是
        `酒馆接口.默认()` —— 最早建的那套，而 `列()` 本来就是按编号升序的，
        所以是第 0 项。**拨过去只是让用户看见"现在用的其实是哪套"**；一旦
        他真去点了一下别的，那一下才写进会话。要是这儿顺手把 0 写成具体编号，
        这段会话就凭空多了一个"配过"的痕迹，以后想分辨"没设过"就分不清了。
        """
        当 = int(当前编号 or 0)
        self.配置.blockSignals(True)      # 填的过程别触发 `_换接口`
        self.配置.clear()
        for 套 in self._套们:
            self.配置.addItem('%s  ·  %s'
                            % (套.名称 or '没名字', 套.模型 or '（没填模型）'),
                            套.编号)
        有 = bool(self._套们)
        if not 有:
            self.配置.addItem('（一套都没配）', 0)
        位 = self.配置.findData(当)
        if 位 < 0 and 有:
            位 = 0
        self.配置.setCurrentIndex(位 if 位 >= 0 else 0)
        self.配置.blockSignals(False)
        self._撑开下拉()
        # ⚠ **换接口得先有"这一段"可写。** 接口编号是存在会话行上的，
        # 中间那栏还没挑中任何一段时（刚开软件、或者这个角色一段对话都还没有）
        # 没有地方写 —— 所以灰着。**但必须把那句理由摆出来**，光灰着不说，
        # 用户只会觉得这个下拉是坏的。
        self.配置.setEnabled(有 and bool(self._会话编号) and not self._忙)
        self._刷接口说明()
        # 下拉里显示的那一套，就是"这一段现在会用哪套"。`_这套接口` 只是给
        # 脚注写模型名用的显示状态 —— 真发出去时用的是 `_拼` 从库里现读的
        # 那一套（`_摆气泡` 还会把它再盖一次），所以这儿对不上也不会发错，
        # 只是脚注会写错名字。
        位真 = self.配置.currentIndex()
        self._这套接口 = (self._套们[位真]
                       if 有 and 0 <= 位真 < len(self._套们) else None)

    def _撑开下拉(self):
        """
        下拉的**宽度**：框本身**封顶**（不撑窗口），弹出的列表按最长条目撑开。

        ⚠ **从前是"按最长的那条把框本身撑开"** —— 名字长（名称 · 模型文件名）时，
        那个最小宽度会变成**整个窗口的最小宽度**，窗口就被顶开了。现在：框本身封顶、
        装不下走省略号（见 `_省略下拉`）、完整名字进 tooltip。

        ⚠ **弹出的列表是另一个窗口**，撑开它**不影响主窗口宽度**，所以那儿照样
        按最长条目撑开，把整条显示全。
        """
        米 = self.配置.fontMetrics()
        宽 = 0
        for i in range(self.配置.count()):
            宽 = max(宽, 米.horizontalAdvance(self.配置.itemText(i)))
        宽 += 56                       # 左右内边距 + 右边那个下拉箭头
        # 框本身：只给一个"够用"的宽度，**不跟着最长那条约**（这就是"不撑窗口"的关键）。
        self.配置.setMinimumWidth(210)
        try:
            屏 = self.screen().availableGeometry().width() if self.screen() else 1200
        except Exception:
            屏 = 1200
        self.配置.view().setMinimumWidth(min(宽, int(屏 * 0.9)))
        # 完整名字进 tooltip（闭合时被省略号截了，靠它看全）。
        当前 = self.配置.currentText()
        self.配置.setToolTip((('这一段用：%s\n\n' % 当前) if 当前 else '') + self._配置说明)

    def _刷接口说明(self):
        """
        下拉旁边那格小字：这会儿能不能换，不能换是因为什么。

        ⚠ **判据必须跟上面那句 `setEnabled` 一模一样** —— 两处对不上的话，
        要么"灰着却不说为什么"，要么"写着不能换其实能点"，都比分不清更糟。
        """
        if not self._套们:
            self.接口说明.setText('（一套接口都没配 —— 去「接口配置」建一套，Ctrl+I）')
        elif self._忙:
            self.接口说明.setText('（这条说完才能换）')
        elif not self._会话编号:
            self.接口说明.setText('（先点个角色、挑一段对话，就能换这一段用哪套）')
        else:
            self.接口说明.setText('')

    def _换接口(self, _=None):
        """
        这一段换一套接口。**写进这段会话的 `接口编号`，只动这一段。**

        ⚠ **刻意不做"顺便记成新对话的默认"。** 那会多出一个只活在内存/设置里
        的状态位，而且用户下次开新对话时用的跟他以为的不是同一套 —— 这种
        "悄悄改了别的会话"是最难解释的一类行为。新对话照旧是"没指定"，
        落到 `酒馆接口.默认()`。
        """
        if self._忙:
            # 兜底（忙的时候下拉本该是灰的）：把显示拨回去，别让它显示成一个
            # 其实没生效的选择。
            self._填配置下拉(self._接口编号)
            QMessageBox.information(self, '还在生成', '等这条说完，或者按「停止」，再换接口。')
            return
        编号 = self.配置.currentData()
        if not self._会话编号 or not 编号:
            return
        编号 = int(编号)
        if 编号 == self._接口编号:
            return
        号 = self._会话编号

        def 好了(_值, 错):
            if 错:
                self.提示.setText('换接口失败：%s' % 错)
                self._填配置下拉(self._接口编号)     # 拨回原来那套
                return
            self._接口编号 = 编号
            self.重读接口()          # 走一遍重读，把 `_这套接口` 也对上
            self.提示.setText('这一段现在用：%s' % self.配置.currentText())

        self._工位.干(lambda 库, n=号, k=编号:
                    酒馆对话.改会话(库, n, 接口编号=k), 好了)

    # ── 进出 ──

    def 换角色(self, 角色编号):
        """
        左栏换了个角色。**消息区先清空**，等右栏挑定一段再 `开会话`。

        整条角色卡（含人设 / 开场白）得重新 `取()`——**不能拿列表行**，
        那个串不含人设也不含简介，拼出来的提示会缺一大块人格（名字还在，
        但"我是谁、我什么来历"全没了）。
        """
        self._角色编号 = 角色编号 or 0
        self._角色 = None
        self.头像 = None
        self._会话编号 = 0
        self._接口编号 = 0              # 换了角色，这一段还没定，别留着上个的
        self._清空()
        self.提示.setText('')
        self._填配置下拉(0)             # 会话编号已经清了，下拉会自动灰掉
        if not 角色编号:
            return

        def 拿到(角色, 错):
            if 错 or 角色 is None:
                return
            self._角色 = 角色
            self.提示.setText('角色：%s' % (角色.名字 or ''))

            def 有头像(图, _错):
                # `取头像` 回的是 `(字节, 类型)`，这里只要字节
                if 图:
                    self.头像 = (图[0] if isinstance(图, (tuple, list))
                              else 图)
                    self._补头像()

            self._工位.干(lambda 库, n=角色编号: 酒馆角色.取头像(库, n), 有头像)

        self._工位.干(lambda 库, n=角色编号: 酒馆角色.取(库, n), 拿到)

    def _补头像(self):
        """
        头像读回来了，给**已经画出来的**气泡补上。

        头像和消息是两次独立的读，谁先回来不定。所以气泡建的时候头像可能
        还没有——那种情况下先空着，等这儿补；补的时候只换那一格头像，气泡
        本身不重画（重画会把用户正在选的字清掉）。
        """
        for 泡 in self._口子.values():
            泡.贴头像(self.头像)

    def 开会话(self, 会话编号):
        """
        切到一段会话。**已经是这一段就直接返回，不重画。**

        右栏选中一行、`要存了` 之后重选、自动落到第一段——几条路都可能对着
        同一段喊一声。每次都清空重画的话，用户正看着的滚动位置会被拽走；
        更要紧的是**正在流式的那条气泡会被拆掉**（字不会丢，库里照存，但眼前
        一闪）。要强制重画走 `刷新()`，那条路是给编辑/删除/重说用的。
        """
        会话编号 = 会话编号 or 0
        if 会话编号 == self._会话编号:
            return
        self._会话编号 = 会话编号
        self._口子 = {}
        self._清空()
        self.会话变了.emit(self._会话编号)
        if self._会话编号:
            self.刷新()

    def 刷新(self):
        """重读这段会话的消息，整个重画。**编辑、删除、重说之后都走这个。**"""
        if not self._会话编号:
            return
        号 = self._会话编号

        def 活(库, n=号):
            # 会话行和消息**一件活里一起读**：下拉要按会话行里的接口编号来拨，
            # 分两次投的话中间可能又切了会话，下拉会拨到别的段上。
            # 顺带把"实际生效的那套配置"也算出来 —— `取或默认` 要 `库`，
            # 主线程没有，只能在这儿算。
            段 = 酒馆对话.取会话(库, n)
            套 = 酒馆接口.取或默认(库, 段.接口编号, '对话') if 段 else None
            return 段, 酒馆对话.取消息(库, n), 套

        def 回来(包, 错):
            if 错:
                QMessageBox.warning(self, '读消息失败', 错)
                return
            if 号 != self._会话编号:      # 这中间用户又切走了，丢掉
                return
            段, 消息们, 套 = 包
            if 段 is not None:
                self._接口编号 = int(段.接口编号 or 0)
            self._填配置下拉(self._接口编号)
            self._这套接口 = 套
            self._画(消息们)

        self._工位.干(活, 回来)

    # ── 画 ──

    def _清空(self):
        """
        把消息区拆空。**顺带把流式那只手松开。**

        ⚠ `_口子` 先清、控件后删，这个顺序是必须的：外面可能还捏着某个气泡
        的引用（流式的定时器、`_重说` 的回调），而控件一 `deleteLater`，
        它的 C++ 对象就没了，再碰一下就是 `Internal C++ object already
        deleted`。**`_口子` 是唯一凭据**——它一空，那些引用就立刻对不上，
        各自都能一眼看出"这条不是我的了"。见 `_还认得吗`。
        """
        self._口子 = {}
        if self._流 is not None:
            self._流['泡'] = None      # 定时器别再往上写，泡已经没了
        while self.柱.count() > 1:                 # 留最后那个 stretch
            件 = self.柱.takeAt(0)
            标 = 件.widget()
            if 标 is not None:
                # ⚠ **这儿不能 `setParent(None)`。** 那一句会把气泡变成一个
                # **顶层窗口**——实测拿到的窗口标志是 `0x0800F001`，里头
                # `WindowSystemMenuHint | WindowMinimizeButtonHint |
                # WindowMaximizeButtonHint | WindowCloseButtonHint` 全在，
                # 也就是**标题栏右上角那三个键**。Windows 上换父窗口是"先把
                # 原生窗口建出来、再隐藏"，那一下就是一个个带三键的小窗口
                # 往外弹；紧接着 `deleteLater()` 生效，它又消失。
                # 表现：每点一次角色，消息区清空一次，就弹一串小窗口闪一下。
                #
                # `hide()` 一样能立刻让它不再画（`takeAt` 已经从布局里摘掉了，
                # 不隐藏的话它会**留在原位置继续画**），但它压根不去动父子关系，
                # 也就没有原生窗口这一趟。
                标.hide()
                标.deleteLater()

    def _还认得吗(self, 泡):
        """
        这个气泡还是不是消息区里那一个。**碰它之前必须先问。**

        它可能是上一次重画留下的残影（`_清空` 之后 C++ 对象已经没了），
        也可能序号被新的一条顶掉了。两种都不能再碰——前一种一碰就抛，
        后一种会改到**别的会话**的消息上去。
        """
        return 泡 is not None and self._口子.get(泡.序号) is 泡

    def _画(self, 消息们):
        self._清空()
        for m in (消息们 or []):
            说 = m.说话人 or '用户'
            泡 = 气泡(self.里, 说, 名字=self._谁(说), 头像=self.头像,
                     正文=m.内容 or '', 坏了=(说 == '系统'))
            泡.序号 = m.序号
            self._接上(泡)
            self._口子[m.序号] = 泡
        self._限宽()
        QTimer.singleShot(0, lambda: 酒馆样式.滚动到底(self.区, 若已在底部=False))

    def _谁(self, 说):
        if 说 == '角色':
            return (self._角色.名字 if self._角色 else '') or '角色'
        return '你' if 说 == '用户' else '系统'

    def _接上(self, 泡):
        """新造的气泡挂上信号、插进消息区。**造气泡的地方都得走这个。**"""
        泡.要编辑.connect(self._编辑)
        泡.要重说.connect(self._重说)
        泡.要删.connect(self._删)
        泡.setParent(self.里)
        self.柱.insertWidget(self.柱.count() - 1, 泡)

    def _限宽(self, _=None):
        """所有气泡最大宽度 = 视口 70%。消息区一改大小就重新算一遍。"""
        宽 = int(self.区.viewport().width() * 0.70)
        for 泡 in self._口子.values():
            泡.限宽(宽)

    def resizeEvent(self, 事):
        super().resizeEvent(事)
        self._限宽()
        # 拉窗口的时候顺带按到底——不然窗口一矮，最新那条就跑到视口下面去了，
        # 而用户什么都没干
        酒馆样式.滚动到底(self.区)

    # ── 流式 ──

    def _收块(self, 会话, 序号, 段):
        """
        网络那头来了一块。**只堆缓冲，不碰界面**（见文件开头）。

        对不上的块直接丢：用户生成到一半切了会话，回来的块不能糊到现在的
        气泡上。
        """
        流 = self._流
        if 流 is None or 流['会话'] != 会话 or 流['序号'] != 序号:
            return
        流['缓冲'].append(段)

    def _刷流式(self):
        """
        定时器到点：**攒够了才贴，不是到点就贴。**

        ⚠ 到点就贴的话，模型一个字一个字吐（中文 token 常常就是一个字），
        屏幕上就是一个字一个字往外蹦。攒够 `成串字数` 再贴才像在写字；但
        模型慢的时候不能无限憋，所以还有 `憋不住毫秒` 那条线兜底。
        """
        流 = self._流
        # `泡` 可能是 `None`（这中间用户刷了/切了会话，气泡已经没了）。
        # 那就把这个缓冲倒掉——**库里那份照存不误**，回来的块本来也只影响
        # 眼前这一格。
        if 流 is None or 流['泡'] is None or not 流['缓冲']:
            return
        # ⚠ **用户在选文字的时候别重绘。** `QLabel.setText()` 会把选区清掉
        # ——用户正想复制半句话，字一来选区就没了，鼠标一松复制的是空。
        # 跳一帧不影响观感，缓冲还在，下一次照样补上。
        if 流['泡'].有选中():
            return
        现在 = 毫秒()
        攒够 = sum(len(块) for 块 in 流['缓冲']) >= 成串字数
        if not 攒够 and 现在 - 流['上次贴'] < 憋不住毫秒:
            return
        字 = ''.join(流['缓冲'])
        流['缓冲'].clear()
        流['上次贴'] = 现在
        流['泡'].追加(字)
        # R1 这类推理模型先吐一大段 think 才开口，那期间气泡上一个字
        # 都没有、屏幕像死了——往脚标贴一行「思考中…」让人知道它在干活。
        # 思考结束或定稿（`_定稿` 往脚标写时间）都会把它盖掉。
        滤 = 流['泡']._滤
        if 滤 is not None:
            if 滤.在思考:
                流['泡'].脚('思考中…')
            elif 滤.思考过:
                流['泡'].脚('')
        self._限宽()
        酒馆样式.滚动到底(self.区)

    def _收结束(self, 会话, 序号, 全文, 断了):
        """
        生成完了。**存是一定要存的**，哪怕用户已经切走了——不然这一次生成
        白跑了，回来再看这段会话是空的。
        """
        在屏上 = (会话 == self._会话编号 and 序号 in self._口子)
        元 = {'模型': self._模型名(), '打断': bool(断了)}

        def 回来(_值, 错):
            # ⚠ **收摊要等到存完**，不能在这儿之前。占位那行现在是空内容，
            # 存档没完就松手的话，用户这中间再发一条会跟这次回复挤同一个
            # 序号——见 `_开工` 那段。
            self._收摊()
            if 错:
                QMessageBox.warning(self, '存这条消息失败', 错)
                return
            self.要存了.emit()
            if not 在屏上:
                return
            泡 = self._口子.get(序号)
            if 泡 is not None:
                self._定稿(泡, 全文, 断了)

        self._工位.干(lambda 库, n=会话, i=序号, t=全文, m=元:
                    酒馆对话.覆盖消息(库, n, i, '角色', t, m), 回来)

    def _收坏(self, 会话, 序号, 错):
        """
        生成失败。**气泡留着、标红，不从 `_口子` 里摘掉。**

        库里那一行也还在（占位时插的，内容空）。两样都留着是有意的：用户
        看得见是哪条炸的，而且直接对着它按「重说」就能重来——号还占着。
        """
        self._收摊()
        泡 = self._口子.get(序号) if 会话 == self._会话编号 else None
        if 泡 is not None:
            泡.标坏(错)
            self.提示.setText('生成失败')
            return
        QMessageBox.warning(self, '生成失败', 错)

    def _定稿(self, 泡, 全文, 断了):
        """
        收尾一条气泡：切 Markdown。

        ⚠ **切之前记滚动位置，切完放回去。** Markdown 渲染出来跟纯文本的
        高度不一样（`**` 这类标记收掉了），不记不还原的话，界面会在用户
        眼皮底下自己跳一下。
        """
        条 = self.区.verticalScrollBar()
        位 = 条.value()
        贴底 = 条.value() >= 条.maximum() - 8
        泡.定稿(全文)
        self._限宽()
        泡.脚('%s%s' % ('已被打断 · ' if 断了 else '', _时(毫秒())))
        if 贴底:
            酒馆样式.滚动到底(self.区, 若已在底部=False)
        else:
            条.setValue(位)          # 用户在翻旧消息，别把他拽走
        self.提示.setText('已打断' if 断了 else '')

    def _收摊(self):
        """这次生成整个结束（成了、坏了、断了都走这儿）。**忙也就到此为止。**"""
        self._流 = None
        self._忙 = False
        self.表.stop()
        self.发送状态()

    def 发送状态(self):
        """发送 / 停止两个钮的可用状态。**只看 `_忙` 这一处，别分散着设。**"""
        self.发.setEnabled(not self._忙)
        self.停.setEnabled(self._流 is not None)
        # 接口下拉同理：生成期间不给换（`_拼` 已经读过接口编号了，这时候换
        # 会让人以为"这一条用的就是新换的那套"，其实不是）。没有会话可写的
        # 时候也灰着 —— 换了没地方记。
        self.配置.setEnabled(not self._忙 and bool(self._会话编号)
                          and bool(self._套们))
        self._刷接口说明()

    def _模型名(self):
        return (self._这套接口.模型 if self._这套接口 else '') or ''

    # ── 发 ──

    def _发(self):
        """
        发一条消息。**顺序一步都不能少：**

          1. 先 `追加消息` 落盘 —— 落盘了界面才敢显示。反过来的话界面上
             已经"发出去了"、库里其实没有，一刷新就没了
          2. 空标题的话拿首句前 20 字当标题 —— 顺手，而且这是唯一时机
             （只有这会儿才知道第一句话是什么）
          3. 占生成位：`下个序号` 拿到序号，回来的块按这个序号路由
          4. 才轮到生成线程出门

        每一步坏掉的结果都是**看得见、能收拾**的：断在 1~2 之间，最坏是
        会话多一条没回复的消息，界面上看得见、能删能重说；断在 3 之后，
        最坏是一个空位，同样看得见。

        ⚠ **角色选好了、一段对话都还没有的时候，这儿自己开一段再发。**
        原来这儿是弹「先开一段对话」把人挡回去，那是个死胡同：刚建好的角色
        一段对话都没有，用户按他想的顺序（点角色 → 打字 → 发送）走，**永远
        撞在这个弹窗上**，而弹窗让他去右边点，右边是空的。见 `_开一段再发`。
        """
        文 = self.输入.toPlainText().strip()
        if not 文:
            return
        if not self._角色编号:
            QMessageBox.information(self, '先选个角色', '左栏里先点一个角色。')
            return
        if self._忙:
            QMessageBox.information(self, '还在生成', '等这条说完，或者先按停止。')
            return

        # **从这一刻就占住「忙」**，一直占到 `_收摊`。开新对话那一段也是异步
        # 的，不在这儿就占住的话，用户在那几十毫秒里再按一次发送，就会开出
        # 两段对话、两条一模一样的消息。
        self._忙 = True
        self.发送状态()

        if self._会话编号:
            self._发到(self._会话编号, 文)
        else:
            self._开一段再发(文)

    def _发到(self, 号, 文):
        """
        把这一条落盘，然后起生成。**`_发` 和「刚开出来的那段」都走这儿。**

        ⚠ **输入框到这儿才清。** 开新对话那一步是异步的，真坏在那儿的话，
        用户打的那句话还得留在框里——不能让他重打一遍。
        """
        self.输入.clear()

        def 落好了(条, 错):
            if 错:
                QMessageBox.warning(self, '存这条消息失败', 错)
                self._收摊()
                return
            self.要存了.emit()
            泡 = 气泡(self.里, '用户', 名字='你', 正文=条.内容)
            泡.序号 = 条.序号
            self._接上(泡)
            self._口子[条.序号] = 泡
            self._限宽()
            酒馆样式.滚动到底(self.区, 若已在底部=False)
            self._也许起标题(文, 号)
            self._起生成(号)

        self._工位.干(lambda 库, n=号, t=文:
                    酒馆对话.追加消息(库, n, '用户', t), 落好了)

    def _开一段再发(self, 文):
        """
        一段对话都没有，那就先开一段，开好了接着把 `文` 发出去。

        **建会话 + 落开场白在同一件活里做完**（跟 `会话栏.新建` 一个道理）：
        分两次投的话，中间那一小会儿这段会话是空的，而用户正好在这时候点了
        它，看到的就是个空框。

        只有"这个角色一段对话都没有"才走到这儿 —— 有旧对话的情况在
        `会话栏.刷新` 里已经自动落在最近那段上了，不会绕过去另开一段。
        """
        角色 = self._角色编号

        def 活(库):
            卡片 = 酒馆角色.取(库, 角色)
            段 = 酒馆对话.建会话(库, 角色)
            头 = (卡片.开场白 or '').strip() if 卡片 else ''
            if 头:
                酒馆对话.追加消息(库, 段.编号, '角色', 头)
            return 段

        def 回来(段, 错):
            if 错 or 段 is None:
                self._收摊()                 # 忙得松开，不然发送钮永远灰着
                QMessageBox.warning(self, '开新对话失败', str(错))
                return
            self.开会话(段.编号)             # 消息区换上这一段（开场白就在里头）
            self.要存了.emit()               # 右栏该把这一段列出来
            self._发到(段.编号, 文)

        self._工位.干(活, 回来)

    def _也许起标题(self, 文, 会话):
        """标题还空着就拿首句前 20 字起一个。**起过就不再动**，不覆盖用户改的。"""
        def 回来(段, 错):
            if 错 or 段 is None or (段.标题 or '').strip():
                return
            头 = ' '.join(文.split())
            if len(头) > 20:
                头 = 头[:20] + '…'
            self._工位.干(lambda 库, n=会话, t=头:
                        酒馆对话.改标题(库, n, t))
            self.要存了.emit()

        self._工位.干(lambda 库, n=会话: 酒馆对话.取会话(库, n), 回来)

    def _起生成(self, 会话编号):
        """取上下文 → 占序号 → 排一次生成。**取上下文整个在工人线程里跑。**"""
        self._工位.干(lambda 库, n=会话编号: self._拼(库, n),
                    lambda 包, 错: self._开工(会话编号, None, 包, 错))

    def _起生成到(self, 会话编号, 序号):
        """跟 `_起生成` 一样，只是序号**用指定的那个**，不新占一个。重说走这条。"""
        self._工位.干(lambda 库, n=会话编号: self._拼(库, n),
                    lambda 包, 错: self._开工(会话编号, 序号, 包, 错))

    def _拼(self, 库, 会话编号):
        """
        **整个在工人线程里跑。** 回 `(段, 角色, 消息们, 套, 设定, 回收)`。

        `回收` = `{'候选': […], '水位': {…}}`：这次要收成"待定记忆"的纯数据
        （还没落盘）。**落盘不在这儿做** —— 这是生成前的关键路径，而写记忆
        要读-改-写整份条目 + 推水位；`_摆气泡` 排完活之后会另投一件活去写。
        """
        段 = 酒馆对话.取会话(库, 会话编号)
        if 段 is None:
            raise 酒馆错误('这段会话不在了（编号 %s），刷新一下。' % 会话编号)
        角色 = 酒馆角色.取(库, 段.角色编号)
        if 角色 is None:
            raise 酒馆错误('这段会话挂的角色不在了（编号 %s）。' % 段.角色编号)

        设定 = 段.设定()
        if not isinstance(设定, dict):
            # `读JSON` 的约定是"坏掉就给默认值、不抛"。库里的 `预设` 要是被
            # 写成了个列表或者数字，这里不挡一下就是 `AttributeError`。
            设定 = {}

        # 「截多少」这件事**只有 `酒馆大脑.窗口内` 一个地方说了算**——
        # 「上下文」那个窗口要靠它把"哪几条会发出去"画出来给用户看。
        #
        # ⚠ **它同时管两件事**：会话设定里的「条数 / 字数上限」，**以及这套
        # 接口的窗口**（本地模型有硬窗口，只按字数控必然超 —— 见那个函数）。
        # 早先这儿是 `上下文设置` + `从尾部截` 两句，就是少了后面那一半。
        条数, _ = 酒馆大脑.上下文设置(设定)
        套 = 酒馆接口.取或默认(库, 段.接口编号, '对话')

        # ── 长期记忆 ──
        #
        # ⚠ **在这一步读、这一步渲染，塞进 `设定` 的保留键。**
        # 为什么不在这儿直接拼进系统提示：真正发出去的那一份系统提示是在
        # `_摆气泡` 里**又拼了一次**（`拼提示` 这条链上有三个调用点），而
        # `设定` 是唯一能同时到达那三处的**同一个对象** —— 参数化的话漏掉
        # 任意一处都是坏法（漏 `_摆气泡` = 记忆根本没发出去；漏 `窗口内` =
        # 裁 token 时不知道记忆占了多少，提示词越过 n_ctx，llama.cpp 从最
        # 前面截断、**先丢系统提示**，界面上毫无症状）。
        #
        # ⚠ **这个键绝不能落盘**：它是这一轮的临时数据，不是会话预设。
        # 见 `酒馆大脑.拼提示` 里那段注释。
        记忆条 = 酒馆记忆.取(库, 酒馆记忆.角色, 段.角色编号)
        设定['__记忆__'] = 酒馆记忆.聊天块(记忆条, 上限=_记忆预算(套))[0]

        # 全部消息一次读出来，两种用法：`候选` 交给 `窗口内` 裁；`全部` 给
        # 自动回收算边界。⚠ `取最近` 底下本来就是整份读文件再切片，
        # 所以"读全部"不多花一次 I/O。
        全部 = 酒馆对话.取消息(库, 会话编号)
        候选 = 全部[-条数:] if 条数 > 0 else []
        消息们, _, _ = 酒馆大脑.窗口内(角色, 段, 候选, 设定, 套)

        水 = 酒馆记忆.取水位(库, 酒馆记忆.角色, 段.角色编号)
        该收, 到 = _该收的(全部, 消息们, 水.get(int(会话编号), 0), 会话编号)
        新水 = None
        if 到:
            新水 = dict(水)
            新水[int(会话编号)] = 到
        return 段, 角色, 消息们, 套, 设定, {'候选': 该收, '水位': 新水}

    def _开工(self, 会话编号, 指定序号, 包, 错):
        """
        上下文取回来了。占生成位，然后起气泡、排活。

        ⚠ **占位一定要真往库里插一行**，不能只调 `下个序号` 记个数字完事。
        `覆盖消息` 走的是 `替换`（upsert），所以：只记数字的话，库里在回复
        落盘之前**没有这一行**；这中间用户要是又发了一条，那条的 `下个序号`
        会算出**同一个号**，两边撞一起——角色回复落盘时把用户那条**静默
        覆盖掉**。插了行就没这回事：号已经被占住，后来的人只能往后排。

        顺手还顶了会话的 `更新于`，也对——这一问一答是连着的。
        """
        if 错:
            QMessageBox.warning(self, '拼提示失败', 错)
            self._收摊()
            return
        段, 角色, 消息们, 套, 设定, 回收 = 包

        def 占了(条, e):
            if e:
                QMessageBox.warning(self, '占生成位失败', e)
                self._收摊()
                return
            self._摆气泡(会话编号, 条.序号,
                        (段, 角色, 消息们, 套, 设定, 回收))

        if 指定序号 is None:
            self._工位.干(lambda 库, n=会话编号:
                        酒馆对话.追加消息(库, n, '角色', ''), 占了)
        else:
            # 重说：号是定死的，那一行 `_重说` 已经按原样占回来了
            self._摆气泡(会话编号, 指定序号, (段, 角色, 消息们, 套, 设定, 回收))

    def _摆气泡(self, 会话编号, 序号, 包):
        """起一个"正在生成"的气泡，然后真的把活排给生成线程。"""
        if 会话编号 != self._会话编号:      # 这中间用户切走了
            return
        段, 角色, 消息们, 套, 设定, 回收 = 包
        self._角色 = self._角色 or 角色

        泡 = 气泡(self.里, '角色', 名字=self._谁('角色'), 头像=self.头像,
                 正文=酒馆大脑.开场占位)
        泡.序号 = 序号
        self._接上(泡)
        self._口子[序号] = 泡
        self._限宽()
        酒馆样式.滚动到底(self.区, 若已在底部=False)

        self._这套接口 = 套
        # `上次贴` 是"憋不住"那条线的起点，见 `_刷流式`
        self._流 = {'会话': 会话编号, '序号': 序号, '泡': 泡, '缓冲': [],
                   '上次贴': 毫秒()}
        self.表.start()
        self.发送状态()
        self.提示.setText('生成中…')

        # 提示在这一步就拼好，生成线程拿到的是**纯数据**——它碰不着库
        # `本地=` 让 local 协议在系统提示末尾多补一段扮演规矩，见 `酒馆大脑.本地规矩`
        系统, 提示 = 酒馆大脑.拼提示(角色, 段, 消息们, 设定,
                                     本地=酒馆大脑.本地协议(套))
        self._生成线.排(会话编号, 序号, 系统, 提示, 套, 设定)
        self._收待定(段.角色编号, 会话编号, 回收)

    def _收待定(self, 角色编号, 会话编号, 回收):
        """
        把"这次被忘掉的"收成待定记忆。**排完活之后另投一件活。**

        ⚠ **不在 `_拼` 里写。** 那是生成前的关键路径（写完才轮得到模型开跑），
        而写一次记忆要读-改-写整份条目 + 推水位。投给工位是非阻塞的，而且
        `库` 本来就只归工人线程碰 —— 这也正是"生成线一个字节都不碰存储"那条
        形状的延续。

        ⚠ **水位跟条目必须在同一次写里**（`酒馆记忆.收待定` 的 `水位=`）：
        分两次的话，中间死掉就是"条目收了、水位没动"，下一轮同一批会再收一遍。
        """
        该收 = (回收 or {}).get('候选') or []
        水位 = (回收 or {}).get('水位')
        if not 该收 and not 水位:
            return
        角色 = int(角色编号 or 0)
        if not 角色:
            return
        self._工位.干(lambda 库, n=角色, c=该收, w=水位:
                    酒馆记忆.收待定(库, 酒馆记忆.角色, n, c, 水位=w))

    def _停(self):
        if self._生成线.打断():
            self.提示.setText('正在停…')

    # ── 消息上的操作 ──

    def _编辑(self, 泡):
        if not self._还认得吗(泡):
            return
        框 = QDialog(self)
        框.setWindowTitle('改这一条')
        框.resize(560, 320)
        排 = QVBoxLayout(框)
        编 = QPlainTextEdit(泡._原始 or '')
        排.addWidget(编, 1)
        钮 = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        钮.button(QDialogButtonBox.Ok).setText('保存')
        钮.button(QDialogButtonBox.Cancel).setText('取消')
        钮.accepted.connect(框.accept)
        钮.rejected.connect(框.reject)
        排.addWidget(钮)
        if 框.exec() != QDialog.Accepted:
            return
        新 = 编.toPlainText()
        if 新 == 泡._原始:
            return

        def 回来(_值, 错):
            if 错:
                QMessageBox.warning(self, '改这条消息失败', 错)
                return
            self.要存了.emit()
            self.刷新()

        self._工位.干(lambda 库, n=self._会话编号, i=泡.序号, t=新,
                     s=泡.说话人: 酒馆对话.覆盖消息(库, n, i, s, t), 回来)

    def _重说(self, 泡):
        """
        重新生成这一条。**覆盖同一个序号**，不长出重复行。

        这条是「角色」说的才有意义——重说一条用户消息等于替他改口，没道理。
        是用户的就提示他去用「编辑」。

        ⚠ **先把这条从库里删掉，再按老流程取最近 N 条。** 不删的话模型会
        看到自己上一次说的话，变成"接着自己的话往下写"，而不是"重说一遍"。
        删除和摆位之间断掉的话，最坏是这条没了——看得见，而且再点一次重说
        就回来了。
        """
        if not self._还认得吗(泡):
            return
        if 泡.说话人 != '角色':
            QMessageBox.information(self, '这条不用重说',
                                    '这是你说的话。想改的话用「编辑」。')
            return
        if self._忙:
            QMessageBox.information(self, '还在生成', '先等这条说完，或者按停止。')
            return
        号 = self._会话编号
        序号 = 泡.序号
        self._忙 = True
        self.发送状态()

        def 活(库):
            # 删 + 按原号占回来，**一件活里做完**：中间断掉的话要么老内容
            # 还在（等于没重说），要么那号空着——两种都看得见，不会撞号。
            酒馆对话.删消息(库, 号, 序号)
            酒馆对话.覆盖消息(库, 号, 序号, '角色', '')

        def 删好了(_值, 错):
            if 错:
                QMessageBox.warning(self, '重说失败', 错)
                self._收摊()
                return
            # 这一趟是异步的，`泡` 这中间可能已经被刷掉了——那就别碰它，
            # 光把它从 `_口子` 里摘掉（摘之前先确认摘的是它自己）
            if self._口子.get(序号) is 泡:
                self._口子.pop(序号, None)
                # 同 `_清空`：`hide()`，**不是 `setParent(None)`**——那一句会
                # 把它变成带标题栏三键的顶层窗口，弹一下再消失。
                泡.hide()
                泡.deleteLater()
            self._起生成到(号, 序号)

        self._工位.干(活, 删好了)

    def _删(self, 泡):
        if not self._还认得吗(泡):
            return
        if QMessageBox.question(self, '删掉这一条？', '删了就没了。',
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return

        def 回来(_值, 错):
            if 错:
                QMessageBox.warning(self, '删这条失败', 错)
                return
            self.要存了.emit()
            self.刷新()

        self._工位.干(lambda 库, n=self._会话编号, i=泡.序号:
                    酒馆对话.删消息(库, n, i), 回来)

    # ── 外面要的 ──

    def 现在哪段(self):
        return self._会话编号

    def 正在生成(self):
        """这会儿正在收块。**比 `在忙` 窄**——中间那几段异步路它不算。"""
        return self._流 is not None

    def 在忙(self):
        """
        从「按了发送」到「这次生成整个收摊」之间为真。

        「上下文」那个框靠它决定给不给开：生成期间删消息，会被生成线程收尾
        时那句 `覆盖消息(会话, 序号, …)` 原地写回来。见 `酒馆窗口._开上下文`。
        """
        return self._忙

    def 关掉(self):
        """窗口要关了。**停生成线程，然后停界面那个定时器。**"""
        self.表.stop()
        self._生成线.停()
