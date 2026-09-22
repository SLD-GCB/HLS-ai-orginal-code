#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包.py — 鸿胪寺 打包脚本

    python 打包.py          （或者双击 打包.bat）

打出来的东西是「一体化 exe + 依赖库 + 配套文件夹」，在 `dist/` 下：

    鸿胪寺.exe    全部 Python 代码 + Python 运行时 + 标准库（**不含**任何第三方依赖）
    依赖库/       `根依赖` 那 5 个根包 + 从它们递归算出来的**全部传递依赖** ——
                  运行时由 exe 挂进 sys.path。numpy / jinja2（llama_cpp 拖进来的）、
                  pyyaml / httpx / tqdm（huggingface_hub 拖进来的）都从这条路进来，
                  **不手写清单**（见下面「三个支点」第 1 条）
    酒馆数据/     空骨架（模型/ 模型/_暂存/ 头像/ 消息/ 助手转录/ 记忆/）
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
   **所以 `酒馆.py` 和根目录下每一个业务模块一行都不用改** —— 业务模块的清单是
   **现扫的**（`打EXE` 里的 `自家`，只排除入口和本脚本），**往里加模块不用动这个脚本**。
   （2026-09-21 加 `本地agent协议.py` 时验过：它自动就在清单里。
   ⚠ 这里从前写的是"23 个业务模块"，那种数字一加模块就过期 —— 别再把数写进注释。）

3. **DLL 不用额外操心**：PySide6 自己会 `os.add_dll_directory(PySide6包目录)`
   （`PySide6/__init__.py:59`），llama_cpp 自己按 `__file__` 找同级 `lib/`
   （`llama_cpp/llama_cpp.py:38`）。外置之后这两条路照样成立。

# 本地模型为什么非得带两套 DLL

`依赖库/llama_cpp/` 底下有**两个库目录**，是**同一份 Python 代码的两套 DLL**：

    lib/        CUDA 版（自己编的，含 ggml-cuda.dll）
    lib_cpu/    CPU 版（从官方 CPU wheel 里抽的，6.5MB，零 CUDA 依赖）

运行时由 `酒馆本地._挑后端` 二选一，靠 `LLAMA_CPP_LIB_PATH` 指过去
（那是 `llama_cpp.py:36` 原生支持的覆盖点，**一个字节的第三方代码都不用改**）。

为什么非得两套 —— CUDA 那套的依赖链是：

    llama.dll → ggml.dll → ggml-cuda.dll → cublas64_13.dll → cublasLt64_13.dll

Windows 的 `LoadLibrary` 要求**整条链**都满足，缺一环就整个失败。那两个 cublas
不在 wheel 里，在 CUDA 工具包（`CUDA_PATH\bin\x64`，加起来 492MB），
所以**任何没装 CUDA 13 工具包的机器都起不来 `import llama_cpp`**，
本地模型整条链路当场全废（界面上就是那句"引擎状态：没装上"）。

⚠ **这件事跟有没有显卡无关，跟 GPU 层数设几也无关** —— 有 N 卡但只装了驱动的
机器一样起不来（缺的是 cublas，不是驱动），`n_gpu_layers=0` 也走不到那一步。
而打包这台机器装了工具包，`llama_cpp` 自己会把 `CUDA_PATH\bin` 挂进 DLL 搜索
路径（`_ctypes_extensions.py:74`），**所以在开发机上永远看不出来**。

于是两件事都是必需的，各修一半：

  · `备CUDA运行时()` 把那两个 cublas 拷进 `lib/` —— 让 CUDA 那套在别人机器上
    真能加载起来（体积代价 492MB，换来的是"装了 N 卡驱动的机器都能吃 GPU"）
  · `备CPU后端()` 抽一套零 CUDA 依赖的 DLL 进 `lib_cpu/` —— 没卡、没驱动的
    机器上它是**唯一**能跑的那套

