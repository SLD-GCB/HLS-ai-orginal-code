#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆互联页.py — 「局域网互联」框

菜单「接口 → 局域网互联…」开它。这一页把本机变成一个**模型服务端**，
让同一个局域网里的安卓端「鸿胪寺」连上来聊天（电脑跑模型，手机当前端）。

```
┌──────────────────────────────────────────────────────────┐
│ 局域网互联（手机连电脑端鸿胪寺）                          │
│  状态：● 正在服务                                         │
│  端口 [8017]   口令 [••••••] [看]                         │
│  GPU 层数 [-1]  （-1 = 能上的层全塞 GPU）                  │
│                                                          │
│  手机端「接口配置」这样填：                               │
│    协议  honglu（连电脑端鸿胪寺）                         │
│    地址  http://192.168.2.214:8017      [复制]            │
│    密钥  就是上面的口令                                   │
│                                                          │
│  [启动服务] [保存设置]                                    │
│  ⚠ 首次启动 Windows 会弹防火墙提示，勾「专用网络」放行。  │
│  ┌────────────────────────────────────────────────────┐  │
│  │ 日志…                                              │  │
│  └────────────────────────────────────────────────────┘  │
│                                              [关闭]      │
└──────────────────────────────────────────────────────────┘
```

## 谁拥有服务对象

**不是这个框**，是主窗（`酒馆窗口._开互联`）。框只是"开一下关一下"的
操作面板 —— 用户点「关闭」把框收掉，**服务照跑**，手机那头不受影响。
所以服务对象的生死归主窗管（关程序时停掉，见 `酒馆窗口.关掉`）。

## 为什么改了端口/口令要重启服务

监听套接字是绑死在端口上的，改端口只能停了重绑；口令是每次请求现查的，
但为了不让用户以为"改了立刻生效"，运行中干脆把这两格锁住 —— 停一下再改，
清清楚楚。
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QProgressBar, QSpinBox,
                               QVBoxLayout, QWidget)

import 酒馆互联
import 酒馆样式

__all__ = ['互联页']


def _大小(字节):
    """字节 → 「1.2 GB」这种给人看的样子。"""
    try:
        字节 = float(字节 or 0)
    except (TypeError, ValueError):
        return '?'
    for 单位 in ('B', 'KB', 'MB', 'GB', 'TB'):
        if 字节 < 1024 or 单位 == 'TB':
            return '%.1f %s' % (字节, 单位)
        字节 /= 1024.0
    return ''


def _地址们(端口):
    """这台机器上手机能连的地址们。**隧道地址（主机:端口），不是 URL。** 没探到就回空。"""
    return ['%s:%d' % (址, 端口) for 址 in 酒馆互联.局域网地址们()]


