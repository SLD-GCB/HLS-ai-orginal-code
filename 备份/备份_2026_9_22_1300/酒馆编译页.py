#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆编译页.py — 「重编本地模型框架」的框

```
┌────────────────────────────────────────────────────────────────────────┐
│ 编译环境：在  D:\…\编译环境（1.3 GB，12335 个文件）                     │
│   CUDA 13.3 / MSVC …\14.44.35207 / Win SDK 10.0.26100.0 / Python 3.13.2 │
│ 现在这套：CUDA 版（ggml-cuda.dll 在），  62 MB，  2026-09-19 08:25       │
│ 编译目标：sm [120]   本机显卡：NVIDIA GeForce RTX 5050 Laptop GPU       │
│ [开始编译] [停止]   还原上一份 [2026-09-21 20:33 ▾] [还原]              │
│ ┌────────────────────────────────────────────────────────────────────┐ │
│ │ [ 1/…] Building CUDA object …                                      │ │
│ │ …                                                                  │ │
│ └────────────────────────────────────────────────────────────────────┘ │
│ 状态：就绪                                                             │
└────────────────────────────────────────────────────────────────────────┘
```

**为什么要这个东西**：一份 CUDA 版框架只认一种显卡架构（包里的那份是 sm_120 /
RTX 50 系）。别的卡想吃 GPU，就得**在那台机器上重编一次**，而重编要一整套
VS + Win SDK + CUDA 工具链 —— 那些机器上多半没有。所以：工具链跟着包走
（`编译环境/`，由 `打包.py` 的精简表铺出来），这个框负责"用包里的工具链，
给这块卡编一份，然后换上"。

⚠ **一次编译几十分钟，所以三件事必须做对**（都是这个框存在的理由）：
  1. **日志要实时滚** —— 不然用户不知道它是在编还是已经死了。编译输出一行一行
     往这里送（`酒馆编译.编译` 的 `行回调`），不是憋到最后一起给。
  2. **停止要真能停** —— 停的是 `pip → cmake → ninja → nvcc` **一整棵树**，
     见 `酒馆编译._杀树`。只杀 pip 的话后面几层会活下来接着编，下次构建时
     它们还占着文件。
  3. **按钮状态要说清** —— 在编的时候"开始编译"是灰的、"停止"是亮的；
     编完换装前还要说一句"**重启才生效**"（换装换的是下次启动读的那份）。

⚠ **换装在换之前会先试加载一遍**（`酒馆编译.装库` 里的第 4 步）：干净子进程
  `import` 一次，加载不起来就不换、现在这份动都不动。所以"点一下把程序搞死"
  这条路上最关键的那道闸在这儿，不在这个框里。

⚠ **线程形状照抄 `酒馆模型页`**：一次性任务用 daemon `threading.Thread` +
  主线程建的 `QObject` 信号嘴，不用 `QThread`（那份注释里有完整理由）。
  信号嘴由线程自己攥着（闭包里的 `器`），不挂 `self` —— 挂上去框一关它就销毁，
  线程回来喊人时 `emit` 打在死掉的 C++ 对象上。