⚠ `lib_cpu` 的版本号必须跟装着的那个**完全一致**：Python 代码只留一份
（`lib/` 那份），两边 DLL 版本对不上就是"静默变形"，所以 `备CPU后端` 里
拿版本号比一遍，对不上就 `死()`。
"""

import ast
import fnmatch
import glob
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

# ── 本地模型的两套后端 ──────────────────────────────────────────────
# 为什么两套：见文件头「本地模型为什么非得带两套 DLL」。一句话 —— CUDA 那套的
# 依赖链断在 cublas 上，没装 CUDA 13 工具包的机器连 import 都进不去。
#
# CPU 那套存这个目录名。**同一个 llama_cpp 包里**，所以 Python 代码只有一份。
CPU后端目录 = 'lib_cpu'
# 官方 CPU wheel 的来源。**别删 `--extra-index-url`**：PyPI 上同名 wheel 是别的
# 构建，只有这个索引上那份是稳定的 CPU 版。
CPU后端索引 = 'https://abetlen.github.io/llama-cpp-python/whl/cpu'
# 下过的 wheel 存这儿（7MB）。**不在 build/ 底下** —— build/ 每次构建都整个删掉，
# 存那儿等于每次都重下，断网就构建不了。这里是「构建缓存」，清理步骤不碰它。
CPU后端缓存 = '构建缓存'

# ── 编译环境（让别的机器也能自己重编 CUDA 版框架）────────────────────
# 带不带。开着 = 往 dist/ 里塞一份精简过的 VS + Win SDK + CUDA + 便携 Python
# （实测约 1.4GB）；关掉 = 常规包（约 850MB），别人只能重编出 CPU 版。
# 精简表见 `编译环境清单`，为什么剔那些写了依据。
打编译环境 = True
编译环境名 = '编译环境'

# cublas 那两个 DLL 没有开关，**只要 lib/ 里是 CUDA 版就必须补齐**。
# 不给开关是有意的：不补的话，这个包在别人机器上加载失败、静默退到 CPU，
# 而界面上什么都看不出来 —— 正是这次要根除的那种坏法（见文件头）。
# cublas 的文件名从 ggml-cuda.dll 里**现读**（`_缺的CUDA库`），不写死 ——
# 换 CUDA 版本重编之后，链的是 cublas64_12 还是 13 由 DLL 自己说了算。

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


def _装的包目录(包名):
    """
    site-packages 里那个包的目录。没装回 None。

    ⚠ 用 `find_spec` 而不是 `import`：**import 是有副作用的** —— llama_cpp 一
    import 就去加载 DLL，而打包这台机器正好是"能加载"的那台。拿它的成功去
    推断"发出去也能加载"，就是自己骗自己（这次踩的正是这个坑，见文件头）。
    这里只要路径，不要加载。
    """
    try:
        规 = 导入工.find_spec(包名)
    except Exception:
        return None
    if 规 is None:
        return None
    径们 = list(getattr(规, 'submodule_search_locations', None) or [])
    return 径们[0] if 径们 else None


def _导入的DLL(PE径):
    """
    读 PE 的**导入表**，回它静态依赖的 DLL 名字集合。读不出来回空集。

    ⚠ **不能用"在文件字节里搜 .dll 字符串"代替它。** DLL 里到处是日志文本、
      错误提示、别的库的名字，扫出来的比真依赖多得多；照着拷要么塞进一堆
      不相干的文件，要么（更坏）往 `lib/` 里放一个跟系统同名的 DLL，
      把系统的那个盖掉。导入表才是加载器**真会去找**的那几个，一个不多一个不少。

    只解 PE32/PE32+ 的导入目录，够用了 —— 这里只用它来问"还依赖谁"。
    """
    import struct
    try:
        原 = open(PE径, 'rb').read()
    except OSError:
        return set()
    try:
        pe = struct.unpack_from('<I', 原, 0x3c)[0]
        if 原[pe:pe + 4] != b'PE\0\0':
            return set()
        节数 = struct.unpack_from('<H', 原, pe + 6)[0]
        可选长 = struct.unpack_from('<H', 原, pe + 20)[0]
        位 = struct.unpack_from('<H', 原, pe + 24)[0]
        目录偏 = pe + 24 + (112 if 位 == 0x20b else 96)     # 数据目录表：PE32+ 多 16 字节
        导入RVA = struct.unpack_from('<I', 原, 目录偏 + 8)[0]
        节表 = pe + 24 + 可选长
        节们 = []
        for i in range(节数):
            o = 节表 + 40 * i
            # ⚠ 节表这 4 个字段的顺序是 (VirtualSize, VirtualAddress,
            #   SizeOfRawData, PointerToRawData) —— **长度在前、地址在后**。
            #   名字写反了不会报错，只会算出个离谱的偏移、然后一个符号都读不到
            #   （第一次就这么写的，回来的是空集、还看着挺像"这个 DLL 没依赖"）。
            虚长, 虚址, _, 文址 = struct.unpack_from('<IIII', 原, o + 8)
            节们.append((虚址, 虚长, 文址))

        def 转(RVA):
            for 虚址, 虚长, 文址 in 节们:
                if 虚址 <= RVA < 虚址 + max(虚长, 1):
                    return 文址 + (RVA - 虚址)
            return None

        出 = set()
        偏 = 转(导入RVA)
        if 偏 is None:
            return 出
        while True:
            块 = 原[偏:偏 + 20]
            if len(块) < 20 or 块 == b'\0' * 20:       # 全零那项是导入表结束
                break
            名RVA = struct.unpack_from('<I', 块, 12)[0]
            if 名RVA:
                名偏 = 转(名RVA)
                if 名偏 is not None:
                    出.add(原[名偏:原.index(b'\0', 名偏)].decode('ascii', 'replace'))
            偏 += 20
        return 出
    except Exception:
        return set()


def _CUDA缺的(ggml_cuda径):
    """
    CUDA 那套还缺哪些**外部** DLL。回 `{库名: 本机路径}` —— 要补的就是它们，
    已经齐全时回空字典。

    ⚠ **必须沿导入表追到底，不能只看第一层。** ggml-cuda.dll 直接链的是
      cublas64_13.dll，而 cublas64_13.dll 自己又链着 cublasLt64_13.dll（442MB）。
      只看一层会拷进去一个"自己都加载不起来"的 cublas，整条链照样断 —— 而且
      断得**更隐蔽**：`lib/` 里多了一个文件，看起来"补过了"。第一次构建就是
      这么栽的，被 [7] 双后端自检当场抓住。那道自检就是为了这种事存在的。

    ⚠ 只收"CUDA 工具包里找得到"的（`_找CUDA库` 就是这个范围）。Windows 和 MSVC
      自带的那串（KERNEL32 / MSVCP140 / VCRUNTIME140 / api-ms-win-crt-*）
      在工具包里没有，自然落选 —— 它们本来该由系统提供，拷进包只会多个跟系统
      同名的 DLL 去抢搜索顺序（第一次构建就捞了个 VCRUNTIME140 进来）。
      `nvcuda.dll` 也在这条线上：它是**驱动**给的、运行时按名字动态找，
      少它**不会**导致加载失败（找不到就退 CPU），跟 cublas 不是一回事。
    """
    if not os.path.isfile(ggml_cuda径):
        return {}
    库目录 = os.path.dirname(ggml_cuda径)
    缺 = {}
    待看 = [ggml_cuda径]
    看过 = set()
    while 待看:
        径 = 待看.pop()
        if 径 in 看过:
            continue
        看过.add(径)
        for 名 in sorted(_导入的DLL(径)):
            if os.path.isfile(os.path.join(库目录, 名)):
                continue                        # 同级目录里已经有了
            源 = _找CUDA库(名)
            if 源 is None:
                continue                        # 系统自带的，不归我们管
            if 名 not in 缺:
                缺[名] = 源
                待看.append(源)                  # 它自己还可能有依赖，接着追
    return 缺


def _找CUDA库(件名):
    """
    在 **CUDA 工具包**里找 cublas64_13.dll 这类运行库，回路径；找不到回 None。

    两个地方，按"最可能先命中"的顺序：

      `CUDA_PATH\\bin\\x64`   CUDA 13 起运行库挪进了 x64 子目录（12 及以前直接在 bin）
      `CUDA_PATH\\bin`

    ⚠ **只找工具包，不搜 PATH。** 搜 PATH 会捞到一堆无关的东西 —— 第一次就
      顺着 PATH 把 `D:\\python13.3\\VCRUNTIME140.dll` 当成"缺的 CUDA 库"拷进
      `lib/` 了。MSVC 运行库（VCRUNTIME140 / MSVCP140）和 KERNEL32 那串
      api-ms-win-* 是**系统的事**：现在的包一直靠系统提供它们、也一直好使，
      拷一份进 `lib/` 只会多一个跟系统同名的 DLL 去抢搜索顺序。
      **工具包里找不到，就不该是我们补的。**

    ⚠ 只找运行库，不碰 nvcc/头文件/静态库 —— 那是**编译**要的东西，
      跟这里的"让编好的 DLL 能加载"是两件不同的事。
    """
    CUDA = os.environ.get('CUDA_PATH') or os.environ.get('CUDA_HOME')
    if not CUDA:
        return None
    for 子 in (os.path.join('bin', 'x64'), 'bin'):
        径 = os.path.join(CUDA, 子, 件名)
        if os.path.isfile(径):
            return 径
    return None


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
    步(0, 16, '环境自检')

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

    for 名 in ('PySide6', 'llama_cpp'):
        在 = 导入工.find_spec(名) is not None
        print('  %-13s %s' % (名, '在' if 在 else '**缺**'))
        if not 在:
            缺.append('%s 没装' % 名)

    # 这次装的是哪一套 llama_cpp、外部依赖缺不缺 —— 提前摆出来。
    # ⚠ 这里只**看**不拷（真拷在 [5] 备CUDA运行时）。而且**只读路径不 import**：
    #   一 import 就会去加载 DLL，打包这台机器正好是能加载的那台，
    #   拿它的成功去推断"发出去也能加载"就是自欺（这次踩的正是这个坑）。
    包 = _装的包目录('llama_cpp')
    if 包:
        ggml_cuda = os.path.join(包, 'lib', 'ggml-cuda.dll')
        if os.path.isfile(ggml_cuda):
            print('  llama_cpp     CUDA 版（自己编的，lib/ggml-cuda.dll 在）')
            要 = _CUDA缺的(ggml_cuda)
            if 要:
                print('  CUDA 运行时   [5] 要补 %d 个：%s'
                      % (len(要), '、'.join('%s %s' % (n, 大小嘴(os.path.getsize(p)))
                                            for n, p in sorted(要.items()))))
            else:
                print('  CUDA 运行时   看着齐了（**但这里说了不算**：'
                      '真判据是 [7] 双后端自检）')
            if not (os.environ.get('CUDA_PATH') or os.environ.get('CUDA_HOME')):
                print('  ⚠ CUDA_PATH / CUDA_HOME 都没设 —— [5] 全靠它找 cublas，'
                      '找不到就只能发个加载不起来的包（[7] 会拦下）')
        else:
            print('  llama_cpp     纯 CPU 版（lib/ 里没有 ggml-cuda.dll）')

    # 编译环境：要打就得这台机器凑得齐（VS + Win SDK + CUDA + Python）
    if 打编译环境:
        _, 编缺 = 编译环境清单()
        if 编缺:
            缺.append('打编译环境 = True，但这台机器凑不齐工具链，缺：\n'
                      '        %s\n'
                      '        装齐再构建；或者把 打包.py 顶上的 打编译环境 改成 False\n'
                      '        （包会小 1.3GB，但别人拿到手里没法重编 GPU 版）。'
                      % '、'.join(编缺))
        else:
            来源 = _CUDA根(), _MSVC根(), _SDK根()[1]
            print('  编译环境      齐（CUDA %s / MSVC %s / Win SDK %s / Python %s）'
                  % (_CUDA版本(_CUDA根()) or '?',
                     os.path.basename(来源[1] or '?'), 来源[2] or '?',
                     '%d.%d.%d' % sys.version_info[:3]))
    else:
        print('  编译环境      不带（打编译环境 = False，包小 1.3GB）')

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
    步(1, 16, '清理旧产物')
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
    步(3, 16, '复制依赖库（%d 个顶层条目）' % len(顶层们))
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
#  [4][5][7] 本地模型的两套后端
# ══════════════════════════════════════════════════════════════════

def _找轮子(目录, 版本):
    """缓存里找个版本对得上、win_amd64 的 wheel。没有回 None。"""
    if not os.path.isdir(目录):
        return None
    for 件名 in sorted(os.listdir(目录)):
        低 = 件名.lower()
        if not 低.endswith('.whl') or not 低.startswith('llama_cpp_python-'):
            continue
        if 低.startswith('llama_cpp_python-%s-' % 版本) and 'win_amd64' in 低:
            return os.path.join(目录, 件名)
    return None


def 备CPU后端():
    """
    抽一套 CPU 版 DLL 到 `依赖库/llama_cpp/lib_cpu/`。

    从**官方 CPU wheel** 里抽，不自己编：那个轮子就是 CPU 构建，7MB，
    `py3-none` 所以跟装着的 CPython 版本无关，抽 `llama_cpp/lib/*.dll` 就完事。

    ⚠ **`.lib` 一个都不抽**：那是链接期的导入库，运行时零加载（跟 `收缩依赖库`
      里砍掉 CUDA 那份 `.lib` 是同一个道理，21MB）。这里干脆不进包。

    ⚠ **版本必须对得上**。Python 代码只留一份（`lib/` 里那份），`lib_cpu/`
      只出 DLL。两边版本不一致 = 用 0.3.34 的绑定去调 0.3.35 的 C 接口，
      能不能跑全看运气，而且坏起来是"某个参数悄悄不生效"那种，最难查。
      所以这里比一遍版本号，比不齐就 `死()`。
    """
    步(4, 16, '备 CPU 后端（从官方 CPU wheel 抽一套 DLL）')
    import importlib.metadata as 元
    try:
        版本 = 元.version('llama-cpp-python')
    except Exception as 错:
        死('读不到 llama-cpp-python 的版本号（%s）。' % 错)

    缓存 = os.path.join(ROOT, CPU后端缓存)
    os.makedirs(缓存, exist_ok=True)
    轮子 = _找轮子(缓存, 版本)
    if 轮子 is None:
        print('  %s\\ 里没有 %s 的轮子，去官方 CPU 索引下（约 7MB）...'
              % (CPU后端缓存, 版本))
        指令 = [sys.executable, '-m', 'pip', 'download',
                'llama-cpp-python==%s' % 版本, '--only-binary=:all:', '--no-deps',
                '--extra-index-url', CPU后端索引, '-d', 缓存]
        果 = subprocess.run(指令, cwd=ROOT)
        if 果.returncode != 0:
            死('下 CPU wheel 失败。\n'
               '     要么网络不通，要么这个版本在 %s 上没有 win_amd64 轮子。\n'
               '     也可以自己下好丢进 %s\\ 再重跑（脚本只用不删）。'
               % (CPU后端索引, CPU后端缓存))
        轮子 = _找轮子(缓存, 版本)
        if 轮子 is None:
            死('下完了但 %s\\ 里还是找不到 %s 的 win_amd64 轮子。' % (CPU后端缓存, 版本))
    print('  轮子：%s（%s）' % (os.path.basename(轮子), 大小嘴(os.path.getsize(轮子))))

    import zipfile
    try:
        z = zipfile.ZipFile(轮子)
        名们 = z.namelist()
    except Exception as 错:
        死('这个 wheel 读不开（%s：%s）：%s' % (type(错).__name__, 错, 轮子))

    # 版本再核一遍：缓存目录里躺着的是**文件**，文件名能被改、也可能下错版本，
    # 真正说了算的是轮子里那个 dist-info 目录名。
    if not any(n.startswith('llama_cpp_python-%s.dist-info/' % 版本) for n in 名们):
        死('轮子里的版本跟装着的不一致：要 %s，轮子是 %s。\n'
           '     删掉 %s\\ 里那份重新构建，让它重下。'
           % (版本, 轮子, CPU后端缓存))

    要 = {os.path.basename(n): n for n in 名们
          if n.startswith('llama_cpp/lib/') and n.lower().endswith('.dll')}
    必需 = ('llama.dll', 'ggml.dll', 'ggml-cpu.dll', 'ggml-base.dll')
    少 = [n for n in 必需 if n not in 要]
    if 少:
        死('这个 wheel 的 llama_cpp/lib/ 里少了 %s —— 不像是个能用的 CPU 轮子：%s'
           % ('、'.join(少), 轮子))
    CUDA物 = sorted(n for n in 要 if 'cuda' in n.lower())
    if CUDA物:
        死('这个 wheel 里带着 CUDA 的东西（%s）—— 它不是纯 CPU 版，\n'
           '     当兜底用等于白兜（没驱动/没工具包的机器上照样起不来）。'
           % '、'.join(CUDA物))

    目标 = os.path.join(DIST目录, 依赖库名, 'llama_cpp', CPU后端目录)
    if os.path.isdir(目标):
        shutil.rmtree(目标)
    os.makedirs(目标)
    总 = 0
    for 件名 in sorted(要):
        原 = z.read(要[件名])
        with open(os.path.join(目标, 件名), 'wb') as f:
            f.write(原)
        总 += len(原)
        print('    %-18s %8s' % (件名, 大小嘴(len(原))))
    print('  → %s\\%s\\  %s ✓（%d 个 DLL，版本 %s 跟 Python 代码对得上）'
          % (依赖库名, os.path.join('llama_cpp', CPU后端目录),
             大小嘴(总), len(要), 版本))


def 备CUDA运行时():
    """
    把 CUDA 那套 DLL 的**外部依赖**补齐 —— 现在只有 cublas 这两个。

    ⚠ **不补这一手，CUDA 那套在别人机器上根本加载不了。** `llama_cpp` 只把
      wheel 里自带的 DLL 拷了，`ggml-cuda.dll` 静态依赖的 `cublas64_13.dll`
      在 CUDA 工具包里、不在 wheel 里。详细链条见文件头。

    ⚠ **缺哪个文件是现读 ggml-cuda.dll 得到的，不是写死的清单** —— 换 CUDA
      版本重编之后链的是 cublas64_12 还是 13，由 DLL 自己说了算。
    """
    步(5, 16, '备 CUDA 运行时（补 cublas）')
    lib = os.path.join(DIST目录, 依赖库名, 'llama_cpp', 'lib')
    ggml_cuda = os.path.join(lib, 'ggml-cuda.dll')
    if not os.path.isfile(ggml_cuda):
        print('  这套 llama_cpp 里没有 ggml-cuda.dll（装的是纯 CPU 版），'
              '没有要补的。')
        return
    # ⚠ 必须**在拷贝之前**算：这个函数只报"同级目录里没有的"，拷完再算永远是空集。
    要 = _CUDA缺的(ggml_cuda)
    if not 要:
        print('  没有要补的（cublas 已经在 lib/ 里了，或者这机器上找不到它）。')
        return
    for 件名, 源 in sorted(要.items()):
        shutil.copy2(源, os.path.join(lib, 件名))
        print('  + %-20s %8s  ← %s' % (件名, 大小嘴(os.path.getsize(源)), 源))
    剩 = sorted(_CUDA缺的(ggml_cuda))
    if 剩:
        死('拷完了还缺 %s —— 别往下走了，先查清楚。' % '、'.join(剩))
    print('  CUDA 那套的依赖链齐了 ✓（%d 个）' % len(要))


def _试加载(库目录, 剥CUDA):
    """
    起个子进程，只干一件事：把 `依赖库/` 挂到 sys.path 最前，import llama_cpp。

    回 `(成功?, 输出)`。**不加载模型** —— 这一步要证的是"DLL 能不能进来"，
    加载模型是另一码事（要几 GB 内存/显存和几十秒）。

    ⚠ **两边都要剥掉 CUDA 工具包再试**，因为那才是别人机器的样子。不剥的话
      这个自检在打包机上永远过 —— 打包机装着工具包，`llama_cpp` 会把
      `CUDA_PATH\\bin` 挂进 DLL 搜索路径（`_ctypes_extensions.py:74`），
      缺的 cublas 就从那儿被捞到了，**等于没检**。这次踩的正是这个坑。
    """
    环境 = dict(os.environ)
    环境['PYTHONIOENCODING'] = 'utf-8'
    if 库目录:
        # ⚠ **必须是绝对路径**：`os.add_dll_directory` 不吃相对路径（WinError 87
        #   "参数错误"，报出来完全看不出是路径的锅）。dist/ 在构建脚本里是相对
        #   路径写的，所以这儿得自己转一道。第一次构建就栽在这个 87 上。
        库目录 = os.path.abspath(库目录)
        环境['LLAMA_CPP_LIB_PATH'] = 库目录
        环境['MTMD_CPP_LIB'] = 库目录
    else:
        环境.pop('LLAMA_CPP_LIB_PATH', None)
        环境.pop('MTMD_CPP_LIB', None)
    if 剥CUDA:
        环境.pop('CUDA_PATH', None)
        环境.pop('CUDA_HOME', None)
        环境['PATH'] = os.pathsep.join(
            p for p in (环境.get('PATH') or '').split(os.pathsep)
            if p and 'cuda' not in p.lower())
    码 = ('import sys, importlib\n'
          'sys.path.insert(0, %r)\n'
          'm = importlib.import_module("llama_cpp")\n'
          'print("BACKEND-OK", m.__version__)\n'
          % os.path.abspath(os.path.join(DIST目录, 依赖库名)))
    果 = subprocess.run([sys.executable, '-c', 码], cwd=ROOT, env=环境,
                        capture_output=True, text=True, encoding='utf-8',
                        errors='replace')
    出 = ((果.stdout or '') + (果.stderr or '')).strip()
    return (果.returncode == 0 and 'BACKEND-OK' in (果.stdout or '')), 出


def 双后端自检():
    """
    拿**要发出去的那份真货**，两套各 import 一次，两边都得成。

    这是本次改动最该有的一道自检：以前"在开发机上一切正常"是真的正常，
    因为开发机装着 CUDA 工具包；发出去就废。所以判据不能是"我这儿行不行"，
    得是**剥掉工具包之后还行不行**。
    """
    步(8, 16, '双后端自检（剥掉 CUDA 工具包，两套各 import 一次）')
    包 = os.path.join(DIST目录, 依赖库名, 'llama_cpp')
    有CUDA = os.path.isfile(os.path.join(包, 'lib', 'ggml-cuda.dll'))
    有CPU = os.path.isdir(os.path.join(包, CPU后端目录))
    坏 = []

    if 有CUDA:
        ok, 出 = _试加载(None, 剥CUDA=True)
        print('  %s CUDA 那套（lib/）：%s' % ('✓' if ok else '✗',
                                            '能加载' if ok else '起不来'))
        if not ok:
            坏.append('CUDA 那套（lib/）起不来：\n      ' + 出.replace('\n', '\n      '))
    else:
        print('  - 这套里没有 CUDA 版（ggml-cuda.dll 不在），跳过。')

    if 有CPU:
        ok, 出 = _试加载(os.path.join(包, CPU后端目录), 剥CUDA=True)
        print('  %s CPU 兜底那套（%s/）：%s'
              % ('✓' if ok else '✗', CPU后端目录, '能加载' if ok else '起不来'))
        if not ok:
            坏.append('CPU 那套（%s/）起不来：\n      ' % CPU后端目录
                      + 出.replace('\n', '\n      '))
    else:
        坏.append('%s/ 不在 —— CPU 兜底没备上' % CPU后端目录)

    if 坏:
        死('双后端自检没过 —— 这个包发出去，本地模型会在某些机器上全废：\n\n'
           '     ' + '\n\n     '.join(坏))
    print('  两套在"没装 CUDA 工具包"的前提下都能加载 ✓')


# ══════════════════════════════════════════════════════════════════
#  [12] 编译环境（让别的机器也能自己重编 CUDA 版框架）
# ══════════════════════════════════════════════════════════════════
#
# 一份 CUDA 版的框架只认一种显卡架构（这份是 sm_120 / RTX 50 系）。别的卡想用
# GPU，就得**在那台机器上重编一次**。重编要一整套工具链，而这些机器上多半没有，
# 所以把它一起带上 —— 但**用不到的一律不带**：原样 7.2GB，这张表挑完约 1.4GB。
#
# 剔掉的三大笔（每笔都有依据，不是"看着像用不到"）：
#
#   1. `CUDA/lib/x64` 里的三个静态库 2.27GB
#      （nvrtc_static 1.09G + nvJitLink_static 1.01G + nvptxcompiler_static 165M）
#      依据在本机 CMake 的 `FindCUDAToolkit.cmake:1369`：
#          cudart_static DEPS cudart_static_deps     ← 只有 Threads / dl
#      那三个只被 `CUDA::nvJitLink_static` 这类**静态**目标用，ggml-cuda 不碰。
#      剔完 `lib/x64` 从 2289MB 掉到 20MB。
#   2. `CUDA/bin/x64` 里的数学运行库 1.58GB（cufft/cusolver/cusparse/curand/npp*/
#      nvrtc/nvJitLink…）—— 那是**跑**的时候要的，编的时候用的是 `lib/x64` 里的
#      `.lib`。而且 cublas 那两个**运行库** `依赖库/llama_cpp/lib/` 里已经有了
#      （见 `备CUDA运行时`），不重复带。
#   3. MSVC 的 x86 / onecore 库（850MB）、Win SDK 的 winrt + cppwinrt 头（216MB）
#      和各架构的 Lib —— 我们只编 x64。
#
# ⚠ **便携 Python 不是官方 embeddable 包。** embeddable 里没有 `include/` 和
#   `libs/python313.lib`，而编一个 Python 扩展模块**必须有**这两个。所以是
#   "拷本机这份 CPython、再把 site-packages 砍到只剩构建要的那几个包"。
#   Windows 上的 CPython 靠 exe 自己的位置定位标准库，**搬走照样能用**。
#
# ⚠ 建不建得起来，只有"拿它真编一次"能证（见 计划里的验证步骤）。所以这张表
#   每一条都写了为什么留它 —— 下次谁要动，知道该对哪句话负责。

#: 便携 Python 里只留这几个包。都是构建链上的一环：
#:   scikit-build-core 是 llama-cpp-python 的构建后端（要 packaging + pathspec）
#:   cmake / ninja 是它调用的构建工具（cmake 那份还带着 FindCUDAToolkit.cmake）
#:   pip 负责跑这次构建；setuptools / wheel 是它消费产物的老底子
Python包白名单 = ('pip', 'setuptools', 'wheel', 'cmake', 'ninja',
                  'scikit_build_core', 'packaging', 'pathspec', 'typing_extensions')


def _首个(模式们):
    """按顺序试几个 glob，回第一个有命中的最后一个（版本号最大的那个）。"""
    for 模 in 模式们:
        中 = sorted(glob.glob(模))
        if 中:
            return 中[-1]
    return None


def _CUDA根():
    """
    本机的 CUDA 工具包根。找不到回 `None`。

    ⚠ 认的是**带 nvcc 的那份**。只装驱动、只有运行库的机器这里会回 None ——
      那种机器本来就编不了，早点说清楚比编到一半报错强。
    """
    候选 = [os.environ.get('CUDA_PATH'), os.environ.get('CUDA_HOME')]
    基 = r'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA'
    if os.path.isdir(基):
        候选 += [os.path.join(基, 名) for 名 in sorted(os.listdir(基), reverse=True)]
    for 径 in 候选:
        if 径 and os.path.isfile(os.path.join(径, 'bin', 'nvcc.exe')):
            return 径
    return None


def _MSVC根():
    """
    本机 MSVC 工具链目录（`...\\VC\\Tools\\MSVC\\<版本>`）。找不到回 `None`。

    先认 `VCToolsInstallDir`（从 vcvars 那种环境里进来时它是现成的、最准），
    再按"装了哪个版本就在哪个目录"去 glob（VS 有四个装法：Community /
    Professional / Enterprise / BuildTools，都得试）。
    """
    候选 = [os.environ.get('VCToolsInstallDir')]
    for 壳 in (r'C:\Program Files\Microsoft Visual Studio',
               r'C:\Program Files (x86)\Microsoft Visual Studio'):
        for 版 in ('2022', '2019'):
            候选 += sorted(glob.glob(os.path.join(壳, 版, '*', 'VC', 'Tools', 'MSVC', '*')),
                           reverse=True)
    for 径 in 候选:
        if 径 and os.path.isfile(os.path.join(径, 'bin', 'Hostx64', 'x64', 'cl.exe')):
            return 径.rstrip('\\/')
    return None


def _CRT目录():
    """MSVC 运行库的 x64 重分发目录。**cl.exe 自己就要这几个 DLL。**"""
    return _首个([
        r'C:\Program Files\Microsoft Visual Studio\2022\*\VC\Redist\MSVC\*\x64\Microsoft.VC*.CRT',
        r'C:\Program Files (x86)\Microsoft Visual Studio\2022\*\VC\Redist\MSVC\*\x64\Microsoft.VC*.CRT',
        r'C:\Program Files\Microsoft Visual Studio\2019\*\VC\Redist\MSVC\*\x64\Microsoft.VC*.CRT',
    ])


def _SDK根():
    """
    本机 Windows SDK。回 `(根, 版本号)`，找不到回 `(None, '')`。

    版本号挑**三处都齐**的那个（Include 有 um\\windows.h、Lib 有 um\\x64\\*.lib、
    bin 有 rc.exe）—— 挑错版本的话，错会拖到编译进行到一半才报出来。
    """
    根 = None
    for 候选 in (r'C:\Program Files (x86)\Windows Kits\10',
                 os.environ.get('WindowsSdkDir') or ''):
        if 候选 and os.path.isdir(os.path.join(候选, 'Include')):
            根 = 候选
            break
    if 根 is None:
        return None, ''
    for 名 in sorted(os.listdir(os.path.join(根, 'Include')), reverse=True):
        if (os.path.isfile(os.path.join(根, 'Include', 名, 'um', 'windows.h'))
                and os.path.isfile(os.path.join(根, 'Lib', 名, 'um', 'x64', 'kernel32.lib'))
                and os.path.isfile(os.path.join(根, 'bin', 名, 'x64', 'rc.exe'))):
            return 根, 名
    return 根, ''


def 编译环境清单():
    """
    `编译环境/` 要装什么。回 `(条目们, 缺的部件)`，条目是 `(源, 包内相对, 规则)`。

    规则是 dict（键都可以不给，见 `_拷按规则`）。**源全在本机现找**，不写死
    版本号 —— VS 装的是 14.44 还是 14.50、SDK 是 26100 还是别的，这条机器自己说了算。
    """
    CUDA = _CUDA根()
    MSVC = _MSVC根()
    CRT = _CRT目录()
    SDK, SDK版 = _SDK根()
    PY = sys.base_prefix
    纯库 = sysconfig.get_paths()['purelib']

    缺 = []
    if CUDA is None:
        缺.append('CUDA 工具包（带 nvcc 的那种，装完设好 CUDA_PATH）')
    if MSVC is None:
        缺.append('Visual Studio 的 C++ 工具链（cl.exe）')
    if CRT is None:
        缺.append('MSVC 运行库重分发目录（VC\\Redist\\...\\Microsoft.VC*.CRT）')
    if SDK is None or not SDK版:
        缺.append('Windows SDK（rc.exe / mt.exe / um 头与库）')

    条 = []
    if CUDA:
        条 += [
            # bin/：nvcc 全家（含 nvcc.profile、ptxas、nvlink、cudafe++、tileiras…）
            # ⚠ 跳过 x64/ 子目录 —— 那 1.6GB 全是运行时数学库，编译用不到
            (os.path.join(CUDA, 'bin'), 'CUDA/bin', {'跳目录': {'x64'}}),
            # bin/x64 只留支撑件（工具万一要找的运行时小 DLL）
            (os.path.join(CUDA, 'bin', 'x64'), 'CUDA/bin/x64',
             {'留通配': ('cudart64_*.dll', 'nvfatbin*.dll')}),
            # nvvm/：libnvvm + libdevice + **cicc.exe（80MB，nvcc 的前端，必带）**
            (os.path.join(CUDA, 'nvvm'), 'CUDA/nvvm', None),
            (os.path.join(CUDA, 'include'), 'CUDA/include', None),
            # 链接要用的 .lib：剔掉三个静态巨物（依据见上面 [1]）
            (os.path.join(CUDA, 'lib', 'x64'), 'CUDA/lib/x64',
             {'跳文件': {'nvrtc_static.lib', 'nvJitLink_static.lib',
                        'nvptxcompiler_static.lib'}}),
            (os.path.join(CUDA, 'lib', 'cmake'), 'CUDA/lib/cmake', None),
        ]
    if MSVC:
        # ⚠ **MSVC 要照真身的形状摆到 `VS/VC/Tools/MSVC/<版本>/` 底下，一个子目录
        #   都不剔、一层都不压平。**
        #
        #   2026-09-22 把"为什么"挖到底了（在这之前全是猜，猜错的方向记在下面）：
        #   Windows 上的 nvcc 认宿主编译器，靠的是**顺着 cl.exe 往上找"VS 装在哪"**
        #   —— 它认的标记是 `VC\Auxiliary\Build\vcvarsall.bat`。所以：
        #     · 必须摆成 `VS/VC/Tools/MSVC/<版本>/bin/Hostx64/x64/cl.exe`（推得上去）
        #     · 上面还得有 `VS/VC/Auxiliary/Build/vcvarsall.bat`（见 `铺vs骨架`）
        #   缺一层，nvcc 就只给一句
        #       nvcc fatal : Host compiler targets unsupported OS.
        #   —— 那句话跟"操作系统"毫无关系。
        #
        #   ⚠ 之前那版摆成 `编译环境/MSVC/`（直接等于 `<版本>` 那一层），上面既没有
        #     `VC` 也没有 `Auxiliary`。所以当时"补齐 Auxiliary/crt/CodeAnalysis/
        #     modules"全没用 —— **补错了层**。层级、ASCII 路径、中文路径、目录联接、
        #     把原祖先目录名补齐，也都不是原因；同一份 cl.exe（逐字节相同）只要
        #     位置不对就不认。
        #
        #   整棵 +1.1GB（连 x86 / onecore 的库都带）：因为我们**仍然不知道 nvcc
        #   除了那个标记还会碰什么**。谁要精简，先拿 `酒馆编译.py` 在目标机器上真编一次。
        条.append((MSVC, 'VS/VC/Tools/MSVC/' + os.path.basename(MSVC), None))
    if CRT:
        条.append((CRT, 'VS/VC/Redist/CRT', None))
    if SDK and SDK版:
        条 += [
            (os.path.join(SDK, 'Include', SDK版, 'ucrt'), 'WinSDK/Include/ucrt', None),
            (os.path.join(SDK, 'Include', SDK版, 'um'), 'WinSDK/Include/um', None),
            (os.path.join(SDK, 'Include', SDK版, 'shared'), 'WinSDK/Include/shared', None),
            (os.path.join(SDK, 'Lib', SDK版, 'um', 'x64'), 'WinSDK/Lib/um/x64', None),
            (os.path.join(SDK, 'Lib', SDK版, 'ucrt', 'x64'), 'WinSDK/Lib/ucrt/x64', None),
            # rc.exe / mt.exe 要和旁边那一堆依赖 DLL 待在一起（编译GPU版.bat 里
            # 也记着这个坑：光搬 rc/mt 会报 0xc0000135）
            (os.path.join(SDK, 'bin', SDK版, 'x64'), 'WinSDK/bin', None),
        ]
    # ── 便携 Python ──
    # Lib/ 里除了 site-packages 还有一堆玩具（test 137MB、idlelib、tkinter…），
    # 全靠 '跳目录' 剔掉。site-packages 单独按白名单一项项拷。
    条 += [
        (os.path.join(PY, 'python.exe'), 'Python/python.exe', None),
        (os.path.join(PY, 'python3.dll'), 'Python/python3.dll', None),
        (os.path.join(PY, 'python313.dll'), 'Python/python313.dll', None),
        (os.path.join(PY, 'vcruntime140.dll'), 'Python/vcruntime140.dll', None),
        (os.path.join(PY, 'vcruntime140_1.dll'), 'Python/vcruntime140_1.dll', None),
        (os.path.join(PY, 'DLLs'), 'Python/DLLs', None),
        # 编扩展模块要这两个：Python.h 和 python313.lib（embeddable 包里没有）
        (os.path.join(PY, 'include'), 'Python/include', None),
        (os.path.join(PY, 'libs'), 'Python/libs', None),
        (os.path.join(PY, 'Lib'), 'Python/Lib',
         {'跳目录': {'site-packages', 'test', 'tests', 'tkinter', 'turtledemo',
                    'idlelib', 'lib2to3', 'ensurepip', '__pycache__'}}),
    ]
    for 名 in ('ninja.exe', 'cmake.exe'):
        径 = os.path.join(PY, 'Scripts', 名)
        if os.path.isfile(径):
            条.append((径, 'Python/Scripts/' + 名, None))
    条 += _Python包条目(纯库)
    # 源码包：带上的话，重编**不用联网**（pip 直接从这个目录里取 sdist）。
    # 它是 74.9MB —— 在 1.3GB 里不算什么，换来的是"这条路不依赖 PyPI 通不通"。
    源 = _源码包路()
    if 源:
        条.append((源, '源码包/' + os.path.basename(源), None))
    else:
        缺.append('llama-cpp-python 的源码包（sdist）')
    return 条, 缺


#: "VS 骨架"里要摆的那几个文件。名字是从 nvcc 自己的字符串表里抠出来的：
#: 它就是照这几个名字去找 VS 装在哪（`vcvarsall.bat` 是入口，另外三个是同一个
#: 目录里的兄弟）。**多摆两个不花钱，少一个就换 nvcc 版本时才炸**。
vs骨架文件 = ('vcvarsall.bat', 'vcvars64.bat', 'vcvars32.bat', 'vcvarsamd64_arm64.bat')


def 铺vs骨架(目标根):
    """
    在包里摆出那层"VS 形状" —— nvcc 认宿主编译器要的就是它。

    只写**空占位**（就一行注释）：`酒馆编译.py` 的 `铺环境()` 会用 `VSCMD_VER`
    让 nvcc **跳过跑它**，所以内容无所谓。反过来，真塞一份打包机上的
    `vcvarsall.bat` 是有害的 —— 那份会把环境重新指到"打包机装好的 VS"上，
    目标机器上根本没有那个位置，等于把这个包打回"必须装 VS 才能编"。

    回：写下的文件数。
    """
    目 = os.path.join(目标根, 'VS', 'VC', 'Auxiliary', 'Build')
    os.makedirs(目, exist_ok=True)
    for 名 in vs骨架文件:
        with open(os.path.join(目, 名), 'w', encoding='ascii') as f:
            f.write('@echo off\n'
                    'rem Placeholder: nvcc only checks that this file exists, and\n'
                    'rem must never run it. Do not put a real vcvarsall.bat here.\n')
    return len(vs骨架文件)


def _源码包路():
    """`构建缓存/` 里那份 sdist。没有就下一个（见 `备源码包`）。"""
    版本 = _装着的版本()
    目 = os.path.join(ROOT, CPU后端缓存)
    if not os.path.isdir(目) or not 版本:
        return None
    for 件 in sorted(os.listdir(目)):
        if 件.startswith('llama_cpp_python-%s' % 版本) and 件.endswith(('.tar.gz', '.zip')):
            return os.path.join(目, 件)
    return None


def _装着的版本():
    """装着的 llama-cpp-python 版本。没装回空串。"""
    try:
        import importlib.metadata as 元
        return 元.version('llama-cpp-python')
    except Exception:
        return ''


def 备源码包():
    """
    把 llama-cpp-python 的**源码包**（sdist，74.9MB）下到 `构建缓存/`。

    为什么非要它：包里那份便携 Python 要重编，源码从哪来？从这个文件来。
    没有它，重编就得去 PyPI 拉 —— 那样目标机器**没网就编不了**，而"带上编译环境"
    这件事的整个意义就是让那台机器自足。顺带它还锁死了源码版本。

    ⚠ sdist 里带着 llama.cpp 的全部源码（那 74.9MB 基本都是它），所以别再想办法
      "只下一部分"—— 那就是它最小的样子。
    """
    步(6, 16, '备源码包（重编要用的 sdist）')
    版本 = _装着的版本()
    if not 版本:
        死('读不到 llama-cpp-python 的版本号。')
    缓存 = os.path.join(ROOT, CPU后端缓存)
    os.makedirs(缓存, exist_ok=True)
    源 = _源码包路()
    if 源 is None:
        print('  %s\\ 里没有 %s 的源码包，去下一个（约 75MB）...' % (CPU后端缓存, 版本))
        果 = subprocess.run([sys.executable, '-m', 'pip', 'download',
                            'llama-cpp-python==%s' % 版本,
                            '--no-binary', ':all:', '--no-deps',
                            '--no-build-isolation', '-d', 缓存], cwd=ROOT)
        if 果.returncode != 0:
            死('下源码包失败。\n'
               '     要么网络不通，要么这个版本没发 sdist。\n'
               '     也可以自己下好丢进 %s\\ 再重跑。' % CPU后端缓存)
        源 = _源码包路()
        if 源 is None:
            死('%s\\ 里还是找不到 %s 的源码包。' % (CPU后端缓存, 版本))
    print('  %s  %s（版本 %s）' % (os.path.basename(源), 大小嘴(os.path.getsize(源)), 版本))


def _Python包条目(纯库):
    """
    site-packages 里只挑 `Python包白名单` 那几个包（连同它们的 .dist-info）。

    ⚠ `.dist-info` 必须一起带：`pip` / `scikit-build-core` 靠它认"这个包装过没"，
      少了它 pip 会以为环境是空的，甚至重新去网上拉。
    """
    import importlib.metadata as 元
    条 = []
    有 = set(os.listdir(纯库)) if os.path.isdir(纯库) else set()
    for 名 in Python包白名单:
        径 = os.path.join(纯库, 名)
        if os.path.isdir(径):
            条.append((径, 'Python/Lib/site-packages/' + 名, None))
        elif os.path.isfile(径 + '.py'):        # typing_extensions 就是单个文件
            条.append((径 + '.py', 'Python/Lib/site-packages/%s.py' % 名, None))
    for 目 in sorted(有):
        if not 目.endswith('.dist-info'):
            continue
        规范 = re.sub(r'[-_.]+', '-', 目.split('-')[0]).lower()
        if 规范 in {re.sub(r'[-_.]+', '-', n).lower() for n in Python包白名单}:
            条.append((os.path.join(纯库, 目), 'Python/Lib/site-packages/' + 目, None))
    return 条


def _拷按规则(源, 目标, 规则):
    """
    按规则拷，回 `(文件数, 字节数)`。

    规则是 dict，键都可以不给：

      `跳目录`  集合。目录名对上就整棵跳过（任意层）
      `跳文件`  集合。文件名对上就不拷
      `只文件`  集合。**只**拷这些文件，子目录一律不进
      `留通配`  元组。只拷名字匹配的文件（fnmatch），子目录一律不进

    ⚠ 名字里带 `__pycache__` 的一律跳过：那是 .pyc，目标机器上第一次跑会自己
      重新生成，带上只是白占地方。
    """
    只 = (规则 or {}).get('只文件')
    通 = (规则 or {}).get('留通配')
    跳目 = set((规则 or {}).get('跳目录') or ()) | {'__pycache__'}
    跳件 = set((规则 or {}).get('跳文件') or ())

    if os.path.isfile(源):
        os.makedirs(os.path.dirname(目标) or '.', exist_ok=True)
        shutil.copy2(源, 目标)
        return 1, os.path.getsize(目标)

    件数 = 字节 = 0
    for 根, 目们, 件们 in os.walk(源):
        目们[:] = [d for d in 目们 if d not in 跳目]
        if 根 == 源 and (只 is not None or 通 is not None):
            目们[:] = []                      # 只/通配都是"只在这一层挑文件"
        for 件 in 件们:
            if 件 in 跳件:
                continue
            if 只 is not None and 件 not in 只:
                continue
            if 通 is not None and not any(fnmatch.fnmatch(件, p) for p in 通):
                continue
            源件 = os.path.join(根, 件)
            目标件 = os.path.join(目标, os.path.relpath(源件, 源))
            os.makedirs(os.path.dirname(目标件) or '.', exist_ok=True)
            shutil.copy2(源件, 目标件)
            件数 += 1
            字节 += os.path.getsize(目标件)
    return 件数, 字节


def _CUDA版本(根):
    """从 `cuda.h` 里读 `CUDA_VERSION`（13030 → `13.3`）。读不到回空串。"""
    if not 根:
        return ''
    try:
        with open(os.path.join(根, 'include', 'cuda.h'),
                  encoding='utf-8', errors='ignore') as f:
            for 行 in f:
                if 行.startswith('#define CUDA_VERSION'):
                    数 = int(行.split()[-1])
                    return '%d.%d' % (数 // 1000, (数 % 1000) // 10)
    except Exception:
        pass
    return ''


def 写来源(目标根):
    """
    把"这份编译环境从哪来"写成 `来源.txt` 跟着包走。

    ⚠ **这不是装饰。** 半年后有人问"这个包里的 VS 是哪个版本、能不能编 sm_89"，
      光看包里的目录名看不出来（`编译环境/VS/VC/Tools/MSVC/<数字>/` 那串只是
      工具集版本号）。而且出问题时，"在谁哪台机器上打的包"是最先要问的那个问题。
    """
    CUDA = _CUDA根()
    MSVC = _MSVC根()
    SDK, SDK版 = _SDK根()
    行们 = [
        'CUDA：%s（%s）' % (CUDA or '（没有）', _CUDA版本(CUDA) or '?'),
        'MSVC：%s' % (MSVC or '（没有）'),
        'Windows SDK：%s（%s）' % (SDK or '（没有）', SDK版 or '?'),
        'Python：%s（%d.%d.%d）' % (sys.base_prefix, sys.version_info[0],
                                   sys.version_info[1], sys.version_info[2]),
        '打包机器：%s' % (os.environ.get('COMPUTERNAME') or '?'),
        '打包时间：%s' % time.strftime('%Y-%m-%d %H:%M'),
    ]
    with open(os.path.join(目标根, '来源.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(行们) + '\n')
    for 行 in 行们:
        print('  ' + 行)


def 删目录(径):
    """
    删一个目录，删不动就**改名挪开**再尽力删。

    ⚠ 编译会留下**常驻进程**：`cl.exe` 会拉起 `vctip.exe`（VS 遥测）和
      `mspdbsrv.exe`，它们把工具链目录里的 DLL 锁着 —— 直接 `rmtree` 会得到
      `PermissionError: [WinError 5] 拒绝访问`，而那个报错**完全指不出是谁占着**
      （第一次就卡在这儿，看着像权限问题，实际是一个遥测进程）。
      改名不受影响（Windows 打开映像文件用的是 FILE_SHARE_DELETE，实测能把
      整个目录连同已加载的 DLL 一起改名），所以先改名把路让开，再慢慢删。
    """
    try:
        shutil.rmtree(径)
        return
    except Exception:
        pass
    挪 = 径.rstrip('\\/') + '_旧_' + time.strftime('%Y%m%d_%H%M%S')
    try:
        os.rename(径, 挪)
        print('  ⚠ %s 删不动（多半是 cl.exe 留下的 vctip.exe 还占着），先改名挪开'
              % os.path.basename(径))
    except Exception as 错:
        死('删不掉也挪不开：%s（%s：%s）' % (径, type(错).__name__, 错))
    try:
        shutil.rmtree(挪)
    except Exception:
        print('  ⚠ %s 这次删不掉，留着不影响本次构建（下次重跑会再试一次）'
              % os.path.basename(挪))


def 拷编译环境():
    步(13, 16, '拷编译环境（精简：7.2GB → 约 2.5GB）')
    if not 打编译环境:
        print('\n  打编译环境 = False，跳过。')
        print('  后果：这个包只能跑，编不了 —— 别人显卡不吃这份 CUDA 版时没辙。')
        return
    条目们, 缺 = 编译环境清单()
    if 缺:
        死('这台机器凑不齐编译环境，缺：\n     - ' + '\n     - '.join(缺) +
           '\n     装齐再构建；或者把 打包.py 顶上的 打编译环境 改成 False'
           '（那样包小 1.4GB，但别人重编不了）。')
    目标根 = os.path.join(DIST目录, 编译环境名)
    if os.path.isdir(目标根):
        删目录(目标根)

    起 = time.time()
    总件 = 总字 = 0
    分块 = {}
    for 源, 目标, 规则 in 条目们:
        if not os.path.exists(源):
            死('清单里这一项本机没有：%s' % 源)
        目 = os.path.join(目标根, 目标)
        件数, 字节 = _拷按规则(源, 目, 规则)
        总件 += 件数
        总字 += 字节
        块 = 目标.split('/')[0]
        分块[块] = 分块.get(块, 0) + 字节
        print('  %-26s %9s  %5d 件' % (目标 + '/', 大小嘴(字节), 件数))
    骨架件 = 铺vs骨架(目标根)
    总件 += 骨架件
    print('  %-26s %9s  %5d 件' % ('VS/VC/Auxiliary/Build/', '0 B', 骨架件))
    print('  ── 分块 ──')
    for 块 in sorted(分块, key=lambda k: -分块[k]):
        print('  %-26s %9s' % (块, 大小嘴(分块[块])))
    print('  ── 来源 ──')
    写来源(目标根)
    print('  %s/  %s（%d 个文件，%.0f 秒）✓'
          % (编译环境名, 大小嘴(总字), 总件, time.time() - 起))


# ══════════════════════════════════════════════════════════════════
#  [6] 收缩
# ══════════════════════════════════════════════════════════════════

def 收缩依赖库():
    if not 收缩:
        print('\n  收缩 = False，PySide6 原样保留。')
        return
    步(7, 16, '收缩 PySide6（砍零引用的巨物）')
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
    步(9, 16, 'PyInstaller 打 exe（--onefile --windowed）')

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
    步(10, 16, '冒烟自检（offscreen 起 %d 秒）' % 自检秒)

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
    步(11, 16, '建数据目录空骨架')
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
    步(12, 16, '拷配套文件夹')
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
    步(14, 16, '写使用说明.txt')
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
  角色卡.json 接口配置.json 会话.json
  助手设置.json 助手任务.json 记忆.json   小表（一张表一个文件）
  头像/  消息/  记忆/  助手转录/          会长大的大件（一条一个文件）
  %(模型子)s/                          下载的 GGUF 放这里
  ⚠ 接口配置.json 里存着你的 API 密钥，别往外发。

【关于本地模型：包里带了两套框架，程序自己挑】
  %(库)s/llama_cpp 下面有两套 DLL，程序启动时自己挑一套用：

    lib/         CUDA 版（吃显卡；按打包这台机器 RTX 5050 / sm_120 编的）
    %(cpu子)s/    CPU 版（不碰任何 CUDA 东西，慢，但一定跑得起来）

  挑的顺序：这台机器有 NVIDIA 驱动就先试 CUDA 那套，起不来就换 CPU 那套。
  **正在用哪套，「模型」页最底下那行「引擎状态」会写。**
  本地模型这一路**不需要 pip install 任何东西** —— 两套都在包里。

  · 看到"引擎状态：没装上"，先查 %(库)s/llama_cpp 是不是被杀软隔离了，
    或者整个文件夹没拷全（只拷 exe 是跑不起来的）。
  · CUDA 版只认 sm_120（RTX 50 系）。别的 N 卡被选到 CUDA 那套时，可能在
    **加载模型**那一步报 no kernel image 之类的错 —— 去「接口配置」把这套的
    GPU 层数改成 0，它就用 CPU 跑（不用换框架）。想真吃上你那块卡，
    去 %(gpu)s/ 重编一份 CUDA 版，双击 %(gpu)s/编译GPU版.bat。

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
        'cpu子': CPU后端目录,
    }
    径 = os.path.join(DIST目录, '使用说明.txt')
    with open(径, 'w', encoding='utf-8') as f:
        f.write(内容)
    print('  %s ✓' % 径)


# ══════════════════════════════════════════════════════════════════
#  [10] 出口校验 + 摘要
# ══════════════════════════════════════════════════════════════════

def 出口校验(顶层们, 图标png=None):
    步(15, 16, '出口校验')

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

    # ── 本地模型的两套后端 ──
    # 这一段的判据是这次踩坑的直接产物：**"包里带着框架"和"框架能在别人机器上
    # 加载"是两回事**，见文件头。上面那份清单只查了 `lib/llama.dll` 在不在，
    # 而它在不在跟能不能加载毫无关系。
    本地 = os.path.join(库根, 'llama_cpp')
    CPU套 = os.path.join(本地, CPU后端目录)
    for 必 in ('llama.dll', 'ggml.dll', 'ggml-cpu.dll', 'ggml-base.dll'):
        if not os.path.isfile(os.path.join(CPU套, 必)):
            缺.append(os.path.join(依赖库名, 'llama_cpp', CPU后端目录, 必))
    # 反向也拦一道：CPU 兜底那套里混进 CUDA 的东西 = 兜底失效，而它**照样
    # 看起来是装好的**（文件都在、大小也不小），不显式拦就没人会发现。
    if os.path.isdir(CPU套):
        混进 = sorted(n for n in os.listdir(CPU套) if 'cuda' in n.lower())
        if 混进:
            缺.append('⚠ %s 混进了 CUDA 的东西：%s（兜底就白兜了）'
                      % (os.path.join(依赖库名, 'llama_cpp', CPU后端目录),
                         '、'.join(混进)))
    # CUDA 那套：ggml-cuda.dll 在，就必须有它链的 cublas（名字现读 DLL）
    ggml_cuda = os.path.join(本地, 'lib', 'ggml-cuda.dll')
    if os.path.isfile(ggml_cuda):
        还缺 = sorted(_CUDA缺的(ggml_cuda))
        if 还缺:
            缺.append('⚠ %s\\lib\\ 里是 CUDA 版，但缺 %s —— 别人机器上会加载失败、'
                      '静默退到 CPU' % (依赖库名, '、'.join(还缺)))

    数据根 = os.path.join(DIST目录, 数据名)
    for 子 in 数据骨架:
        if not os.path.isdir(os.path.join(数据根, 子)):
            缺.append(os.path.join(数据名, 子))

    # ── 编译环境 ──
    # 带了就必须是**完整**的：缺一个文件，别人拿到手里会编到一半才失败，
    # 而那时候他已经在怀疑"是不是我这台机器的问题"了。
    if 打编译环境:
        编根 = os.path.join(DIST目录, 编译环境名)
        for 必 in ['CUDA/bin/nvcc.exe', 'CUDA/nvvm/bin/cicc.exe',
                   'CUDA/include/cuda_runtime.h', 'CUDA/lib/x64/cudart_static.lib',
                   'VS/VC/Auxiliary/Build/vcvarsall.bat',
                   'WinSDK/bin/rc.exe', 'WinSDK/bin/mt.exe',
                   'WinSDK/Include/um/windows.h', 'WinSDK/Lib/um/x64/kernel32.lib',
                   'Python/python.exe', 'Python/include/Python.h',
                   'Python/libs/python313.lib', 'Python/Scripts/ninja.exe',
                   'Python/Lib/site-packages/cmake/data/bin/cmake.exe',
                   '来源.txt']:
            if not os.path.isfile(os.path.join(编根, *必.split('/'))):
                缺.append(os.path.join(编译环境名, 必))
        # MSVC 工具集那几条不能写死版本号（目录名随打包机的工具集版本走），
        # 跟 `酒馆编译.py` 的 `工具集()` 一个道理，现探。
        for 相 in ('VS/VC/Tools/MSVC/*/bin/Hostx64/x64/cl.exe',
                   'VS/VC/Tools/MSVC/*/lib/x64/msvcrt.lib'):
            if not glob.glob(os.path.join(编根, *相.split('/'))):
                缺.append(os.path.join(编译环境名, 相))
        # 源码包：没有它，重编就得联网去 PyPI 拉（"带上编译环境"的意义就少一半）
        if not os.path.isdir(os.path.join(编根, '源码包')):
            缺.append(os.path.join(编译环境名, '源码包',
                                  '(sdist —— 没有它，重编要联网)'))

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

        步(2, 16, '算依赖闭包')
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
        备CPU后端()
        备CUDA运行时()
        备源码包()
        收缩依赖库()
        双后端自检()

        钩子 = 写运行时钩子()
        ico, 图标png = 备图标()
        打EXE(转排除模块(顶层们), 钩子, ico)

        冒烟自检()
        重建数据骨架()
        拷配套()
        拷编译环境()
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
