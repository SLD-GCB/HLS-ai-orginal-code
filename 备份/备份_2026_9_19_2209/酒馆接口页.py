#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆接口页.py — 模型 API 配置的管理框

```
┌────────────────────────────────────────────────────┐
│ ┌──────────┐  名称  [本地 ollama            ]      │
│ │本地 ollama│  协议  [openai        ▾]              │
│ │DeepSeek  │  地址  [http://127.0.0.1:11434/v1  ]  │
│ └──────────┘  密钥  [••••••••            ]  [看]   │
│ [+新建][删除]  模型  [qwen2.5:7b    ▾] [发现模型]  │
│              温度  [0.70]  最大输出 [         ]     │
│              附加头 [{}                          ]  │
│              [保存]                       [关闭]    │
└────────────────────────────────────────────────────┘
```

**「模型」那格是可编辑的下拉框**：手打照旧打得进，右边那个「发现模型」按钮
会拿表单里现填的协议/地址/密钥去问一句 `GET {地址}/models`，把回来的模型名
灌进下拉里挑（见 `找模型的`）。**不需要先保存**——这个功能就是给"还没填完、
不知道模型该写什么"的时候用的。

**两条配置路线**：每套有一个「给谁用」（对话 / agent）。左边顶上那组切换决定
**看哪一组**，列表只显示那一组；「新建一套」建出来的是当前正在看的那一组。
聊天那条链只认「对话」的，编程助手只认「agent」的 —— 这样给 agent 调温度
不会把聊天带走。⚠ **老数据（这一列是后加的）两边都看得见、两边都能用**，
列表里标着「旧配置」，改一下归属就归到一边。见 `酒馆模型.用途们`。

⚠ **点列表里的一套配置时，必须重新 `取()` 一次整条。** 理由跟
`酒馆角色页` 那条一模一样、而且更险：`酒馆接口.清单列` **故意不含 `密钥`**
（列表要拿去渲染，密钥不该跟着到处跑）。而 `酒馆模型.从行` 对缺的列填默认值。
拿列表行去填表单再保存 → **密钥被空串覆盖，接口当场用不了**，而且界面上
看不出发生了什么。

