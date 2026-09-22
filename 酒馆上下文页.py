#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆上下文页.py — 「这一段对话，这一次到底会发什么出去」

```
┌────────────────────────────────────────────────────────────────────┐
│ 会话：第一次见面      角色：爱丽丝      接口：本地 ollama           │
├────────────────────────────────────────────────────────────────────┤
│ 上下文条数 [20]   字数上限 [24000]              [保存设置]          │
│ 系统提示（拼在角色设定后面，不进消息数组；本地会再补扮演规矩）      │
│ [                                                              ]   │
├──────────────────────────────┬─────────────────────────────────────┤
│ 消息                         │ 发出去的样子                        │
│ ┌──────────────────────────┐ │ ┌─────────────────────────────────┐ │
│ │[发] 1  角色  ……你挡着光了。│ │ │──── system ────                 │ │
│ │[发] 2  用户  你好呀        │ │ │你是爱丽丝……                     │ │
│ │[系] 3  系统  记得用敬语    │ │ │                                 │ │
│ │[截] 4  用户  很早以前…     │ │ │──── user ────                   │ │
│ └──────────────────────────┘ │ │……                               │ │
│ [删掉这条] [清空这段]         │ └─────────────────────────────────┘ │
│ [发]=会发 [系]=折进系统提示   │ 系统提示 812 字 · 消息 6 条 · 2140 字│
│ [截]=被截在外面               │                                     │
├──────────────────────────────┴─────────────────────────────────────┤
│ 还有 4 条被截在外面                       [刷新]        [关闭]      │
└────────────────────────────────────────────────────────────────────┘
```

**这个窗口存在的唯一理由：现在「这次发出去的是什么」是看不见的。**

拼提示那条链上其实发生了好几件事——按条数截、按字数再截一道、`说话人='系统'`
的消息被折进系统提示、首条是角色说的会被挪进系统提示、相邻同角色会被合并。
每一步都有它的道理（见 `酒馆大脑.拼提示` / `规整角色` 那两处），可**它们全在
一个函数里悄悄发生**，用户看到的现象只有一个：「模型怎么好像没记住刚才那句」。

所以右边那栏**不是设计图，是真的把 `拼提示` 调一遍、把它吐出来的东西照原样
摊开**。左边那栏标出每条消息的归宿（会发 / 折进系统提示 / 被截在外面）。

⚠ **「哪几条会发」这个判断不在这儿重算。** 截的规则就是
`酒馆大脑.上下文设置` + `从尾部截`，和 `酒馆对话页._拼` 调的是同一份代码。
在这儿另写一遍的话，界面说会发 20 条、实际发 19 条——而这种不一致**跑起来
一点症状都没有**，是这个窗口最不该出的错（它本来就是拿来"对账"的）。

⚠ **这个框只管一段会话（打开时那一段）。** 没跟着主窗切角色切会话动——
它是模态的，开着的时候主窗点不动，也就没有"切走了"这回事。

════════════════════════════════════════════════════════════════════
同一个文件里的另外两样：`助手上下文页` 和 `上下文框`
════════════════════════════════════════════════════════════════════

`上下文页` 管**聊天会话**；`助手上下文页` 管**编程助手跑过的任务**。两者问的
是同一个问题（"这回到底喂给模型的是什么"），所以现在**装在同一个窗口里**：
`上下文框` 是一个带页签的 `QDialog`，页 0 是前者、页 1 是后者，入口只有
主窗那一个（「上下文…」/ `Ctrl+K`），`看哪` 决定落在哪一页。

⚠ **那两个类现在是 `QWidget`（"页"），不是 `QDialog`（"窗口"）。** 标题栏
和「关闭」都归 `上下文框` —— 一个窗口里放两个"关闭"，用户不知道该按哪个。

⚠ **两个页的分工不一样，别搞混**：`上下文页` 能改（保存预设、删消息），
所以有"忙的时候只读"这回事（见它自己的 docstring）；`助手上下文页` 读的是
**已经存过盘的转录**，跟聊天正在生成的那条毫无关系，所以永远能看能删。

`助手上下文页` 右边那栏是这个任务**最后一次真正发出去的** system + messages，
一个字不多一个不少 —— 那是 `酒馆助手.跑` 在调模型之前当场冻下来的快照（见
`抄本.最近发出`），不是事后重拼的。事后重拼会出错：`消息()` 会二次折叠，
而撞上上下文上限那条路**根本没调模型**，重拼出来的是"没发出去的那一份"。