class 互联页(QDialog):
    """
    「局域网互联」操作面板。服务对象由外面传进来（主窗持有），这个框不管生死。
    """

    def __init__(self, 服务, 父=None):
        super().__init__(父)
        self._服务 = 服务
        self.setWindowTitle('局域网互联（手机连电脑端鸿胪寺）')
        self.resize(620, 560)

        外 = QVBoxLayout(self)

        self._状态标 = QLabel('', self)
        外.addWidget(self._状态标)

        # ── 参数 ──
        表 = QGridLayout()
        self._端口 = QSpinBox(self)
        self._端口.setRange(1, 65535)
        self._端口.setValue(int(服务.端口))
        表.addWidget(QLabel('端口', self), 0, 0)
        表.addWidget(self._端口, 0, 1)

        self._口令 = QLineEdit(self)
        self._口令.setText(服务.口令)
        self._口令.setPlaceholderText('别留空 —— 手机靠它确认「对面就是你这台电脑」')
        self._看口令 = 酒馆样式.做按钮('看', self._切看口令, '显示 / 隐藏口令', self)
        口令行 = QHBoxLayout()
        口令行.addWidget(self._口令, 1)
        口令行.addWidget(self._看口令)
        口令盒 = QWidget(self)
        口令盒.setLayout(口令行)
        表.addWidget(QLabel('口令', self), 1, 0)
        表.addWidget(口令盒, 1, 1)

        self._gpu = QSpinBox(self)
        self._gpu.setRange(-1, 9999)
        self._gpu.setValue(int(服务.gpu层))
        表.addWidget(QLabel('GPU 层数', self), 2, 0)
        表.addWidget(self._gpu, 2, 1)
        外.addLayout(表)

        外.addWidget(酒馆样式.灰色小字(
            'GPU 层数：-1 = 能塞进显存的全塞 GPU；0 = 纯 CPU。'
            '这一项由电脑这边定，手机不用操心用哪块卡。', self))

        外.addWidget(酒馆样式.灰色小字(
            '口令是这道隧道的**身份锚**：服务端证书由它派生，手机用同一口令校验 —— '
            '不知道口令的人连不上、也冒充不了。**别留空。**', self))

        # ── 手机端怎么填 ──
        外.addWidget(QLabel('手机端「接口配置」这样填（地址是"主机:端口"，不是网址）：', self))
        self._地址 = QPlainTextEdit(self)
        self._地址.setReadOnly(True)
        self._地址.setFixedHeight(96)
        外.addWidget(self._地址)
        址行 = QHBoxLayout()
        址行.addStretch(1)
        址行.addWidget(酒馆样式.做按钮('复制地址', self._复制地址,
                                   '把第一行地址拷进剪贴板', self))
        址行.addWidget(酒馆样式.做按钮('复制公钥指纹', self._复制指纹,
                                   '把公钥指纹拷进剪贴板（手机端填）', self))
        外.addLayout(址行)

        # ── 开关 ──
        钮行 = QHBoxLayout()
        self._开关 = 酒馆样式.做按钮('启动服务', self._切开关,
                                 '起 / 停这台模型服务', self)
        # ⚠ **别把按钮属性叫成 `self._保存`** —— 那会跟下面的方法 `_保存` 同名。
        # PySide6 对"绑定方法当槽"是按**名字**在接收者上找的，实例属性（按钮）一盖，
        # 点下去就成了"把按钮当函数调" → `QPushButton object is not callable`。
        self._保存钮 = 酒馆样式.做按钮('保存设置', self._保存,
                                  '把端口 / 口令 / GPU 层数存下来，下次开机照旧', self)
        self._自启 = QCheckBox('开机自动启动', self)
        钮行.addWidget(self._开关)
        钮行.addWidget(self._保存钮)
        钮行.addWidget(self._自启)
        钮行.addStretch(1)
        外.addLayout(钮行)

        外.addWidget(酒馆样式.灰色小字(
            '⚠ 首次启动时 Windows 会弹防火墙提示，勾「专用网络」放行 —— '
            '不放行的话手机连不上，但电脑本机自己连还是通的。', self))
        外.addWidget(酒馆样式.灰色小字(
            '⚠ 手机和电脑要在同一个局域网（同一个 WiFi / 路由器下）。', self))

        # ── 正在搬的模型（远程下载 / 收手机传来的）──
        self._传标 = QLabel('', self)
        self._取消下载钮 = 酒馆样式.做按钮(
            '取消下载', self._取消下载,
            '取消电脑这边的远程下载，并删掉没下完的半成品', self)
        传行 = QHBoxLayout()
        传行.addWidget(self._传标, 1)
        传行.addWidget(self._取消下载钮)
        外.addLayout(传行)
        self._传条 = QProgressBar(self)
        self._传条.setRange(0, 1000)
        self._传条.setTextVisible(True)
        外.addWidget(self._传条)
        self._传标.setVisible(False)
        self._传条.setVisible(False)
        self._取消下载钮.setVisible(False)

        # ── 日志 ──
        外.addWidget(QLabel('日志', self))
        self._日志框 = QPlainTextEdit(self)
        self._日志框.setReadOnly(True)
        外.addWidget(self._日志框, 1)

        关行 = QHBoxLayout()
        关行.addStretch(1)
        关行.addWidget(酒馆样式.做按钮('关闭', self.reject, '', self))
        外.addLayout(关行)

        # 定时刷日志（服务对象是别处共用的，日志随时在长）
        self._钟 = QTimer(self)
        self._钟.timeout.connect(self._刷日志)
        self._钟.start(500)

        self._装载设置()
        self._刷状态()

    # ── 设置 ──────────────────────────────────────────────────────

    def _装载设置(self):
        存 = 酒馆互联.读配置()
        self._自启.setChecked(bool(存.get('自动启动')))
        # 端口 / 口令 / gpu 以"服务对象当前值"为准（它启动时读的就是配置）

    def _切看口令(self):
        if self._口令.echoMode() == QLineEdit.Password:
            self._口令.setEchoMode(QLineEdit.Normal)
            self._看口令.setText('遮')
        else:
            self._口令.setEchoMode(QLineEdit.Password)
            self._看口令.setText('看')

    # ── 刷新 ──────────────────────────────────────────────────────

    def _刷状态(self):
        在跑 = self._服务.在跑()
        self._状态标.setText('状态：● 正在服务' if 在跑 else '状态：○ 已停止')
        端口 = int(self._端口.value())
        址们 = _地址们(端口)
        行 = ['  地址     %s' % (址们[0] if 址们 else '（没探到局域网地址，检查网络）'),
              '  口令     %s' % ('（上面那个）' if self._服务.口令 else '（留空）'),
              '  公钥指纹  %s' % (self._服务.指纹 or '（启动服务后才有）'),
              '  协议     honglu（连电脑端鸿胪寺 · TLS 加密隧道）']
        if len(址们) > 1:
            行.append('  （这台机器有多个网段，还可能是：%s）'
                     % '、'.join(址们[1:]))
        self._地址.setPlainText('\n'.join(行))

        self._开关.setText('停止服务' if 在跑 else '启动服务')
        # 运行中锁住端口 / 口令 / gpu（改端口得重绑，见文件头）
        for 件 in (self._端口, self._口令, self._看口令, self._gpu):
            件.setEnabled(not 在跑)
        self._保存钮.setEnabled(not 在跑)

    def _刷日志(self):
        # ── 进度条：这会儿电脑在搬哪个模型 ──
        进 = self._服务.进度()
        if 进.get('在传'):
            类 = '下载到电脑' if 进.get('类型') == '下载' else '收到手机传来'
            已 = 进.get('已', 0) or 0
            总 = 进.get('总', 0) or 0
            self._传标.setText('%s：%s　（%s）' % (类, 进.get('名', ''), 进.get('阶段', '')))
            self._传标.setVisible(True)
            self._传条.setVisible(True)
            # 取消按钮只在"电脑在**远程下载**"时给；上传是手机推上来的，取消在手机那头。
            self._取消下载钮.setVisible(进.get('类型') == '下载')
            if 总 > 0:
                self._传条.setRange(0, 1000)
                self._传条.setValue(int(已 * 1000 / 总))
                self._传条.setFormat('%d%%　%s / %s' % (int(已 * 100 / 总), _大小(已), _大小(总)))
            else:
                self._传条.setRange(0, 0)          # 不确定进度（对面没给总大小）
                self._传条.setFormat('已 %s' % _大小(已))
        else:
            self._传标.setVisible(False)
            self._传条.setVisible(False)
            self._取消下载钮.setVisible(False)

        文 = '\n'.join(self._服务.日志)
        if 文 != self._日志框.toPlainText():
            滚动 = self._日志框.verticalScrollBar()
            在底 = 滚动.value() >= 滚动.maximum() - 4
            self._日志框.setPlainText(文)
            if 在底:
                滚动.setValue(滚动.maximum())

    # ── 动作 ──────────────────────────────────────────────────────

    def _取消下载(self):
        """取消电脑这边的远程下载 —— 停，并把没下完的半成品删掉（见 `下载任务._跑`）。"""
        try:
            self._服务.取消下载()
        except Exception as 错:
            QMessageBox.warning(self, '取消下载失败', str(错))

    def _复制地址(self):
        址们 = _地址们(int(self._端口.value()))
        if not 址们:
            QMessageBox.information(self, '没地址', '没探到局域网地址，检查一下网络连接。')
            return
        QApplication.clipboard().setText(址们[0])
        QMessageBox.information(self, '复制好了', '已复制：\n%s' % 址们[0])

    def _复制指纹(self):
        指 = (self._服务.指纹 or '').strip()
        if not 指:
            QMessageBox.information(self, '还没有', '先启动服务，公钥指纹才会生成。')
            return
        QApplication.clipboard().setText(指)
        QMessageBox.information(self, '复制好了', '已复制公钥指纹：\n%s' % 指)

    def _切开关(self):
        if self._服务.在跑():
            self._服务.停止()
            self._刷状态()
            return
        # 启动前把界面上的值灌进服务对象
        self._服务.端口 = int(self._端口.value())
        self._服务.口令 = self._口令.text()
        self._服务.gpu层 = int(self._gpu.value())
        try:
            self._服务.启动()
        except OSError as 错:
            QMessageBox.warning(
                self, '起不来',
                '端口 %d 起不来：%s\n\n'
                '多半是端口被占（别的程序在用，或本程序已经开着另一个实例）。'
                '换个端口，或者把占着它的程序关掉再试。'
                % (self._服务.端口, 错))
            return
        except Exception as 错:
            QMessageBox.warning(self, '起不来', '%s：%s' % (type(错).__name__, 错))
            return
        self._刷状态()

    def _保存(self):
        try:
            酒馆互联.写配置(端口=int(self._端口.value()),
                          口令=self._口令.text(),
                          gpu层=int(self._gpu.value()),
                          自动启动=self._自启.isChecked())
        except Exception as 错:
            QMessageBox.warning(self, '存不下来',
                                '写不进设置文件：%s：%s' % (type(错).__name__, 错))
            return
        # 让服务对象也跟上（下次启动读的就是它）
        self._服务.端口 = int(self._端口.value())
        self._服务.口令 = self._口令.text()
        self._服务.gpu层 = int(self._gpu.value())
        QMessageBox.information(self, '存好了', '下次开机就按这套来。')
