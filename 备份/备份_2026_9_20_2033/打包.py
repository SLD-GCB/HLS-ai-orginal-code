#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包.py — 鸿胪寺 打包脚本

    python 打包.py          （或者双击 打包.bat）

打出来的东西是「一体化 exe + 依赖库 + 配套文件夹」，在 `dist/` 下：

    酒馆.exe      全部 Python 代码 + Python 运行时 + 标准库（**不含**任何第三方依赖）
    依赖库/       PySide6 / llama_cpp / numpy / requests / modelscope / huggingface_hub
                  及其全部传递依赖 —— 运行时由 exe 挂进 sys.path
    酒馆数据/     空骨架（模型/ 头像/ 消息/ 助手转录/ 记忆/）
    GPU-model/    编译GPU版.bat + 验证GPU.py
    使用说明.txt

⚠ **exe 和 `依赖库/` 必须待在同一个目录里。** 少一层、挪个窝，程序就起不来。

# 这套切法为什么不会「exe 里排了、依赖库里忘了拷」

`算闭包()` 从 5 个根包递归算出全部传递依赖，得到的**顶层条目**一份两用：

  · 拷进 `依赖库/` 的就是它们
  · `--exclude-module` 排掉的也是它们

所以「exe 里装了什么」和「依赖库里装了什么」是同一份真源推出来的，天然对得上。
这正是这个项目最怕的那种坏法——**静默缺依赖**：程序不报错，只是某个功能悄悄
退化成别的样子。所以绝不能靠两张手写清单去对齐。

# 三个支点

1. **依赖闭包**用 `importlib.metadata` + `packaging.requirements` 算。
   必须**求值环境标记**、**跳过 `extra == ...`**——不这么做的话，modelscope
   声明的可选依赖会把 torch / tensorflow / datasets 全拖进来，37 个包瞬间变 648 个。

2. **运行时钩子**（见 `写运行时钩子`）在 PyInstaller bootstrap 阶段执行，
   比 `酒馆.py` 里那些 `from PySide6.QtCore import ...` 早。
   **所以 `酒馆.py` 和 23 个业务模块一行都不用改。**

3. **DLL 不用额外操心**：PySide6 自己会 `os.add_dll_directory(PySide6包目录)`
   （`PySide6/__init__.py:59`），llama_cpp 自己按 `__file__` 找同级 `lib/`
   （`llama_cpp/llama_cpp.py:38`）。外置之后这两条路照样成立。
