#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆样式.py — 配色、QSS、头像裁剪、小零件

**配色和 QSS 都写在这个文件里，不 import 霸下的 `主题.py`。**
原来那份要靠把霸下目录塞进 `sys.path` 才 import 得到，那条路已经拆了——
ai酒馆 现在是个纯粹的 MySQL 客户端（照隔壁 `taowu-v2.1` 那样连），
手上不需要、也不该有霸下的代码。

样式分两层，和以前一样：

  1. `_基础QSS()` —— 管通用控件（按钮 / 输入框 / 列表 / 滚动条…），
     用的是**类型选择器**，所以主题一换，整窗跟着换。
  2. `_附加QSS()` —— 管主题里压根没有的那些，主要是**气泡**。
     它们用 `QFrame + QLabel` 搭，并且给了 objectName，类型选择器扫不到。

⚠ **`QLabel` 不在基础 QSS 的规则里。** 所以「想跟主题走」的控件就用
`QPushButton` / `QLineEdit` / `QListWidget` 这些；只有气泡那种基础样式里
没有的东西，才在附加 QSS 里自己定义。
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import (QColor, QIcon, QPainter, QPainterPath, QPixmap)

__all__ = ['明暗们', '配色', '装', '圆角头像', '头像控件图', '做按钮',
           '气泡样式', '滚动到底', '灰色小字']

明暗们 = ('light', 'dark')

当前明暗 = 'light'

#: 两档配色，键名是英文的——QSS 里要按名字取，中文键在拼串时容易看岔。
_档 = {
    'light': {
        'bg': '#F8FAFC', 'surface': '#FFFFFF', 'primary': '#2563EB',
        'text': '#0F172A', 'text_secondary': '#64748B',
        'border': '#E2E8F0', 'hover': '#F1F5F9', 'active': '#DBEAFE',
        'danger': '#DC2626', 'select': '#FFFFFF',
    },
    'dark': {
        'bg': '#0F172A', 'surface': '#1E293B', 'primary': '#3B82F6',
        'text': '#F1F5F9', 'text_secondary': '#94A3B8',
        'border': '#334155', 'hover': '#334155', 'active': '#1E40AF',
        'danger': '#F87171', 'select': '#FFFFFF',
    },
}


def 配色():
    """当前这一档的配色字典。"""
    return _档[当前明暗]


def 装(app, 明暗='light'):
    """
    给 `QApplication` 上样式。**换主题时再调一次就行。**

    ⚠ 收的是 **`QApplication`**，不是 `QMainWindow`——传错了不报错，只是
    不生效，那种问题最难查。
    """
    global 当前明暗
    if 明暗 not in 明暗们:
        明暗 = 'light'
    当前明暗 = 明暗
    app.setStyleSheet(_基础QSS() + _附加QSS())


