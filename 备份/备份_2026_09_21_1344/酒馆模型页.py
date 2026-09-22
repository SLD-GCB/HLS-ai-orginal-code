#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆模型页.py — 本地 GGUF 模型的管理框（下载 / 删除 / 看清单）

```
┌──────────────────────────────────────────────────────────────┐
│ 已下载（酒馆数据/模型/）        │ 下载新模型                  │
│ ┌──────────────────────────┐   │ 来源 [hf-mirror.com     ▾] │
│ │R1-Distill-Qwen-7B-Q4…    │   │ 挑选方式 [精选清单      ▾] │
│ │  4.7 GB          [删除]  │   │ ┌───────────────────────┐ │
│ │qwen2.5-3b…q4_k_m.gguf    │   │ │── DeepSeek 系 ──      │ │
│ └──────────────────────────┘   │ │ R1-Distill-Qwen-1.5B  │ │
│ [刷新]                         │ │ R1-Distill-Qwen-7B    │ │
│                                │ │── 国外·通用 ──        │ │
│ 引擎状态：llama-cpp 可用       │ │ …                     │ │
│                                │ └───────────────────────┘ │
│                                │ 文件 [Q4_K_M.gguf（4.7G）▾]│
│                                │ [开始下载] [取消]          │
│                                │ ████████░░ 62% 2.9/4.7G   │
└──────────────────────────────────────────────────────────────┘
```

**挑选方式有三页**（`QStackedWidget`）：精选清单 / 搜索 / 自己填仓库。
三种模式最后都把一个"要下的文件"灌进同一个「文件」下拉——`_开始下载`
只认它，出口单一，好测也好读。

⚠ **精选用 `QListWidget` 不用 `QComboBox`。** 几十条加分组标题，
下拉框拉出来太长；`QTreeWidget` 的折叠对这个规模是杀鸡用牛刀。
分组标题行 `setFlags(Qt.NoItemFlags)`——不可选中、不可点，只是个视觉分隔。

⚠ **精选上面那一格「类别」是筛子，不是第四种挑选方式。**
它跟「来源」一样是**独立维度**：同一批档位按"拿来干什么"再分一刀
（`通用` / `agent 专用` / `聊天专用`，判据见 `酒馆本地.推荐用途们`）。
它在 `_画精选` 里过滤，`_按档变` / `_挑中的` **一个字没动** ——
被筛掉的行压根不在列表里，绕不过去。

⚠ **换了「来源」要重算精选哪几条可用。** ModelScope 只镜像了一部分 HF
仓库（mradermacher、HauhauCS 那些它没有），所以有些档位在 modelscope 下
是灰的（`_按来源变`）。灰掉而不是"自动把源切过去"，因为下拉框跟用户抢
比灰掉更让人困惑。

**线程形状照抄 `酒馆接口页.找模型的`**：「列出文件」和「下载」都是
一次性 daemon `threading.Thread` + 主线程建的 `QObject` 信号嘴。理由
那份注释写全了——一次性任务不值得 `QThread`，而且阻塞调用掐不断：
取消只是置旗，**生效点是这次下载回来之后**，之前它该下还在下。

**进度不靠 SDK 回调。** 两个源的 SDK 都没有干净的应用层进度回调
（modelscope 的 `snapshot_download`、hf 的 `hf_hub_download` 都自己往终端
画 tqdm）。这里是一个 500ms 的 `QTimer` 去数**这一次下载专用的那个暂存
目录**（`酒馆本地.暂存目录(源, 仓库, 模式)`）的字节——版本无关，断点续传
时起始进度自动是对的。数的是"这一次"的目录，所以别的源、别的量化档位
留下的残留不会串进来。