"""

import ast
import importlib.util as 导入工
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import time
import warnings

__all__ = ['main']


# ══════════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════════

EXE名 = '鸿胪寺'
入口 = '酒馆.py'

# 软件图标。
# 把一张源图丢到项目根、名字对上（png / jpg / bmp / ico），构建时自动转成多分辨率 ico
# 喂给 `--icon`。**找不到就跳过** —— 没图标的 exe 照样跑，不该让整个构建卡死。
# ⚠ svg 读不了（Pillow 不认），要发去软件图标/ 那种 svg，先把源转成 png。
图标名 = EXE名
图标后缀们 = ('.png', '.jpg', '.jpeg', '.bmp', '.ico')
图标尺寸 = [16, 24, 32, 48, 64, 128, 256]
# 源图四周留白太多、主体太小的话把它调小（taowu 那份用的是 0.55 = 中心裁切 55%）。
# 1.0 = 不裁，整张等比塞进正方形画布 —— 默认走这条，**不切掉你画的任何东西**。
图标裁切 = 1.0
# 源图是"圆角方形 + 四角底色"的话，把四个角抠成透明（从四个角逐个泛洪，
# 只吃连通的那块底色，**碰不到图案内部**）。不做的话大尺寸下会露四个白角。
# 底是深色的（比如深色任务栏）尤其明显。
图标抠角 = True
# 泛洪的容差。JPEG 在圆角边界上会留一圈振铃，太小抠不干净、太大会漏进图案。
图标容差 = 40

# 依赖闭包的根。改这里 = 同时改「依赖库装什么」和「exe 排什么」。
根依赖 = ['PySide6', 'llama-cpp-python', 'requests', 'modelscope', 'huggingface-hub']

依赖库名 = '依赖库'
数据名 = '酒馆数据'
GPU目录 = 'GPU-model'
BUILD目录 = 'build'
DIST目录 = 'dist'
工作目录 = os.path.join(BUILD目录, '_pyi')     # PyInstaller 自己的地盘，跟临时文件分开

本脚本 = '打包.py'

# 收缩 = True：把 PySide6 里这个纯 QtWidgets 应用一辈子碰不到的东西砍掉，
# 634MB → 约 200MB。清单见下面 `瘦身目录` / `瘦身通配`。
# 关掉它 = 字节原样的完整 PySide6。**外置 PySide6 万一加载失败，第一个该试的就是关它。**
收缩 = True

# 打完用 offscreen 平台起一次 exe，N 秒不退就说明 PySide6 / llama_cpp 那串依赖
# 真的加载起来了（起不来会当场退出并吐 traceback）。它顺带验证了 `收缩` 没砍坏东西。
自检 = True
自检秒 = 15

# 数据目录的空骨架。全是**空目录**，绝不带开发机上的 json —— 接口配置.json 里是 API 密钥。
数据骨架 = ['模型', os.path.join('模型', '_暂存'), '头像', '消息', '助手转录', '记忆']

# ── PySide6 瘦身清单 ────────────────────────────────────────────────
# 项目实测只 import QtCore / QtGui / QtWidgets（全项目 `from PySide6.X` 只有这三种），
# 下面这些一个都碰不到：
瘦身目录 = [
    'resources',      # 整个是 QtWebEngine 的 icudtl / *.pak，105MB
    'translations',   # Qt 自带翻译，60MB（本项目没有翻译需求）
    'qml',            # QML 运行时，30MB
]
瘦身通配 = [
    'Qt6WebEngine*.dll',    # 196MB，独占大头
    'QtWebEngine*.pyd',
    'Qt6Quick*.dll',
    'Qt6Qml*.dll',
    'Qt6Multimedia*.dll',
    'avcodec-*.dll', 'avformat-*.dll', 'avutil-*.dll',   # QtMultimedia 的 FFmpeg 底座
    'swresample-*.dll', 'swscale-*.dll',
    'qmlls.exe',
]
# ⚠ `plugins/`（18MB，最大单项 2.5MB）**整个保留**，不冒这个险；
#   `opengl32sw.dll`（20MB）也保留 —— 那是没显卡驱动时的软件渲染退路。

# llama_cpp 的 `lib/*.lib` 是链接期的导入库，运行时一个都不加载（21MB）。
瘦身后缀 = ['.lib']

# 有这些顶层条目不带 `__init__.py`，但是运行时真要用的 DLL 目录，得放行。
额外顶层 = {'numpy.libs'}

# ── 扫「外部依赖要用哪些标准库」时的过滤 ──────────────────────────
# 为什么非得扫：见 `扫标准库需求`。这两张表只负责**别把噪声扫进来**。
#
# 依赖包里免不了夹着 tests/ 和 test_*.py，它们 import 的东西运行时根本用不到。
# `tkinter` 尤其要命 —— 那会把 Tcl/Tk 一整套拖进 exe，十几 MB 打水漂。
扫跳过目录 = {'tests', 'test', 'testing', 'docs', 'doc', 'examples', 'example',
              'scripts', '__pycache__'}
扫挡掉模块 = {'tkinter', 'unittest', 'doctest', 'pdb', 'pydoc', 'venv', 'test',
              'idlelib', 'turtle', 'turtledemo', 'ensurepip', 'lib2to3',
              'curses', 'this', 'antigravity'}


# ══════════════════════════════════════════════════════════════════
#  小工具
# ══════════════════════════════════════════════════════════════════

ROOT = os.path.dirname(os.path.abspath(__file__))


def 修控制台():
    """Windows 控制台默认 GBK —— 不修的话本脚本第一条中文输出就崩。"""
    try:
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for 名 in ('stdout', 'stderr'):
        流 = getattr(sys, 名, None)
        设 = getattr(流, 'reconfigure', None)
        if 设 is None:
            continue
        if (getattr(流, 'encoding', '') or '').lower().replace('-', '') == 'utf8':
            continue
        try:
            设(encoding='utf-8', errors='replace')
        except Exception:
            pass


def 大小嘴(字节):
    """12345678 → '11.8 MB'"""
    for 单位 in ('B', 'KB', 'MB', 'GB'):
        if 字节 < 1024 or 单位 == 'GB':
            return '%.1f %s' % (字节, 单位)
        字节 /= 1024.0
    return ''


def 目录大小(径):
    if os.path.isfile(径):
        return os.path.getsize(径)
    if not os.path.isdir(径):
        return 0
    总 = 0
    for 根, _目们, 件们 in os.walk(径):
        for 件 in 件们:
            try:
                总 += os.path.getsize(os.path.join(根, 件))
            except OSError:
                pass
    return 总


def 步(号, 共, 说明):
    print('\n' + '─' * 62)
    print('  [%s/%s] %s' % (号, 共, 说明))
    print('─' * 62)


def 死(说明):
    """响亮地失败。这个项目最怕的就是「跳过一句就过」。"""
    print('\n' + '!' * 62)
    print('  ✗ ' + 说明)
    print('!' * 62)
    try:
        input('\n  按 Enter 退出...')
    except EOFError:                 # 非交互（CI / 重定向）没有 stdin
        pass
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════
#  [0] 环境自检
# ══════════════════════════════════════════════════════════════════

def 环境自检():
    步(0, 10, '环境自检')

    缺 = []
    try:
        import PyInstaller
        print('  PyInstaller   %s' % getattr(PyInstaller, '__version__', '?'))
    except Exception as 错:
        缺.append('PyInstaller（pip install pyinstaller）：%s' % 错)

    try:
        import packaging.requirements     # noqa: F401
        print('  packaging     在')
    except Exception as 错:
        缺.append('packaging（pip install packaging）：%s' % 错)

    import importlib.util as 导入工
    for 名 in ('PySide6', 'llama_cpp'):
        print('  %-13s %s' % (名, '在' if 导入工.find_spec(名) else '**缺**'))
        if 导入工.find_spec(名) is None:
            缺.append('%s 没装' % 名)

    print('  Python        %s' % sys.version.split()[0])

    if not os.path.isfile(os.path.join(ROOT, 入口)):
        缺.append('入口脚本不存在：%s' % 入口)

    if 缺:
        死('环境自检没过：\n     - ' + '\n     - '.join(缺))
    print('  环境齐 ✓')


# ══════════════════════════════════════════════════════════════════
#  [1] 清理
# ══════════════════════════════════════════════════════════════════

def 清理():
    """
    清掉旧产物。

    ⚠ **不吞错。** 删不掉只有一种原因——上一次跑起来的程序还在（冒烟自检留下的，
    见 `冒烟自检`）。要是配一句 `ignore_errors=True` 把它咽下去，后面就会以
    「复制依赖库时 WinError 32」的形式炸出来，离真正的原因隔着十万八千里。
    """
    步(1, 10, '清理旧产物')
    for 名 in (BUILD目录, DIST目录):
        if not os.path.exists(名):
            continue
        try:
            shutil.rmtree(名)
        except Exception as 错:
            死('删不掉 %s/（%s：%s）。\n'
               '     多半是上一次打包跑起来的 %s.exe 还活着，占着里面的文件。\n'
               '     去任务管理器结束它，或者：taskkill /F /IM %s.exe'
               % (名, type(错).__name__, 错, EXE名, EXE名))
        print('  已删 %s/' % 名)
    for 根, 目们, _件们 in os.walk(ROOT, topdown=False):
        if '__pycache__' in 目们:
            shutil.rmtree(os.path.join(根, '__pycache__'), ignore_errors=True)
    print('  清理完成 ✓')


# ══════════════════════════════════════════════════════════════════
#  [2] 依赖闭包
# ══════════════════════════════════════════════════════════════════

def _规范(名):
    """PEP 503 规范化：`llama_cpp_python` / `llama-cpp-python` 是同一个东西。"""
    return re.sub(r'[-_.]+', '-', 名).lower()


def 算闭包():
    """
    从 `根依赖` 递归算出全部传递依赖。

    ⚠ 两处必须做对，否则要么漏、要么炸：

      · **求值环境标记**（`; python_version < "3.9"` 之类）—— 本机跑不上的就不该收
      · **跳过 `extra == ...`** —— 那是可选依赖。不跳的话 modelscope 会把
        torch / tensorflow / datasets 全拖进来，37 个包变 648 个、几个 GB。

    返回 (发行包清单, 顶层条目集合, 没装上但被依赖的包名)。
    """
    import importlib.metadata as 元

    from packaging.requirements import Requirement

    名表 = {}
    for 发行 in 元.distributions():
        名 = 发行.metadata['Name']
        if 名:
            名表[_规范(名)] = 名

    收 = {}
    没装 = set()
    待办 = [_规范(r) for r in 根依赖]
    while 待办:
        键 = 待办.pop()
        if 键 in 收 or 键 in 没装:
            continue
        名 = 名表.get(键)
        if 名 is None:
            没装.add(键)
            continue
        收[键] = 名
        for 条 in (元.requires(名) or []):
            try:
                要 = Requirement(条)
            except Exception:
                continue                      # 元数据里写坏了的行，跳过
            if 要.marker is not None:
                if 'extra' in str(要.marker):
                    continue                  # 可选依赖，不要
                try:
                    if not 要.marker.evaluate({'extra': ''}):
                        continue
                except Exception:
                    pass
            待办.append(_规范(要.name))
    return 收, 没装


def 顶层条目(发行包名, 纯库目录):
    """
    一个发行包在 site-packages 里的顶层条目（`PySide6/`、`llama_cpp/`、
    `numpy.libs/`、`typing_extensions.py` …）。

    从 RECORD 里取首段，然后**只留运行时真能 import 的东西**：

      · 目录要有 `__init__.py`（真包），或者是 `numpy.libs` 这种 DLL 目录
      · 单个文件要是 `*.py` / `*.pyd` / `*.pyi`
      · `bin/` `include/` `lib/` `..` 一律扔掉 —— 实测 llama-cpp-python 的 RECORD
        里就带着 `bin/*.dll`（69MB，编译副产物），而运行时只认 `llama_cpp/lib/`
        （`llama_cpp/llama_cpp.py:38`），拷了纯属白占地方
    """
    import importlib.metadata as 元

    出 = set()
    try:
        件们 = 元.distribution(发行包名).files or []
    except Exception:
        return 出

    for 项 in 件们:
        段 = 项.parts
        if not 段:
            continue
        首 = 段[0]
        if not 首 or 首.startswith('.') or '..' in 段:
            continue
        if 首.endswith('.dist-info') or 首.endswith('.egg-info'):
            continue
        径 = os.path.join(纯库目录, 首)
        if not os.path.exists(径):
            continue
        if os.path.isdir(径):
            if 首 in 额外顶层 or os.path.isfile(os.path.join(径, '__init__.py')):
                出.add(首)
        elif 首.endswith(('.py', '.pyd', '.so')):
            出.add(首)
    return 出


# ══════════════════════════════════════════════════════════════════
#  [3] 依赖库
# ══════════════════════════════════════════════════════════════════

def 拷依赖库(顶层们, 收, 纯库目录):
    步(3, 10, '复制依赖库（%d 个顶层条目）' % len(顶层们))
    目标根 = os.path.join(DIST目录, 依赖库名)
    os.makedirs(目标根, exist_ok=True)
    忽略 = shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo')

    for 首 in sorted(顶层们, key=str.lower):
        源 = os.path.join(纯库目录, 首)
        if not os.path.exists(源):
            continue
        if os.path.isdir(源):
            shutil.copytree(源, os.path.join(目标根, 首), ignore=忽略)
        else:
            shutil.copy2(源, os.path.join(目标根, 首))
        print('  %-24s %8s' % (首 + ('/' if os.path.isdir(源) else ''),
                              大小嘴(目录大小(源))))

    # .dist-info 一起带过去：`importlib.metadata` 靠它报版本，
    # 少了它某些库（modelscope 之类）取 version() 会抛 PackageNotFoundError。
    # 体积可忽略，换的是「行为跟开发机一模一样」。
    import importlib.metadata as 元
    for 名 in sorted(收.values(), key=str.lower):
        try:
            发行 = 元.distribution(名)
        except Exception:
            continue
        元名 = None
        for 项 in (发行.files or []):
            if 项.parts and 项.parts[0].endswith(('.dist-info', '.egg-info')):
                元名 = 项.parts[0]
                break
        if 元名 is None:
            continue
        源 = os.path.join(纯库目录, 元名)
        if os.path.isdir(源):
            shutil.copytree(源, os.path.join(目标根, 元名), ignore=忽略)
    print('  依赖库 %s ✓' % 大小嘴(目录大小(目标根)))


def 拷解释器DLL():
    """
    把 `python3.dll` 拷进依赖库。**少了它整个 PySide6 起不来。**

    ⚠ 而且报错极其误导 —— 报的是

        DLL load failed while importing Shiboken: 找不到指定的模块

    看着像 PySide6 装坏了、或者被 `收缩` 砍多了，实际缺的是解释器自己的一小块。

    事情是这样：Python 安装根目录下有**两个** DLL——

      · `python313.dll`  真本体（几 MB）
      · `python3.dll`    稳定 ABI 的转发层（几十 KB），所有按 ABI 编译的扩展模块
                         导入表里链的都是**它**，由它转发到本体

    `Shiboken.pyd` 的导入表（实测）就是 `shiboken6.abi3.dll` + **`python3.dll`**。
    开发机上 `python3.dll` 躺在 `D:\\python13.3\\` 里，靠解释器目录被找到；
    **PyInstaller 只带 `python313.dll`，从不带 `python3.dll`**，
    `依赖库/` 里自然也没有 —— 冻结进程里就没人提供它了。

    放在 `依赖库/` 根下就行：运行时钩子把整个 `依赖库/` 加进了 DLL 搜索目录，
    那里面的任何 DLL 都找得到。
    """
    源 = os.path.join(sys.base_prefix, 'python3.dll')
    if not os.path.isfile(源):
        # 不是所有发行版都带稳定 ABI 层（有些精简版删了）。
        # 真缺了的话，PySide6 那条路会在冒烟自检里当场炸出来 —— 那时候再查。
        print('  ⚠ %s 不存在，跳过（冒烟自检会告诉你影不影响）' % 源)
        return
    目 = os.path.join(DIST目录, 依赖库名, 'python3.dll')
    shutil.copy2(源, 目)
    print('  python3.dll（稳定 ABI 转发层）  %s' % 大小嘴(os.path.getsize(目)))


# ══════════════════════════════════════════════════════════════════
#  [4] 收缩
# ══════════════════════════════════════════════════════════════════

def 收缩依赖库():
    if not 收缩:
        print('\n  收缩 = False，PySide6 原样保留。')
        return
    步(4, 10, '收缩 PySide6（砍零引用的巨物）')
    根 = os.path.join(DIST目录, 依赖库名, 'PySide6')
    if not os.path.isdir(根):
        死('依赖库里没有 PySide6/ —— 闭包算错了，停下来看看。')

    前 = 目录大小(根)
    删了 = 0

    for 名 in 瘦身目录:
        径 = os.path.join(根, 名)
        if os.path.isdir(径):
            n = 目录大小(径)
            shutil.rmtree(径, ignore_errors=True)
            删了 += n
            print('  - %-16s %8s' % (名 + '/', 大小嘴(n)))

    import fnmatch
    for 件名 in os.listdir(根):
        径 = os.path.join(根, 件名)
        if not os.path.isfile(径):
            continue
        中 = any(fnmatch.fnmatch(件名, 式) for 式 in 瘦身通配)
        尾 = any(件名.endswith(式) for 式 in 瘦身后缀)
        if 中 or 尾:
            n = os.path.getsize(径)
            os.remove(径)
            删了 += n
            print('  - %-40s %8s' % (件名, 大小嘴(n)))

    # llama_cpp 的 lib/*.lib：链接期导入库，运行时零加载
    for 子 in ('llama_cpp',):
        目 = os.path.join(DIST目录, 依赖库名, 子, 'lib')
        if os.path.isdir(目):
            for 件名 in os.listdir(目):
                if any(件名.endswith(式) for 式 in 瘦身后缀):
                    径 = os.path.join(目, 件名)
                    n = os.path.getsize(径)
                    os.remove(径)
                    删了 += n
            print('  - %-40s 链接期 .lib' % (子 + '/lib/'))

    后 = 目录大小(根)
    print('  PySide6 %s → %s，共省 %s ✓' % (大小嘴(前), 大小嘴(后), 大小嘴(删了)))


# ══════════════════════════════════════════════════════════════════
#  [5] exe
# ══════════════════════════════════════════════════════════════════

def 扫标准库需求():
    """
    外部依赖用到的**标准库**模块清单 —— 从**实际发出去的 `依赖库/`** 反推。

    ⚠ 这是 A 方案（代码进 exe、依赖全外置）必须补的一块，漏了必炸。

    起因：那些依赖全被 `--exclude-module` 排掉了，于是 PyInstaller 的静态图
    **走不进它们的代码**，也就看不见 urllib3 里那句
    `from http.client import IncompleteRead`。标准库那一块没被打进去，
    结果是 exe 起来了、PySide6 也起来了，然后死在

        ModuleNotFoundError: No module named 'http'

    （实打实踩到的：第一版就是这么死的。修完 `python3.dll` 紧接着撞上它。）

    所以这里反过来做：把 `依赖库/` 里每个包的 `.py` 扫一遍，挑出它们 import 的
    **标准库**，显式喂给 `--hidden-import`。第三方模块一概不要 —— 那些正是
    要留在 `依赖库/` 里的东西，补进 exe 就白拆了。

    扫描只取第一层，剩下的交给 PyInstaller 自己沿 import 往下走。
    """
    纯库 = sysconfig.get_paths()['purelib']
    标准 = set(sys.stdlib_module_names)
    库根 = os.path.join(DIST目录, 依赖库名)

    要 = set()
    扫过 = 0
    for 首 in sorted(os.listdir(库根)):
        if 首.endswith(('.dist-info', '.egg-info', '.pth')):
            continue
        源 = os.path.join(纯库, 首)
        if not os.path.exists(源):
            continue
        if os.path.isdir(源):
            件们 = []
            for 根, 目们, fs in os.walk(源):
                目们[:] = [d for d in 目们 if d not in 扫跳过目录]
                件们 += [os.path.join(根, f) for f in fs
                        if f.endswith('.py') and not f.startswith('test_')
                        and not f.endswith('_test.py')]
        elif 首.endswith('.py'):
            件们 = [源]
        else:
            continue

        for 件 in 件们:
            try:
                # 有些依赖里的正则写字面量会触发 SyntaxWarning，压掉免得刷屏
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    树 = ast.parse(open(件, encoding='utf-8', errors='ignore').read())
            except Exception:
                continue
            扫过 += 1
            for 节 in ast.walk(树):
                if isinstance(节, ast.Import):
                    for 别 in 节.names:
                        要.add(别.name)
                elif isinstance(节, ast.ImportFrom):
                    if 节.level:
                        continue                 # 相对导入，包内的事
                    if 节.module:
                        要.add(节.module)
                        # `from http import client` 那种，光看 module 会漏掉 client
                        for 别 in 节.names:
                            要.add(节.module + '.' + 别.name)

    出 = set()
    for 名 in 要:
        if 名.split('.')[0] not in 标准:
            continue
        if 名.split('.')[0] in 扫挡掉模块:
            continue
        try:
            # `from dataclasses import dataclass` 会拼出 `dataclasses.dataclass`，
            # 那不是模块。交给 PyInstaller 只会换回一条 warning，先自己筛掉。
            if 导入工.find_spec(名) is not None:
                出.add(名)
        except Exception:
            pass
    print('  扫了 %d 个 .py → 需要补进 exe 的标准库 %d 个' % (扫过, len(出)))
    return 出


# ══════════════════════════════════════════════════════════════════
#  图标
# ══════════════════════════════════════════════════════════════════

def _抠四角(图):
    """
    把四个圆角外那块底色抠成透明。

    从四个角**各自**泛洪一次：底色被圆角那道弧切成了四块互不相连的"口袋"，
    所以泛洪一走到图案边界就停，**碰不到图案内部**——哪怕图案里也有近乎同色的
    浅色（比如那几片浅金瓦）。这正是不用"把所有白像素变透明"那种做法的理由。

    容差也不能省：JPEG 在圆角那道弧上留了一圈振铃，逐个像素比的话，
    边界上会剩一圈没抠干净的底。
    """
    from PIL import ImageDraw
    出 = 图.copy()
    宽, 高 = 出.size
    for 角 in ((0, 0), (宽 - 1, 0), (0, 高 - 1), (宽 - 1, 高 - 1)):
        ImageDraw.floodfill(出, 角, (0, 0, 0, 0), thresh=图标容差)
    return 出


def 备图标():
    """
    找源图 → 产两样东西，返回 `(ico路径, png路径)`；没源图返回 `(None, None)`。

      · `build/图标/鸿胪寺.ico` —— 多分辨率，喂 `--icon`（资源管理器 / 桌面快捷方式上那个）
      · `dist/鸿胪寺.png`       —— **跟 exe 同级**，喂运行时的 `app.setWindowIcon`
                                   （窗口左上角 / 任务栏上那个）

    两样都出自**同一张处理过的图**（裁切 + 抠角），所以任务栏和资源管理器里
    是同一个样子，不会一个圆角一个白角。

    两类源图都能用：
      · 已经是正方形、主体占满 —— 原样（`图标裁切 = 1.0`）
      · 画布大、主体小 —— 调小 `图标裁切`，中心裁到那一块再放大

    ⚠ 这一步**失败不致命**。图标是锦上添花，缺了 exe 照样跑；
       把构建卡在这里，等于让一张图挡住整个发布。
    """
    from PIL import Image, ImageDraw     # noqa: F401  （ImageDraw 在 _抠四角 里用）

    源 = None
    for 尾 in 图标后缀们:
        径 = os.path.join(ROOT, 图标名 + 尾)
        if os.path.isfile(径):
            源 = 径
            break
    if 源 is None:
        print('  ⚠ 没找到图标源（%s + %s 之一），不出图标。'
              % (图标名, '/'.join(图标后缀们)))
        return None, None

    目 = os.path.join(BUILD目录, '图标')
    os.makedirs(目, exist_ok=True)
    os.makedirs(DIST目录, exist_ok=True)     # 步骤顺序一变，dist 还没影的时候也多
    ico = os.path.join(目, 图标名 + '.ico')
    png = os.path.join(DIST目录, 图标名 + '.png')

    try:
        # `.ico` 源图：自己原样当 exe 图标，同时摊平成 png 供运行时用。
        if 源.lower().endswith('.ico'):
            if os.path.abspath(源) != os.path.abspath(ico):
                shutil.copy2(源, ico)
            Image.open(源).convert('RGBA').save(png, 'PNG')
            print('  图标: %s（本来就是 ico，原样用 + 摊平成 png）' % os.path.basename(源))
            return ico, png

        图 = Image.open(源)
        if 图.mode != 'RGBA':
            图 = 图.convert('RGBA')
        宽, 高 = 图.size

        if 图标裁切 < 1.0:
            切宽, 切高 = int(宽 * 图标裁切), int(高 * 图标裁切)
            图 = 图.crop(((宽 - 切宽) // 2, (高 - 切高) // 2,
                         (宽 - 切宽) // 2 + 切宽, (高 - 切高) // 2 + 切高))

        if 图标抠角:
            图 = _抠四角(图)

        # 等比塞进正方形画布，四周透明留白 —— **不拉伸、不切内容**
        宽, 高 = 图.size
        边 = max(宽, 高)
        画布 = Image.new('RGBA', (边, 边), (0, 0, 0, 0))
        画布.paste(图, ((边 - 宽) // 2, (边 - 高) // 2))

        画布.save(ico, 'ICO', sizes=[(s, s) for s in 图标尺寸])
        # 运行时那份发全分辨率：Qt 要自己缩到标题栏那十几像素，
        # 先缩成 256 再让它缩第二次，白丢一层清晰度。
        画布.save(png, 'PNG')

        print('  图标: %s → %s（%d 档，%s）+ %s（%s）'
              % (os.path.basename(源), os.path.relpath(ico, ROOT),
                 len(图标尺寸), 大小嘴(os.path.getsize(ico)),
                 os.path.relpath(png, ROOT), 大小嘴(os.path.getsize(png))))
        return ico, png
    except Exception as 错:
        print('  ⚠ 图标转换失败（%s：%s），这次不出图标。' % (type(错).__name__, 错))
        return None, None


# ══════════════════════════════════════════════════════════════════
#  运行时钩子
# ══════════════════════════════════════════════════════════════════

_钩子模板 = '''# -*- coding: utf-8 -*-
# 打包.py 生成的 PyInstaller 运行时钩子。**不要手改**，改 打包.py 里的 `_钩子模板`。
#
# 它跑在 PyInstaller bootstrap 阶段，**早于** 酒馆.py 里那些 from PySide6... 的 import，
# 干的只有一件事：把 exe 同级的「依赖库/」挂进 sys.path，让外置的第三方依赖能被 import 到。
#
# sys.path 插在 0 号位 = 优先于冻结包里的任何东西。
import os
import sys

基 = os.path.dirname(os.path.abspath(sys.executable))
库 = os.path.join(基, '__依赖库__')
if os.path.isdir(库):
    sys.path.insert(0, 库)
    if hasattr(os, 'add_dll_directory'):
        os.add_dll_directory(库)
else:
    # 依赖库缺席 —— **必须出声**，而且得让双击的人看得见。
    #
    # 这是这个包最容易被踩的一步（"我只把 exe 拷走了"）。窗口模式下**没有终端**，
    # 所以不能靠 stderr（那东西在无控制台时是 None，写它会 AttributeError，
    # 把人从"缺依赖"引到一条完全无关的报错上）。走一个原生弹窗：
    # 一句人话，总好过甩一条 ModuleNotFoundError。
    提示 = ('找不到 __依赖库__/ 目录。\\n\\n'
           '请把 __依赖库__/ 和 __EXE__.exe 放在同一个目录里再启动。\\n\\n'
           '（现在这个 exe 在：%s）' % 基)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, 提示, '__EXE__ — 启动失败', 0x10)
    except Exception:
        pass
    try:
        sys.stderr.write(提示 + '\\n')
    except Exception:
        pass
    # 直接走人。再往下就是一条注定失败的 import 链，报出来的东西只会误导人。
    os._exit(2)
'''


def 转排除模块(顶层们):
    """
    顶层条目 → `--exclude-module` 能吃的模块名。

    大部分条目名字就是模块名（`PySide6`、`llama_cpp`、`typing_extensions.py`），
    但有两类不是，得挑出来：

      · `numpy.libs/` —— DLL 目录，不是模块，没有可排除的东西
      · `81d243bd...__mypyc.cp313-win_amd64.pyd` —— 数字开头，本来就不是个能 import 的名字

    排除清单允许比依赖库清单**窄**（排不掉的东西最坏也只是多占点 exe 体积），
    但绝不能比它**宽** —— 宽了就是把运行时真会 import 的模块从 exe 里踢出去。
    """
    出 = set()
    for 首 in 顶层们:
        名 = 首
        if 名.endswith('.libs'):
            continue
        if 名.endswith('.py'):
            名 = 名[:-3]
        elif 名.endswith(('.pyd', '.so')):
            名 = 名.split('.')[0]          # 去掉 ABI 后缀
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', 名):
            出.add(名)
    return 出


def 写运行时钩子():
    径 = os.path.join(BUILD目录, '_rth_依赖库.py')
    os.makedirs(BUILD目录, exist_ok=True)
    源 = _钩子模板.replace('__依赖库__', 依赖库名).replace('__EXE__', EXE名)
    with open(径, 'w', encoding='utf-8') as f:
        f.write(源)
    return os.path.abspath(径)


def 打EXE(排除们, 钩子径, 图标径=None):
    步(5, 10, 'PyInstaller 打 exe（--onefile --windowed）')

    # 项目自己的模块：静态 import 图本来就收得到，这里显式兜底。
    # ⚠ 打包.py 必须排除 —— 它是构建脚本，不该进发行包。
    自家 = sorted(f[:-3] for f in os.listdir(ROOT)
                 if f.endswith('.py') and f not in (入口, 本脚本))

    # 外部依赖要用的标准库：图里走不到它们，必须自己扫出来补上。
    标准库 = 扫标准库需求()
    # `collections.abc` 扫得出来（`find_spec` 认它），但 PyInstaller 的模块图里
    # 它不是一个独立模块 —— 真身是 `_collections_abc`，只是被 `collections`
    # 挂了个别名。喂给它只会换回一行
    #     ERROR: Hidden import 'collections.abc' not found
    # 而 `collections` 本身也在清单里，早就把它带进去了，所以这句纯噪音。
    标准库 -= {'collections.abc'}

    指令 = [
        sys.executable, '-m', 'PyInstaller',
        '--name=' + EXE名,
        '--onefile',
        # 窗口模式：双击只出 Qt 窗口，不弹黑色终端。
        # ⚠ 代价是 `酒馆.py` 里那些 `print(..., file=sys.stderr)` 的告警没地方去了
        #   （`print` 在 file 为 None 时安静地什么都不做，不会抛），
        #   所以启动期的致命错误改走原生弹窗 —— 见 `_钩子模板`。
        '--windowed',
        '--noconfirm',
        '--log-level=WARN',
        '--distpath=' + os.path.abspath(DIST目录),
        '--workpath=' + os.path.abspath(工作目录),
        '--specpath=' + os.path.abspath(工作目录),
        '--runtime-hook=' + 钩子径,
    ]
    if 图标径:
        指令.append('--icon=' + os.path.abspath(图标径))
    # 依赖库装什么，exe 就排什么 —— 同一份真源。
    for 名 in sorted(排除们, key=str.lower):
        指令.append('--exclude-module=' + 名)
    for 名 in 自家:
        指令.append('--hidden-import=' + 名)
    for 名 in sorted(标准库):
        指令.append('--hidden-import=' + 名)
    指令.append(入口)

    print('  入口      : %s' % 入口)
    print('  排除模块  : %d 个（= 依赖库的顶层条目）' % len(排除们))
    print('  兜底 hidden-import: %d 个项目模块 + %d 个标准库模块'
          % (len(自家), len(标准库)))
    print('  >> python -m PyInstaller ... %s\n' % 入口)

    果 = subprocess.run(指令, cwd=ROOT)
    if 果.returncode != 0:
        死('PyInstaller 失败（returncode=%s）。上面的输出就是原因。' % 果.returncode)

    径 = os.path.join(DIST目录, EXE名 + '.exe')
    if not os.path.isfile(径):
        死('PyInstaller 说成功了，但 %s 不在。' % 径)
    print('  %s  %s ✓' % (径, 大小嘴(os.path.getsize(径))))


# ══════════════════════════════════════════════════════════════════
#  [6] 冒烟自检
# ══════════════════════════════════════════════════════════════════

def 杀进程树(程):
    """
    把冒烟自检起的那个进程**连子进程一起**干掉。

    ⚠ 必须 `taskkill /T`，不能只 `Popen.terminate()`：PyInstaller 的 onefile 是
    **两层** —— 外层的引导进程解压完就 spawn 一个子进程去真正跑程序。
    `terminate()` 打在外层引导进程上，**里层那个会活下来**。

    实打实踩到的：一次冒烟自检之后，一个 `鸿胪寺.exe` 悄没声地活到了下一次打包，
    把 `dist/` 锁住 —— 构建死在「复制依赖库 WinError 32」上，而那个报错跟真正
    的原因隔着十万八千里。顺带一提，那次能查出根因，全靠 `清理` 被改成不吞错。

    ⚠ `taskkill` 走绝对路径：这台机器的 PATH 里**没有 System32**
    （隔壁 `GPU-model/编译GPU版.bat` 开头也记着这个坑），直接写命令名会
    FileNotFoundError。
    """
    if sys.platform == 'win32':
        工具 = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                           'System32', 'taskkill.exe')
        try:
            subprocess.run([工具, '/F', '/T', '/PID', str(程.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    try:
        程.terminate()
    except Exception:
        pass
    try:
        程.wait(timeout=15)
    except Exception:
        try:
            程.kill()
            程.wait(timeout=10)
        except Exception:
            pass


def 冒烟自检():
    """
    offscreen 平台起一次 exe：依赖链（PySide6 + llama_cpp + 那串传递依赖）只要
    有一个没挂上，进程会立刻退出并吐 traceback；15 秒还活着就算过。

    它同时验证了 `收缩` 没砍坏东西 —— offscreen 平台插件就在
    `PySide6/plugins/platforms/` 里，砍错了当场现形。

    ⚠ 这一步会**让程序真的跑一遍**，所以它会自己把 `酒馆数据/` 建出来。
       收尾的 `重建数据骨架()` 会把它整个删掉重来，保证发行包里是干净的空壳。
    """
    if not 自检:
        print('\n  自检 = False，跳过冒烟自检。')
        return
    步(6, 10, '冒烟自检（offscreen 起 %d 秒）' % 自检秒)

    exe = os.path.abspath(os.path.join(DIST目录, EXE名 + '.exe'))
    环境 = dict(os.environ)
    环境['QT_QPA_PLATFORM'] = 'offscreen'          # 不弹窗
    环境['PYTHONIOENCODING'] = 'utf-8'

    print('  启动 %s ...' % exe)
    起 = time.time()
    try:
        程 = subprocess.Popen([exe], cwd=os.path.abspath(DIST目录), env=环境,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding='utf-8', errors='replace')
    except Exception as 错:
        死('起不来：%s' % 错)

    time.sleep(自检秒)
    活 = 程.poll() is None
    # ⚠ 现在就量。下面 terminate + wait 还要花好几秒，等收尾完再量，
    #   打出来的就不是"跑了多久没退"，而是"跑了多久没退 + 我们杀它花了多久"。
    跑过 = time.time() - 起

    if 活:
        杀进程树(程)
    try:
        出 = 程.communicate(timeout=10)[0] or ''
    except Exception:
        出 = ''

    if 活:
        print('  跑了 %.0f 秒还在 → 依赖链通 ✓' % 跑过)
        if 出.strip():
            print('  （期间输出）')
            for 行 in 出.strip().splitlines()[-15:]:
                print('    ' + 行)
    else:
        print('  ✗ %.1f 秒就退了（returncode=%s）。下面是它的输出：\n' %
              (跑过, 程.returncode))
        for 行 in (出 or '（空）').strip().splitlines()[-40:]:
            print('    ' + 行)
        死('冒烟自检没过 —— 这个包发出去是起不来的。\n'
           '     先试 `收缩 = False` 重打（PySide6 砍多了）；还是不行就把\n'
           '     PySide6 改回打进入 exe（去掉 --exclude-module=PySide6，换 --collect-all=PySide6）。')


# ══════════════════════════════════════════════════════════════════
#  [7] 配套文件夹
# ══════════════════════════════════════════════════════════════════

def 重建数据骨架():
    步(7, 10, '建数据目录空骨架')
    根 = os.path.join(DIST目录, 数据名)
    # 冒烟自检可能留了程序自己写的东西 —— 整个删掉重来，保证是空壳
    if os.path.exists(根):
        shutil.rmtree(根, ignore_errors=True)
    for 子 in 数据骨架:
        os.makedirs(os.path.join(根, 子), exist_ok=True)
    print('  %s/' % 数据名)
    for 子 in 数据骨架:
        print('    %s/' % 子)
    print('  （全是空目录；程序第一次运行也会自己建）✓')


def 拷配套():
    步(8, 10, '拷配套文件夹')
    源 = os.path.join(ROOT, GPU目录)
    目 = os.path.join(DIST目录, GPU目录)
    if not os.path.isdir(源):
        print('  ⚠ %s/ 不存在，跳过' % GPU目录)
        return
    if os.path.exists(目):
        shutil.rmtree(目, ignore_errors=True)
    shutil.copytree(源, 目)
    print('  %s/  %s ✓' % (GPU目录, 大小嘴(目录大小(目))))


def 写使用说明():
    步(9, 10, '写使用说明.txt')
    内容 = """╔══════════════════════════════════════════════════════════╗