def _基础QSS():
    c = 配色()
    return """
QWidget { background: %(bg)s; color: %(文字)s; }
QMainWindow, QDialog { background: %(bg)s; }

/* ⚠ **QLabel 得透明，否则整句话看不见。**
   上面那条 `QWidget` 的底色会落到**每一个** QLabel 上。气泡里的正文就是一个
   QLabel，于是那行字是画在**自己那块不透明白底**上的：用户那条气泡底是蓝的、
   字是白的，白字压白底——**一个字都看不见**；角色那条只是底色接近，看着像块
   脏印子。QLabel 是文字，不该有自己的底。 */
QLabel { background: transparent; }

QPushButton {
    background: %(面)s; color: %(文字)s;
    border: 1px solid %(边)s; border-radius: 6px;
    padding: 5px 14px; min-height: 20px;
}
QPushButton:hover { background: %(悬停)s; }
QPushButton:pressed { background: %(活动)s; }
QPushButton:disabled { color: %(次要)s; }

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: %(面)s; color: %(文字)s;
    border: 1px solid %(边)s; border-radius: 6px;
    padding: 4px 8px; selection-background-color: %(主色)s;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 1px solid %(主色)s;
}
QComboBox::drop-down { border: none; width: 18px; }
/* ⚠ **可编辑的下拉框（`接口页` 里「模型」那格），这一条不能少。**
   可编辑的 `QComboBox` 里面那行字是它自己的一个 `QLineEdit` 子控件，
   上面 `QLineEdit` 那条会**落到它头上**——于是框里再套一个小框、还多一道
   边，看着像画错了。把它按透明处理，外观整个交给外面的 `QComboBox`。 */
QComboBox QLineEdit { background: transparent; border: none; padding: 0; }
QComboBox QAbstractItemView {
    background: %(面)s; color: %(文字)s;
    border: 1px solid %(边)s; selection-background-color: %(活动)s;
}

QListWidget {
    background: %(面)s; color: %(文字)s;
    border: 1px solid %(边)s; border-radius: 6px;
}
QListWidget::item { padding: 6px; border-radius: 4px; }
QListWidget::item:hover { background: %(悬停)s; }

QGroupBox {
    border: 1px solid %(边)s; border-radius: 6px;
    margin-top: 10px; padding-top: 8px;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }

QMenuBar, QMenu { background: %(面)s; color: %(文字)s; }
QMenu::item:selected, QMenuBar::item:selected { background: %(活动)s; }
QToolBar { background: %(面)s; border-bottom: 1px solid %(边)s; spacing: 4px; }

QSplitter::handle { background: %(边)s; }
QSplitter::handle:horizontal { width: 1px; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical {
    background: %(边)s; border-radius: 5px; min-height: 24px;
}
QScrollBar::handle:vertical:hover { background: %(次要)s; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal {
    background: %(边)s; border-radius: 5px; min-width: 24px;
}
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QStatusBar { background: %(面)s; color: %(次要)s; }
QToolTip { background: %(面)s; color: %(文字)s; border: 1px solid %(边)s; }
""" % {'bg': c['bg'], '面': c['surface'], '主色': c['primary'],
       '文字': c['text'], '次要': c['text_secondary'], '边': c['border'],
       '悬停': c['hover'], '活动': c['active']}


def _附加QSS():
    c = 配色()
    return """
/* ── 酒馆自己的东西：基础样式里压根没有的那些 ───────────────────── */

/* 气泡分两层：外面 `气泡行` 是**一整行**（撑满消息区宽，只管左右对齐），
   里面那个 `气泡*` 才是真正有底色、圆角、跟着字走的那一块。
   行自己不画底色——不写这条的话它会跟着 `QWidget` 那条变成一整条背景色的
   横条，气泡和气泡之间就分不开了。 */
QFrame#气泡行 { background: transparent; border: none; }

/* 气泡。用 QFrame + QLabel 搭，基础 QSS 的类型选择器扫不到 QFrame。 */
QFrame#气泡角色 {
    background: %(面)s;
    border: 1px solid %(边)s;
    border-radius: 10px;
}
/* 自己那条：白的。底色跟着 `%(面)s` 走——浅色档就是纯白 #FFFFFF；深色档是
   #1E293B。**不能写死 `#FFFFFF`**：深色主题里正文色是近白的 #F1F5F9，
   写死白底就是"白字压白底"，又回到一个字看不见那个毛病上去了。 */
QFrame#气泡用户 {
    background: %(面)s;
    border: 1px solid %(边)s;
    border-radius: 10px;
}
QFrame#气泡系统 {
    background: %(悬停)s;
    border: 1px dashed %(边)s;
    border-radius: 10px;
}
/* 气泡里的字色得跟着气泡底色走，不能跟着全局走。
   ⚠ 自己那条**必须从白改成正文色**：底一换成灰，白字就又是"看不见"那个
   毛病了（见文件里 `QLabel { background: transparent; }` 那条注释）。 */
QFrame#气泡角色 QLabel, QFrame#气泡系统 QLabel,
QFrame#气泡用户 QLabel { color: %(正文字)s; }

/* 名字、时间那行小字 */
QLabel#小字 { color: %(次要)s; font-size: 11px; }

/* 报错的气泡：整条红边，让人一眼看到是哪一条出的问题 */
QFrame#气泡坏了 {
    background: %(面)s;
    border: 1px solid %(危险)s;
    border-radius: 10px;
}
QFrame#气泡坏了 QLabel { color: %(危险)s; }

/* 列表条目：选中态用活动色。**类型选择器，不是按 objectName 一个个列。**
   ⚠ 这儿原来是 `QListWidget#角色列表::item:selected, QListWidget#会话列表::item:selected`
   —— 漏了「接口配置」那个列表（它没设 objectName），于是它选中的那一行
   走的是 Qt 默认：底色被 QSS 带成活动色（很浅），字色却是调色板里的
   `HighlightedText`（白）——**白字压浅蓝底，整行字就看不见了**。
   而新存一套配置之后恰恰就是选中它，所以"一建完配置左边那行就没了"。
   写成类型选择器，以后新加列表不会再漏。 */
QListWidget::item:selected {
    background: %(活动)s;
    color: %(正文字)s;
}

/* 消息区不要边框，它自己就是一整块背景 */
QScrollArea#消息区 { border: none; background: transparent; }

/* 状态栏那条分隔线 */
QStatusBar { border-top: 1px solid %(边)s; }
""" % {'主色': c['primary'], '面': c['surface'], '边': c['border'],
       '正文字': c['text'], '次要': c['text_secondary'],
       '悬停': c['hover'], '活动': c['active'], '危险': c['danger']}