⚠ **生成中不给下载、不给删。** 引擎那条生成握着 `酒馆本地.锁`，下载完
挪文件、删模型跟它并发没好处；而且正在跑的模型文件被 mmap 着，Windows
上根本删不掉。所以按钮在 `在生成()` 时直接拦住，把话说明白。
"""

import os
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QProgressBar, QSplitter,
                               QStackedWidget, QVBoxLayout, QWidget)

import 酒馆本地
import 酒馆接口
import 酒馆样式

__all__ = ['模型页']

#: 进度条多久数一次 `_暂存/` 的字节。
数进度毫秒 = 500

#: 挑选方式那三页的页号，跟 `self.页` 的加入顺序一一对应。
页精选, 页搜索, 页手动 = 0, 1, 2


class 干活的(QObject):
    """
    「列出文件」「搜索」「下载」那几条一次性线程的**信号嘴**。

    ⚠ 每个对象**由它那条线程自己攥着**（闭包里的 `器`），不挂到 `self`
    上——挂上去框一关它就销毁，线程回来喊人时 `emit` 打在死掉的 C++
    对象上。攥在线程手里就总比这次活活得久；框关了没人接信号是空放，
    Qt 自动断连接，不报错。见 `酒馆接口页.找模型的` 那份完整记录。
    """

    列好了 = Signal(list)          # [(仓库内路径, 字节数)]
    搜到了 = Signal(list)          # [{'仓库','下载','喜欢','源'}]
    下好了 = Signal(list)          # 落进模型目录的文件名们
    坏了 = Signal(str)             # 一句人话


def _大小嘴(字节):
    """字节数 → 「4.7 GB」/「812 MB」。"""
    if 字节 >= 1e9:
        return '%.1f GB' % (字节 / 1e9)
    return '%.0f MB' % (字节 / 1e6)


def _下载量嘴(数):
    """下载次数 → 「12.3k」。搜索结果的列表太窄，放不下完整数字。"""
    if 数 >= 1000000:
        return '%.1fM' % (数 / 1e6)
    if 数 >= 1000:
        return '%.1fk' % (数 / 1e3)
    return str(数)


class 模型页(QDialog):
    """
    管理本地 GGUF 模型。**自己走 `工位`**（删除前查哪些配置在用）；
    `exec()` 完外面读 `变过了`——列表动过，接口页该重灌模型下拉。
    """

    def __init__(self, 工位, 父=None):
        super().__init__(父)
        self._工位 = 工位
        self.变过了 = False
        self._远程 = []            # 「列出文件」问回来的 [(路径, 字节数)]
        self._结果 = []            # 「搜索」问回来的 [{'仓库','下载',…}]
        self._下着 = False         # 有一条下载线程在路上
        self._期望 = 0             # 这次下载大概多少字节（估算进度用）
        self._本次暂 = ''          # 这次下载的暂存目录（进度只数它）
        self._取消旗 = None

        self.setWindowTitle('本地模型')
        self.resize(820, 560)

        外 = QVBoxLayout(self)
        分 = QSplitter(Qt.Horizontal)
        外.addWidget(分, 1)

        # ── 左：已下载 ──
        左 = QWidget()
        左排 = QVBoxLayout(左)
        左排.setContentsMargins(0, 0, 0, 0)
        左排.addWidget(QLabel('已下载（%s）' % 酒馆本地.模型目录()))
        self.清单 = QListWidget()
        左排.addWidget(self.清单, 1)
        左钮 = QHBoxLayout()
        左钮.addWidget(酒馆样式.做按钮('刷新', self.刷清单))
        左钮.addWidget(酒馆样式.做按钮('删除选中', self._删))
        左排.addLayout(左钮)
        分.addWidget(左)

        # ── 右：下载 ──
        右 = QWidget()
        右排 = QVBoxLayout(右)
        右排.setContentsMargins(0, 0, 0, 0)
        右排.addWidget(QLabel('下载新模型（挑一个文件下，不是整个仓库）'))

        # 来源：**和"挑哪个模型"是两个独立维度**——同一档位换源重下是常见
        # 需求（这个源慢/断了换另一个）。所以单独一栏、一直可见。
        源排 = QHBoxLayout()
        源排.addWidget(QLabel('来源'))
        self.来源 = QComboBox()
        for 项 in 酒馆本地.下载源:
            self.来源.addItem(项['名'], 项['标'])
        self.来源.setToolTip('从哪个源下。ModelScope 国内最稳但仓库不全；'
                            'hf-mirror.com 仓库最全、国内也能直连；'
                            'HuggingFace 官方要梯子但门禁/私有仓库登录后能下')
        self.来源.setCurrentIndex(self.来源.findData(酒馆本地.默认源))
        源排.addWidget(self.来源, 1)
        右排.addLayout(源排)

        方排 = QHBoxLayout()
        方排.addWidget(QLabel('挑选方式'))
        self.方式 = QComboBox()
        self.方式.addItem('精选清单', 页精选)
        self.方式.addItem('搜索仓库', 页搜索)
        self.方式.addItem('自己填仓库', 页手动)
        方排.addWidget(self.方式, 1)
        右排.addLayout(方排)

        self.页 = QStackedWidget()
        右排.addWidget(self.页, 1)

        # ── 页①：精选清单 ──
        页一 = QWidget()
        页一排 = QVBoxLayout(页一)
        页一排.setContentsMargins(0, 0, 0, 0)

        # 类别：**跟「来源」一样是个独立维度** —— 同一批档位，按"拿来干什么"
        # 再分一刀。挑进列表的理由只有一个：光看名字看不出 `Seed-Coder` 和
        # `GLM-Z1` 是给两种完全不同用途的。
        #
        # ⚠ 显示名和值分开存：值是 `酒馆本地.推荐用途们` 的短码，显示名带
        # "专用"两字才说得清 —— 单写 `agent`，看不出是"给 agent 用"还是
        # "需要一个 agent"。
        类排 = QHBoxLayout()
        类排.addWidget(QLabel('类别'))
        self.类别 = QComboBox()
        self.类别.addItem('全部', None)
        self.类别.addItem('通用', '通用')
        self.类别.addItem('agent 专用', 'agent')
        self.类别.addItem('聊天专用', '对话')
        self.类别.setToolTip(
            '按**拿来干什么**筛，不是按强弱。\n\n'
            '  · agent 专用 —— 练过工具调用 / 代码 / 指令跟随。要它干活选这个。\n'
            '  · 聊天专用 —— 角色扮演、创作、纯聊天。\n'
            '    其中 DeepSeek R1 蒸馏系是**推理型**：一开口就是大段草稿，\n'
            '    草稿会吃光输出预算，**做 agent 不划算**（实测过）。\n'
            '  · 通用 —— 两边都行，但都不是它的强项。\n\n'
            '⚠ 同一条档位只占一类：「代码和聊天都不差」的通用模型归「通用」，\n'
            '只有**偏科在代码 / 工具那一侧**的才归「agent 专用」。')
        self.类别.setCurrentIndex(0)
        类排.addWidget(self.类别, 1)
        页一排.addLayout(类排)

        self.精选 = QListWidget()
        self.精选.setToolTip('内置档位，全部保证 8G 显存能全量上卡')
        页一排.addWidget(self.精选, 1)
        脚 = QLabel(酒馆本地.精选脚注)
        脚.setWordWrap(True)
        # 脚注用次要色——它是解释，不是可点的东西，别让它看着像一条档位
        脚.setStyleSheet('color: %s;' % 酒馆样式.配色()['text_secondary'])
        页一排.addWidget(脚)
        self.页.addWidget(页一)

        # ── 页②：搜索 ──
        页二 = QWidget()
        页二排 = QVBoxLayout(页二)
        页二排.setContentsMargins(0, 0, 0, 0)
        搜排 = QHBoxLayout()
        self.关键词 = QLineEdit()
        self.关键词.setPlaceholderText('比如 roleplay、uncensored、deepseek gguf')
        self.关键词.returnPressed.connect(self._搜索)
        搜排.addWidget(self.关键词, 1)
        self.搜按钮 = 酒馆样式.做按钮('搜索', self._搜索,
                                   '按关键词搜仓库（只回带 GGUF 的）')
        搜排.addWidget(self.搜按钮)
        页二排.addLayout(搜排)
        self.结果 = QListWidget()
        self.结果.setToolTip('点一条就会去列它的 GGUF 文件')
        页二排.addWidget(self.结果, 1)
        页二排.addWidget(QLabel('搜索是仓库级的。点一条 → 自动列出它的 GGUF → 挑文件下载'))
        self.页.addWidget(页二)

        # ── 页③：自己填仓库 ──
        页三 = QWidget()
        页三排 = QVBoxLayout(页三)
        页三排.setContentsMargins(0, 0, 0, 0)
        仓排 = QHBoxLayout()
        仓排.addWidget(QLabel('仓库'))
        self.仓库 = QLineEdit()
        self.仓库.setPlaceholderText('比如 unsloth/DeepSeek-R1-Distill-Qwen-7B-GGUF')
        仓排.addWidget(self.仓库, 1)
        self.列文件 = 酒馆样式.做按钮('列出文件', self._列文件,
                                  '问一句这个仓库里有哪些 GGUF')
        仓排.addWidget(self.列文件)
        页三排.addLayout(仓排)
        页三排.addStretch(1)
        self.页.addWidget(页三)

        # ── 公用：文件 / 按钮 / 进度 ──
        # ⚠ 三种模式**最后都往这一个下拉里灌**「要下的那一个文件」。
        # 出口单一，`_开始下载` 只认它，不用为三种模式各写一条下载路。
        件排 = QHBoxLayout()
        件排.addWidget(QLabel('文件'))
        self.文件 = QComboBox()
        件排.addWidget(self.文件, 1)
        右排.addLayout(件排)

        下钮 = QHBoxLayout()
        self.开始 = 酒馆样式.做按钮('开始下载', self._开始下载)
        self.取消 = 酒馆样式.做按钮('取消', self._取消下载)
        self.取消.setEnabled(False)
        下钮.addWidget(self.开始)
        下钮.addWidget(self.取消)
        下钮.addStretch(1)
        右排.addLayout(下钮)

        self.进度 = QProgressBar()
        self.进度.setRange(0, 100)
        self.进度.setValue(0)
        右排.addWidget(self.进度)
        self.进度字 = QLabel('断点续传：下到一半关了，下次自动接着下。')
        self.进度字.setWordWrap(True)
        右排.addWidget(self.进度字)
        分.addWidget(右)
        分.setSizes([340, 480])

        # ── 底：引擎状态 ──
        if 酒馆本地.可用():
            self._引擎标 = QLabel('引擎状态：llama-cpp 可用。'
                                'GPU 层数在「接口配置」里每套各调各的。')
        else:
            self._引擎标 = QLabel('引擎状态：没装上（%s）。\n'
                                'CPU 版：pip install llama-cpp-python，装完重启程序。'
                                % (酒馆本地.不可用原因() or '没装 llama-cpp-python'))
            self._引擎标.setStyleSheet('color: %s;' % 酒馆样式.配色()['danger'])
        self._引擎标.setWordWrap(True)
        外.addWidget(self._引擎标)

        # 进度定时器：数本次下载的暂存目录，见文件头「进度不靠 SDK 回调」
        self._表 = QTimer(self)
        self._表.setInterval(数进度毫秒)
        self._表.timeout.connect(self._数进度)

        # ── 联动的线 ──
        self.来源.currentIndexChanged.connect(self._按来源变)
        self.方式.currentIndexChanged.connect(self._按方式变)
        self.精选.currentItemChanged.connect(self._按档变)
        self.类别.currentIndexChanged.connect(self._画精选)
        self.结果.currentItemChanged.connect(self._按结果变)

        self._画精选()
        self._按来源变()
        self._按方式变()
        self.刷清单()

    # ── 左：已下载清单 ──

    def 刷清单(self):
        """重数一遍模型目录。磁盘读，毫秒级，直接主线程干。"""
        self.清单.clear()
        for 名, 大 in 酒馆本地.列本地():
            QListWidgetItem('%s\n%s' % (名, _大小嘴(大)), self.清单)

    def _删(self):
        项 = self.清单.currentItem()
        if 项 is None:
            QMessageBox.information(self, '先选一个', '左边清单里先点一个模型。')
            return
        名 = 项.text().split('\n')[0]
        if 酒馆本地.在生成():
            QMessageBox.information(self, '正在生成',
                                    '模型这会儿正在说话。等这条说完再删。')
            return

        def 回来(用着的, 错):
            if 错:
                QMessageBox.warning(self, '查配置失败', 错)
                return
            几套 = len([套 for 套 in (用着的 or []) if 套.模型 == 名])
            提 = '「%s」会被删掉，删了要聊就得重新下。' % 名
            if 几套:
                提 += '\n\n⚠ 有 %d 套接口配置正指着它，那些配置会发不出去。' % 几套
            if QMessageBox.question(self, '删掉这个模型？', 提) != QMessageBox.Yes:
                return
            try:
                没了 = 酒馆本地.删本地(名)
            except OSError as 错:
                # Windows 上正被引擎 mmap 着的文件删不掉，报这个
                QMessageBox.warning(self, '删不掉', '%s\n（这个模型可能正被'
                                    '引擎占着——重启程序后再删）' % 错)
                return
            if 没了:
                self.变过了 = True
                self.刷清单()

        # 「哪些配置指着它」要读库，走工位
        self._工位.干(lambda 库: 酒馆接口.列(库), 回来)

    # ── 右：三种挑选方式的联动 ──

    def _当前源(self):
        """当前选的源标识。"""
        return self.来源.currentData() or 酒馆本地.默认源

    def _画精选(self, _现=None):
        """
        把 `推荐模型` 铺进列表：按 `组` 插分组标题，按 `类别` 筛。

        ⚠ 组标题行 `setFlags(Qt.NoItemFlags)`——**不可选中**。不挡的话
        用户能"选中"一条标题，然后 `_挑中的` 拿到一个没有 `仓库` 的条目。
        `Qt.UserRole` 挂档位字典（标题行挂 None），`_按档变` 靠它取值。

        ⚠ **筛选只在"画"这一步做，`_按档变` / `_挑中的` 一个字没动** ——
        它们只认 `Qt.UserRole` 里挂的档位字典，被筛掉的行压根不在列表里，
        绕不过去。多写一处判断就多一处会漂的地方。

        ⚠ **老档位没有 `用途` 这一列时一律算 `通用`** —— 不能让加这列之前
        的数据被一筛就没。
        """
        要 = self.类别.currentData()
        self.精选.clear()
        上组 = None
        for 档 in 酒馆本地.推荐模型:
            if 要 and (档.get('用途') or '通用') != 要:
                continue
            组 = 档.get('组') or ''
            if 组 != 上组:
                self.精选.addItem('── %s ──' % 组)
                题 = self.精选.item(self.精选.count() - 1)
                题.setFlags(Qt.NoItemFlags)
                题.setData(Qt.UserRole, None)
                题.setTextAlignment(Qt.AlignLeft)
                上组 = 组
            项 = QListWidgetItem(档['名字'])
            项.setData(Qt.UserRole, 档)
            self.精选.addItem(项)
        if self.精选.currentRow() < 0:
            self._选第一条能选的()

    def _选第一条能选的(self):
        """默认选中第一条**能用的**档位（跳过分组标题和当前源没有的）。"""
        for i in range(self.精选.count()):
            项 = self.精选.item(i)
            if 项.data(Qt.UserRole) and (项.flags() & Qt.ItemIsEnabled):
                self.精选.setCurrentRow(i)
                return
        self.精选.setCurrentRow(-1)

    def _按来源变(self):
        """
        换了来源：重算精选里哪几条可用，并丢掉已经不成立的东西。

        ⚠ 变灰是必须的：ModelScope 只镜像了一部分 HF 仓库（实测没有的只有
        mradermacher 全系和 HauhauCS 那条，见 `酒馆本地.推荐模型`）。不灰的话
        用户点下去要等网络报错才知道。可用性判断本身在 `酒馆本地.源上有`
        ——**不在这儿再写一遍**，那边是唯一知道"哪个源有哪些仓库"的地方。

        ⚠ **搜索结果要清掉**——那是上一个源搜出来的仓库名，换源之后未必还
        存在，留着就是一堆点了会报错的条目。
        """
        源 = self._当前源()
        for i in range(self.精选.count()):
            项 = self.精选.item(i)
            档 = 项.data(Qt.UserRole)
            if not 档:
                continue
            能用 = 酒馆本地.源上有(档, 源)
            项.setFlags((Qt.ItemIsEnabled | Qt.ItemIsSelectable) if 能用
                       else Qt.NoItemFlags)
            项.setToolTip('' if 能用 else
                        '这个仓库 ModelScope 上没有（它是社区量化仓库，'
                        '只在 HF 系）。把「来源」切到 hf-mirror.com 就能下。')

        self.结果.clear()
        self._结果 = []
        self._远程 = []
        self.文件.clear()

        现 = self.精选.currentItem()
        if 现 is None or not (现.flags() & Qt.ItemIsEnabled):
            self._选第一条能选的()
            self.进度字.setText('换了来源。灰掉的那几条在当前来源上没有——'
                              '切到 hf-mirror.com 就都有了。')

    def _按方式变(self):
        """切 `QStackedWidget` 的那一页，并把提示语换成本页该说的话。"""
        页 = self.方式.currentData()
        self.页.setCurrentIndex(页 if 页 is not None else 页精选)
        if 页 == 页精选:
            self.进度字.setText('在精选里挑一档，直接「开始下载」。'
                              '灰掉的是当前来源没有的。')
        elif 页 == 页搜索:
            self.进度字.setText('搜仓库名（只回带 GGUF 的），'
                              '点一条就会去列它的文件。')
        else:
            self.进度字.setText('填「owner/仓库名」，先「列出文件」挑一个，'
                              '再「开始下载」。')

    def _按档变(self, 现, _旧=None):
        """精选中选了一条：把它的文件名灌进「文件」下拉。"""
        if 现 is None:
            return
        档 = 现.data(Qt.UserRole)
        if not 档:
            return
        self._远程 = []
        self.文件.clear()
        大 = int(档.get('约字节') or 0)
        self.文件.addItem('%s（%s）' % (档['模式'], _大小嘴(大)))
        self._期望 = 大
        推 = 酒馆本地.首选源(档)
        if 推 != self._当前源():
            self.进度字.setText('这一档一般从「%s」下（那边最快），'
                              '不过当前来源上也下得动。' % 酒馆本地.源名(推))

    def _按结果变(self, 现, _旧=None):
        """
        搜索里点了一条：填进「仓库」并**自动去列文件**。

        搜索结果是仓库级的，用户还得挑具体那个 .gguf——自动列一次省一步，
        列出来的东西直接进公用的「文件」下拉。
        """
        if 现 is None:
            return
        位 = self.结果.row(现)
        if 位 < 0 or 位 >= len(self._结果):
            return
        仓库 = self._结果[位]['仓库']
        self.仓库.setText(仓库)
        self._列文件()

    def _锁下载(self, 锁):
        """
        下载中把该禁的都禁掉，只留「取消」。

        ⚠ **必须连「来源」「挑选方式」一起禁**：这两个能换掉 `_挑中的()`
        的结果，而下载线程里那一份是开始那一刻就定死的——不禁的话界面显示
        的和真在下的是两回事。
        """
        for 件 in (self.来源, self.方式, self.精选, self.关键词, self.搜按钮,
                   self.结果, self.仓库, self.列文件, self.文件):
            件.setEnabled(not 锁)
        self.开始.setEnabled(not 锁)
        self.取消.setEnabled(锁)
        if not 锁:
            self._按来源变()          # 把精选的灰/可点状态按当前源重算回来

    # ── 右：列文件 ──

    def _列文件(self):
        # 「自己填仓库」页的按钮，和搜索选中后自动调，都走这一条
        仓库 = self.仓库.text().strip()
        if not 仓库:
            QMessageBox.information(self, '先填仓库',
                                    '比如 unsloth/DeepSeek-R1-Distill-Qwen-7B-GGUF')
            return
        源 = self._当前源()
        self.列文件.setEnabled(False)
        self.进度字.setText('正在问 %s 里有哪些 GGUF（来源：%s）…'
                          % (仓库, self.来源.currentText()))

        器 = 干活的()

        def 跑():
            try:
                件们 = 酒馆本地.列远程文件(仓库, 源)
            except Exception as 错:
                器.坏了.emit(str(错))
            else:
                器.列好了.emit(件们)

        器.列好了.connect(self._收到文件)
        器.坏了.connect(self._收坏)
        threading.Thread(target=跑, daemon=True).start()

    def _搜索(self):
        """按关键词搜仓库。结果进 `self.结果`，点一条才去列文件。"""
        词 = self.关键词.text().strip()
        if not 词:
            QMessageBox.information(self, '先填关键词',
                                    '比如 roleplay、uncensored、deepseek gguf')
            return
        源 = self._当前源()
        self.搜按钮.setEnabled(False)
        self.进度字.setText('正在搜「%s」（来源：%s）…'
                          % (词, self.来源.currentText()))

        器 = 干活的()

        def 跑():
            try:
                条们 = 酒馆本地.搜模型(词, 源)
            except Exception as 错:
                器.坏了.emit(str(错))
            else:
                器.搜到了.emit(条们)

        器.搜到了.connect(self._收到结果)
        器.坏了.connect(self._收坏)
        threading.Thread(target=跑, daemon=True).start()

    def _收到结果(self, 条们):
        self.搜按钮.setEnabled(True)
        self._结果 = list(条们 or [])
        self.结果.clear()
        for 条 in self._结果:
            self.结果.addItem('%s  ·  %s↓' % (条['仓库'], _下载量嘴(条['下载'])))
        if self._结果:
            self.进度字.setText('搜到 %d 个仓库。点一条就会去列它的 GGUF 文件。'
                              % len(self._结果))
        else:
            self.进度字.setText('这个来源上没搜到。换个关键词，或者换个「来源」——'
                              'ModelScope 的仓库比 HF 少很多。')

    def _收到文件(self, 件们):
        self.列文件.setEnabled(self.方式.currentData() == 页手动)
        self._远程 = list(件们 or [])
        self.文件.clear()
        for 径, 大 in self._远程:
            self.文件.addItem('%s（%s）' % (径, _大小嘴(大)))
        if self._远程:
            self._期望 = int(self._远程[0][1] or 0)
            self.进度字.setText('问到 %d 个 GGUF，挑一个再「开始下载」。'
                              '（只下选中的这个，不是整个仓库）' % len(self._远程))
        else:
            self.进度字.setText('这个仓库里没有 GGUF 文件。')

    def _收坏(self, 错):
        self._锁下载(False)
        self._下着 = False
        self._表.stop()
        self.进度字.setText(错)

    # ── 右：下载 ──

    def _挑中的(self):
        """
        当前该下哪个：`（源标识, 仓库, 模式, 期望字节）`，没挑好回 `None`。

        ⚠ 源**在这儿现取**（`self._当前源()`），不缓存：`_开始下载` 拿到的
        就是按下按钮那一刻界面上显示的源，不会和用户看到的对不上。
        """
        源 = self._当前源()
        页 = self.方式.currentData()

        if 页 == 页精选:
            现 = self.精选.currentItem()
            档 = 现.data(Qt.UserRole) if 现 else None
            if not 档:
                self.进度字.setText('先在精选清单里挑一档。')
                return None
            if not (现.flags() & Qt.ItemIsEnabled):
                self.进度字.setText('这一档在「%s」上没有。换个「来源」再试。'
                                  % self.来源.currentText())
                return None
            return 源, 档['仓库'], 档['模式'], int(档.get('约字节') or 0)

        仓库 = self.仓库.text().strip()
        位 = self.文件.currentIndex()
        if not 仓库 or 位 < 0 or 位 >= len(self._远程):
            self.进度字.setText('先填仓库、列出文件、挑一个——'
                              '或者直接在「精选清单」里挑一档。')
            return None
        径, 大 = self._远程[位]
        return 源, 仓库, 径, int(大 or 0)

    def _开始下载(self):
        if self._下着:
            return                          # 按钮已经禁了，这是防连点
        if 酒馆本地.在生成():
            QMessageBox.information(self, '正在生成',
                                    '模型这会儿正在说话。等这条说完再下。')
            return
        挑 = self._挑中的()
        if 挑 is None:
            return
        源, 仓库, 模式, 期望 = 挑

        器 = 干活的()
        self._取消旗 = threading.Event()

        def 跑():
            try:
                落了 = 酒馆本地.下载(仓库, 模式, 源)
            except Exception as 错:
                器.坏了.emit(str(错))
            else:
                if self._取消旗.is_set():
                    # 点了取消之后这次下载才回来——文件还是挪进去了（它是
                    # 完整下完的，不该扔），只是不当成"下载成功"来报
                    器.坏了.emit('已取消。文件其实已经下完了，在左边清单里。')
                else:
                    器.下好了.emit(落了)

        器.下好了.connect(self._收下载)
        器.坏了.connect(self._收坏)
        self._下着 = True
        self._期望 = 期望
        # 进度只数这一次的暂存目录（按 源/仓库/模式 分好的那个），
        # 见文件头「进度不靠 SDK 回调」
        self._本次暂 = 酒馆本地.暂存目录(源, 仓库, 模式)
        self._锁下载(True)
        self.进度.setValue(0)
        self.进度字.setText('开始下载 %s（来源：%s）…'
                          '（取消要等这一段下完才生效）'
                          % (模式, self.来源.currentText()))
        self._表.start()
        threading.Thread(target=跑, daemon=True).start()

    def _取消下载(self):
        """
        置旗。**不是立刻停**——两个 SDK 的那一次阻塞调用都掐不断
        （跟 `酒馆大脑._读客` 记录的 Windows 阻塞读一个道理），生效点是
        它回来之后：不报成功、界面收摊。已经下完的分片留在本次的暂存
        目录里，下次接着下。
        """
        if self._取消旗 is not None:
            self._取消旗.set()
        self.进度字.setText('取消中……要等这一段下完才生效（已下的部分会留着，'
                          '下次自动接着下）。')
        self.取消.setEnabled(False)

    def _收下载(self, 落了):
        self._下着 = False
        self._表.stop()
        self._锁下载(False)
        self.进度.setValue(100)
        self.进度字.setText('下好了：%s。到「接口配置」里建一套 local 配置、'
                          '「模型」那格填这个文件名就能聊了。' % '、'.join(落了))
        self.变过了 = True
        self.刷清单()

    def _数进度(self):
        """
        500ms 数一次**这次下载的暂存目录**的字节。见文件头「进度不靠 SDK 回调」。

        ⚠ **只数 `self._本次暂`，不数整个 `_暂存/`。** 那里堆着别的源、
        别的量化档位留下的残留，数全量的话进度条开局就跳、没下完先顶 99%。

        ⚠ **半成品也要一起数**（modelscope 的 `._____temp`、hf 的 `.cache`）
        ——那正是"已下载字节"所在的地方。把它们剪掉反而会一直显示 0%，
        直到 SDK 原子改名那一刻才跳到 100%。
        """
        暂 = self._本次暂 or 酒馆本地.暂存目录()
        总 = 0
        for 根, _目们, 件们 in os.walk(暂):
            for 件 in 件们:
                try:
                    总 += os.path.getsize(os.path.join(根, 件))
                except OSError:
                    pass                    # 正写着的文件，下次再数
        if self._期望 > 0:
            比 = min(99, int(总 * 100 / self._期望))
            self.进度.setValue(比)
            self.进度字.setText('下载中…… %s / 约 %s（%d%%）'
                              % (_大小嘴(总), _大小嘴(self._期望), 比))
        else:
            self.进度.setValue(0)
            self.进度字.setText('下载中…… 已下 %s' % _大小嘴(总))