║                    鸿胪寺 — 使用说明                      ║
╚══════════════════════════════════════════════════════════╝

【启动】
  双击  %(exe)s.exe  → 主界面直接打开。
  没有黑色终端窗口跟着弹出来。

【目录说明】
  %(exe)s.exe      主程序（全部 Python 代码 + Python 运行时）
  %(库)s/          第三方依赖（PySide6 / llama_cpp / numpy …）
  %(数据)s/        你的全部数据（角色卡、会话、记忆、模型…）
  %(png)s     软件图标（窗口和任务栏上那张；删了不影响能用，只是图标变默认的）
  %(gpu)s/         想自己重编 CUDA 版 llama 的话看这里
  本文件           就是它

【⚠ 两条硬规矩】
  1. %(库)s/ 必须和 %(exe)s.exe 待在同一个目录里，不能拆开、不能改名。
     缺了它程序起不来 —— 会**弹个框**告诉你，不是悄没声地没反应。
  2. 整个文件夹一起拷走。只拷 exe 是跑不起来的。

【数据都在 %(数据)s/，备份就备份它】
  角色卡.json 接口配置.json 会话.json  小表（一张表一个文件）
  头像/  消息/  记忆/  助手转录/       会长大的大件（一条一个文件）
  %(模型子)s/                        下载的 GGUF 放这里
  ⚠ 接口配置.json 里存着你的 API 密钥，别往外发。