⚠ **左栏的 `[折]/[发]/[未]` 用快照里冻的 `折到/共步`，不用转录本末尾那个
`折到`。** 同一个理由：末尾那个会被"拼命折但没发出去"的那次改高。
"""

import os
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QSpinBox, QSplitter, QTabWidget,
                               QVBoxLayout, QWidget)

import 酒馆大脑
import 酒馆对话
import 酒馆接口
import 酒馆本地
import 酒馆记忆
import 酒馆角色
import 酒馆助手存
import 酒馆样式
import 酒馆工具
from 酒馆存储 import 酒馆错误

__all__ = ['上下文页', '助手上下文页', '上下文框', '记忆板']

#: 消息列表里那一行的摘要留多长。**够认出是哪条就行**——整条正文在右边
#: 的预览里一个字不少，这儿列长了只会把列表撑成一面墙。
摘要宽 = 42


def _摘要(文):
    """一行摘要：换行压成空格，太长就切掉。"""
    文 = ' '.join((文 or '').split())
    return 文[:摘要宽] + ('…' if len(文) > 摘要宽 else '')


# ── 记忆板（聊天那页和助手那页共用一块）──────────────────────────────

class 记忆板(QWidget):
    """
    改一块**长期记忆**：勾选 + 编辑条目。**同一个类用两次** —— 「聊天会话」
    那页绑角色记忆，「编程助手」那页绑当前工作目录的项目记忆，差别只有挂载点
    （`类型` + `键`）。

    ⚠ **它不认识 `库`。** 读写全投给 `工位`（`库` 只归工人线程，见 `酒馆工人`）。

    ⚠ **它不认识"这次到底发了什么"。** 它只管条目本身；"发出去的样子"在右边
    那栏，由 `拼提示` 真拼一遍给出来。两处各算各的必然对不上 —— 而这个功能
    存在的全部意义就是"发出去的和看到的是同一份"。

    ⚠ **勾选框一按就落盘，改正文要按「保存这条」。** 前者是一个完整的意图
    （勾上/取消），再让他点一次「保存」是多余的；后者写到一半的字不该落盘。
    """

    def __init__(self, 工位, 说明='', 预算查=None, 块=None, 父=None):
        super().__init__(父)
        self._工位 = 工位
        self._类型 = ''
        self._键 = ''
        self._预算查 = 预算查      # 可调用 → 这次记忆最多多少 token
        self._块 = 块              # 可调用(条目们, 上限) → (文本, 带, 没带, token)
        self._条目们 = []
        self._位 = -1              # 正在编辑第几条（-1 = 没选中）

        外 = QVBoxLayout(self)
        外.setContentsMargins(0, 0, 0, 0)
        外.setSpacing(4)
        self.抬头 = 酒馆样式.灰色小字(说明)
        self.抬头.setWordWrap(True)
        外.addWidget(self.抬头)

        排 = QHBoxLayout()
        排.setContentsMargins(0, 0, 0, 0)
        self.列表 = QListWidget()
        self.列表.setMaximumHeight(108)
        self.列表.setToolTip('勾上的才会拼进提示词。「自动」那些是软件替你收回来的'
                           '候选（默认不勾），点「保存这条」之前它们不会生效')
        self.列表.currentRowChanged.connect(self._选了)
        self.列表.itemChanged.connect(self._勾变了)
        排.addWidget(self.列表, 1)
        self.正文 = QPlainTextEdit()
        self.正文.setPlaceholderText('选中左边一条，在这儿改它写了什么')
        self.正文.setMaximumHeight(108)
        排.addWidget(self.正文, 1)
        外.addLayout(排)

        底 = QHBoxLayout()
        self.统计 = 酒馆样式.灰色小字('')
        self.统计.setWordWrap(True)
        底.addWidget(self.统计, 1)
        底.addWidget(酒馆样式.做按钮('新增一条', self._新加, '加一条自己写的记忆'))
        底.addWidget(酒馆样式.做按钮(
            '保存这条', self._存这条,
            '写回盘上。\n\n⚠ 记忆一变，下一轮模型要把整段提示重新读一遍，'
            '会明显慢一下 —— 只影响那一轮'))
        self.删钮 = 酒馆样式.做按钮('删掉这条', self._删这条, '删了找不回来')
        底.addWidget(self.删钮)
        外.addLayout(底)

    # ── 挂载点 ──

    def 设挂载点(self, 类型, 键, 说明=None):
        """
        换到另一块记忆（换了会话/角色/目录）→ 重读。

        ⚠ **挂载点没变就不重读** —— 这页每次刷新都会调它一次，每次都重读的话
        用户正在改的那条会被刷掉。
        """
        类型, 键 = str(类型 or ''), str(键 or '')
        if 说明 is not None:
            self.抬头.setText(说明)
        if (类型, 键) == (self._类型, self._键):
            return
        self._类型, self._键 = 类型, 键
        self._位 = -1
        self.正文.setPlainText('')
        self._读()

    def 重读(self):
        self._读()

    def 现在哪块(self):
        return self._类型, self._键

    # ── 读 ──

    def _读(self):
        if not (self._类型 and self._键):
            self._条目们 = []
            self._列表重画()
            self.正文.setPlainText('')
            self.统计.setText('')
            return
        类型, 键 = self._类型, self._键
        self._工位.干(lambda 库, c=类型, k=键: 酒馆记忆.取(库, c, k), self._画)

    def _画(self, 条目们, 错):
        if 错:
            self.统计.setText('读记忆失败：%s' % 错)
            return
        self._条目们 = [dict(x) for x in (条目们 or []) if isinstance(x, dict)]
        self._位 = -1
        self._列表重画()
        self.正文.setPlainText('')
        self._刷统计()

    # ── 画 ──

    def _列表重画(self):
        # ⚠ **重画的时候必须静音。** 底下要一项一项 `setCheckState`，那会发
        # `itemChanged` —— 不静音的话每画一次列表就等于"用户把每条都勾了一遍"，
        # 一路写回盘上。
        self.列表.blockSignals(True)
        self.列表.clear()
        for i, 条 in enumerate(self._条目们):
            项 = QListWidgetItem('%s %s' % (
                '[自动]' if 条.get('来源') == '自动' else '[我写的]',
                _摘要(条.get('文') or '')))
            项.setFlags(项.flags() | Qt.ItemIsUserCheckable)
            项.setCheckState(Qt.Checked if 条.get('开') else Qt.Unchecked)
            项.setData(Qt.UserRole, i)
            项.setToolTip((条.get('文') or '')[:2000])
            self.列表.addItem(项)
        self.列表.blockSignals(False)
        if self._条目们:
            self.列表.setCurrentRow(0)

    def _选了(self, 行):
        self._收正文()
        if 0 <= 行 < len(self._条目们):
            self._位 = 行
            self.正文.setPlainText(self._条目们[行].get('文') or '')
        else:
            self._位 = -1
            self.正文.setPlainText('')

    def _刷统计(self, 后话=''):
        """
        底下那行小字。

        ⚠ **数字取自"真拼一遍"的结果**（`酒馆记忆.渲染`，跟页面用的是同一个
        函数），不是自己按"勾了几条"另数一遍 —— 那样界面说的和真发出去的
        会不是一份。
        ⚠ 标签要写清这是**记忆本身**的 token：右边那栏那句"系统提示 N 字"是
        **整份 system（含记忆、含人设、含规矩）**，两个数不一样，不说清会被
        当成对不上。
        """
        上限 = 0
        if self._预算查 is not None:
            try:
                上限 = int(self._预算查() or 0)
            except Exception:
                # 探测用的回调，跨在"页面 → 这块板"之间，坏了不该把界面带崩
                上限 = 0
        条目们 = self._条目们
        if self._块 is not None:
            文本, 带, 没带, 大 = self._块(条目们, 上限)
        else:
            文本, 带, 没带, 大 = 酒馆记忆.渲染(条目们, 上限)
        开 = sum(1 for x in 条目们 if x.get('开'))
        说 = '记忆：启用 %d 条 · 共 %d 字 ≈ %d token' % (开, len(文本), 大)
        if 没带:
            说 += '（窗口只装得下 %d 条，还有 %d 条这次没带上）' % (带, 没带)
        elif 开:
            说 += '（这次全都带得上）'
        if 后话:
            说 += ' ' + 后话
        self.统计.setText(说)

    # ── 改 ──

    def _收正文(self):
        """把编辑框里的字收进内存那份。**任何写回之前都要先收。**"""
        if 0 <= self._位 < len(self._条目们):
            self._条目们[self._位]['文'] = self.正文.toPlainText()

    def _勾变了(self, 项):
        i = 项.data(Qt.UserRole)
        if not isinstance(i, int) or not (0 <= i < len(self._条目们)):
            return
        self._收正文()
        self._条目们[i]['开'] = (项.checkState() == Qt.Checked)
        self._写回()

    def _新加(self):
        """加一条空的。**先不落盘** —— 还什么都没写呢；写完点「保存这条」。"""
        self._收正文()
        self._条目们.append({'文': '', '开': True, '来源': '手写'})
        self._列表重画()
        self.列表.setCurrentRow(len(self._条目们) - 1)
        self.正文.setFocus()
        self.统计.setText('新的一条还没保存 —— 写完点「保存这条」。')

    def _存这条(self):
        self._收正文()
        if not (0 <= self._位 < len(self._条目们)):
            self.统计.setText('先在左边选一条（或者点「新增一条」）。')
            return
        if not (self._条目们[self._位].get('文') or '').strip():
            self.统计.setText('这条是空的 —— 写点东西再保存，或者点「删掉这条」。')
            return
        self._写回('已保存。')

    def _删这条(self):
        self._收正文()
        if not (0 <= self._位 < len(self._条目们)):
            self.统计.setText('先在左边选一条。')
            return
        条 = self._条目们[self._位]
        # ⚠ **机器收回来的候选不问，用户自己写/启用过的要问。**
        # 那种候选一屏可能几十条，逐条弹确认没人受得了；而它们本来就是
        # "摆着等你过目"的东西，删掉丢的也只是"这次任务被折叠的步骤"。
        # 用户写的、或者他亲手启用过的，才是真的删了找不回来。
        if 条.get('来源') != '自动' or 条.get('开'):
            if QMessageBox.question(
                    self, '删掉这条记忆？',
                    '「%s」\n\n**删了就没了。**'
                    % ' '.join(str(条.get('文') or '').split())[:200],
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No) != QMessageBox.Yes:
                return
        self._条目们.pop(self._位)
        self._位 = -1
        self._列表重画()
        self.正文.setPlainText('')
        self._写回('删掉了。')

    def _写回(self, 后话=''):
        """
        整份写回盘上。**先剔掉空条目** —— `酒馆记忆.存` 也会剔，但这里先剔
        一份留着的，是为了让界面上显示的和盘上的一致（不然刚点「新增一条」
        又去勾了别的，那条空的会从盘上消失而列表里还挂着）。
        """
        if not (self._类型 and self._键):
            return
        类型, 键 = self._类型, self._键
        留 = [dict(x) for x in self._条目们 if str(x.get('文') or '').strip()]
        丢了 = len(self._条目们) - len(留)

        def 好了(_值, 错):
            if 错:
                self.统计.setText('保存失败：%s' % 错)
                self._读()          # 盘上是什么样，就以盘上为准重画一遍
                return
            self._条目们 = 留
            self._位 = min(self._位, len(留) - 1)
            self._列表重画()
            self._刷统计(后话 + ('（有 %d 条空的没存）' % 丢了 if 丢了 else ''))

        self._工位.干(lambda 库, c=类型, k=键, 条=留:
                    酒馆记忆.存(库, c, k, 条), 好了)


# ── 聊天会话的上下文 ────────────────────────────────────────────────

class 上下文页(QWidget):
    """
    看和调这一段对话的上下文。**只读 + 改会话预设，不碰消息本身的内容**
    （删消息是另一回事，那个走 `酒馆对话.删消息`，和聊天页删的是同一条路）。

    `改过了` 置位表示期间动过消息或预设，外面该把消息区和会话栏刷一遍。

    ⚠ **它是个"页"，不是"窗口"。** 早先它自己是个 `QDialog`（带标题栏、带
    自己的「关闭」），现在挂在 `上下文框` 的页签里 —— 外壳归那个框管，
    一个窗口里不能有两个"关闭"。所以这个类里**没有** `exec()/reject()`。

    ⚠ **`忙查` 是"这会儿能不能改"的判据**，一个无参可调用（主窗传
    `对话页.在忙`）。生成期间删消息会被生成线程收尾时那句
    `覆盖消息(…, 序号, …)` 原地写回来 —— 删了又长出来。以前靠"忙就不给开
    这个框"来防，现在合并了，那道规矩会把「编程助手」那页一起挡在外面，
    所以改成**这一页只读**：出问题的动作（保存/删/清空）灰掉，看照旧能看。
    """

    def __init__(self, 工位, 父=None, 会话编号=0, 忙查=None):
        super().__init__(父)
        self._工位 = 工位
        self._会话编号 = 会话编号 or 0
        self._全部 = []              # 这一段现有的全部消息
        self._尾部 = []              # 按条数/字数截完、真往模型送的那几条
        self._忙查 = 忙查            # 见类 docstring；None = 永不忙
        self._套 = None              # 这一段用的接口配置（取记忆预算要用它的窗口）
        self.改过了 = False

        外 = QVBoxLayout(self)
        # 外壳（标题、尺寸、关闭）都在 `上下文框` 那边，这儿只管内容
        外.setContentsMargins(8, 8, 8, 8)

        # ── 抬头：这是哪一段 ──
        self.抬头 = QLabel('')
        self.抬头.setWordWrap(True)
        外.addWidget(self.抬头)

        # ── 两个旋钮 ──
        钮 = QHBoxLayout()
        钮.addWidget(QLabel('上下文条数'))
        self.条数 = QSpinBox()
        self.条数.setRange(0, 100000)
        self.条数.setSpecialValueText('默认')
        # 0 = 「没配过」，走 `酒馆大脑.默认上下文条数`。和 `酒馆接口页` 里
        # 温度那格的 -1 是同一个约定：**「不设」和「设成 0」得分得开**。
        self.条数.setToolTip('最近多少条进上下文。0 = 用默认（%d 条）'
                          % 酒馆大脑.默认上下文条数)
        钮.addWidget(self.条数)
        钮.addSpacing(16)
        钮.addWidget(QLabel('字数上限'))
        self.字数 = QSpinBox()
        self.字数.setRange(0, 10000000)
        self.字数.setSpecialValueText('默认')
        self.字数.setSingleStep(1000)
        self.字数.setToolTip('再按字符数封一道顶。0 = 用默认（%d 字）\n'
                          '只按条数封是不够的：一条几万字的设定楼就能顶爆上下文'
                          % 酒馆大脑.默认字数上限)
        钮.addWidget(self.字数)
        钮.addSpacing(16)
        self.保存 = 酒馆样式.做按钮('保存设置', self._保存,
                                '写进这段会话的「预设」，只影响这一段')
        钮.addWidget(self.保存)
        钮.addStretch(1)
        外.addLayout(钮)

        # ── 系统提示 ──
        外.addWidget(QLabel('系统提示（会话级的。拼在角色设定后面，不进消息数组；'
                          '本地 GGUF 会再自动补一段扮演规矩）'))
        self.提示框 = QPlainTextEdit()
        self.提示框.setPlaceholderText('留空就不加这一段')
        self.提示框.setMaximumHeight(96)
        外.addWidget(self.提示框)

        # ── 长期记忆 ──
        #
        # ⚠ **挂在"系统提示框"下面是有意的**：它跟用户自己写的那段系统提示
        # 是同一类东西（都拼进 system），放一起最自然；而且右边那栏立刻就能
        # 看到它拼进去的样子 —— 自带对账。
        self.记忆 = 记忆板(
            self._工位,
            说明='长期记忆 —— 挂在这个角色身上，她的所有对话都共享。'
                '勾上的会拼进系统提示。',
            预算查=self._记忆预算,
            块=酒馆记忆.聊天块, 父=self)
        外.addWidget(self.记忆)

        # ── 左：消息清单；右：真发出去的样子 ──
        分 = QSplitter(Qt.Horizontal)
        外.addWidget(分, 1)

        左 = QWidget()
        左排 = QVBoxLayout(左)
        左排.setContentsMargins(0, 0, 0, 0)
        左排.addWidget(QLabel('消息'))
        self.列表 = QListWidget()
        左排.addWidget(self.列表, 1)
        左钮 = QHBoxLayout()
        左钮.addWidget(酒馆样式.做按钮('删掉这条', self._删一条))
        左钮.addWidget(酒馆样式.做按钮('清空这段', self._清空))
        左钮.addStretch(1)
        左排.addLayout(左钮)
        图例 = 酒馆样式.灰色小字(
            '[发] 会发出去   [系] 折进系统提示   [截] 被截在外面')
        图例.setWordWrap(True)
        左排.addWidget(图例)
        分.addWidget(左)

        右 = QWidget()
        右排 = QVBoxLayout(右)
        右排.setContentsMargins(0, 0, 0, 0)
        右排.addWidget(QLabel('发出去的样子（system + 消息数组，已经规整过）'))
        self.预览 = QPlainTextEdit()
        self.预览.setReadOnly(True)
        self.预览.setPlaceholderText('')
        右排.addWidget(self.预览, 1)
        self.右手 = 酒馆样式.灰色小字('')
        self.右手.setWordWrap(True)
        右排.addWidget(self.右手)
        分.addWidget(右)
        分.setSizes([420, 560])

        # ── 底下 ──
        底 = QHBoxLayout()
        self.报错 = 酒馆样式.灰色小字('')
        self.报错.setWordWrap(True)
        底.addWidget(self.报错, 1)
        self.刷新钮 = 酒馆样式.做按钮('刷新', self._读)
        底.addWidget(self.刷新钮)
        # 「关闭」不在这儿 —— 归 `上下文框` 管，见类 docstring
        外.addLayout(底)

        self._读()

    # ── 读 ──

    def _读(self):
        """
        重读这一段并重画。**整趟在工人线程里跑。**

        读三次库（会话 / 角色 / 消息）外加拼一次提示，都是本机 JSON 文件，
        快是快，可**库是工人线程独占的**——主线程直接碰它就是两条线程同时
        读写同一份文件。这条没有例外（见 `酒馆工人` 那份说明）。
        """
        if not self._会话编号:
            self._空着()
            return
        号 = self._会话编号

        def 活(库):
            段 = 酒馆对话.取会话(库, 号)
            if 段 is None:
                raise 酒馆错误('这段会话不在了（编号 %s）。' % 号)
            角色 = 酒馆角色.取(库, 段.角色编号)
            设定 = 段.设定()
            if not isinstance(设定, dict):
                设定 = {}
            条数, _ = 酒馆大脑.上下文设置(设定)
            全部 = 酒馆对话.取消息(库, 号)
            套 = 酒馆接口.取或默认(库, 段.接口编号, '对话')
            # ⚠ **记忆也要在这儿塞进 `设定`**，跟 `酒馆对话页._拼` 一个做法、
            # 一个渲染函数。不塞的话右边显示的是"没记忆"的那一份，而实际
            # 发出去的有 —— 这个框存在的全部意义就是让这两份对得上
            # （见文件头那条"界面说的和发的不是一份"）。
            设定['__记忆__'] = 酒馆记忆.聊天块(
                酒馆记忆.取(库, 酒馆记忆.角色, 段.角色编号),
                上限=酒馆记忆.预算(酒馆大脑.本机窗口(套)))[0]
            # ⚠ **真的调一遍 `窗口内`**（它内部就是 `拼提示`），不是照着它的
            # 逻辑在这儿再实现一遍。右边要显示的就是"发出去的样子"，那就得是
            # 同一个函数吐出来的 —— `酒馆对话页._拼` 走的也是这一个。
            # ⚠ **必须跟聊天页一模一样**：两处各算一遍的话，界面说会发 20 条、
            # 实际发了 12 条，而这种不一致**跑起来一点症状都没有**，只能靠读
            # 代码发现。
            尾部, 系统, 数组 = 酒馆大脑.窗口内(
                角色, 段, 酒馆对话.取最近(库, 号, 条数), 设定, 套)
            return 段, 角色, 套, 设定, 全部, 尾部, 系统, 数组

        self._工位.干(活, self._画)

    def _记忆预算(self):
        """这一页记忆的 token 上限 —— 给记忆板算"这次带得下几条"。

        ⚠ **跟聊天那条链路上的是同一个算法**（`酒馆记忆.预算`）：两处各算
        一份的话，记忆板说"带得下 12 条"、实际发出去只有 9 条，而这种不一致
        只能靠读代码发现。
        """
        return 酒馆记忆.预算(酒馆大脑.本机窗口(self._套))

    def _空着(self):
        self.抬头.setText('没有打开的会话。先在中间那栏挑一段对话，再点「上下文」。')
        self._可改(False)
        self.列表.clear()
        self.预览.setPlainText('')
        self.右手.setText('')
        self.记忆.设挂载点('', '')

    def _画(self, 包, 错):
        if 错:
            QMessageBox.warning(self, '读上下文失败', 错)
            return
        段, 角色, 套, 设定, 全部, 尾部, 系统, 数组 = 包
        self._全部 = 全部
        self._尾部 = 尾部
        self._套 = 套

        self.抬头.setText(
            '会话：%s      角色：%s      接口：%s'
            % (段.标题 or '（没标题）', (角色.名字 if 角色 else '（不在了）'),
               (套.名称 or '（没名字）') if 套 else '（一套都没配）'))

        # 记忆板绑到**这个角色的**那份记忆上。角色没了就清空（`键=0`）。
        self.记忆.设挂载点(
            酒馆记忆.角色 if 角色 else '',
            角色.编号 if 角色 else '',
            说明='长期记忆 —— 挂在角色「%s」身上，她的所有对话都共享。'
                '勾上的会拼进系统提示。'
                % ((角色.名字 if 角色 else '（角色不在了）'),))

        # ⚠ **本地模型多一层约束，必须说出来。** 下面那个「字数上限」只是
        # 用户设的上限；**真正发出去几条由 `n_ctx` 说了算**（本地 4096 的
        # 窗口，光系统提示 + 一次生成就占掉一半）。不说的话，用户看着
        # 「字数上限 24000」而实际只发了 1 条，只会觉得程序坏了。
        窗口 = 酒馆大脑.本机窗口(套)
        if 窗口:
            self.抬头.setText(
                self.抬头.text() +
                '\n⚠ 这套是本地模型，窗口只有 %d token —— 「字数上限」只是'
                '你设的上限，**实际发几条由窗口决定**（下面灰掉的就是没发的）。'
                '想把历史留长一点，去「接口配置」把这套的 n_ctx 调大。'
                % 窗口)

        # 旋钮照**这一段真实存的**填，不是照默认值填
        self.条数.setValue(int(设定.get('上下文条数') or 0))
        self.字数.setValue(int(设定.get('字数上限') or 0))
        self.提示框.setPlainText((设定.get('系统提示') or ''))

        self._列消息()
        self._画预览(系统, 数组, 套)
        # ⚠ **`_可改` 必须放在最后一行。** `_画预览` 结尾会把 `报错` 那行清空
        # （它是在清上一次的错），只读说明也写在同一格，放前面会被它抹掉。
        self._可改(True)

    def _列消息(self):
        在里头 = set(m.序号 for m in self._尾部)
        灰 = QColor(酒馆样式.配色()['text_secondary'])
        self.列表.clear()
        for m in self._全部:
            if m.序号 not in 在里头:
                # 被截在外面的灰掉。**灰的是"不参与这次生成"这件事本身**，
                # 不是在说这条消息有问题——所以不删、只标。
                标, 暗 = '[截]', True
            elif m.说话人 == '系统':
                标, 暗 = '[系]', False
            else:
                标, 暗 = '[发]', False
            项 = QListWidgetItem('%s %d  %s  %s'
                               % (标, m.序号, m.说话人, _摘要(m.内容)))
            项.setData(Qt.UserRole, m.序号)
            项.setToolTip((m.内容 or '')[:2000])
            if 暗:
                项.setForeground(灰)
            self.列表.addItem(项)

    def _画预览(self, 系统, 数组, 套):
        """
        把 `拼提示` 吐出来的东西照原样摊开。

        `system` 是**一个字符串**（身份 + 人设 + 示例对话 + 系统消息 + 会话
        系统提示 + 开场白说明，本地协议再补一段扮演规矩，拼起来的），不是
        一个 role 条目——几家协议的系统提示走法各不相同，折成一个字符串是
        `拼提示` 那边定的（见它的 docstring），这儿照显示。
        """
        块 = []
        if 系统:
            块.append('──── system ────\n' + 系统)
        for 条 in 数组:
            块.append('──── %s ────\n%s' % (条['role'], 条['content']))
        文 = '\n\n'.join(块) if 块 else '（这次什么都没得发）'
        # 末尾再贴一段"上一次真正发出去的那串"，见 `_补真实发出` 的说明。
        文 += self._补真实发出(套)
        self.预览.setPlainText(文)
        # 预览默认停在末尾，一进来该看到的是最上面那段 system
        self.预览.moveCursor(QTextCursor.Start)

        字 = len(系统 or '') + sum(len(条['content']) for 条 in 数组)
        截了 = len(self._全部) - len(self._尾部)
        说 = '系统提示 %d 字 ＋ 消息 %d 条（共 %d 字）' % (len(系统 or ''),
                                                       len(数组), 字)
        if 截了:
            说 += '；另外 %d 条被截在外面' % 截了
        缺 = 酒馆大脑.校验配置(套)
        if 缺:
            # 「这次发得出去吗」和「发什么」是同一件事的两半，顺手说了
            说 += '。⚠ 这套接口还发不出去，缺：%s' % '、'.join(缺)
        # ⚠ **系统提示自己就把窗口占满**时要明说。`窗口内` 对这种情况**没有
        # 保护**（它的下限只是"至少留一条消息"）—— 提示词真超了 `n_ctx`，
        # llama.cpp 会从最前面截断，先丢的正是系统提示（人设、规矩、记忆全没），
        # 而界面上一点异常都没有。用户能看到的只有"模型怎么好像不记得人设了"。
        窗口 = 酒馆大脑.本机窗口(套)
        系统大 = 酒馆工具.估token(系统)
        if 窗口 and 系统大 > 窗口 * 0.8:
            说 += ('。⚠ **光系统提示（人设 + 规矩 + 记忆）就占了窗口的 %d%%**'
                  ' —— 历史几乎没地方了。去上面停用几条记忆，'
                  '或者去「接口配置」把 n_ctx 调大。'
                  % int(系统大 * 100.0 / 窗口))
        self.右手.setText(说)
        self.报错.setText('')

    def _补真实发出(self, 套):
        """
        预览末尾再贴一段：**上一次真正发出去的那串**（模型实际吃掉的东西）。

        ⚠ **上面那些块是重算出来的 messages，不是模型真正读到的东西。**
        中间还隔着一层「按模型自己的聊天模板渲染」—— 同一个 messages，不同
        模板渲染出来的串可以差到十万八千里，而**我们的角色设定有没有被装进去**
        正是在那一步决定的。渲染只有拿到模型实例才做得了，为了看预览去加载
        一个 5G 模型不划算，所以由真正发的那一方留一份快照
        （`酒馆本地.最近发出`），这儿只负责显示。

        ⚠ **模型对不上就不贴。** 快照是"上一次"的，用户可能刚换了接口/模型 ——
        贴出来会让人以为这就是当前这套要发的东西，比不贴更坏。
        """
        try:
            快 = 酒馆本地.最近发出() or {}
        except Exception:
            快 = {}
        路 = 快.get('路') or ''
        模 = 快.get('模型') or ''
        现在 = os.path.basename(酒馆本地.定位(getattr(套, '模型', '') or ''))
        if not 路:
            return ('\n\n──── 实际发出去的样子 ────\n'
                    '（还没生成过。模型真正读到的那串要到生成时才渲染得出来，'
                    '所以先没有。）')
        if 模 and 现在 and 模 != 现在:
            return ('\n\n──── 实际发出去的样子 ────\n'
                    '（上一次是用 `%s` 发的，跟现在这套接口的模型不是同一个，'
                    '不贴了 —— 贴出来会误导。）' % 模)
        头 = '──── 实际发出去的样子 ────\n走了哪条路：%s\n%s\n%s\n%s\n\n' % (
            路, 快.get('说明') or '', self._画像说明(快), self._控制说明(快))

    def _画像说明(self, 快):
        """
        这个模型**是什么** —— 协议层问清楚的那几样，摊开给人看。

        ⚠ 它是**全链路唯一一处**说"这个模型认不认 system / 认不认 tools /
        模板丢没丢"的地方。以前这些信息散在几处各自判，现在归到 `酒馆协议.画像`。

        ⚠ **认 system = 不是**是最该被看见的一行：它意味着模型自带的模板会
        把角色设定丢掉，我们只好改位置（路标会写"并入首条"）。
        """
        画 = 快.get('画像') or {}
        if not 画:
            return '模型画像：这一版之前发出去的，没记录。'
        行 = ['族 %s' % (画.get('族') or '认不出'),
             'arch %s' % (画.get('arch') or '?'),
             '认 system：%s' % ('**是**' if 画.get('认system') else '**不是**'),
             '认 tools：%s' % ('是' if 画.get('认tools') else '不是')]
        if 画.get('回退'):
            行.append('⚠ **这个 GGUF 没有聊天模板**')
        else:
            行.append('模板 %d 字' % int(画.get('模板字') or 0))
        return '模型画像：' + ' · '.join(行)

    def _控制说明(self, 快):
        """
        协议层这次**动了哪几样**，翻成人话。没动就明写"没动"。

        ⚠ **这一段的价值在于"没动"也要说。** 用户开了 `人格锁` 却看不到效果，
        跟"关着没生效"是两件事 —— 不说清楚，他会一直在那儿找为什么。
        """
        控 = 快.get('控制') or {}
        if not 控:
            return '协议层控制：这一版之前发出去的，没记录。'
        # ⚠ 分两栏：**动的**（用户能感知的行为改变）和**自动的**（协议层一直
        # 开着的保护）。混在一起说，"一样都没动"那档就永远轮不到 ——
        # 因为补停串每个族都有，看着像"动了"。
        动 = []
        if 控.get('要点'):
            动.append('人格要点**复述到了最后一条用户消息末尾**')
        if 控.get('预填'):
            动.append('**预填了开头**（%r）—— 它不在生成结果里，'
                     '是补吐给界面的，所以界面上看着完整' % 控['预填'])
        if 控.get('分流'):
            动.append('**思维链分流开着** —— 闭合标记之前的内容不会给用户看')
        if 快.get('只思维'):
            动.append('⚠ **这次它只出了思维链、一个字正文都没有** —— '
                     '把「分流」设成「关」能看到草稿，或者换个不带思考的模型')
        自动 = []
        if 控.get('禁生效'):
            禁 = 控.get('禁吐') or []
            自动.append('**中间段开着**：解码期掐掉 %d 样东西（%s%s）—— '
                      '它们**压根吐不出来**，不是吐完再剪'
                      % (len(禁), '、'.join(禁[:3]), '…' if len(禁) > 3 else ''))
        elif 控.get('禁吐'):
            自动.append('⚠ 中间段**没生效**（%d 样要掐的东西编不成 token）'
                      % len(控['禁吐']))
        if 控.get('补停'):
            自动.append('补了停止串 %s（按族补的回合标记，防它自己演用户）'
                      % '、'.join(控['补停']))
        if not 控.get('分流'):
            自动.append('思维链分流没开')
        尾 = ('\n  · ' + '\n  · '.join(自动)) if 自动 else ''
        if not 动:
            return ('协议层控制：**一样都没动**（人格锁关着、没预填、没开分流）'
                    '—— 发出去的就是拼提示的原文。' + 尾)
        return '协议层控制：\n  · ' + '\n  · '.join(动) + 尾
        串 = 快.get('串') or ''
        if not 串:
            return '\n\n' + 头 + '（这次没渲染出提示词，见上面那句说明。）'
        return '\n\n' + 头 + 串

    def _忙吗(self):
        """
        这会儿能不能改（`忙查` 说不能改就不改）。**`忙查` 抛错也当"能改"。**

        ⚠ **别让一个探测用的回调把整页带崩。** 它跨在"主窗 → 这一页"之间，
        将来改了那边的形状这边就可能炸；而它只是个只读判断，失败时按
        "能改"处理最多是回到合并之前的老行为，不至于整页打不开。
        """
        if self._忙查 is None:
            return False
        try:
            return bool(self._忙查())
        except Exception:
            return False

    def _可改(self, 行不行):
        """
        这一页能不能改。**判据有两层：这次读回来有没有东西（`行不行`），
        以及聊天这会儿是不是正在生成（`忙查`）。**

        ⚠ **`列表`（左边那列消息）也在里面，不能漏。** 它看着像个清单，
        其实是删消息的入口（选中一条再点「删掉这条」）—— 只灰按钮不灰它，
        等于留了一条侧门。

        ⚠ **灰着的时候必须在 `报错` 那行写明为什么**，否则用户只看到一个
        点不动、也不解释的界面，跟"程序坏了"分不出来。判据跟上面同一个
        来源（`_忙吗`），两处对不上就会出"灰着不说"或者"写着不能改其实能改"。
        """
        能改 = bool(行不行) and not self._忙吗()
        for 件 in (self.条数, self.字数, self.提示框, self.保存, self.列表):
            件.setEnabled(能改)
        if 能改:
            self.报错.setText('')
        elif self._忙吗():
            # ⚠ **话要说准：灰的到底是什么。** 记忆板**不在**灰的范围内 ——
            # 改记忆对正在进行的那条生成没有影响（系统提示早在 `_摆气泡`
            # 那一步就拼好交出去了），下一轮才生效。把它跟"消息不能改"
            # 混在一句里说，用户会以为记忆也改不了。
            self.报错.setText(
                '聊天正在生成 —— 消息和预设现在只能看（改、删都灰着）。'
                '**记忆可以改**，下一轮才生效。（等这条说完就能改消息了。）')

    # ── 写 ──

    def _保存(self):
        """
        把三个旋钮写进这段会话的 `预设`。

        ⚠ **只覆盖这三个键，别的原样留着。** `预设` 里还住着
        `temperature` / `top_p` / `max_tokens`（见 `酒馆大脑.可覆盖参数`），
        整份换掉就是把用户设的采样参数静默清了。所以是"读出来 → 改这三个
        → 写回去"，而且**三步在一件活里连着做完**——分两次投活的话，中间
        插进来的东西会被整份盖掉。

        ⚠ **这份 `设定` 是当场从库里重读的，不是外面那个带 `__记忆__` 的。**
        这一点必须一直保持：`__记忆__` 是那一轮的临时数据（见
        `酒馆大脑.拼提示` 里那段注释），要是从外面把带它的 `设定` 整份写回来，
        记忆就被永久烙进会话预设了 —— 而它本来该跟着角色记忆走。
        """
        号 = self._会话编号

        def 活(库, n=号, 条=int(self.条数.value()),
              限=int(self.字数.value()),
              提=self.提示框.toPlainText()):
            段 = 酒馆对话.取会话(库, n)
            if 段 is None:
                raise 酒馆错误('这段会话不在了（编号 %s）。' % n)
            设定 = 段.设定()
            if not isinstance(设定, dict):
                设定 = {}
            # 0 = 「不设」→ **把键删掉**，不是存一个 0。留着 0 的话
            # `上下文设置` 那边 `or` 一下也能退回默认，但盘上从此多一个
            # "配过、配成 0"的痕迹，以后想分辨就分不清了。
            if 条 > 0:
                设定['上下文条数'] = 条
            else:
                设定.pop('上下文条数', None)
            if 限 > 0:
                设定['字数上限'] = 限
            else:
                设定.pop('字数上限', None)
            提 = (提 or '').strip()
            if 提:
                设定['系统提示'] = 提
            else:
                设定.pop('系统提示', None)
            酒馆对话.改会话(库, n, 预设=设定)

        def 好了(_值, 错):
            if 错:
                self.报错.setText('保存失败：%s' % 错)
                return
            self.改过了 = True
            self._读()                # 读回来重画：预设真存成什么样，以库为准

        self._工位.干(活, 好了)

    # ── 消息上的操作 ──

    def _选的序号(self):
        项 = self.列表.currentItem()
        if 项 is None:
            QMessageBox.information(self, '先挑一条', '上面列表里先点一条。')
            return 0
        return int(项.data(Qt.UserRole))

    def _删一条(self):
        序号 = self._选的序号()
        if not 序号:
            return
        项 = self.列表.currentItem()
        if QMessageBox.question(self, '删掉这条？',
                                '序号 %d：%s\n\n删了就没了。这条消息本身会被'
                                '删掉，不只是这次不发。'
                                % (序号, _摘要(项.toolTip())),
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        号 = self._会话编号

        def 好了(_值, 错):
            if 错:
                self.报错.setText('删失败：%s' % 错)
                return
            self.改过了 = True
            self._读()

        self._工位.干(lambda 库, n=号, i=序号: 酒馆对话.删消息(库, n, i), 好了)

    def _清空(self):
        数 = len(self._全部)
        if not 数:
            return
        if QMessageBox.question(self, '清空这段对话？',
                                '这 %d 条消息全删掉，会话本身留着。\n\n'
                                '**删了就没了。**' % 数,
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        号 = self._会话编号

        def 好了(_值, 错):
            if 错:
                self.报错.setText('清空失败：%s' % 错)
                return
            self.改过了 = True
            self._读()

        self._工位.干(lambda 库, n=号: 酒馆对话.清空消息(库, n), 好了)


# ── 编程助手的上下文 ────────────────────────────────────────────────

def _时间(毫秒数):
    """epoch 毫秒 → 「09-19 18:54」。**清单里用，年份不占地方。**"""
    try:
        return time.strftime('%m-%d %H:%M', time.localtime(int(毫秒数) / 1000.0))
    except (OSError, ValueError, TypeError):
        return '（没记时间）'


def _参摘要(参):
    """工具参数压成一小段。长的（整个文件）只留开头 —— 全文在转录里。"""
    if not isinstance(参, dict):
        return ''
    return '，'.join('%s=%s' % (k, ' '.join(str(v).split())[:24])
                    for k, v in list(参.items())[:2])


def _步摘要(步):
    """一步 → 一行摘要：调了什么、拿到什么。"""
    条 = []
    for r in (步.get('结果们') or []):
        头 = (r.get('文') or '').split('\n')[0][:40]
        条.append('%s(%s) → %s'
                  % (r.get('名') or '?', _参摘要(r.get('参')), 头 or '（空）'))
    if not 条:
        # 没调工具的一步 = 模型说了段话（多半就是最后那个结论）
        return '说了一段话：%s' % _摘要(步.get('助手') or '')
    return ' ／ '.join(条)


def _参数块(参):
    """工具参数摊开。**长的（整个文件）不截** —— 那正是要看的上下文。"""
    行 = []
    for k, v in (参 or {}).items():
        s = v if isinstance(v, str) else repr(v)
        if '\n' in s:
            行.append('  %s =' % k)
            行.append(s)
            行.append('  （%s 结束）' % k)
        else:
            行.append('  %s = %s' % (k, s))
    return '\n'.join(行)


def _步原文(序号, 步, 共步):
    """
    一步 → 一屏原文。**这是"当时喂进去的东西"里，属于这一步的那一半。**

    另一半（system + 完整消息数组）在「发出去的样子」那张页上，而且是**最后一
    次**发出时的样子 —— 两者对不上的地方（折叠、后续步骤）靠左栏的 [折]/[发]
    标记看出来。

    ⚠ **一个字都不截。** 这个框存在的理由就是"当时到底喂给模型的是什么"，
    而工具结果里常常就是整个文件；截了等于把要看的东西删掉。（`read_file`
    自己带的「（已截断）」是**模型读到的原文**，照搬，别在这儿再截一道。）
    """
    段 = ['第 %d 步（共 %d 步）' % (序号, 共步), '']
    原 = 步.get('原始') or ''
    文 = 步.get('助手') or ''

    段.append('──── 模型这一轮的输出 ────')
    if 原 and 原 != 文:
        # 工具调用是当文本吐出来的：那几段被 `解工具调用` 从正文里挖走了，
        # 所以**正文可能是空的，而原文有整整一个文件那么长**。两个都摆出来，
        # 并且说清哪个是哪个 —— 只摆一个都会让人以为丢了东西。
        段.append('（原文，含它吐出来的 <tool_call>）')
        段.append(原)
        if 文.strip():
            段.append('')
            段.append('（上面抠出来的正文，也就是聊天气泡里显示的那份）')
            段.append(文)
    elif 原 or 文:
        段.append(原 or 文)
    else:
        段.append('（这一轮它一个字都没写 —— 正文和工具调用都是空的）')

    结果们 = 步.get('结果们') or []
    if not 结果们:
        段.append('')
        段.append('──── 这一步没有调工具 ────')
        return '\n'.join(段)

    for j, r in enumerate(结果们, 1):
        名 = r.get('名') or '?'
        段.append('')
        段.append('──── 工具调用 %d/%d：%s ────' % (j, len(结果们), 名))
        if 名 == '（系统）':
            段.append('（这是我们自己塞回去的一句话，不是它调的工具）')
        参 = r.get('参')
        if isinstance(参, dict) and 参:
            段.append('参数：')
            段.append(_参数块(参))
        elif 参:
            段.append('参数：%r' % (参,))
        段.append('')
        段.append('结果（喂回给模型的原文）：')
        段.append(r.get('文') or '（空）')
    return '\n'.join(段)


class 助手上下文页(QWidget):
    """
    编程助手跑过的任务：**每一步干了什么，以及最后一次真正发出去的提示词。**

    ⚠ **整趟读库在工人线程里跑**（跟 `上下文页._读` 一个道理）：库是工人
    线程独占的，主线程直接碰它就是两条线程同时读写同一份文件。

    `任务编号` 给 0 就是"落在最近跑的那次上"（清单按创建时间降序）。

    ⚠ **它是个"页"，不是"窗口"**（跟 `上下文页` 一样）：外壳和那个「关闭」
    都归 `上下文框` 管。这一页没有线程，也没有"忙不忙"这回事 —— 读的是
    已经存过盘的转录，跟聊天那边正在生成的那条毫无关系。
    """

    def __init__(self, 工位, 父=None, 任务编号=0, 目录='', 窗口=0):
        super().__init__(父)
        self._工位 = 工位
        self._要选 = int(任务编号 or 0)   # 只在**下一次读**里用一次，用完清掉
        self._现在 = 0                    # 现在显示的是哪一次任务
        self._行 = None                   # 它的清单行
        self._转录 = None                 # 它的转录（可能没了）
        self._目录 = str(目录 or '')      # 项目记忆按这个目录挂，见 `设目录`
        self._窗口 = int(窗口 or 0)       # 助手那套接口的 n_ctx，给记忆算预算

        外 = QVBoxLayout(self)
        外.setContentsMargins(8, 8, 8, 8)

        # ── 挑哪一次任务 ──
        顶 = QHBoxLayout()
        顶.addWidget(QLabel('任务'))
        self.任务 = QComboBox()
        self.任务.setMinimumWidth(460)
        self.任务.setToolTip('跑过的任务都在这儿，最近跑的排最前面。'
                            '切换只是换个读法，不会重新跑')
        self.任务.currentIndexChanged.connect(self._换)
        顶.addWidget(self.任务, 1)
        外.addLayout(顶)

        self.抬头 = QLabel('')
        self.抬头.setWordWrap(True)
        外.addWidget(self.抬头)

        # ── 项目记忆 ──
        #
        # ⚠ **跟右边的"发出去的样子"是配套的**：勾上的会出现在 system 里，
        # 立刻能在右边看到 —— 自带对账。
        self.记忆 = 记忆板(
            self._工位, 预算查=self._记忆预算, 块=酒馆记忆.助手块, 父=self)
        外.addWidget(self.记忆)

        # ── 左：每一步；右：两样（发出去的样子 / 选中那一步的原文）──
        分 = QSplitter(Qt.Horizontal)
        外.addWidget(分, 1)

        左 = QWidget()
        左排 = QVBoxLayout(左)
        左排.setContentsMargins(0, 0, 0, 0)
        左排.addWidget(QLabel('这次任务的每一步'))
        self.列表 = QListWidget()
        # ⚠ **列表得能点。** 早先它只是个清单，点了一点反应没有 —— 而右边
        # 只画"最后一次发出去的样子"，某一步的原文就再也没有地方能看到了。
        self.列表.currentItemChanged.connect(self._看这步)
        左排.addWidget(self.列表, 1)
        图例 = 酒馆样式.灰色小字(
            '[发] 最后一次发出去的时候有它   [折] 当时已经折成一行存根   '
            '[未] 记下来之后没再发出去\n'
            '点某一步 → 右边切到「这一步的原文」（模型吐了什么、工具的完整参数和结果）')
        图例.setWordWrap(True)
        左排.addWidget(图例)
        分.addWidget(左)

        右 = QWidget()
        右排 = QVBoxLayout(右)
        右排.setContentsMargins(0, 0, 0, 0)
        self.右堆 = QTabWidget()

        页1 = QWidget()
        排1 = QVBoxLayout(页1)
        排1.setContentsMargins(0, 0, 0, 0)
        self.预览 = QPlainTextEdit()
        self.预览.setReadOnly(True)
        排1.addWidget(self.预览, 1)
        self.右手 = 酒馆样式.灰色小字('')
        self.右手.setWordWrap(True)
        排1.addWidget(self.右手)
        self.右堆.addTab(页1, '发出去的样子')

        页2 = QWidget()
        排2 = QVBoxLayout(页2)
        排2.setContentsMargins(0, 0, 0, 0)
        self.步文 = QPlainTextEdit()
        self.步文.setReadOnly(True)
        排2.addWidget(self.步文, 1)
        self.右手2 = 酒馆样式.灰色小字('')
        self.右手2.setWordWrap(True)
        排2.addWidget(self.右手2)
        self.右堆.addTab(页2, '这一步的原文')

        右排.addWidget(self.右堆, 1)
        分.addWidget(右)
        分.setSizes([440, 580])

        # ── 底下 ──
        底 = QHBoxLayout()
        self.报错 = 酒馆样式.灰色小字('')
        self.报错.setWordWrap(True)
        底.addWidget(self.报错, 1)
        底.addWidget(酒馆样式.做按钮('刷新', self._读, '重读一遍（跑完新任务要看就点它）'))
        self.删 = 酒馆样式.做按钮('删掉这次任务', self._删,
                               '清单行和转录一起删，删了找不回来')
        self.删.setEnabled(False)
        底.addWidget(self.删)
        # 「关闭」不在这儿 —— 归 `上下文框` 管，见类 docstring
        外.addLayout(底)

        # 记忆板绑"当前工作目录"那一份。**放在 `_读()` 之前** —— 它跟"有没有
        # 任务记录"没关系（一个目录可能还没跑过任务，但约定可以先写下来）。
        self._绑记忆()
        self._读()

    # ── 读 ──

    def _读(self, 编号=None):
        """
        重读任务清单 + 选中的那一次。**整趟一件活。**

        ⚠ **清单行和转录要在同一件活里取回来**（`取一件`）：分两次投的话，
        中间那次刷新可能正好撞上「删掉这次任务」，于是"行还在、转录没了"，
        画出来是一个平白无故空掉的步骤列表。
        """
        要 = self._要选 if 编号 is None else int(编号 or 0)
        self._要选 = 0

        def 活(库):
            行们 = 酒馆助手存.列任务(库)
            选 = 要 or (int(行们[0].get('编号') or 0) if 行们 else 0)
            return 行们, 选, (酒馆助手存.取一件(库, 选) if 选 else (None, None))

        self._工位.干(活, self._画)

    def _换(self, _=None):
        """下拉换了。空数据（占位那条）不动。"""
        编号 = self.任务.currentData()
        if 编号:
            self._读(int(编号))

    def _画(self, 包, 错):
        if 错:
            QMessageBox.warning(self, '读助手记录失败', 错)
            return
        行们, 选, (行, 转录) = 包
        self._填任务(行们, 选)
        self._现在 = 选
        self._行 = 行
        self._转录 = 转录
        self.报错.setText('')
        # 换了一次任务，上一次挑中的那一步就不作数了：把「这一步的原文」
        # 清掉、并把右边切回「发出去的样子」—— 留着上一条任务的第 3 步
        # 摆在那儿，用户会以为它是这一条任务的。
        self.步文.setPlainText('')
        self.右手2.setText('')
        self.右堆.setCurrentIndex(0)

        if not 选:
            self._空着()
            return
        self._画抬头(行, 转录)
        self.删.setEnabled(True)
        self._画步骤(转录)
        self._画预览(转录)

    def 设目录(self, 目录):
        """
        换成另一个工作目录的项目记忆（`上下文框` 从主窗那边接过来的）。

        ⚠ **它跟"选哪一次任务"是两回事**：任务记录是全局的（哪个目录跑的都在
        一张清单里），而项目记忆**按目录分**。切换任务时目录不变。
        """
        self._目录 = str(目录 or '')
        self._绑记忆()

    def _绑记忆(self):
        """把记忆板绑到"当前工作目录"那份项目记忆上。"""
        d = self._目录
        self.记忆.设挂载点(
            酒馆记忆.目录 if d else '', d,
            说明=('项目记忆 —— 挂在工作目录「%s」上，在这个目录里跑的任务'
                 '都会带上它。勾上的会拼进助手的系统提示。' % d) if d
            else '项目记忆 —— 还没选工作目录，所以没有可看的那一份。')

    def _记忆预算(self):
        """
        这份项目记忆的 token 上限。

        ⚠ **窗口是从外面传进来的**（`助手页` 那边按这套接口的 `n_ctx` 算出来
        的那个），这一页自己读不到 —— 它手上只有任务记录，没有接口配置。
        传进来的理由跟别处一样：**两个地方各算一份，界面说的和真发出去的
        就会不是一份**。
        """
        return 酒馆记忆.预算(self._窗口, 助手记忆上限)

    def _填任务(self, 行们, 选):
        """把清单灌进下拉。**填的过程要静音** —— 不然自己触发自己的 `_换`。"""
        self.任务.blockSignals(True)
        self.任务.clear()
        for r in (行们 or []):
            self.任务.addItem(
                '%s · %s · %s'
                % (_时间(r.get('创建于')), r.get('任务') or '（没记任务）',
                   r.get('结局') or '（没记结局）'),
                int(r.get('编号') or 0))
        位 = self.任务.findData(选)
        self.任务.setCurrentIndex(位 if 位 >= 0 else -1)
        self.任务.blockSignals(False)
        self.任务.setEnabled(bool(行们))

    def _空着(self):
        self.抬头.setText('还没有跑过的任务记录。跑一次任务，这儿就会有它 ——'
                        '记录是存盘的，关了软件再开也还在。')
        self.列表.clear()
        self.预览.setPlainText('')
        self.右手.setText('')
        self.删.setEnabled(False)

    def _画抬头(self, 行, 转录=None):
        """
        抬头那一块：这次任务是什么、在哪跑的、什么结局、用的哪个模型。

        ⚠ 模型名在**转录**里、不在清单行里（清单行是整张读进内存的小表，
        见 `酒馆助手存`）。所以两样都得传进来。
        """
        行 = 行 or {}
        说明 = 行.get('结局说明') or ''
        转录 = 转录 or {}
        谁 = ' · '.join(x for x in (
            (转录.get('接口') or ''), (转录.get('模型') or '')) if x)
        self.抬头.setText(
            '任务：%s\n工作目录：%s\n结局：%s%s    共 %s 步%s'
            % (行.get('任务') or '（没记）',
              行.get('工作目录') or '（没记）',
              行.get('结局') or '（没记）',
               ('（%s）' % 说明) if 说明 else '',
              行.get('步数') or 0,
              ('    用的是：%s' % 谁) if 谁 else ''))

    # ── 左：点某一步 → 右边看它的原文 ──

    def _看这步(self, 现在, _前=None):
        """
        点左边某一步，右边换到「这一步的原文」。

        ⚠ **不能只填内容、不切页。** 右边默认停在「发出去的样子」，不切过去
        的话用户只看到"点了没反应" —— 这正是这个框第一版的毛病。

        ⚠ **`现在` 可以是 `None`**（`列表.clear()` 就会发一个 None 过来）。
        那时候什么都不做 —— 否则换任务的时候会用一条空值去查 `步们`。
        """
        序号 = int(现在.data(Qt.UserRole) or 0) if 现在 is not None else 0
        if not 序号:
            return
        步们 = (self._转录 or {}).get('步们') or []
        i = 序号 - 1
        if not (0 <= i < len(步们)):
            # 清单和转录对不上（转录文件是手改过的之类）。**不装作没事** ——
            # 说清楚是这一步没记下来。
            self.步文.setPlainText(
                '第 %d 步查不到 —— 这次的转录里只有 %d 步。\n'
                '（转录文件可能被手改过，或者只存下了一半。）' % (序号, len(步们)))
            self.右手2.setText('')
        else:
            self.步文.setPlainText(_步原文(序号, 步们[i], len(步们)))
            self.右手2.setText(
                '第 %d 步（共 %d 步）· 这里是他当时喂给模型的这一半；'
                '「发出去的样子」那张页是**最后一次**发出时的完整提示词'
                % (序号, len(步们)))
        self.步文.moveCursor(QTextCursor.Start)
        self.右堆.setCurrentIndex(1)

    # ── 左：每一步 ──

    def _画步骤(self, 转录):
        """
        每一步的归宿。`[折]/[发]/[未]` 的判据见文件头那段 —— **用快照里冻
        的 `折到/共步`，不是转录本末尾的 `折到`。**

        `共步` 是"最后一次发出去的时候，已经记完了几步"，所以第 `共步 + 1`
        步开始就是"发出去之后才记下的"。判据取的是**最后那一步的视角**：
        用户要看的本来也是"这一次到底发了什么"。
        """
        步们 = (转录 or {}).get('步们') or []
        最后 = (转录 or {}).get('最后') or {}
        有最后 = bool(最后)
        折到 = int(最后.get('折到') or 0)
        共步 = int(最后.get('共步') or 0)

        灰 = QColor(酒馆样式.配色()['text_secondary'])
        self.列表.clear()
        for i, 步 in enumerate(步们, 1):
            if not 有最后:
                标, 暗 = '[未]', True
            elif i <= 折到:
                标, 暗 = '[折]', True
            elif i <= 共步:
                标, 暗 = '[发]', False
            else:
                标, 暗 = '[未]', True
            项 = QListWidgetItem('%s %d  %s' % (标, i, _步摘要(步)))
            项.setData(Qt.UserRole, i)          # 点它的时候要知道点的是第几步
            项.setToolTip((步.get('助手') or '')[:2000])
            if 暗:
                项.setForeground(灰)
            self.列表.addItem(项)

    # ── 右：真发出去的样子 ──

    def _画预览(self, 转录):
        """
        摊开**这一次真正发出去的东西**。**不重拼、不重算。**

        ⚠ **优先摊 `串`** —— 那是引擎渲染完之后、真正喂给模型的那一串
        （含模板自己加的 BOS / 角色标记，以及系统提示最后落到了哪个位置）。
        `系统` / `消息` 只是**我们准备好的原料**，不是发出去的东西：模板可能
        把 system 挪个位置、并进第一条 user、或者整段丢掉。只摊原料的话，
        用户看到的和模型吃到的**不是一件事** —— 而"它为什么没照规矩来"
        这种问题的答案，恰恰只在那串上。

        ⚠ `串` 要等引擎渲染完才有（`跑一轮` 调完模型补进快照的），所以
        **第一轮就失败、或者很早以前存下的记录会没有** —— 那就退回摊原料，
        并在说明里写明这是原料。
        """
        最后 = (转录 or {}).get('最后') or {}
        if not 最后:
            self.预览.setPlainText(
                '这次任务**没有把提示词发出去**。\n\n'
                '第一步就撞上了别的东西（比如上下文装不下、或者一开始模型\n'
                '调用就出错了），所以没有"发出去的样子"可看。左边那栏是它\n'
                '实际走到哪儿。')
            self.右手.setText('')
            return

        串 = 最后.get('串') or ''
        系统 = 最后.get('系统') or ''
        消息 = [条 for 条 in (最后.get('消息') or []) if isinstance(条, dict)]
        if 串:
            块 = ['──── 真正发出去的那一串（渲染后）────\n' + 串]
            大小 = '渲染后 %d 字' % len(串)
        else:
            块 = []
            if 系统:
                块.append('──── system（原料，没经过渲染）────\n' + 系统)
            for 条 in 消息:
                块.append('──── %s（原料，没经过渲染）────\n%s'
                         % (条.get('role') or '?', 条.get('content') or ''))
            大小 = ('system %d 字 ＋ 消息 %d 条（共 %d 字）'
                   % (len(系统), len(消息),
                      len(系统) + sum(len(条.get('content') or '') for 条 in 消息)))
        self.预览.setPlainText('\n\n'.join(块) if 块 else '（这一份是空的）')
        # 预览默认停在末尾，一进来该看到的是最上面那段
        self.预览.moveCursor(QTextCursor.Start)

        # ⚠ **"装了哪条路"必须摆出来。** `模板` 是正常；`并入首条` 是补偿过
        # （说明这个模型不吃 system 插槽）；`装不进去` / `回退` 是**真出了问题**
        # —— 那两种情况下模型根本没看到我们的系统提示，而它不会报错。
        说 = ['这是第 %s 步发出去的一份：%s，按 %s token 估'
             % (最后.get('第') or '?', 大小,
                '{:,}'.format(int(最后.get('小号') or 0)))]
        路 = 最后.get('路') or ''
        if 路:
            说.append('装法：%s' % 路)
        if 最后.get('强制'):
            说.append('⚠ 这一轮是**强制轮** —— 提示词末尾替它把工具调用开了个头')
        预填 = 最后.get('预填') or ''
        if 预填:
            说.append('预填：%s' % 预填.replace('\n', '⏎'))
        折 = int(最后.get('折到') or 0)
        if 折:
            说.append('前面 %d 步当时已经折成存根' % 折)
        self.右手.setText('；'.join(说))

    # ── 删 ──

    def _删(self):
        if not self._现在:
            return
        编号 = self._现在
        任务 = (self._行 or {}).get('任务') or '（没记任务）'
        if QMessageBox.question(
                self, '删掉这次任务？',
                '「%s」的这次记录会删掉（每一步、工具结果、发出去的提示词\n'
                '全没）。它不在任何聊天里，删了不影响角色和会话。\n\n'
                '**删了就没了。**' % 任务,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return

        def 好了(_值, 错):
            if 错:
                self.报错.setText('删失败：%s' % 错)
                return
            self._读()

        self._工位.干(lambda 库, n=编号: 酒馆助手存.删任务(库, n), 好了)


# ── 合成一个窗口 ────────────────────────────────────────────────────

class 上下文框(QDialog):
    """
    「上下文」—— **一个窗口，两个页签**。

        页 0「聊天会话」  这一段对话这次会发什么（`上下文页`）
        页 1「编程助手」  助手跑过的任务发过什么（`助手上下文页`）

    **为什么要合。** 这两样问的是同一个问题的两半 ——"这回到底喂给模型的
    是什么" —— 却各自是一个窗口、各有一个「关闭」、各自一个快捷键入口。
    现在入口只有一个（主窗的「上下文…」/ `Ctrl+K`），`看哪` 决定落在
    哪一页：**在编程助手页按 Ctrl+K 就落在「编程助手」那一页**，不用自己再切。

    ⚠ **两个页都是构造时就立刻读库的**（各自往工位投一件活）。不搞懒加载 ——
    本机 JSON，两件活而已；懒加载换来的是"切过去是空的、还得再点一下刷新"。

    ⚠ **`改过了` 是透传的**（见下面那个 property）。主窗 exec 完靠它决定
    要不要把消息区和会话栏刷一遍 —— 在这里另存一份的话，会话页内部置了
    `True` 而壳上还是 `False`，表现就是"在框里删了消息，回来那条还画着"。
    """

    def __init__(self, 工位, 父=None, 会话编号=0, 任务编号=0, 看哪=0,
                 忙查=None, 目录='', 窗口=0):
        super().__init__(父)
        self.setWindowTitle('上下文')
        self.resize(1040, 700)

        外 = QVBoxLayout(self)

        self.页堆 = QTabWidget()
        self.会话页 = 上下文页(工位, self.页堆, 会话编号, 忙查=忙查)
        self.助手页 = 助手上下文页(工位, self.页堆, 任务编号,
                                目录=目录, 窗口=窗口)
        self.页堆.addTab(self.会话页, '聊天会话')
        self.页堆.addTab(self.助手页, '编程助手')
        # `看哪` 只认 0/1：越界的值喂给 `setCurrentIndex` 是**静默不生效**，
        # 界面上看着就像"默认页没按说的来"，不如当场夹回 0。
        self.页堆.setCurrentIndex(1 if int(看哪 or 0) == 1 else 0)
        外.addWidget(self.页堆, 1)

        底 = QHBoxLayout()
        self.说明 = 酒馆样式.灰色小字(
            '左边挑，右边看。两个页各管各的，互不影响 —— 「聊天会话」动的是'
            '这一段对话的预设和消息，「编程助手」动的是助手存下来的任务记录。')
        self.说明.setWordWrap(True)
        底.addWidget(self.说明, 1)
        底.addWidget(酒馆样式.做按钮('关闭', self.reject, 'Esc 也能关'))
        外.addLayout(底)

    @property
    def 改过了(self):
        """聊天那一页有没有动过消息或预设。**只认会话页的** —— 删掉一次助手
        任务记录不影响聊天那边，不该让主窗去刷消息区（见类 docstring）。"""
        return bool(self.会话页.改过了)
