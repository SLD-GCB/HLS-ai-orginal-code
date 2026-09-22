#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆角色页.py — 左栏：角色列表 + 搜索 + 增删改

```
┌───────────┐
│ 🔍 搜索    │
│ ┌───────┐ │
│ │头 爱丽丝│ │   ← 每条带 32px 圆角头像
│ │头 鲍勃  │ │
│ └───────┘ │
│ [+新建][改][删] │
└───────────┘
```

**存储一个字节都不在这儿碰。** 所有读写都投给 `工位`（工人线程），
回话在主线程里被叫。这个文件里出现 `库` 的地方，一处都不该有——除了把它
转手交给 `工位`。

⚠ **编辑一条已经有记录的角色时，必须重新 `取()` 一次整条。** 列表用的是
`酒馆角色.清单列`，那个列名串**故意不含** `人设` / `开场白` / `示例对话`
（列表不需要，读出来白费内存）。而 `酒馆模型.从行` 对缺的列**填默认值**。
所以拿列表行去开编辑框、再保存，那三个字段会被默认值（空串）**静默抹掉**
——用户写了几百字的人设就这么没了，而且没有任何提示。这是这个文件里
最要命的一条。
"""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

import 酒馆角色
import 酒馆样式

__all__ = ['角色页', '角色编辑框']

头像边长 = 32


def _短(文, 长=60):
    """一行摘要。多行文本摊平，太长截断——列表里一格放不下换行。"""
    文 = ' '.join((文 or '').split())
    return 文 if len(文) <= 长 else 文[:长 - 1] + '…'


class 角色编辑框(QDialog):
    """
    新建 / 编辑一个角色。

    `角色` 传 `None` 是新建。**编辑时传进来的必须是从 `取()` 拿到的整条**，
    不能是列表行——理由见这个文件的开头。
    """

    def __init__(self, 父, 角色=None, 头像=None):
        super().__init__(父)
        self._老 = 角色
        self._头像 = 头像                     # 现有的头像 bytes，没有就是 None
        self._换过头像 = False
        self.setWindowTitle('新建角色' if 角色 is None else '编辑角色')
        self.resize(560, 620)

        外 = QVBoxLayout(self)
        外.setSpacing(10)

        # ── 头像 ──
        顶 = QHBoxLayout()
        self.头像标 = QLabel()
        self.头像标.setFixedSize(72, 72)
        self.头像标.setAlignment(Qt.AlignCenter)
        self.头像标.setStyleSheet('border: 1px dashed %s; border-radius: 8px;'
                                % 酒馆样式.配色()['border'])
        self._刷头像()
        顶.addWidget(self.头像标)
        顶.addWidget(酒馆样式.做按钮('选一张…', self._选图))
        顶.addWidget(酒馆样式.做按钮('去掉', self._去图))
        顶.addStretch(1)
        外.addLayout(顶)

        # ── 字段 ──
        表 = QFormLayout()
        表.setLabelAlignment(Qt.AlignRight)
        self.名字 = QLineEdit((角色.名字 if 角色 else '') or '')
        self.名字.setPlaceholderText('非空，列表里靠它认人')
        self.简介 = QLineEdit((角色.简介 if 角色 else '') or '')
        self.简介.setPlaceholderText('一句话的身份/来历。会拼进系统提示，也显示在角色列表里')
        self.标签 = QLineEdit((角色.标签 if 角色 else '') or '')
        self.标签.setPlaceholderText('逗号分开，随便写')
        表.addRow('名字', self.名字)
        表.addRow('简介', self.简介)
        表.addRow('标签', self.标签)
        外.addLayout(表)

        外.addWidget(QLabel('人设（系统提示的主体：性格、说话方式。'
                          '会拼在「你就是<名字>」和「简介」后面）'))
        self.人设 = QPlainTextEdit((角色.人设 if 角色 else '') or '')
        self.人设.setPlaceholderText('空着也行——系统提示里本来就会写上'
                                  '「你就是<名字>」和「简介」；这里再补'
                                  '性格和说话方式')
        外.addWidget(self.人设, 2)

        外.addWidget(QLabel('开场白（新建会话时自动落成第一条消息）'))
        self.开场白 = QPlainTextEdit((角色.开场白 if 角色 else '') or '')
        外.addWidget(self.开场白, 1)

        外.addWidget(QLabel('示例对话（风格示范，会拼进系统提示。'
                          '多轮建议写成「名字：台词」）'))
        self.示例对话 = QPlainTextEdit((角色.示例对话 if 角色 else '') or '')
        self.示例对话.setPlaceholderText('比如：乌玛：小宝贝，今天过得怎么样？')
        外.addWidget(self.示例对话, 1)

        # ── 报错行 + 按钮 ──
        self.报错 = QLabel()
        self.报错.setWordWrap(True)
        self.报错.setStyleSheet('color: %s;' % 酒馆样式.配色()['danger'])
        self.报错.hide()
        外.addWidget(self.报错)

        钮 = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        钮.button(QDialogButtonBox.Ok).setText('保存')
        钮.button(QDialogButtonBox.Cancel).setText('取消')
        钮.accepted.connect(self.accept)
        钮.rejected.connect(self.reject)
        外.addWidget(钮)

    # ── 头像 ──

    def _刷头像(self):
        图 = 酒馆样式.圆角头像(self._头像, 72, 0.18) if self._头像 else None
        if 图 is not None and not 图.isNull():
            self.头像标.setPixmap(图)
        else:
            self.头像标.setText('无头像')

    def _选图(self):
        路, _ = QFileDialog.getOpenFileName(
            self, '挑一张头像', '', '图片 (*.png *.jpg *.jpeg *.bmp *.webp)')
        if not 路:
            return
        from PySide6.QtGui import QPixmap
        原 = QPixmap(路)
        if 原.isNull():
            self._报('这个文件 Qt 读不出来，换一张试试（PNG / JPG 最稳）')
            return
        # ⚠ 在这儿就缩到 512 并存成 PNG。**别把原图原样塞进库**——一张手机
        # 拍的照片好几 MB，库里躺一堆那个，每次开列表都要读。
        self._头像 = 酒馆样式.头像控件图(原, 512)
        self._换过头像 = True
        self._刷头像()

    def _去图(self):
        self._头像 = None
        self._换过头像 = True
        self._刷头像()

    # ── 收尾 ──

    def _报(self, 文字):
        self.报错.setText(文字)
        self.报错.show()

    def 字段(self):
        """对话框里现在填的东西。`accept()` 里校验通过了才该拿它。"""
        return {
            '名字': self.名字.text().strip(),
            '简介': self.简介.text().strip(),
            '人设': self.人设.toPlainText(),
            '开场白': self.开场白.toPlainText(),
            '示例对话': self.示例对话.toPlainText(),
            '标签': self.标签.text().strip(),
        }

    def accept(self):
        """
        **校验不过就留在框里，别关。** 关掉的话用户填的一屏字全没了，
        还得从头再来一遍——这是对话框最基本的体面。
        """
        明明 = self.字段()['名字']
        if not 明明:
            self._报('名字不能空。`名字` 列是非空的，而且列表里全靠它认人。')
            self.名字.setFocus()
            return
        super().accept()


class 角色页(QWidget):
    """
    左栏。**对外只有三个信号和几个刷新入口**，别的都是内部事。

        self._角色页.选中了.connect(self.某处)   # 参数是角色编号

    新建 / 编辑 / 删除都自己走 `工位`，做完发 `改过了` 让外面知道该刷新了。
    """

    选中了 = Signal(int)          # 选中了哪个角色（编号）
    改过了 = Signal()             # 角色增删改过，外面该重读了

    def __init__(self, 工位, 父=None):
        super().__init__(父)
        self._工位 = 工位
        self._角色们 = []          # 当前读回来的角色（列表行，缺人设那几个字段）
        self._头们 = {}            # {角色编号: 头像 bytes}
        self._显示的 = []          # 过滤后真正画出来的那些
        self._当前 = 0             # 选中的角色编号，0 = 没选
        #: **最后一次告诉外面的**是哪个编号。和 `_当前` 不是一回事：
        #: `_当前` 是"面板认为选中的"，这个是"外面已经知道了的"。两个不一样
        #: 的时候才需要发信号。见 `_报选中`。
        self._已报 = 0

        外 = QVBoxLayout(self)
        外.setContentsMargins(8, 8, 8, 8)
        外.setSpacing(8)

        self.搜索 = QLineEdit()
        self.搜索.setPlaceholderText('搜名字 / 简介 / 标签')
        self.搜索.setClearButtonEnabled(True)
        self.搜索.textChanged.connect(self._按搜索重画)
        外.addWidget(self.搜索)

        self.列表 = QListWidget()
        self.列表.setObjectName('角色列表')
        self.列表.setIconSize(QSize(头像边长, 头像边长))
        self.列表.currentItemChanged.connect(self._选变了)
        self.列表.itemDoubleClicked.connect(lambda _: self._编辑())
        外.addWidget(self.列表, 1)

        钮 = QHBoxLayout()
        钮.setSpacing(6)
        钮.addWidget(酒馆样式.做按钮('新建', self._新建, '建一个新角色'))
        钮.addWidget(酒馆样式.做按钮('编辑', self._编辑, '改这个角色'))
        钮.addWidget(酒馆样式.做按钮('删除', self._删除, '连同他的会话和消息一起删'))
        外.addLayout(钮)

        self.刷新()

    # ── 读 ──

    def 刷新(self):
        """重读角色清单。**回来之后保持原来选中的那个还选中。**"""
        原来 = self._当前

        def 回来(行们, 错):
            if 错:
                QMessageBox.warning(self, '读角色列表失败', 错)
                return
            # ⚠ `酒馆角色.列` 回来的**已经是 `角色卡` 了**，不能再套一层
            # `从行`——那会拿一个对象去 `in` 一个字符串，当场 TypeError。
            # （原来就是这么写的，结果是左栏永远是空的，而且错只印在
            # 终端上、界面上什么都不说。）
            self._角色们 = list(行们 or [])
            # 头像单独一张表，得一条条取——角色数量一般不大，够用。
            # **取完再画**，不然列表会先空着、再一张张蹦出来。
            self._头们 = {}
            self._取头像(0, 原来)

        self._工位.干(lambda 库: 酒馆角色.列(库), 回来)

    def _取头像(self, i, 保持):
        """一条条取头像，取完统一重画。"""
        if i >= len(self._角色们):
            self._按搜索重画(保持=保持)
            return
        编号 = self._角色们[i].编号

        def 回来(图, 错):
            if 图:
                # `取头像` 回的是 `(字节, 类型)`，这里只要字节
                self._头们[编号] = 图[0]
            self._取头像(i + 1, 保持)

        self._工位.干(lambda 库, n=编号: 酒馆角色.取头像(库, n), 回来)

    # ── 画 ──

    def _按搜索重画(self, _=None, 保持=None):
        要选的 = self._当前 if 保持 is None else 保持
        查 = (self.搜索.text() or '').strip().lower()
        if 查:
            筛 = [r for r in self._角色们
                  if 查 in (r.名字 or '').lower()
                  or 查 in (r.简介 or '').lower()
                  or 查 in (r.标签 or '').lower()]
        else:
            筛 = list(self._角色们)
        self._显示的 = 筛
        self._画(要选的)

    def _画(self, 要选的=None):
        要选的 = self._当前 if 要选的 is None else 要选的
        self.列表.blockSignals(True)          # 重画时别乱发「选中了」
        self.列表.clear()
        for 角 in self._显示的:
            项 = QListWidgetItem()
            项.setData(Qt.UserRole, 角.编号)
            # 没头像也要塞一张空图进去，不然那一列的左边缘参差不齐
            项.setIcon(酒馆样式.圆角头像(self._头们.get(角.编号), 头像边长))
            项.setText('%s\n%s' % (角.名字 or '（没名字）', _短(角.简介, 28)))
            self.列表.addItem(项)
        # 把原来那个重新选上；找不到就算了（可能刚被删了）
        选中行 = -1
        for i in range(self.列表.count()):
            if self.列表.item(i).data(Qt.UserRole) == 要选的:
                选中行 = i
                break
        if 选中行 >= 0:
            self.列表.setCurrentRow(选中行)
        self.列表.blockSignals(False)

        # ⚠ **列表里那行亮了，不等于外面知道了。** 上面从头到尾关着信号画，
        # `setCurrentRow` 这一下发不出 `currentItemChanged`——而"新建完顺手
        # 选上它"（`_开编辑框`）走的恰恰就是这条路。
        #
        # 不补这一下的后果，实测就是这个：建完角色左边那行是亮的，可是三个
        # 面板谁都不知道选了谁，中栏 `_角色编号` 还是 0，**一按发送就弹
        # 「先选个角色」**；更难受的是那一行**已经就是 current 了**，用户再
        # 去点它 `currentItemChanged` 根本不再发——**点也点不动，看着像卡死**。
        if 选中行 >= 0:
            self._当前 = 要选的
            self._报选中(要选的)
        elif 要选的:
            # 选中的那个没了（比如刚删掉），告诉外面一声，两边别对不上
            self._当前 = 0
            self._报选中(0)

    def _取选中的(self):
        """列表上现在选着的那个角色卡（列表行，**缺人设那几个字段**）。"""
        项 = self.列表.currentItem()
        if 项 is None:
            return None
        编号 = 项.data(Qt.UserRole)
        for 角 in self._角色们:
            if 角.编号 == 编号:
                return 角
        return None

    def _报选中(self, 编号):
        """
        选中的角色**真的换了**才告诉外面一声。

        ⚠ 去重是必须的，不是省事：`刷新()` 每次都会走到 `_画`，重画时选中的
        通常还是原来那个。无条件发的话外面（`主窗._换了角色`）会把中栏整个
        清掉重载——**按一下 F5，正看着的聊天记录就闪一下**。去重之后"没变
        就不发"，重画是安静的。
        """
        if 编号 == self._已报:
            return
        self._已报 = 编号
        self.选中了.emit(编号)

    def _选变了(self, 现在, _前):
        if 现在 is None:
            self._当前 = 0
        else:
            self._当前 = 现在.data(Qt.UserRole)
        self._报选中(self._当前)

    # ── 写 ──

    def _新建(self):
        self._开编辑框(None)

    def _编辑(self):
        行 = self._取选中的()
        if 行 is None:
            QMessageBox.information(self, '先选一个', '左栏里先点一个角色。')
            return
        # ⚠ **必须重新 `取()` 一次整条。** 列表行是从 `清单列` 来的，那个
        # 串不含 `人设` / `开场白` / `示例对话`，而 `从行` 会给缺的列填
        # 默认值（空串）。拿列表行开编辑框再保存 = 那三个字段被**静默清空**。
        def 回来(整个, 错):
            if 错:
                QMessageBox.warning(self, '读这个角色失败', 错)
                return
            if 整个 is None:
                QMessageBox.information(self, '没了', '这个角色已经不在了，刷新一下。')
                self.刷新()
                return

            def 有头像(图, _错):
                self._开编辑框(整个, (图[0] if isinstance(图, (tuple, list)) else 图)
                              if 图 else None)

            self._工位.干(lambda 库, n=行.编号: 酒馆角色.取头像(库, n), 有头像)

        self._工位.干(lambda 库, n=行.编号: 酒馆角色.取(库, n), 回来)

    def _开编辑框(self, 角色, 头像=None):
        框 = 角色编辑框(self, 角色, 头像)
        if 框.exec() != QDialog.Accepted:
            return
        字段 = 框.字段()
        换过 = 框._换过头像
        新头像 = 框._头像

        if 角色 is None:
            def 回来(新, 错):
                if 错:
                    QMessageBox.warning(self, '建角色失败', 错)
                    return
                if 换过 and 新头像:
                    self._工位.干(lambda 库, n=新.编号, g=新头像:
                                酒馆角色.存头像(库, n, g, 'image/png'))
                self.改过了.emit()
                # 新建完顺手选上它——不然用户还得自己去列表里找
                self._当前 = 新.编号
                self.刷新()
            self._工位.干(lambda 库, f=字段: 酒馆角色.建(库, **f), 回来)
            return

        def 回来(_值, 错):
            if 错:
                QMessageBox.warning(self, '改角色失败', 错)
                return
            if 换过:
                if 新头像:
                    self._工位.干(lambda 库, n=角色.编号, g=新头像:
                                酒馆角色.存头像(库, n, g, 'image/png'))
                else:
                    self._工位.干(lambda 库, n=角色.编号:
                                酒馆角色.删头像(库, n))
            self.改过了.emit()
            self._当前 = 角色.编号
            self.刷新()

        self._工位.干(lambda 库, n=角色.编号, f=字段:
                    酒馆角色.改(库, n, **f), 回来)

    def _删除(self):
        行 = self._取选中的()
        if 行 is None:
            QMessageBox.information(self, '先选一个', '左栏里先点一个角色。')
            return
        # 删角色是**连带的**：会话和消息一起没（酒馆角色.删 里那句子查询）。
        # 所以必须问一声，不能顺手就删。
        问 = QMessageBox.question(
            self, '删掉这个角色？',
            '「%s」连同他的所有会话和消息会一起删掉，**删了找不回来**。\n\n'
            '确定要删吗？' % (行.名字 or '（没名字）'),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if 问 != QMessageBox.Yes:
            return

        def 回来(_值, 错):
            if 错:
                QMessageBox.warning(self, '删角色失败', 错)
                return
            self._当前 = 0
            # ⚠ 删完也得报一声「没选中了」。`刷新()` 那条路报不出来——它传下去
            # 的 `要选的` 已经是 0，`_画` 里那个 `elif 要选的` 不成立。不报的
            # 话外面还捏着那个刚被删掉的编号：中栏 `_角色编号` 是死的，用户
            # 接着打字发送，就是拿一个不存在的角色去建会话。
            self._报选中(0)
            self.改过了.emit()
            self.刷新()

        self._工位.干(lambda 库, n=行.编号: 酒馆角色.删(库, n), 回来)

    # ── 外面要的 ──

    def 现在选的是(self):
        return self._当前

    def 选编号(self, 编号):
        """外面（比如刚启动时）让它选中某个角色。找不到就什么都不做。"""
        for i in range(self.列表.count()):
            if self.列表.item(i).data(Qt.UserRole) == 编号:
                self.列表.setCurrentRow(i)
                return True
        return False