【关于本地模型（显存）】
  %(库)s/llama_cpp 里的 CUDA 版是按打包这台机器（RTX 5050 / sm_120）编的。
  换一台显卡不一样的电脑，本地模型可能起不来 —— 那种情况去 %(gpu)s/
  重编一份 CUDA 版，或者把「接口配置」里的 GPU 层数改成 0 走纯 CPU。
  在别的机器上想重编，双击 %(gpu)s/编译GPU版.bat。

【要连网吗】
  只用在线 API 的话，不需要梯子。
  下模型走 modelscope / hf-mirror 不用梯子；走 huggingface.co 才需要。
""" % {
        'exe': EXE名,
        '库': 依赖库名,
        '数据': 数据名,
        'gpu': GPU目录,
        'png': 图标名 + '.png',
        '模型子': os.path.join(数据名, '模型'),
    }
    径 = os.path.join(DIST目录, '使用说明.txt')
    with open(径, 'w', encoding='utf-8') as f:
        f.write(内容)
    print('  %s ✓' % 径)


# ══════════════════════════════════════════════════════════════════
#  [10] 出口校验 + 摘要
# ══════════════════════════════════════════════════════════════════

def 出口校验(顶层们, 图标png=None):
    步(10, 10, '出口校验')

    缺 = []
    if not os.path.isfile(os.path.join(DIST目录, EXE名 + '.exe')):
        缺.append('%s.exe' % EXE名)

    # 有图标源就必须有这张 png —— 运行时的 setWindowIcon 全靠它，
    # 缺了不报错、只是窗口和任务栏退回 Qt 默认图标，属于"静默变形"，得拦。
    if 图标png and not os.path.isfile(图标png):
        缺.append(os.path.relpath(图标png, ROOT))

    库根 = os.path.join(DIST目录, 依赖库名)
    # exe 里排掉了这些模块，依赖库里就必须有 —— 少一个都不行。
    # 抽查几个「缺了不崩、只静默变形」的：llama_cpp（本地模型全废）、
    # numpy（llama_cpp 的硬依赖）、yaml（modelscope 读配置）。
    for 必 in ['PySide6' + os.sep + '__init__.py',
               'PySide6' + os.sep + 'QtWidgets.pyd',
               'PySide6' + os.sep + 'plugins' + os.sep + 'platforms',
               'llama_cpp' + os.sep + 'llama_cpp.py',
               'llama_cpp' + os.sep + 'lib' + os.sep + 'llama.dll',
               'numpy' + os.sep + '__init__.py',
               'requests' + os.sep + '__init__.py',
               'modelscope' + os.sep + '__init__.py',
               'huggingface_hub' + os.sep + '__init__.py',
               # 稳定 ABI 转发层。少了它 PySide6 全线起不来，而报错报的是
               # 「找不到 shiboken」—— 见 `拷解释器DLL` 那段。
               'python3.dll']:
        if not os.path.exists(os.path.join(库根, 必)):
            缺.append(os.path.join(依赖库名, 必))

    数据根 = os.path.join(DIST目录, 数据名)
    for 子 in 数据骨架:
        if not os.path.isdir(os.path.join(数据根, 子)):
            缺.append(os.path.join(数据名, 子))

    # 开发机的私货绝不许混进发行包
    for 私 in ('接口配置.json', '会话.json', '角色卡.json', '记忆.json',
               '助手设置.json', '助手任务.json'):
        if os.path.exists(os.path.join(数据根, 私)):
            缺.append('⚠ 发行包里混进了开发机数据：%s（里面可能有 API 密钥）'
                      % os.path.join(数据名, 私))

    if 缺:
        死('出口校验没过，缺：\n     - ' + '\n     - '.join(缺))
    print('  %s ✓' % (
        '、'.join(['exe', '%s（%d 个顶层条目）' % (依赖库名, len(顶层们)), 数据名])))


def 摘要():
    print('\n' + '=' * 62)
    print('  ✅ 打包完成 — dist/')
    print('=' * 62)
    总 = 0
    for 名 in sorted(os.listdir(DIST目录)):
        径 = os.path.join(DIST目录, 名)
        n = 目录大小(径)
        总 += n
        print('  %-22s %10s%s' % (名, 大小嘴(n), '/' if os.path.isdir(径) else ''))
    print('  ' + '-' * 40)
    print('  %-22s %10s' % ('合计', 大小嘴(总)))
    print("""
  发布 = 把整个 dist/ 文件夹一起拷走（不能只拷 exe）。
  exe 和 %s/ 必须同级，缺了起不来。