"""

import os
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QSpinBox, QVBoxLayout)

import 酒馆编译
import 酒馆样式

__all__ = ['编译页']

#: 日志区最多留多少行。一次 CUDA 编译能刷出几万行，不设上限就是让它慢慢吃内存。
日志上限 = 4000

#: 状态行多久刷一次"还在编吗"（顺便把已跑时长报出来）。
心跳毫秒 = 1000


class 干活的(QObject):
    """
    编译那条线程的**信号嘴**。见文件头最后那条（形状照抄 `酒馆模型页.干活的`）。

    `来了一行` 会送很多次（编译输出逐行），其他两个各送一次。
    """

    来了一行 = Signal(str)
    好了 = Signal(str)          # 一句人话：换装结果
    坏了 = Signal(str)          # 一句人话：哪儿出的问题


class 编译页(QDialog):
    """
    「重编本地模型框架」的框。**不动 `依赖库/` 里那两套 DLL 的机制** ——
    它只负责把 `lib/`（CUDA 那套）换成新编的一份，CPU 兜底那套一个字不碰。
    """

    def __init__(self, 父=None):
        super().__init__(父)
        self._在跑 = False
        self._停旗 = None
        self._起于 = 0.0
        self._行数 = 0
        self._填过架构 = False          # 用户自己动过那格没有（动过就别再拿探测值盖）

        self.setWindowTitle('重编本地框架')
        self.resize(880, 620)
        外 = QVBoxLayout(self)

        # ── 环境状态 ──
        self.环境标 = QLabel(self)
        self.环境标.setWordWrap(True)
        外.addWidget(self.环境标)
        self.来源标 = 酒馆样式.灰色小字('', self)
        self.来源标.setWordWrap(True)
        外.addWidget(self.来源标)
        self.库标 = QLabel(self)
        self.库标.setWordWrap(True)
        外.addWidget(self.库标)

        # ── 目标架构 ──
        行 = QHBoxLayout()
        行.addWidget(QLabel('编译目标：sm', self))
        self.架构 = QSpinBox(self)
        self.架构.setRange(30, 999)
        self.架构.setFixedWidth(90)
        self.架构.setToolTip('显卡的 compute capability。本机探测到会自动填；'
                            '给别的机器编要拿到那张卡的机器上编。')
        self.架构.valueChanged.connect(self._架构动过)
        行.addWidget(self.架构)
        self.卡标 = QLabel('', self)
        行.addWidget(self.卡标)
        行.addStretch(1)
        外.addLayout(行)

        # ── 按钮 ──
        钮行 = QHBoxLayout()
        self.开始 = 酒馆样式.做按钮('开始编译', self._开始, '编一份 CUDA 版（几十分钟）', self)
        self.停止 = 酒馆样式.做按钮('停止', self._停, '杀掉整棵编译进程树', self)
        self.停止.setEnabled(False)
        钮行.addWidget(self.开始)
        钮行.addWidget(self.停止)
        钮行.addStretch(1)
        钮行.addWidget(QLabel('还原上一份：', self))
        self.备份 = QComboBox(self)
        self.备份.setMinimumWidth(190)
        钮行.addWidget(self.备份)
        self.还原钮 = 酒馆样式.做按钮('还原', self._还原, '把 lib/ 换回某一份旧备份', self)
        钮行.addWidget(self.还原钮)
        外.addLayout(钮行)

        # ── 日志 ──
        self.日志 = QPlainTextEdit(self)
        self.日志.setReadOnly(True)
        self.日志.setMaximumBlockCount(日志上限)
        self.日志.setPlaceholderText('编译输出会滚在这里。'
                                    '第一次编要几十分钟，让它跑着就行。')
        外.addWidget(self.日志, 1)

        # ── 状态行 ──
        self.状态 = QLabel('就绪。', self)
        self.状态.setWordWrap(True)
        外.addWidget(self.状态)

        self._心跳 = QTimer(self)
        self._心跳.setInterval(心跳毫秒)
        self._心跳.timeout.connect(self._跳一下)
        self._起于 = 0.0

        self._刷状态()

    # ── 刷新 ──────────────────────────────────────────────────────

    def _刷状态(self):
        """把"环境在不在、现在这套是什么、备份有哪些"重新问一遍填上。"""
        根 = 酒馆编译.环境目录()
        if 根 is None:
            self.环境标.setText('编译环境：**不在** —— 这个包里没带（打包时'
                              '「打编译环境」是关的），或者被人删了。')
            self.环境标.setStyleSheet('color: %s;' % 酒馆样式.配色()['danger'])
        else:
            齐, 缺 = 酒馆编译.齐不齐()
            大小 = _大小嘴(_目录大小(根))
            格 = 酒馆样式.配色()
            if 齐:
                self.环境标.setText('编译环境：在　%s（%s）' % (根, 大小))
                self.环境标.setStyleSheet('')
            else:
                self.环境标.setText('编译环境：不齐 —— 缺 %s' % '、'.join(缺))
                self.环境标.setStyleSheet('color: %s;' % 格['danger'])
            self.来源标.setText('　'.join('%s %s' % (名, 值) for 名, 值 in 酒馆编译.来源())
                             or '（来源.txt 不在，不知道这份工具链是从哪拷的）')
        self.开始.setEnabled(根 is not None and not self._在跑)

        库 = 酒馆编译.当前库()
        格 = 酒馆样式.配色()
        if not 库['在']:
            self.库标.setText('现在这套：找不到 lib/（依赖库/llama_cpp 不在了？）')
            self.库标.setStyleSheet('color: %s;' % 格['danger'])
        else:
            self.库标.setText('现在这套：%s，%s，%s'
                            % ('CUDA 版（ggml-cuda.dll 在）' if 库['有CUDA']
                               else '**不是 CUDA 版**（ggml-cuda.dll 不在）',
                               _大小嘴(库['大小']), 库['时间']))
            self.库标.setStyleSheet('' if 库['有CUDA'] else 'color: %s;' % 格['danger'])

        名, 架 = 酒馆编译.显卡()
        self.卡标.setText('本机显卡：%s' % 名 if 名 else
                        '本机显卡：没探测到（没装驱动，或者这台机器没有 N 卡）')
        # 探测到的架构只在**用户没自己动过那格**时盖上去 —— 他手动改成 89 之后，
        # 每次刷状态又给他拨回 120 是最烦的那种"贴心的自动"
        if 架 and not self._填过架构:
            self._自设 = True               # 见 `_架构动过`：这行不是用户动的
            self.架构.setValue(int(架))
            self._自设 = False

    def _架构动过(self, _值):
        """用户动过那格 —— 以后别再拿探测值盖他。"""
        if not getattr(self, '_自设', False):
            self._填过架构 = True

        self.备份.clear()
        for 名, 说明 in 酒馆编译.备份们():
            self.备份.addItem(说明, 名)
        self.还原钮.setEnabled(self.备份.count() > 0 and not self._在跑)
        if self.备份.count() == 0:
            self.备份.addItem('（还没有备份）', '')

    def _跳一下(self):
        if not self._在跑:
            return
        秒 = int(time.time() - self._起于)
        self.状态.setText('正在编译…… 已经跑了 %d 分 %d 秒（输出 %d 行）。'
                        '第一次编通常几十分钟，别关窗口；要停就点「停止」。'
                        % (秒 // 60, 秒 % 60, self._行数))

    # ── 编译 ──────────────────────────────────────────────────────

    def _开始(self):
        if self._在跑:
            return
        齐, 缺 = 酒馆编译.齐不齐()
        if not 齐:
            QMessageBox.warning(self, '编不了', '编译环境不齐：\n\n· '
                               + '\n· '.join(缺))
            return
        架 = str(self.架构.value())
        本机 = 酒馆编译.显卡()[1]
        if 本机 and 架 != 本机:
            if QMessageBox.question(
                    self, '确认',
                    '你要编 sm_%s，而这台机器上的卡是 sm_%s。\n\n'
                    '编出来的那份只能用在 sm_%s 那类卡上，在这台机器上跑不了'
                    '（会退回 CPU）。确定继续吗？' % (架, 本机, 架)) != QMessageBox.Yes:
                return

        器 = 干活的()
        self._停旗 = threading.Event()
        self._行数 = 0
        self._起于 = time.time()

        def 跑():
            try:
                轮, _日志 = 酒馆编译.编译(架, 行回调=器.来了一行.emit,
                                        停旗=self._停旗)
                器.来了一行.emit('编译完成，正在换装（换之前会先试加载一遍）…')
                话 = 酒馆编译.装库(轮)
            except Exception as 错:
                器.坏了.emit(str(错))
                return
            器.好了.emit(话)

        器.来了一行.connect(self._收行)
        器.好了.connect(self._收好)
        器.坏了.connect(self._收坏)

        self._在跑 = True
        self.开始.setEnabled(False)
        self.停止.setEnabled(True)
        self.还原钮.setEnabled(False)
        self.日志.clear()
        self.状态.setText('正在编译 sm_%s ……' % 架)
        self._心跳.start()
        threading.Thread(target=跑, daemon=True).start()

    def _停(self):
        """
        置旗 + 让整棵树被杀。**真能停** —— `酒馆编译._杀树` 走 `taskkill /T`，
        不是只 terminate 最外层那个 pip（那会留下 cmake / ninja / nvcc 接着编）。
        """
        if self._停旗 is not None:
            self._停旗.set()
        self.停止.setEnabled(False)
        self.状态.setText('正在停…… 会连 cmake / ninja / nvcc 一整棵进程树一起杀。')

    def _收行(self, 行):
        self._行数 += 1
        self.日志.appendPlainText(行)

    def _收好(self, 话):
        self._在跑 = False
        self._心跳.stop()
        self.停止.setEnabled(False)
        self.状态.setText(话.replace('\n', '　'))
        self._刷状态()
        QMessageBox.information(self, '编好了', 话)

    def _收坏(self, 话):
        self._在跑 = False
        self._心跳.stop()
        self.开始.setEnabled(True)
        self.停止.setEnabled(False)
        self.状态.setText(话.splitlines()[0] if 话 else '失败了。')
        self._刷状态()
        QMessageBox.warning(self, '没成', 话)

    # ── 还原 ──────────────────────────────────────────────────────

    def _还原(self):
        名 = self.备份.currentData()
        if not 名:
            return
        if QMessageBox.question(
                self, '还原',
                '把 lib/ 换回 %s 那份？\n\n换完要重启程序才生效。'
                % self.备份.currentText()) != QMessageBox.Yes:
            return
        try:
            话 = 酒馆编译.还原(名)
        except Exception as 错:
            QMessageBox.warning(self, '还原失败', str(错))
            return
        self.状态.setText(话.replace('\n', '　'))
        self._刷状态()
        QMessageBox.information(self, '还原好了', 话)

    # ── 关窗 ──────────────────────────────────────────────────────

    def closeEvent(self, 事):
        """
        编到一半关窗口要问一句。**不是"关了就不编了"** —— 线程是 daemon，
        窗口关掉它照样在跑，跑完还会自动换装。所以要说清楚，别让人以为关了就是停了。
        """
        if self._在跑:
            选 = QMessageBox.question(
                self, '还在编',
                '编译还在跑。关掉这个窗口**不会停下它**（编完照样会自动换装）。\n\n'
                '要停下吗？',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if 选 == QMessageBox.Yes and self._停旗 is not None:
                self._停旗.set()
        事.accept()


# ── 小工具（这一页自己用的）────────────────────────────────────────

def _目录大小(径):
    """目录一共多少字节。算不出来就当 0（这里只是显示用）。"""
    总 = 0
    for 根, _目们, 件们 in os.walk(径):
        for 件 in 件们:
            try:
                总 += os.path.getsize(os.path.join(根, 件))
            except OSError:
                pass
    return 总


def _大小嘴(字节):
    """字节数 → 「1.3 GB」/「812 MB」。跟 模型页 那个一个意思。"""
    for 单位 in ('B', 'KB', 'MB', 'GB'):
        if 字节 < 1024 or 单位 == 'GB':
            return '%.1f %s' % (字节, 单位)
        字节 /= 1024.0
    return ''