# ── 头像 ────────────────────────────────────────────────────────────

def 圆角头像(数据, 边长=48, 圆角比例=0.28):
    """
    `bytes` 图片 → 一张裁成圆角的 `QPixmap`。给不出来就返回空图。

    用 Qt 干，**不引 PIL**：少一个依赖，本项目也没装。而且头像存进库之前
    就在这里缩过一次了（见 `头像控件图`），库里不会躺着几 MB 的原图。

    圆角比例 0.28 是照常见聊天软件的观感定的，不是精确的圆——**纯圆会把
    正方形头像裁掉四个角太多**，人物卡片那种图尤其明显。
    """
    if not 数据:
        return QPixmap()
    原 = QPixmap()
    if not 原.loadFromData(数据):
        return QPixmap()
    # `KeepAspectRatioByExpanding` + 居中裁，保证填满正方形不留白边
    缩放过 = 原.scaled(边长, 边长, Qt.KeepAspectRatioByExpanding,
                      Qt.SmoothTransformation)
    出 = QPixmap(边长, 边长)
    出.fill(Qt.transparent)
    画 = QPainter(出)
    画.setRenderHint(QPainter.Antialiasing)
    路 = QPainterPath()
    路.addRoundedRect(0, 0, 边长, 边长, 边长 * 圆角比例, 边长 * 圆角比例)
    画.setClipPath(路)
    # 居中放：缩放后一边可能比边长长，多出来的两边各切一半
    画.drawPixmap((边长 - 缩放过.width()) // 2,
                 (边长 - 缩放过.height()) // 2, 缩放过)
    画.end()
    return 出


def 头像控件图(图, 最大边=512):
    """
    `QPixmap` → PNG `bytes`，存库用。超过 `最大边` 就先等比缩小。

    **这一层缩放在存之前做，不是显示的时候做。** 显示时缩只是显示小了，
    库里照样躺着一张几 MB 的原图，每次取头像都要读它一遍。
    """
    from PySide6.QtCore import QBuffer, QByteArray
    要存 = 图
    if 图.width() > 最大边 or 图.height() > 最大边:
        要存 = 图.scaled(最大边, 最大边, Qt.KeepAspectRatio,
                        Qt.SmoothTransformation)
    字节 = QByteArray()
    缓 = QBuffer(字节)
    缓.open(QBuffer.WriteOnly)
    # JPEG 不要——透明通道会丢，头像那种圆角裁过的图会变成黑底
    要存.save(缓, 'PNG')
    缓.close()
    return bytes(字节)


# ── 小零件 ──────────────────────────────────────────────────────────

def 做按钮(文字, 槽=None, 提示='', 父=None):
    """
    工具栏按钮。**图标就算了，直接用文字** —— 这条链上全是中文界面，
    配 emoji 图标会遇上字体缺字（Windows 上框框一片），反而更难看。

    ⚠ **能传父就传父。** 不传（`父=None`）建出来的是一个**无父控件**，
    而按 Qt 的定义，无父控件就是顶层窗口——只要在挂进布局之前碰一下
    `show()` / `setVisible(True)`，屏幕上就**真的会弹出一个带标题栏三键的
    小窗口**，等 `addWidget` 认了爹它才消失。实测就是这条链上 `气泡`
    那个「一打开就不断弹出小窗」的毛病，详见 `灰色小字` 和 `酒馆对话页`
    里 `气泡.__init__` 那段。
    """
    from PySide6.QtWidgets import QPushButton
    钮 = QPushButton(文字, 父)
    钮.setCursor(Qt.PointingHandCursor)
    if 提示:
        钮.setToolTip(提示)
    if 槽 is not None:
        钮.clicked.connect(槽)
    return 钮


def 灰色小字(文字='', 父=None):
    """
    一行次要信息（名字、时间、条数）。

    ⚠ **能传父就传父。** 这不是讲究，是这儿真踩过的坑：气泡里那行名字是
    `灰色小字(名字)` 建出来的**无父** QLabel，紧接着一句
    `名标.setVisible(bool(名字))`——而无父控件就是顶层窗口，`setVisible(True)`
    就是 `show()`，于是**每画一条角色消息就在屏幕上弹一个带标题栏三键的小窗**
    （图见工程根目录 `报错图.jpg`），下一行 `addWidget` 挂上父之后它又消失，
    看着就是"不断弹出、然后就没了"。一屏有几句就弹几个。
    """
    from PySide6.QtWidgets import QLabel
    标 = QLabel(文字, 父)
    标.setObjectName('小字')
    return 标


def 气泡样式(框, 类别):
    """
    给气泡 `QFrame` 定身份。**类别是 objectName**，样式在上面那份 QSS 里。

    ⚠ **要挂在有底色的那一层上，不是外面那个整行。** 一条消息是两层
    （行 + 气泡），传错了就是"一整行都染色"，看着像一根通条色带。

    只有三种：`角色` / `用户` / `系统`。加 `坏` 是在原类别上再挂一层红边，
    用 `属性` 选择器做不到（同一条 QSS 里就一个 objectName 好使），所以
    `坏了` 直接换成 `气泡坏了` 这个样式。
    """
    if 类别 == '坏':
        框.setObjectName('气泡坏了')
    elif 类别 == '用户':
        框.setObjectName('气泡用户')
    elif 类别 == '系统':
        框.setObjectName('气泡系统')
    else:
        框.setObjectName('气泡角色')
    # 改了 objectName 必须重新 polish，不然 Qt 不会重算样式
    框.style().unpolish(框)
    框.style().polish(框)


def 滚动到底(区, 若已在底部=True):
    """
    滚到最底下。**默认只在"用户本来就在底部"时才滚。**

    用户往上翻旧消息的时候，新来的块要是把他拽回底部，那是很烦的事——
    这条是聊天界面的基本礼貌。想无条件滚就传 `若已在底部=False`。
    """
    条 = 区.verticalScrollBar()
    if 若已在底部:
        # 留一点余量：正好在底部和差几个像素，观感上是一回事
        在底 = 条.value() >= 条.maximum() - 8
        if not 在底:
            return False
    条.setValue(条.maximum())
    return True