""" % 依赖库名)
    try:
        input('  按 Enter 退出...')
    except EOFError:
        pass


# ══════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════

def main():
    修控制台()
    os.chdir(ROOT)

    print('=' * 62)
    print('  鸿胪寺 打包 —— 一体化 exe + %s + 配套文件夹' % 依赖库名)
    print('=' * 62)
    print('  exe 装代码，第三方依赖进 %s/，两者同级' % 依赖库名)
    print('  PySide6 收缩：%s   冒烟自检：%s' % ('开' if 收缩 else '关',
                                              '开' if 自检 else '关'))

    try:
        纯库目录 = sysconfig.get_paths()['purelib']

        环境自检()
        清理()

        步(2, 10, '算依赖闭包')
        收, 没装 = 算闭包()
        print('  %d 个发行包（从 %d 个根包递归）' % (len(收), len(根依赖)))
        if 没装:
            print('  ⚠ 声明了但没装的（跳过）：%s' % '、'.join(sorted(没装)))

        顶层们 = set()
        for 键 in 收:
            顶层们 |= 顶层条目(收[键], 纯库目录)
        for 必 in ('llama_cpp', 'PySide6'):
            if 必 not in 顶层们:
                死('闭包里没有 %s —— 依赖算错了，停下来看看。' % 必)
        print('  → %d 个顶层条目：%s' % (len(顶层们),
                                      '、'.join(sorted(顶层们, key=str.lower))))

        拷依赖库(顶层们, 收, 纯库目录)
        拷解释器DLL()
        收缩依赖库()

        钩子 = 写运行时钩子()
        ico, 图标png = 备图标()
        打EXE(转排除模块(顶层们), 钩子, ico)

        冒烟自检()
        重建数据骨架()
        拷配套()
        写使用说明()
        出口校验(顶层们, 图标png)
        摘要()

    except KeyboardInterrupt:
        print('\n\n  用户取消。')
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as 错:
        import traceback
        traceback.print_exc()
        死('构建出错：%s：%s' % (type(错).__name__, 错))


if __name__ == '__main__':
    main()