⚠ **密钥现在是明文落盘的。** 就写在 `酒馆数据/接口配置.json` 里，程序旁边
那个目录，打得开就读得到。这一条第一期就写在那儿了，不是这个文件的新问题，
但在这里最显眼——所以密钥框默认打码，旁边给一个「看」按钮，**要看得自己点**。
"""

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
                               QVBoxLayout, QWidget)

import json
import threading

import 酒馆大脑
import 酒馆本地
import 酒馆接口
import 酒馆样式
from 酒馆模型 import 接口配置, 工具格式们, 写JSON
from 酒馆存储 import 酒馆错误

__all__ = ['接口页']

协议名 = {'openai': 'OpenAI 兼容（ollama / vLLM / DeepSeek…）',
         'anthropic': 'Anthropic（Claude）',
         'gemini': 'Google Gemini',
         'local': '本地 GGUF 模型（llama.cpp，不联网）'}


class 找模型的(QObject):
    """
    「发现模型」那次请求的**信号嘴**。

    ⚠ **为什么是一条一次性 `threading.Thread`，而不是 `生成线` 那种 `QThread`。**

    `生成线` 是**长命**的（开机建一条，用到关机），所以它有哨兵、有停旗、
    有 `停()`，值得那一套。这个是**点一下按钮起一次**的，套 QThread 反而危险：

      · `QThread` 对象**在跑的时候被销毁，Qt 会直接 abort**
        （"QThread: Destroyed while thread is still running"）；
      · 要躲开它就得在关框时 `wait()`——而一次已经发出去的阻塞
        `requests.get` **等不了、也没法打断**（`酒馆大脑._读客` 那份记录里
        实测过：关连接掐不断一次阻塞读）。于是只剩两条路：把「关框」卡住
        几十秒，或者留一条随时会 abort 的线程。
      · 一次性线程是 daemon：**它跑完自己散，没有人需要等它。** 框关掉了
        它照样把请求做完，只是那一发 `emit` 没人接——**接收方（框）销毁时
        Qt 会自动断掉那条连接**，所以是空放，不报错也不炸。

    这个对象**建在主线程**，所以从别的线程 `emit` 是**队列投递**，
    槽在主线程里跑。整个玄机就这一条，和 `酒馆工人.工位` 那份一个道理。
    """

    好了 = Signal(list)          # 模型名们
    坏了 = Signal(str)           # 一句人话


class 接口页(QDialog):
    """
    管理接口配置。**自己走 `工位`**，外面只要 `exec()` 完读一下 `选中的`
    就行；`改过了` 置位表示期间增删改过，外面该挑一套重新贴到状态栏上。
    """

    def __init__(self, 工位, 父=None, 选中编号=0):
        super().__init__(父)
        self._工位 = 工位
        self._行们 = []            # 列表行（无密钥）
        self._整条 = None          # 当前正在编辑的整条（有密钥）
        self._编号 = 0
        self.选中的 = 选中编号
        self.改过了 = False
        self._问着 = False         # 「发现模型」那条线程还在路上
        self._能动 = False         # 表单现在允许改吗，见 `_可改`

        self.setWindowTitle('接口配置')
        self.resize(760, 520)

        外 = QVBoxLayout(self)
        分 = QSplitter(Qt.Horizontal)
        外.addWidget(分, 1)

        # ── 左：列表 ──
        左 = QWidget()
        左排 = QVBoxLayout(左)
        左排.setContentsMargins(0, 0, 0, 0)
        # ⚠ **两条路线分开之后，左边这张列表要能按组看。**
        # 聊天只看得见「对话」那一组，编程助手只看得见「agent」那一组 ——
        # 所以配置页也得分开看，不然"我给 agent 配的那套"和"聊天的"混在一列里，
        # 改错一个就影响另一条路线，而这在界面上完全看不出来。
        组排 = QHBoxLayout()
        组排.addWidget(QLabel('看哪一组'))
        self.看组 = QComboBox()
        for 名 in 酒馆接口.用途们:
            self.看组.addItem('%s配置' % 名, 名)
        self.看组.setToolTip('只影响左边列哪几套。\n'
                           '⚠ 标着「旧配置」的是加这一列之前建的，'
                           '两边都能用、两边都看得见 —— 改一下「给谁用」就归到一边了。')
        self.看组.currentIndexChanged.connect(lambda _: self.刷新(self._编号))
        组排.addWidget(self.看组, 1)
        左排.addLayout(组排)
        self.列表 = QListWidget()
        self.列表.currentItemChanged.connect(self._选中变了)
        左排.addWidget(self.列表, 1)
        左钮 = QHBoxLayout()
        左钮.addWidget(酒馆样式.做按钮('新建一套', self._新建))
        左钮.addWidget(酒馆样式.做按钮('删除这套', self._删除))
        左排.addLayout(左钮)
        分.addWidget(左)

        # ── 右：表单 ──
        右 = QWidget()
        右排 = QVBoxLayout(右)
        右排.setContentsMargins(0, 0, 0, 0)
        self.表 = QFormLayout()
        表 = self.表
        表.setLabelAlignment(Qt.AlignRight)

        self.名称 = QLineEdit()
        self.名称.setPlaceholderText('非空，存多套时全靠它认人')
        self.协议 = QComboBox()
        for 名 in 酒馆接口.协议们:
            self.协议.addItem(协议名.get(名, 名), 名)
        self.地址 = QLineEdit()
        self.地址.setPlaceholderText('http://127.0.0.1:11434/v1')
        # 可编辑的下拉：手打照旧打得进，发现回来的那些在右边箭头里挑。
        # `NoInsert` 是**故意的**——不然手打的每个名字都会往列表里塞一份，
        # 打错一个字就在下拉里留一条永久的垃圾。
        self.模型 = QComboBox()
        self.模型.setEditable(True)
        self.模型.setInsertPolicy(QComboBox.NoInsert)
        self.模型.setPlaceholderText('qwen2.5:7b / claude-sonnet-5 / gemini-2.5-pro；本地填 GGUF 文件名')
        self.模型.setToolTip('可以直接打字，也可以按「发现模型」问一下这个地址上有哪些；'
                            'local 协议下「发现模型」就是数一遍 酒馆数据/模型/ 里的 GGUF')
        模型排 = QHBoxLayout()
        模型排.setContentsMargins(0, 0, 0, 0)
        模型排.addWidget(self.模型, 1)
        self.找 = 酒馆样式.做按钮('发现模型', self._找模型,
                               '拿上面填的协议/地址/密钥问一句它有哪些模型')
        模型排.addWidget(self.找)
        self.管模型 = 酒馆样式.做按钮('管理模型…', self._管模型,
                                  '下载 / 删除本地 GGUF 模型（仅 local 协议用得着）')
        模型排.addWidget(self.管模型)
        # ⚠ **整行要包进 QWidget 容器。** `QFormLayout` 藏不了一行裸布局，
        # local 协议下「密钥」这行没意义要整行隐掉，只能 `容器.hide()` +
        # 把它的 label 也藏了。模型行同理（要跟着藏「管理模型…」的显隐）。
        self.模型盒 = QWidget()
        self.模型盒.setLayout(模型排)

        密钥排 = QHBoxLayout()
        密钥排.setContentsMargins(0, 0, 0, 0)
        self.密钥 = QLineEdit()
        self.密钥.setEchoMode(QLineEdit.Password)
        self.密钥.setPlaceholderText('sk-…（本地 ollama 之类可以留空）')
        看 = 酒馆样式.做按钮('看', self._翻密钥, '按住看明文——这块是明文存的')
        看.setCheckable(True)
        密钥排.addWidget(self.密钥, 1)
        密钥排.addWidget(看)
        self.密钥盒 = QWidget()
        self.密钥盒.setLayout(密钥排)

        self.格式 = QComboBox()
        for 名 in 工具格式们:
            self.格式.addItem(名, 名)
        self.格式.setToolTip(
            '这个模型把工具调用吐成**哪种形状**。⚠ **按模型选，不是按协议选。**\n'
            '\n'
            '实测：qwen2.5 系回 JSON（{"name":…,"arguments":{…}}），\n'
            'Qwen3 系回 **XML**（<function=工具名><parameter=参数名>值</parameter>）。\n'
            '为什么会不一样：**模型聊天模板里教的是哪套，它就吐哪套** —— '
            '我们下发的 tools= 只是原料，渲染成什么形状是模板说了算。\n'
            '\n'
            '⚠ 选它**只影响提示词怎么教它**（以及"自动"=不特别教）。\n'
            '**解析那边两种都认，故意不按这一格收紧** —— 认多一种没有代价，'
            '认少一种就是整轮白费。\n'
            '\n'
            '现在只有「编程助手」用得上它，而助手只跑本地模型。')

        表.addRow('名称', self.名称)
        # ⚠ **这一格决定"这套只出现在哪一边"。** 聊天和编程助手是两条独立的
        # 配置路线：给 agent 调成 0.2 的温度（别让它绕路）不该把聊天也带走。
        # 同一个本地模型想在两边都用、但参数不同 —— 那就建两套，模型填一样的。
        self.用途 = QComboBox()
        for 名 in 酒馆接口.用途们:
            self.用途.addItem(名, 名)
        self.用途.setToolTip(
            '这套配置给谁用：\n'
            '  · 对话 —— 只出现在聊天那个接口下拉里\n'
            '  · agent —— 只出现在编程助手那个下拉里\n\n'
            '两边的采样参数互不影响（agent 建议温度 0.2，聊天照旧）。\n'
            '⚠ 「新建一套」建出来的是**当前左边正在看的那一组**。')
        表.addRow('给谁用', self.用途)
        表.addRow('协议', self.协议)
        表.addRow('地址', self.地址)
        表.addRow('密钥', self.密钥盒)
        表.addRow('模型', self.模型盒)
        表.addRow('工具格式', self.格式)

        参排 = QHBoxLayout()
        self.温度 = QDoubleSpinBox()
        self.温度.setRange(-1.0, 2.0)
        self.温度.setSingleStep(0.05)
        self.温度.setDecimals(2)
        self.温度.setSpecialValueText('不设')
        self.温度.setValue(-1.0)          # -1 = 不发这个参数，让对面用自己的默认
        self.温度.setToolTip('不设的话就不发这个参数，交给服务端默认')
        self.上限 = QSpinBox()
        self.上限.setRange(0, 1000000)
        self.上限.setSpecialValueText('不设')
        self.上限.setValue(0)
        self.上限.setToolTip('Anthropic 必填；留「不设」会按 4096 走')
        参排.addWidget(QLabel('temperature'))
        参排.addWidget(self.温度)
        参排.addSpacing(12)
        参排.addWidget(QLabel('max_tokens'))
        参排.addWidget(self.上限)
        参排.addStretch(1)
        表.addRow('采样', 参排)

        # ── 仅 local 协议可见的两行 ──
        # top_k / repeat_penalty 是角色扮演实际会拧的两个旋钮（控发散、控
        # 复读），值得上 UI；top_p 不进——它和 temperature/top_k 功能重叠，
        # 四个全摆上来这行就挤爆了，真要用还可以手写 JSON / 会话预设。
        本地排1 = QHBoxLayout()
        本地排1.setContentsMargins(0, 0, 0, 0)
        self.上下文数 = QSpinBox()
        self.上下文数.setRange(512, 131072)
        self.上下文数.setSingleStep(512)
        # ⚠ **空白表单的默认值是 8192，不是 4096。** 4096 在本地模型上太小
        # （系统提示 + 一次生成就占掉一半，历史基本放不下）。
        self.上下文数.setValue(8192)
        self.上下文数.setToolTip('n_ctx：模型一次能看到的总长度（提示+生成）。'
                                '本地建议 8192 起，干长活（编程助手）建议 16384。\n'
                                '别开太大——KV 占显存，装不下会退回 CPU 跑，反而慢十倍')
        self.显卡层数 = QSpinBox()
        self.显卡层数.setRange(-1, 999)
        # ⚠ **空白表单的默认值是 -1（全上 GPU），不是 0（纯 CPU）。**
        # 这里踩过：0 是"一层都不上显卡"，而**新建出来根本没人会想到去动这一格**
        # —— 于是新配的本地模型一直是 CPU 在跑，每秒三五个字，用户只会觉得
        # "本地模型就是慢"。真正的默认必须是"装得下就全上"。
        self.显卡层数.setValue(-1)
        self.显卡层数.setSpecialValueText('全上 GPU')
        self.显卡层数.setToolTip('n_gpu_layers：多少层放上显卡。0 = 纯 CPU；'
                                '-1（全上 GPU）= 装得下就全上。'
                                'CPU 版 llama-cpp 这个设了也没用')
        本地排1.addWidget(QLabel('n_ctx'))
        本地排1.addWidget(self.上下文数)
        本地排1.addSpacing(12)
        本地排1.addWidget(QLabel('GPU 层数'))
        本地排1.addWidget(self.显卡层数)
        本地排1.addStretch(1)
        self.本地盒1 = QWidget()
        self.本地盒1.setLayout(本地排1)
        表.addRow('本地', self.本地盒1)

        本地排2 = QHBoxLayout()
        本地排2.setContentsMargins(0, 0, 0, 0)
        self.顶K = QSpinBox()
        self.顶K.setRange(0, 200)
        self.顶K.setSpecialValueText('不设')
        self.顶K.setValue(0)
        self.顶K.setToolTip('top_k：每步只在最好的 K 个候选里挑。常用 20–50')
        self.复读罚 = QDoubleSpinBox()
        self.复读罚.setRange(0.0, 2.0)
        self.复读罚.setSingleStep(0.01)
        self.复读罚.setDecimals(2)
        self.复读罚.setSpecialValueText('不设')
        self.复读罚.setValue(0.0)
        self.复读罚.setToolTip('repeat_penalty：压重复。1.0 = 不罚，角色扮演'
                              '常用 1.05–1.15；太大会说话颠三倒四')
        本地排2.addWidget(QLabel('top_k'))
        本地排2.addWidget(self.顶K)
        本地排2.addSpacing(12)
        本地排2.addWidget(QLabel('repeat_penalty'))
        本地排2.addWidget(self.复读罚)
        本地排2.addStretch(1)
        # 一键填角色扮演常用的采样值。放在这一行是因为它改的四个旋钮里
        # 有两个就在这行；而这行本身**只在 local 下可见**（`_按协议变表单`
        # 会连 `本地盒2` 一起 hide），按钮自动跟着只对本地露出。
        self.推荐 = 酒馆样式.做按钮(
            '角色扮演推荐', self._填推荐,
            '把 temperature / max_tokens / top_k / repeat_penalty 调成角色扮演'
            '常用的值：0.8 / 1024 / 40 / 1.10。\n\n'
            '⚠ **顺手把「GPU 层数」拉成 -1（全上 GPU）。**\n'
            '这一格要是 0，模型是**纯 CPU** 在跑，每秒三五个字 —— '
            '而新建出来根本没人会想到去动它，所以一键填的时候必须带上。\n'
            '⚠ **不动 n_ctx** —— 那是"留多长的历史"，是取舍不是对错，'
            '自己按机器和用法定（本地建议 8192 起）。', self)
        本地排2.addWidget(self.推荐)
        # agent 那套的推荐值跟角色扮演**不是一回事**：角色扮演要它"有味道"，
        # agent 要它"照做、别绕路、别写散文"。温度 0.8 的 agent 会反复解释
        # 打算干什么、换个说法重来，那些全是白等的秒数。
        self.推荐agent = 酒馆样式.做按钮(
            'agent 推荐', self._填agent推荐,
            '把 temperature / top_k / repeat_penalty 调成干代码活常用的值：'
            '0.2 / 20 / 1.05。\n\n'
            '⚠ **顺手把「GPU 层数」拉成 -1（全上 GPU）** —— 这一格是 0 的话，'
            '9B 跑起来每秒三五个字，比全上卡慢五到十倍。\n'
            '⚠ **不动 max_tokens** —— 编程助手那一步的输出上限由「设置…」里的'
            '「单步输出」说了算，这一格它不看。\n'
            '⚠ **不动 n_ctx** —— 但 agent 干长活吃上下文，本地模型记得调到 '
            '8192 以上，8G 显卡建议 16384。',
            self)
        本地排2.addWidget(self.推荐agent)
        self.本地盒2 = QWidget()
        self.本地盒2.setLayout(本地排2)
        表.addRow('采样·本地', self.本地盒2)
        右排.addLayout(表)

        self.本地警告 = QLabel('本地引擎没装：pip install llama-cpp-python，'
                             '装完重启程序。')
        self.本地警告.setWordWrap(True)
        self.本地警告.setStyleSheet('color: %s;' % 酒馆样式.配色()['danger'])
        self.本地警告.hide()
        右排.addWidget(self.本地警告)

        self._附加头标 = QLabel('附加头（JSON。一般留空；有些服务要额外的鉴权头）')
        右排.addWidget(self._附加头标)
        self.附加头 = QPlainTextEdit('{}')
        self.附加头.setMaximumHeight(72)
        右排.addWidget(self.附加头)

        self.报错 = QLabel()
        self.报错.setWordWrap(True)
        self.报错.setStyleSheet('color: %s;' % 酒馆样式.配色()['danger'])
        self.报错.hide()
        右排.addWidget(self.报错)

        右钮 = QHBoxLayout()
        self.保存 = 酒馆样式.做按钮('保存', self._保存)
        右钮.addWidget(self.保存)
        右钮.addStretch(1)
        关 = 酒馆样式.做按钮('关闭', self.reject)
        右钮.addWidget(关)
        右排.addLayout(右钮)
        分.addWidget(右)
        分.setSizes([220, 540])

        self.协议.currentIndexChanged.connect(self._按协议变表单)
        self._按协议变表单()
        self.刷新(选中编号)

    # ── 读 ──

    def _看哪一组(self):
        """左边现在看的是哪一组（`'对话'` / `'agent'`）。见 `看组` 那一格。"""
        return self.看组.currentData() or 酒馆接口.用途们[0]

    def 刷新(self, 要选的=None):
        要选的 = self._编号 if 要选的 is None else 要选的
        组 = self._看哪一组()

        def 回来(行们, 错):
            if 错:
                QMessageBox.warning(self, '读接口配置失败', 错)
                return
            # ⚠ 这些**没有密钥**。只能拿来画列表，不能拿来填表单。
            # 而且 `酒馆接口.列` 回来的已经是 `接口配置` 对象了，不能再套
            # 一层 `从行`——那会拿一个对象去 `in` 一个字符串，当场 TypeError。
            self._行们 = list(行们 or [])
            self.列表.blockSignals(True)
            self.列表.clear()
            for 套 in self._行们:
                # ⚠ **老数据（`''`）要标出来。** 它是加「用途」这一列之前建的，
                # 两边都能用、两边都看得见。不标的话用户在两个组里都看到同一套，
                # 会以为"怎么删不掉"或者"我明明改过归属了"。
                旧 = '' if 套.归谁() else '   ← 旧配置：两边都能用'
                项 = QListWidgetItem('%s\n%s%s'
                                  % (套.名称 or '（没名字）',
                                     套.协议 or '', 旧))
                项.setData(Qt.UserRole, 套.编号)
                self.列表.addItem(项)
            self.列表.blockSignals(False)

            if not self._行们:
                self._清表单()
                self._编号 = 0
                self._整条 = None
                self._可改(False)
                return
            # 原来选的那套还在就选它，不在就选第一套
            找 = 要选的
            if not any(套.编号 == 找 for 套 in self._行们):
                找 = self._行们[0].编号
            for i in range(self.列表.count()):
                if self.列表.item(i).data(Qt.UserRole) == 找:
                    self.列表.setCurrentRow(i)
                    break

        self._工位.干(lambda 库, g=组: 酒馆接口.列(库, g), 回来)

    def _选中变了(self, 现在, _前):
        if 现在 is None:
            return
        编号 = 现在.data(Qt.UserRole)
        self._编号 = 编号
        self.选中的 = 编号
        # ⚠ **重新 `取()` 整条。** 列表行是 `清单列` 来的，不含 `密钥`，
        # 直接拿它填表单再保存就是把密钥清空。见文件开头那条。
        self._可改(False)

        def 回来(整条, 错):
            if 错:
                QMessageBox.warning(self, '读这套配置失败', 错)
                return
            if 整条 is None:
                self.刷新()
                return
            self._整条 = 整条
            self._填表单(整条)
            self._可改(True)

        self._工位.干(lambda 库, n=编号: 酒馆接口.取(库, n), 回来)

    # ── 表单 ──

    def _按协议变表单(self, *_):
        """
        跟着协议下拉把表单变形成该有的样子。**每次选协议、填表单、清表单
        之后都要过一遍**——不然从一套 local 切到一套 openai，表单会留着
        上一套的形状。
        """
        本地 = (self.协议.currentData() == 'local')
        for 件 in (self.地址, self.密钥盒):
            件.setVisible(not 本地)
            标 = self.表.labelForField(件)
            if 标 is not None:
                标.setVisible(not 本地)
        self.附加头.setVisible(not 本地)
        self._附加头标.setVisible(not 本地)
        for 件 in (self.本地盒1, self.本地盒2, self.管模型):
            件.setVisible(本地)
        for 件 in (self.本地盒1, self.本地盒2):
            标 = self.表.labelForField(件)
            if 标 is not None:
                标.setVisible(本地)
        # llama_cpp 没装时红字常驻——只提示，不拦着配（配好了装完就能用）
        self.本地警告.setVisible(本地 and not 酒馆本地.可用())

    def _管模型(self):
        """开「本地模型」管理框。回来后本地列表可能变了，重灌模型下拉。"""
        import 酒馆模型页
        框 = 酒馆模型页.模型页(self._工位, self)
        框.exec()
        if 框.变过了 and self.协议.currentData() == 'local':
            原 = self.模型.currentText()
            self.模型.clear()
            self.模型.addItems([名 for 名, _大 in 酒馆本地.列本地()])
            self.模型.setCurrentText(原)

    def _清表单(self):
        self.名称.setText('')
        self.地址.setText('')
        # 下拉框没有 `setText`，是 `setCurrentText`——已经填好的那些
        # **不打字也能清掉**，这一格允许为空（「发现模型」就是不知道才用的）
        self.模型.setCurrentText('')
        self.密钥.setText('')
        # ⚠ 归属回**当前左边正在看的那一组**，不是固定的"对话" ——
        # 在 agent 那组点「新建一套」，建出来就该是 agent 的。
        位 = self.用途.findData(self._看哪一组())
        self.用途.setCurrentIndex(位 if 位 >= 0 else 0)
        self.协议.setCurrentIndex(0)
        self.格式.setCurrentIndex(0)      # 0 = 自动
        self.温度.setValue(-1.0)
        self.上限.setValue(0)
        self.上下文数.setValue(8192)
        # ⚠ 空白表单给的是**全上 GPU**（-1），不是 0（纯 CPU）。见这两格
        # 建控件那儿的注释 —— 0 会让新配的本地模型一直用 CPU 跑。
        self.显卡层数.setValue(-1)
        self.顶K.setValue(0)
        self.复读罚.setValue(0.0)
        self.附加头.setPlainText('{}')
        self.报错.hide()
        self._按协议变表单()

    def _填推荐(self):
        """
        一键填角色扮演常用的采样参数。**只调这四个**：

            temperature      0.8    甜区在 0.75~0.9：太低说话干巴、来回复读，
                                    太高（>1.0）开始胡言乱语、人格飘。
            max_tokens      1024    一段像样回复够用，同时压着模型别灌水
                                    （配合提示词里"一次回复别太长"那条）。
                                    ⚠ **别给太小**：R1 这类推理模型先吐几百
                                    个 token 的思考链，`滤思考` 又把思考全滤
                                    掉——给 512 的话思考还没结束就被截断，
                                    气泡里一个字都剩不下。
            top_k             40    常用区间 20~50 的中值：收窄采样空间、
                                    又不至于每次挑同一个词。
            repeat_penalty  1.10    常用区间 1.05~1.15 的中值：压住复读，
                                    又不至于把正常词组罚得说不利索。

        ⚠ **不碰 n_ctx / GPU 层数。** 那两个是"跑不跑得动、上不上显卡"，
        跟扮演风格无关；一键顺带把显存配置也改了，对用户是意外副作用。

        ⚠ **2026-09-19 改了口径：GPU 层数现在顺手拉成 -1。** 原因是实测踩到：
        空白表单的默认值是 `0 = 纯 CPU`，而**没人会想到去动那一格** ——
        于是新建出来的本地配置一直用 CPU 跑 9B（每秒三五个字），用户只会觉得
        "本地模型就是慢"。这不是"风格参数"，是"能不能用"的门槛，必须带上。
        `n_ctx` 仍然不动 —— 那是"留多长历史"的取舍，不是对错。
        """
        self.温度.setValue(0.8)
        self.上限.setValue(1024)
        self.顶K.setValue(40)
        self.复读罚.setValue(1.10)
        # ⚠ 顺手把显卡拉满，见 docstring 里 2026-09-19 那条。
        self.显卡层数.setValue(-1)

    def _填agent推荐(self):
        """
        一键填**编程助手**常用的采样值。跟 `_填推荐`（角色扮演那套）是两套值，
        因为两边要的东西不一样：

            temperature      0.2     **这条最要紧。** 角色扮演要 0.8（说话有味道），
                                    可 agent 要的是"照做"：温度高了它会反复解释
                                    打算干什么、换个说法重来、自己发挥 —— 全是
                                    白等的秒数（实测每一步都要重付一次 prefill）。
            top_k             20    候选收窄，更收敛。
            repeat_penalty  1.05    略压重复。**比角色扮演那档低** —— 罚太狠
                                    模型会为了躲开重复词而绕路，反而更慢。

        ⚠ **不动 `max_tokens`。** 编程助手那一步的输出上限由「设置…」里的
        「单步输出」说了算（`跑一轮` 显式盖掉），这一格它**根本不看** ——
        填了只会让人以为它有用。

        ⚠ **不动 `n_ctx`**，跟 `_填推荐` 一个道理：那是"留多长历史"的取舍，
        不是对错。但这一格对 agent 比采样参数还要命（转录本预算全看它），
        所以按钮的提示里点了名，让用户自己按机器调。

        ⚠ **顺手把 `GPU 层数` 拉成 -1（全上 GPU）** —— 跟 `_填推荐` 一样，
        见那儿的 docstring。**这一条对 agent 尤其要命**：编程助手每一步都要
        全量重算提示词，纯 CPU 跑 9B 是每秒三五个字，一个任务能拖到十几分钟。
        """
        self.温度.setValue(0.2)
        self.顶K.setValue(20)
        self.复读罚.setValue(1.05)
        self.显卡层数.setValue(-1)

    def _填表单(self, 套):
        self.名称.setText(套.名称 or '')
        # `归谁()` 把认不出来的值（手打的、老数据没有这一列）归一成 `''`。
        # ⚠ `''` 是"老数据"，**下拉里没有这一项** —— 这时候先按当前看的这一组
        # 显示，用户不改它、直接保存的话就会归到这一组（那正是他想要的）。
        归 = 套.归谁() or self._看哪一组()
        位置 = self.用途.findData(归)
        self.用途.setCurrentIndex(位置 if 位置 >= 0 else 0)
        位置 = self.协议.findData(套.协议)
        self.协议.setCurrentIndex(位置 if 位置 >= 0 else 0)
        # `式样()` 会把认不出来的值（手打的、老数据没有这一列）归一成「自动」，
        # 所以这儿不用再判一次
        位置 = self.格式.findData(套.式样())
        self.格式.setCurrentIndex(位置 if 位置 >= 0 else 0)
        self.地址.setText(套.地址 or '')
        self.模型.setCurrentText(套.模型 or '')
        self.密钥.setText(套.密钥 or '')
        参 = 套.参数()
        # 「不设」用 -1 / 0 表示：这样"没配过"和"配成 0"能分开，
        # 合并参数那边也只覆盖真正写过的键
        self.温度.setValue(float(参['temperature'])
                         if 参.get('temperature') is not None else -1.0)
        self.上限.setValue(int(参['max_tokens'])
                         if 参.get('max_tokens') is not None else 0)
        # ⚠ 这两格**没配过**时的兜底值也是 8192 / -1（跟空白表单一致）。
        # ⚠ 但**配过 0 的照样回填 0** —— `is not None` 这一步不能省，
        # 不然"我故意用 CPU 跑"会被界面悄悄改成全上卡。
        self.上下文数.setValue(int(参['n_ctx'])
                             if 参.get('n_ctx') is not None else 8192)
        self.显卡层数.setValue(int(参['n_gpu_layers'])
                             if 参.get('n_gpu_layers') is not None else -1)
        self.顶K.setValue(int(参['top_k'])
                        if 参.get('top_k') is not None else 0)
        self.复读罚.setValue(float(参['repeat_penalty'])
                           if 参.get('repeat_penalty') is not None else 0.0)
        self.附加头.setPlainText(套.附加头 or '{}')
        self.报错.hide()
        self._按协议变表单()

    def _读数(self):
        """表单 → 建/改用的参数字典。**校验不过就抛 `酒馆错误`。**"""
        名称 = self.名称.text().strip()
        if not 名称:
            raise 酒馆错误('名称不能空——存多套的时候全靠它认人。')
        参 = {}
        if self.温度.value() >= 0:
            参['temperature'] = round(float(self.温度.value()), 3)
        if self.上限.value() > 0:
            参['max_tokens'] = int(self.上限.value())
        # ⚠ **本地键只在 local 时写。** `n_ctx` / `n_gpu_layers` 这类键混进
        # HTTP 协议的 `采样参数` 里，会被 `_备openai` 整份塞进请求体发给
        # 对端（`酒馆大脑.本地专属参数` 那道过滤是兜底，这里是源头）。
        if self.协议.currentData() == 'local':
            参['n_ctx'] = int(self.上下文数.value())
            参['n_gpu_layers'] = int(self.显卡层数.value())
            if self.顶K.value() > 0:
                参['top_k'] = int(self.顶K.value())
            if self.复读罚.value() > 0:
                参['repeat_penalty'] = round(float(self.复读罚.value()), 3)
        头文 = (self.附加头.toPlainText() or '').strip() or '{}'
        # ⚠ **不能用 `酒馆模型.读JSON` 来验。** 那个的约定是"坏掉就给默认值、
        # 不抛"——那是给读库用的（库里存着旧版本写的东西，读不出来也得能开起来）。
        # 用在这儿就等于**把用户填错的 JSON 静默换成 `{}`**，他改的东西没了
        # 还什么提示都没有。这里要的是照直报错，所以自己 `json.loads`。
        try:
            头 = json.loads(头文)
        except ValueError as 错:
            raise 酒馆错误('附加头不是合法 JSON：%s' % 错)
        if not isinstance(头, dict):
            raise 酒馆错误('附加头得是一个 JSON 对象，比如 `{}` 或者 '
                           '`{"X-Api-Version": "1"}`，不能是列表或数字。')
        return {'名称': 名称, '协议': self.协议.currentData(),
                '用途': self.用途.currentData() or '对话',
                '地址': self.地址.text().strip(), '密钥': self.密钥.text(),
                '模型': self.模型.currentText().strip(),
                '工具格式': self.格式.currentData() or '自动',
                '采样参数': 写JSON(参), '附加头': 写JSON(头)}

    def _可改(self, 行不行):
        # 「发现模型」按 `行不行 and not self._问着` 开合，理由见 `_找模型`：
        # 一次请求在路上时不该能点第二下，而请求回来时又要按当时的表单状态
        # 恢复，所以结果那头统一叫回这里，不自己 `setEnabled(True)`。
        self._能动 = 行不行
        for 件 in (self.名称, self.用途, self.协议, self.地址, self.模型, self.密钥,
                   self.格式, self.温度, self.上限, self.上下文数,
                   self.显卡层数, self.顶K, self.复读罚, self.推荐,
                   self.推荐agent, self.附加头,
                   self.管模型, self.保存):
            件.setEnabled(行不行)
        self.找.setEnabled(行不行 and not self._问着)

    def _答(self, 文字, 坏=False):
        """
        表单底下那行字。**报错是红的，平常消息是灰的。**

        "问到 12 个模型"这种话再弹成红的，就成了"一按按钮就出一行红字"，
        看着像又出事了——所以颜色得分开。
        """
        self.报错.setStyleSheet('color: %s;' % (
            酒馆样式.配色()['danger'] if 坏
            else 酒馆样式.配色()['text_secondary']))
        self.报错.setText(文字)
        self.报错.show()

    def _报(self, 文字):
        self._答(文字, 坏=True)

    def _翻密钥(self):
        看 = self.sender()
        self.密钥.setEchoMode(QLineEdit.Normal if (看 and 看.isChecked())
                            else QLineEdit.Password)

    # ── 发现模型 ──

    def _找模型(self):
        """
        问这套地址上有哪些模型，灌进「模型」那个下拉里。

        **拿的是表单里现填的东西，不是库里存着的那条**——所以还没保存、甚至
        还没新建过一套，都能先问一句再决定模型写什么。这也正是它存在的理由：
        「模型」那格空着的时候，人根本不知道该写什么。

        ⚠ **不能在这个线程里直接问。** `酒馆大脑.列模型` 是一条阻塞的
        `requests.get`，地址不通要等满 30 秒；在主线程上等就是**界面整个卡死**
        30 秒，连「关闭」都点不动（没有事件循环在转）。所以丢给一条一次性
        线程，理由和形状见 `找模型的`。
        """
        if self._问着:
            return                      # 按钮已经禁了，这是防连点
        试 = 接口配置(协议=self.协议.currentData(),
                      地址=self.地址.text().strip(),
                      密钥=self.密钥.text(),
                      附加头=self.附加头.toPlainText() or '{}')

        # ⚠ **这个对象由这条线程自己攥着**（就是闭包里的 `器`），**不能挂到
        # `self` 上**。挂上去的话框一关它就跟着销毁，线程回来喊人时 `emit`
        # 打在已经删掉的 C++ 对象上——PySide 那边是抛 `RuntimeError`，
        # 而且是在一条没人接的线程里抛。攥在线程手里就总比这次请求活得久。
        器 = 找模型的()

        def 跑():
            try:
                名们 = 酒馆大脑.列模型(试)
            except (酒馆大脑.错配置, 酒馆大脑.错模型) as 错:
                # 这两类的文案本来就是写给人看的（"地址还空着——…"），
                # 再糊一层类名上去只是噪音
                器.坏了.emit(str(错))
            except Exception as 错:
                器.坏了.emit('%s：%s' % (type(错).__name__, 错))
            else:
                器.好了.emit(名们)

        self._问着 = True
        self._可改(self._能动)          # 重算一遍，这一下会把「发现模型」禁掉
        self._答('正在问 %s …' % (试.地址 or '（地址还没填）'))
        器.好了.connect(self._收到模型)
        器.坏了.connect(self._收模型错)
        threading.Thread(target=跑, daemon=True).start()

    def _收到模型(self, 名们):
        self._问着 = False
        self._可改(self._能动)          # 按钮按当时的表单状态恢复，不是硬开
        原 = self.模型.currentText()
        self.模型.clear()
        self.模型.addItems(名们)
        # **原来填着的那个要留着。** 发现一趟不该把人已经填好的东西冲掉
        # （比如他手打了一个这服务不列出来的自定义名）。
        self.模型.setCurrentText(原)
        if not 原:
            # 空着的就默认选中第一个——按这个按钮的人本来就是不知道填什么
            self.模型.setCurrentIndex(0)
        self._答('问到 %d 个模型，点「模型」右边那个箭头挑一个。' % len(名们))

    def _收模型错(self, 错):
        self._问着 = False
        self._可改(self._能动)
        self._报(错)

    # ── 写 ──

    def _新建(self):
        self.列表.clearSelection()
        self._编号 = 0
        self._整条 = None
        self._清表单()
        self._可改(True)
        self.名称.setFocus()

    def _保存(self):
        try:
            字段 = self._读数()
        except Exception as 错:
            # 校验错显示在框内，**不关框**——关掉的话用户填的一屏就没了
            self._报(str(错))
            return

        # 保存前先用 `酒馆大脑` 那道校验过一眼：缺地址缺模型这种，
        # 早点说比等到真发请求时收一个莫名其妙的 400 强。
        试 = 接口配置(名称=字段['名称'], 协议=字段['协议'],
                      地址=字段['地址'], 模型=字段['模型'])
        缺 = 酒馆大脑.校验配置(试)

        if self._编号:
            def 回来(_值, 错):
                if 错:
                    self._报(str(错))
                    return
                self.改过了 = True
                self.刷新(self._编号)
                if 缺:
                    self._报('存下了，但还发不出去，缺：%s' % '、'.join(缺))

            self._工位.干(lambda 库, n=self._编号, f=字段:
                        酒馆接口.改(库, n, **f), 回来)
        else:
            def 回来(新, 错):
                if 错:
                    self._报(str(错))
                    return
                self.改过了 = True
                self.选中的 = 新.编号
                self.刷新(新.编号)
                if 缺:
                    self._报('存下了，但还发不出去，缺：%s' % '、'.join(缺))

            self._工位.干(lambda 库, f=字段: 酒馆接口.建(库, **f), 回来)

    def _删除(self):
        if not self._编号:
            QMessageBox.information(self, '先选一套', '上面列表里先点一套。')
            return
        名 = self.名称.text() or '（没名字）'
        if QMessageBox.question(self, '删掉这套配置？',
                                '「%s」会被删掉。已经用了它的会话会退回到默认那套。'
                                % 名,
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        编号 = self._编号

        def 回来(_值, 错):
            if 错:
                self._报(str(错))
                return
            self.改过了 = True
            self._编号 = 0
            self._整条 = None
            self.刷新(0)

        self._工位.干(lambda 库, n=编号: 酒馆接口.删(库, n), 回来)
