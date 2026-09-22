#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆编译.py — 用包内那份编译环境，重编本地模型框架（CUDA 版）

**这个文件不 import Qt。** 跟 `酒馆本地` / `酒馆大脑` 一个道理：编译这层最需要
能脱离界面单测，绑上 Qt 就没法痛快测了。

一件事：**把 `依赖库/llama_cpp` 里那套 CUDA DLL，换成按这块显卡重编的一份。**

    找环境   `编译环境/` 在不在、齐不齐（它由 打包.py 的 `拷编译环境` 铺出来）
    探架构   nvcuda 问一下这张卡的 compute capability（RTX 5050 → sm_120）
    铺环境   PATH / INCLUDE / LIB / CUDA_PATH / CMAKE_ARGS 全指向包内那份工具链
    编       pip wheel llama-cpp-python==<版本>（**只出 wheel，不 install**）
    装       抽 wheel 里的 llama_cpp/lib/*.dll，换掉 `依赖库/llama_cpp/lib/`

⚠ **为什么是 `pip wheel` 而不是 `pip install`**：我们要的只是几个编出来的 DLL。
install 会把整个包（含 Python 代码）装进那个便携 Python —— 包里又多养一份
llama_cpp，而且装完还得回 site-packages 里翻。出 wheel 既能只取要的，
也方便校验产物（要确认里面有 `ggml-cuda.dll`）。

⚠ **版本号从装着的那份现读，不写死。** `依赖库/llama_cpp` 里的 Python 代码只有
一份，DLL 版本对不上就是静默变形（拿 0.3.34 的绑定调 0.3.35 的 C 接口，
坏起来是"某个参数悄悄不生效"那种，最难查）。

⚠ **换装不用关程序。** 实测过：DLL 已经加载进进程时，Windows 照样允许把
整个 `lib/` 目录改名（映像文件是用 FILE_SHARE_DELETE 打开的）。所以换装是
"改名三次"的瞬时操作，不是拷 492MB。但**正在跑的那个进程用的还是旧 DLL**，
新的一份要下次启动才生效 —— 所以换完必须提示重启。

⚠ **为什么目标机器没装 CUDA 工具包也能编**：nvcc 只要求驱动（它要判断"这张卡
支持 sm_X"），不要求本机装工具包 —— 工具包就在我们包里。所以只要有能跑这份
CUDA 版的驱动，这条路就是通的。
"""

import fnmatch
import glob
import os
import shutil
import subprocess
import sys
import threading
import time

__all__ = ['编译错', '环境目录', '齐不齐', '工具集', '有链接外壳', '来源', '显卡',
           '库目录', '当前库', '铺环境', '版本', '源码包', '编译', '装库', '备份们', '还原',
           '认后端', '后端开关', '后端话', '后端短话', '默认后端']


class 编译错(Exception):
    """编译这一层出的所有错。文案写给人看，界面直接显示。"""


#: `编译环境/` 里**必须有**的文件。抽查的是"真要调用的那几个"，不是"随便几个文件"：
#: 少一个都会在编译进行到一半才炸，而这几个各自对应一次真实调用。
#:
#: ⚠ **MSVC 工具集那几条故意不在这里**：它的目录名带版本号（`14.44.35207`），
#:   而装的是 14.44 还是 14.50 由打包那台机器说了算 —— 写死就等于下次换台机器
#:   打包时这里先误报。交给 `工具集()` 现探。
关键件 = (
    ('CUDA/bin/nvcc.exe', 'nvcc（CUDA 编译器）'),
    ('CUDA/nvvm/bin/cicc.exe', 'cicc（nvcc 的前端）'),
    ('CUDA/include/cuda_runtime.h', 'CUDA 头文件'),
    ('CUDA/lib/x64/cudart_static.lib', 'CUDA 静态运行库'),
    ('CUDA/lib/x64/cublas.lib', 'cublas 导入库'),
    # ⚠ **这一条是"包里能不能编译"的守门人**（2026-09-22 挖出来的）：
    #   nvcc 认宿主编译器靠的不是 cl.exe 这个文件，是 cl.exe **上面那几层**摆成
    #   VS 安装的样子 —— 它顺着 `bin\Hostx64\x64` 往上找 `VC\Auxiliary\Build\
    #   vcvarsall.bat`。找不到就只吐一句
    #       nvcc fatal : Host compiler targets unsupported OS.
    #   （跟"操作系统"一点关系没有）。层级见 `铺环境()` 的注释，别改。
    ('VS/VC/Auxiliary/Build/vcvarsall.bat', 'VS 骨架（nvcc 认宿主编译器全靠它）'),
    ('WinSDK/bin/rc.exe', 'rc.exe（资源编译器）'),
    ('WinSDK/bin/mt.exe', 'mt.exe（清单工具）'),
    ('WinSDK/Include/um/windows.h', 'Windows SDK 头文件'),
    ('WinSDK/Lib/um/x64/kernel32.lib', 'Windows SDK 库'),
    ('Python/python.exe', '便携 Python'),
    ('Python/include/Python.h', 'Python 开发头文件'),
    ('Python/libs/python313.lib', 'Python 链接库'),
    ('Python/Scripts/ninja.exe', 'ninja（构建工具）'),
    ('Python/Lib/site-packages/cmake/data/bin/cmake.exe', 'cmake'),
)

#: 换装时给旧 `lib/` 备份起的名字前缀。
备份前缀 = 'lib_旧_'


def 根目录():
    """
    程序在哪儿。**打包后是 exe 那层**（`依赖库/`、`编译环境/` 都在它旁边），
    开发时是源码根。跟 `酒馆存储.默认数据目录` 判断 `sys.frozen` 是一个道理。
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _纯ASCII(路):
    """这个路径能不能用 ASCII 表示。空串算作"不能用"。"""
    try:
        (路 or '').encode('ascii')
        return bool(路)
    except UnicodeEncodeError:
        return False


def _挑落脚点():
    """
    挑一个**纯 ASCII、而且现在就能在里面建目录**的位置。找不到回 `''`。

    按「最可能能写」排：`C:\\Users\\Public` → `%ProgramData%` → 盘根 → `%TEMP%`。
    ⚠ 不优先用盘根：普通账号往往写不进去（要管理员），而且谁都不喜欢盘根多一个目录。
    ⚠ `%TEMP%` 排在最后是因为**用户名带中文时它本身就是中文**（`C:\\Users\\张三\\...`），
      `_纯ASCII` 会把它挡掉 —— 挡掉之后还得看前面几个。
    """
    候选 = [os.environ.get('PUBLIC') or '',
            os.environ.get('ProgramData') or '']
    盘 = os.path.splitdrive(根目录())[0]
    if 盘:
        候选.append(盘 + os.sep)
    候选.append(os.environ.get('TEMP') or '')
    for 母 in 候选:
        if not _纯ASCII(母) or not os.path.isdir(母):
            continue
        try:
            探 = os.path.join(母, '.hls_write_test')
            os.makedirs(探, exist_ok=True)
            os.rmdir(探)
            return 母
        except OSError:
            continue
    return ''


#: ASCII 镜像用哪个名字（必须是纯 ASCII）。
镜像名 = 'HonglusiBuildEnv'


def 镜像目录(真):
    """
    给「编译环境」套一个**纯 ASCII 的目录联接**，回那个联接的路径；不需要或弄不了就回原路。

    ⚠ **为什么非要有这一层**（2026-09-22 挖出来的，代价是用户一整天）：

      CMake/Ninja 把链接命令写进 **`.rsp` 响应文件**（里面是 **UTF-8**），
      而 MSVC 的 `link.exe` 是**按 ANSI 代码页读**那个文件的。包一旦在中文目录下
      （`...\\ai酒馆\\dist\\编译环境\\CUDA\\lib\\x64\\cudart.lib`），link 读到的就是
      一串乱码，于是报

          LINK : fatal error LNK1181 / LNK1104: 无法打开输入文件 "...cudart.lib"

      —— 而那个文件明明就在那儿。命令行短的时候不走 rsp（参数走宽字符 API），
      所以前面几百个编译步骤、连 `cmake configure` 都能过，**只在最后链接那一下炸**，
      极难往"路径编码"上想。

      （对照：`GPU-model/编译GPU版.bat` 那条路没事，因为它全程在 `Program Files`
        和 `%TEMP%` 里跑，一个中文都没有。实测：同一条命令、同一个 cudart.lib，
        走中文路径 → `LNK1104`；走 ASCII 联接 → 链接成功。）

    联接只是一个目录项，**不复制那 2.5GB**；`mklink /J` 也不需要管理员权限。
    """
    if _纯ASCII(真):
        return 真                                  # 本来就是干净路径，别多事
    母 = _挑落脚点()
    if not 母:
        return 真                                  # 实在没地方：照旧，链接那步会报错
    镜 = os.path.join(母, 镜像名)
    try:
        # ⚠ 用 `lexists` 而不是 `isdir`：**悬空的联接**（指过去那个包被删/被挪了）
        #   `isdir` 会回 False，于是这里会跳过、然后 `mklink` 因为"名字已存在"失败 ——
        #   最后静默退回中文路径，又开始报"打不开 xxx.lib"。这种坑不能再踩第二次。
        if os.path.lexists(镜):
            if os.path.isdir(镜) and os.path.normcase(os.path.realpath(镜)) == \
                    os.path.normcase(os.path.realpath(真)):
                return 镜                          # 已经指向这份环境，直接用
            os.rmdir(镜)                           # 悬空 / 指向别处（换过包）→ 拆了重来
        r = subprocess.run(['cmd', '/c', 'mklink', '/J', 镜, 真],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0 and os.path.isdir(os.path.join(镜, 'CUDA')):
            return 镜
    except OSError:
        pass
    return 真


def 环境目录():
    """
    `编译环境/` 在哪。找不到回 `None`。

    ⚠ 环境变量 `酒馆_编译环境` 优先：给两种人用 —— 开发时工具链不在源码旁边
      （`dist/编译环境/` 才是），以及用户想把工具链放在别处（U 盘、别的盘）。
    ⚠ 回的是**套过 ASCII 镜像之后**的路径（见 `镜像目录()`）：这一步之后，
      铺环境 / 源码包 / 产物目录全都落在无中文的路径上，才不会踩 link.exe 那个坑。
    """
    指 = (os.environ.get('酒馆_编译环境') or '').strip()
    if 指 and os.path.isdir(指):
        return 镜像目录(os.path.abspath(指))
    默认 = os.path.join(根目录(), '编译环境')
    return 镜像目录(默认) if os.path.isdir(默认) else None


def 临时目录():
    """
    给 pip / cmake / cl 用的临时目录。**系统那份带中文时**才另起一个（回 `''` 表示不用动）。

    ⚠ 同一个坑的另一半（见 `镜像目录()`）：pip 把 sdist 解到 `%TEMP%`、cmake 把中间
      产物也放那儿，而这些路径**会进 `.rsp`**。用户名是中文的机器（`C:\\Users\\张三`）
      `%TEMP%` 就是中文 —— 那样链接照样会炸，且报的是"文件打不开"。
      系统那份本来就干净时**一个字都不动**（绝大多数机器都这样）。
    """
    现 = os.environ.get('TEMP') or ''
    if _纯ASCII(现):
        return ''
    母 = _挑落脚点()
    if not 母:
        return ''
    目 = os.path.join(母, 'HonglusiBuildTemp')
    try:
        os.makedirs(目, exist_ok=True)
    except OSError:
        return ''
    return 目


def 工具集():
    """
    包内那份 MSVC 工具集。回 `(版本号, 绝对路径)`；没有回 `('', None)`。

    ⚠ **版本号是现探的，不写死。** 真身的目录名就是版本号
      （`VC\\Tools\\MSVC\\14.44.35207\\`），`打包.py` 是照打包那台机器上的名字
      原样拷过来的 —— 那台机器装的是 14.44 还是 14.50，这边不能假定。
    """
    根 = 环境目录()
    if 根 is None:
        return '', None
    母 = os.path.join(根, 'VS', 'VC', 'Tools', 'MSVC')
    if not os.path.isdir(母):
        return '', None
    for 名 in sorted(os.listdir(母), reverse=True):
        径 = os.path.join(母, 名)
        if os.path.isfile(os.path.join(径, 'bin', 'Hostx64', 'x64', 'cl.exe')):
            return 名, 径
    return '', None


def 有链接外壳(工具=None):
    """
    这个包里装没装「链接外壳」（`打包.py 的 装链接外壳()`）。

    ⚠ **它是"中文路径能不能编"的唯一判据。** 装了它，包里的 `link.exe` 就是我们的
      外壳：它会先给 link 的 `.rsp` 参数文件补上 UTF-8 BOM，再转给真身
      （`link-real.exe`）。于是路径里有没有中文都不影响链接。

    ⚠ 判据就一条：工具集目录里**有没有 `link-real.exe`**。只有装过外壳才会有这个
      名字 —— 微软原装的叫 `link.exe`。不用比大小、不用看版本，名字本身就是记号。

    `工具` 可以传 `工具集()` 已经探好的那份，省一次目录遍历。
    """
    if 工具 is None:
        _版, 工具 = 工具集()
    if not 工具:
        return False
    return os.path.isfile(os.path.join(工具, 'bin', 'Hostx64', 'x64', 'link-real.exe'))


def 齐不齐():
    """回 `(齐了没, 缺的清单)`。清单里是**人话**（"cl.exe（C++ 编译器）"），不是路径。"""
    根 = 环境目录()
    if 根 is None:
        return False, ['这个包里没带「编译环境/」（打包时 打编译环境 = False？）']
    缺 = [说明 for 相, 说明 in 关键件
          if not os.path.isfile(os.path.join(根, *相.split('/')))]
    if 工具集()[1] is None:
        缺.append('MSVC 工具集（cl.exe / link.exe）')
    return (not 缺), 缺


def 来源():
    """
    这份编译环境是从哪台机器、哪几个版本拷来的 —— 读打包时写下的 `来源.txt`。
    回 `[(名, 值), ...]`；文件不在就回空列表（界面显示"来源不明"）。
    """
    根 = 环境目录()
    if 根 is None:
        return []
    径 = os.path.join(根, '来源.txt')
    if not os.path.isfile(径):
        return []
    出 = []
    try:
        with open(径, encoding='utf-8') as f:
            for 行 in f:
                行 = 行.strip()
                if 行 and '：' in 行:
                    名, _, 值 = 行.partition('：')
                    出.append((名.strip(), 值.strip()))
    except Exception:
        return []
    return 出


def _包目录():
    """`llama_cpp` 这个包在哪儿（发行包里就是 `依赖库/llama_cpp`）。没有回 `None`。"""
    import importlib.util as 导入工
    try:
        规 = 导入工.find_spec('llama_cpp')
    except Exception:
        return None
    if 规 is None:
        return None
    径们 = list(getattr(规, 'submodule_search_locations', None) or [])
    return 径们[0] if 径们 else None


def 库目录():
    """要换的那份 DLL 目录（`<包>/lib`，也就是 CUDA 那套）。没有回 `None`。"""
    包 = _包目录()
    return os.path.join(包, 'lib') if 包 else None


def 版本():
    """当前这份 llama_cpp 的版本（要编的就是它）。读不到回空串。"""
    try:
        import importlib.metadata as 元
        return 元.version('llama-cpp-python')
    except Exception:
        return ''


def 当前库():
    """
    现在 `lib/` 里是哪一套、多大、什么时候换的。回 `{...}`（给界面显示用）。

    `后端` 是关键那格：`'cuda'` / `'vulkan'` / `''`。**空了说明这份吃不了显卡** ——
    要么打的是 CPU 兜底那套，要么有人把它换错了。
    （判据是 `认后端()`：看那个**后端专属**的 DLL 在不在，不是看 `llama.dll` ——
    两套都有 `llama.dll`，拿它判根本分不出来。）
    """
    lib = 库目录()
    if lib is None or not os.path.isdir(lib):
        return {'在': False, '后端': '', '大小': 0, '时间': '', '目录': lib or ''}
    总 = 最新 = 0
    for 根, _目, 件们 in os.walk(lib):
        for 件 in 件们:
            径 = os.path.join(根, 件)
            try:
                总 += os.path.getsize(径)
                最新 = max(最新, os.path.getmtime(径))
            except OSError:
                pass
    return {'在': True,
            '后端': 认后端(lib),
            '大小': 总,
            '时间': time.strftime('%Y-%m-%d %H:%M', time.localtime(最新)) if 最新 else '',
            '目录': lib}


def 备份们():
    """
    换装留下的旧 `lib/`（`lib_旧_<时间>/`），新的在前。回 `[(名字, 说明), ...]`。

    ⚠ 这也是**唯一的退路**：新编的那份跑不起来时，界面上"还原上一份"就靠它。
      所以换装时绝不能把它删掉，只能改名。
    """
    包 = _包目录()
    if 包 is None:
        return []
    出 = []
    for 名 in sorted(os.listdir(包), reverse=True):
        if 名.startswith(备份前缀) and os.path.isdir(os.path.join(包, 名)):
            出.append((名, 时间戳说明(名[len(备份前缀):])))
    return 出


def 时间戳说明(戳):
    """`20260921_2033` → `2026-09-21 20:33`。认不出来就原样回。"""
    try:
        return '%s-%s-%s %s:%s' % (戳[0:4], 戳[4:6], 戳[6:8], 戳[9:11], 戳[11:13])
    except Exception:
        return 戳


def 源码包():
    """
    包内那份源码包（`编译环境/源码包/llama_cpp_python-<版本>.tar.gz`）。
    有它 = **不用联网**就能编；没有回 `None`，`编译` 会走 pip 正常联网那条路。
    """
    根 = 环境目录()
    版本号 = 版本()
    if 根 is None or not 版本号:
        return None
    目 = os.path.join(根, '源码包')
    if not os.path.isdir(目):
        return None
    for 件 in sorted(os.listdir(目)):
        if 件.startswith('llama_cpp_python-%s' % 版本号) and 件.endswith(('.tar.gz', '.zip')):
            return os.path.join(目, 件)
    return None


# ── 显卡 ────────────────────────────────────────────────────────────

def 显卡(序=0):
    """
    本机第 `序` 张卡：回 `(名字, 架构号)`，例如 `('NVIDIA GeForce RTX 5050 Laptop GPU', '120')`。
    没卡 / 没驱动 / 读不出来一律回 `('', '')`。

    ⚠ 走**驱动 API**（nvcuda.dll）而不是 CUDA 运行时 —— 运行时是工具包的一部分，
      而这里要问的恰恰是"还没装工具包的机器上有没有卡"。能问出来，才谈得上
      "用包里那份工具链给这张卡编一份"。

    ⚠ 这里不是判据，是给界面填个默认值。任何一步不干净都当没有，
      用户还能自己把架构号填进去（给别的卡编要拿到那张卡的机器上编，见文件头）。
    """
    if sys.platform != 'win32':
        return '', ''
    try:
        import ctypes
        核 = ctypes.WinDLL('nvcuda.dll')
        核.cuInit.argtypes = [ctypes.c_uint]
        核.cuInit.restype = ctypes.c_int
        if 核.cuInit(0) != 0:
            return '', ''
        数 = ctypes.c_int(0)
        核.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
        核.cuDeviceGetCount.restype = ctypes.c_int
        if 核.cuDeviceGetCount(ctypes.byref(数)) != 0 or 数.value <= 序:
            return '', ''
        卡 = ctypes.c_int(0)
        核.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
        核.cuDeviceGet.restype = ctypes.c_int
        if 核.cuDeviceGet(ctypes.byref(卡), 序) != 0:
            return '', ''
        缓 = ctypes.create_string_buffer(256)
        核.cuDeviceGetName.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        核.cuDeviceGetName.restype = ctypes.c_int
        核.cuDeviceGetName(缓, 256, 卡)
        名 = 缓.value.decode('utf-8', 'replace').strip()
        核.cuDeviceGetAttribute.argtypes = [ctypes.POINTER(ctypes.c_int),
                                          ctypes.c_int, ctypes.c_int]
        核.cuDeviceGetAttribute.restype = ctypes.c_int
        值 = ctypes.c_int(0)
        # 75 / 76 = CU_DEVICE_ATTRIBUTE_COMPUTE_CAPABILITY_MAJOR / MINOR
        大 = 小 = 0
        if 核.cuDeviceGetAttribute(ctypes.byref(值), 75, 卡) == 0:
            大 = 值.value
        if 核.cuDeviceGetAttribute(ctypes.byref(值), 76, 卡) == 0:
            小 = 值.value
        return 名, ('%d%d' % (大, 小)) if 大 > 0 else ''
    except Exception:
        return '', ''


# ── 编译后端 ────────────────────────────────────────────────────────
#
# 一条产物只认一个后端，编出来的是哪一套，由这里定：

#: 后端名 → 传给 CMake 的开关。`GGML_*` 就是 llama.cpp 的后端总开关。
后端开关 = {'cuda': '-DGGML_CUDA=on', 'vulkan': '-DGGML_VULKAN=on'}

#: 后端名 → 界面上给人看的叫法（挡位那行用这个，说清是哪家的卡）。
后端话 = {'cuda': 'NVIDIA 显卡（CUDA）', 'vulkan': 'AMD 显卡（Vulkan）'}

#: 后端名 → 短叫法。**塞进句子里用的**，比如"现在这套：CUDA 版" ——
#: 用长名字会变成"NVIDIA 显卡（CUDA） 版"，那个"版"字粘在括号后面读着别扭。
后端短话 = {'cuda': 'CUDA 版', 'vulkan': 'Vulkan 版'}

#: 后端名 → 那根"少了它就不是这个后端"的柱子。
#: ⚠ Vulkan 那条用通配：llama.cpp 里这个 DLL 的确切名字要等真编一次才敢写死
#:   （见过的写法有 `ggml-vulkan.dll` 之类），用通配就不怕差一个字。
后端柱子 = {'cuda': 'ggml-cuda.dll', 'vulkan': 'ggml-vulkan*.dll'}

#: 默认后端。加 Vulkan 之前只有 CUDA，这里保持原样。
默认后端 = 'cuda'


def 认后端(lib):
    """
    一个 DLL 目录是哪一套：`'cuda'` / `'vulkan'` / `''`（都不是，说明吃不了显卡）。

    ⚠ 光看 `llama.dll` 在不在**判不出来** —— 两套都有它。要认的是那个**后端专属**的
      `ggml-cuda.dll` / `ggml-vulkan*.dll`。
    """
    if not lib or not os.path.isdir(lib):
        return ''
    if os.path.isfile(os.path.join(lib, 'ggml-cuda.dll')):
        return 'cuda'
    if glob.glob(os.path.join(lib, 'ggml-vulkan*.dll')):
        return 'vulkan'
    return ''


# ── 铺环境 ──────────────────────────────────────────────────────────

def 铺环境(架构='', 后端=默认后端):
    """
    按包内那份工具链铺一套环境变量，回一个能直接喂 `subprocess` 的 dict。

    工具链认四样，缺一样都会以很奇怪的方式失败：

      `PATH`       工具在哪（nvcc / cl / rc / mt / ninja）
      `INCLUDE`    头文件在哪，**顺序不能乱**：MSVC → WinSDK ucrt → um → shared
      `LIB`        链接库在哪，同上：MSVC → WinSDK ucrt → um → CUDA
      `CUDA_PATH`  nvcc **只认这一个**去找 nvvm / include / lib，不看 PATH

    `架构` 给了就一并把 `CMAKE_ARGS` 写出来（ggml 的 CUDA 开关 + 目标架构 +
    rc/mt 的绝对路径）。**这几条是照 `GPU-model/编译GPU版.bat` 里验过的那份抄的**，
    别自己发挥。

    ⚠ 全用**绝对路径**：`编译环境/` 在哪是运行时才知道的（整个文件夹会被挪窝，
      也可能被 `酒馆_编译环境` 指到别处）。
    ⚠ **PATH 的顺序是照 `编译GPU版.bat` 抄的**，别重排：System32 补回最前面、
      然后 WinSDK 的 rc/mt、再 CRT、再 cl 目录。
    """
    根 = 环境目录()
    if 根 is None:
        raise 编译错('这个包里没带「编译环境/」，编不了。')
    版, 工具 = 工具集()
    if 工具 is None:
        raise 编译错('编译环境里没有 MSVC 工具集'
                    '（应为 VS/VC/Tools/MSVC/<版本>/bin/Hostx64/x64/cl.exe）。')

    # 路径带中文要不要拦？**看有没有「链接外壳」。**
    #   有 → 放行。外壳先给 link 的 `.rsp` 补 UTF-8 BOM 再转给真身，中文路径照编
    #        （见 `有链接外壳()` / `打包.py 的 装链接外壳()`）。
    #   没有 → 拦下。老包没这个外壳，编到链接那步必然报"打不开 xxx.lib"，那话跟真因
    #        差着十万八千里；宁可在这儿把话说白。
    if not _纯ASCII(根) and not 有链接外壳(工具):
        raise 编译错(
            '编译环境在带中文的路径下：\n  %s\n'
            '而 MSVC 的 link.exe 读不了这种路径（链接时会报"打不开 ...lib"）。\n'
            '这个程序试着在 C:\\Users\\Public 下建一个 ASCII 目录联接，但没成功。\n'
            '办法一：把整个软件（连同 编译环境/）挪到一个**路径里没有中文**的地方再编。\n'
            '办法二：换一个**重新打过**的包 —— 新包里那个 link.exe 换成了我们自己的\n'
            '        外壳，装到哪、路径有没有中文都不影响。' % 根)

    def 径(*段):
        return os.path.normpath(os.path.join(根, *段))

    C, S, Y = 径('CUDA'), 径('WinSDK'), 径('Python')
    系统 = os.environ.get('SystemRoot', r'C:\Windows')
    cl目录 = os.path.join(工具, 'bin', 'Hostx64', 'x64')
    环境 = dict(os.environ)
    环境['PATH'] = os.pathsep.join([
        # ⚠ System32 / Windows 先补回最前面。编译GPU版.bat 顶上那段注释记着代价：
        #   少了它，xcopy / where 这些系统自带命令全废，rc.exe / mt.exe 找不到，
        #   而 mt.exe 缺 System32 里的 msvcp140.dll 会直接报 0xc0000135。
        os.path.join(系统, 'System32'), 系统,
        os.path.join(系统, 'System32', 'Wbem'),
        os.path.join(系统, 'System32', 'WindowsPowerShell', 'v1.0'),
        # rc.exe / mt.exe 那一窝（要和它们的依赖 DLL 待在一起，.bat 里踩过）
        os.path.join(S, 'bin'),
        # CRT 排在 cl 目录前面：cl.exe / link.exe 自己就要 msvcp140.dll /
        # vcruntime140.dll，目标机器没装 VC 运行库时只能从这儿拿
        径('VS', 'VC', 'Redist', 'CRT'),
        # ⚠ cl.exe 必须从 `bin/Hostx64/x64` 这一层进 PATH —— 上面那几层的形状
        #   就是 nvcc 认宿主编译器的依据，见下面 `VSCMD_VER` 那段
        cl目录,
        os.path.join(C, 'bin'), Y, os.path.join(Y, 'Scripts'),
        环境.get('PATH', ''),
    ])
    环境['INCLUDE'] = os.pathsep.join([
        os.path.join(工具, 'include'), os.path.join(S, 'Include', 'ucrt'),
        os.path.join(S, 'Include', 'um'), os.path.join(S, 'Include', 'shared'),
    ])
    环境['LIB'] = os.pathsep.join([
        os.path.join(工具, 'lib', 'x64'), os.path.join(S, 'Lib', 'ucrt', 'x64'),
        os.path.join(S, 'Lib', 'um', 'x64'), os.path.join(C, 'lib', 'x64'),
    ])
    环境['CUDA_PATH'] = C
    环境.pop('CUDA_HOME', None)          # 少一个"另一个 CUDA 在哪"的歧义

    # ⚠ 系统 %TEMP% 带中文的机器上，必须换一个干净临时目录 —— 理由见 `临时目录()`：
    #   pip 解包 / cmake 中间产物都落在 %TEMP%，而这些路径会进 link.exe 的 `.rsp`。
    干净临时 = 临时目录()
    if 干净临时:
        环境['TMP'] = 环境['TEMP'] = 干净临时

    # ── 让 nvcc 别去跑 VC 的 vcvars 批处理，直接用我们铺的这一套 ──
    #
    # ⚠ **这一段是"包里自带工具链"能不能成立的关键**（2026-09-22 实测定的）。
    #
    #   Windows 上的 nvcc 不靠环境变量认宿主编译器，它干这两件事：
    #
    #     ① 顺着 PATH 里的 cl.exe 往上找"VS 装在哪" —— 认的是
    #        `VC\Auxiliary\Build\vcvarsall.bat` 这个标记。找不到就吐
    #            nvcc fatal : Host compiler targets unsupported OS.
    #        （那句跟"操作系统"毫无关系，它其实是在说"我认不出这个位置上的
    #         宿主编译器"。昨天就是被这句话骗了半天。）
    #        所以包里必须**照 VS 的形状摆**：`VS/VC/Tools/MSVC/<版本>/bin/
    #        Hostx64/x64/cl.exe` + `VS/VC/Auxiliary/Build/vcvarsall.bat`
    #        —— 由 `打包.py` 的 `拷编译环境` 铺，`关键件` 里守着。
    #
    #     ② 找到之后**去跑那个 vcvars 批处理**铺环境，再拿它铺出来的 cl 编。
    #        包里的 bat 只是个空占位（真跑也铺不出东西），而真机上的那份会指向
    #        真机装好的 VS —— 那就又回到"只有装了 VS 的机器能编"。
    #        所以这一步必须跳过：开关就是 `VSCMD_VER`。
    #        （nvcc 文档原话：cl.exe 在 PATH 里、`VSCMD_VER` 已设置，就跳过批处理。
    #          实测**值是什么都行**，填 `x` 也认 —— 它只看有没有。）
    #
    #   ⚠ 别再回头试这两条死路：`-ccbin`（给目录报 `Failed to run "<目录>"
    #     (拒绝访问)`，给 cl.exe 全路径报 `Failed to preprocess host compiler
    #     properties`）和 `-use-local-env`（没有上面那个骨架，照样报"目标系统
    #     不支持"）。
    环境['VSCMD_VER'] = '17.0'           # 只被 nvcc 当"环境已就绪"的开关看

    # cl.exe 会拉起一个常驻的 `vctip.exe`（VS 遥测），它**锁住工具链目录里的
    # DLL** —— 下次想更新工具链时那个目录删都删不掉（`WinError 5 拒绝访问`，
    # 而且报错指不出是谁占着）。这两个变量是让它别拉 / 别多话，尽力而为。
    环境['VSCMD_SKIP_SENDTELEMETRY'] = '1'
    环境['VCTIP_NOLOGO'] = '1'
    环境['CMAKE_GENERATOR'] = 'Ninja'
    环境['CMAKE_MAKE_PROGRAM'] = os.path.join(Y, 'Scripts', 'ninja.exe')
    环境['PYTHONIOENCODING'] = 'utf-8'
    # Vulkan 那条路要额外指一样：**SDK 在哪**。
    # ⚠ 这条必须设：llama.cpp 的 CMake 用 `find_package(Vulkan REQUIRED)`，而它认的
    #   就是 `VULKAN_SDK` 这个环境变量（据此找 include / lib / glslc）。包里那份
    #   SDK 摆在 `编译环境/VulkanSDK/`，形状照真身摆 —— 见 `打包.py 的 编译环境清单`。
    # ⚠ `Bin` 也要进 PATH：`glslc` 是 CMake 直接按名字去调的，不进 PATH 它找不到。
    if 后端 == 'vulkan':
        套 = os.path.join(根, 'VulkanSDK')
        环境['VULKAN_SDK'] = 套
        环bin = os.path.join(套, 'Bin')
        if os.path.isdir(环bin):
            环境['PATH'] = 环bin + os.pathsep + 环境.get('PATH', '')

    # 后端开关 + 架构。
    # ⚠ **Vulkan 那条一个架构参数都不能传。** 传了 `CMAKE_CUDA_ARCHITECTURES`，
    #   CMake 就会去找 CUDA 工具链 —— 而 A 卡那台机器上根本没有，于是报一堆
    #   "找不到 CUDA"，看着像缺件，其实是我们自己把 CUDA 招来的。
    #   这也是"给 A 卡编"最容易踩的一脚。
    段 = [后端开关.get(后端, 后端开关[默认后端])]
    if 后端 == 默认后端 and 架构:
        段.append('-DCMAKE_CUDA_ARCHITECTURES=%s' % 架构)
    # rc / mt 跟后端无关 —— 那是 MSVC 那套的事（编译资源、嵌清单），两条路都要。
    for 名, 相 in (('CMAKE_RC_COMPILER', 'WinSDK/bin/rc.exe'),
                   ('CMAKE_MT', 'WinSDK/bin/mt.exe')):
        径2 = os.path.join(根, *相.split('/'))
        if os.path.isfile(径2):
            # ⚠ **这里必须换成正斜杠。** `CMAKE_ARGS` 是被 **shlex** 拆的，
            #   反斜杠在那套规则里是转义符 —— 写 `D:\a\rc.exe` 进去，
            #   cmake 收到的是 `D:arc.exe`（反斜杠全没了），于是报
            #   「RC Pass 1 failed … no such file or directory」，
            #   而那行日志里路径还是一副"对过"的样子，极难看出来。
            #   （编译GPU版.bat 里写的也是 `C:/btmp2/sdk/rc.exe` 正斜杠。）
            段.append('-D%s=%s' % (名, 径2.replace('\\', '/')))
    环境['CMAKE_ARGS'] = ' '.join(段)
    return 环境


# ── 编译 ────────────────────────────────────────────────────────────

def _杀树(程):
    """
    连子进程一起干掉。**必须 `/T`**：pip → cmake → ninja → nvcc 是一棵树，
    只杀 pip 的话后面几层会活下来接着编译，下次构建时它们还占着文件。
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
        程.wait(timeout=15)
    except Exception:
        try:
            程.kill()
        except Exception:
            pass


def _收遥测():
    """
    收掉 `cl.exe` 留下的 `vctip.exe`（VS 遥测）。

    ⚠ 它是 `cl.exe` 编完拉起来的**常驻**进程，镜像就落在工具链目录里 —— 所以
      编译跑完之后，`编译环境/`（以及上层的 `dist/`）**整个删不掉**，报的却是
      `WinError 5 拒绝访问` 或「Device or resource busy」，指不出是谁占着。
      用户想删 dist/、我们想换工具链，都会被它挡下来。

    ⚠ `_杀树()` 带不走它：那杀的是 pip → cmake → ninja → nvcc 这棵树，而 vctip
      是 cl.exe 自己拉起来的，早就跟这棵树脱钩了（实测能活到构建结束之后很久）。

    ⚠ 不在收尾路径上做"有没有残留"的判断 —— 编译失败、被停，同样会留下它，所以
      不管成不成都要收一次（调用点在 `编译()` 里 `程.wait()` 之后）。

    `VSCMD_SKIP_SENDTELEMETRY` / `VCTIP_NOLOGO`（见 `铺环境`）是"别拉"的尽力而为，
      实测拦不住；新打的包里已经没有这个 exe（`打包.py` 的 `摘遥测件()`），
      这里是给老包和真机环境兜底的。
    """
    if sys.platform != 'win32':
        return
    工具 = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                       'System32', 'taskkill.exe')
    try:
        subprocess.run([工具, '/F', '/IM', 'vctip.exe'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _看旗(程, 停旗):
    """盯着停旗，一置位就把整棵树杀了。**编译一次几十分钟，光靠"读一行查一次旗"
    不够 —— 中间可能有几分钟一句话都不输出。**"""
    while 程.poll() is None:
        if 停旗 is not None and 停旗.is_set():
            _杀树(程)
            return
        time.sleep(0.5)


def _解码(原):
    """
    子进程吐出来的一行 → 字符串。

    ⚠ **不能假定是 UTF-8。** cmake / ninja / nvcc 在中文 Windows 上按本地代码页
      （GBK）输出，硬按 UTF-8 解出来就是一片 `鐖\xac瀛樹竴浣撳紡` —— 路径里带中文
      （我们这个项目就在中文目录下）时尤其难看，而且排错时真正要看的就是路径。
      跟 `酒馆工具._解码` 是同一套级联：先严格试 UTF-8，再试 GBK，都不成就替换。
    """
    for 码 in ('utf-8', 'gbk'):
        try:
            return 原.decode(码)
        except UnicodeDecodeError:
            continue
    return 原.decode('utf-8', 'replace')


def 编译(架构, 行回调=None, 停旗=None, 后端=默认后端):
    """
    真编一份。回 `(wheel 路径, 日志全文)`。

    命令就是官方那套源码构建（跟 `GPU-model/编译GPU版.bat` 里的等价）：

        python -m pip wheel llama-cpp-python==<版本> \\
               --no-binary :all: --no-deps --no-build-isolation -w <产物目录>

    `后端` 决定编哪一套：`'cuda'`（NVIDIA）或 `'vulkan'`（AMD，也吃 Intel / 核显）。
    **只有 CUDA 那条需要 `架构`** —— Vulkan 的产物是 SPIR-V，与显卡型号无关，
    编一次谁都吃，所以它那条路一个架构号都不传。

    ⚠ `--no-build-isolation`：构建依赖（cmake / ninja / scikit-build-core）已经在
      包里那份便携 Python 里了，再开一个隔离环境会去网上拉一遍 —— 那正是我们要
      避免的。**这条是"能离线编"的关键。**
    ⚠ 产物写在 `编译环境/_编译产物/`，**不碰那份便携 Python**：出的是 wheel，
      不是 install。装在包里那份 Python 上纯属污染，而且还得再去 site-packages 里翻。
    """
    行回调 = 行回调 or (lambda 行: None)
    if 后端 not in 后端开关:
        raise 编译错('不认识这个后端：%s' % 后端)
    根 = 环境目录()
    齐, 缺 = 齐不齐()
    if not 齐:
        raise 编译错('编译环境不齐，缺：\n  · ' + '\n  · '.join(缺))
    # 只有 CUDA 需要架构号。Vulkan 缺了它照样编 —— 别在这儿把人拦下。
    if 后端 == 默认后端 and not 架构:
        raise 编译错('没给编译目标（比如 RTX 5050 是 120）。')
    版本号 = 版本()
    if not 版本号:
        raise 编译错('读不到 llama-cpp-python 的版本号 —— 依赖库/llama_cpp 那份不在了？')

    产物 = os.path.join(根, '_编译产物')
    os.makedirs(产物, exist_ok=True)
    指令 = [os.path.join(根, 'Python', 'python.exe'), '-m', 'pip', 'wheel',
            'llama-cpp-python==%s' % 版本号,
            '--no-binary', ':all:', '--no-deps', '--no-build-isolation',
            '-w', 产物]
    源 = 源码包()
    if 源:
        指令 += ['--no-index', '--find-links', os.path.dirname(源)]
        行回调('用包里的源码包，不联网：%s' % os.path.basename(源))
    else:
        行回调('包里没有源码包，走网络去下 %s 的源码…' % 版本号)

    if 后端 == 默认后端:
        行回调('目标：sm_%s' % 架构)
    else:
        行回调('目标：%s —— 这条路不需要架构号（产物与显卡型号无关）'
              % 后端话.get(后端, 后端))
    行回调('>> ' + ' '.join(指令))
    环境 = 铺环境(架构, 后端)
    try:
        程 = subprocess.Popen(指令, cwd=产物, env=环境, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              bufsize=0)          # 二进制读，自己按 `_解码` 解
    except Exception as 错:
        raise 编译错('起不了编译器：%s：%s' % (type(错).__name__, 错))

    threading.Thread(target=_看旗, args=(程, 停旗), daemon=True).start()
    行们 = []
    try:
        for 原行 in 程.stdout:
            行 = _解码(原行).rstrip()
            行们.append(行)
            行回调(行)
    finally:
        try:
            程.stdout.close()
        except Exception:
            pass
    程.wait()
    _收遥测()          # 成不成都要收：见 `_收遥测()`，不收的话 dist/ 就删不掉

    if 停旗 is not None and 停旗.is_set():
        raise 编译错('已停止。编译到一半的东西没用了，下次得从头来。')
    if 程.returncode != 0:
        尾 = '\n'.join(行们[-40:])
        raise 编译错('编译失败（退出码 %s）。日志最后 40 行：\n%s' % (程.returncode, 尾))

    轮子 = None
    for 件 in sorted(os.listdir(产物)):
        if 件.startswith('llama_cpp_python-%s' % 版本号) and 件.endswith('.whl'):
            轮子 = os.path.join(产物, 件)
    if 轮子 is None:
        归 = '\n'.join(行们[-20:])
        raise 编译错('pip 说成功了，但产物目录里没有 wheel。最后 20 行：\n%s' % 归)
    行回调('产物：%s' % 轮子)
    return 轮子, '\n'.join(行们)


# ── 换装 ────────────────────────────────────────────────────────────

def 试加载(库目录, 秒=300):
    """
    开个子进程，把 `库目录` 当成本地引擎的 DLL 目录，真 `import llama_cpp` 一次。
    回 `(成不成, 输出)`。

    ⚠ **必须另起一个进程。** 这些 DLL 在本进程里早就加载了（程序启动时就加载了），
      再指别的目录一点用没有 —— 模块在 `sys.modules` 里缓存着、DLL 也已经映射进
      地址空间。只有干净进程问得准。

    ⚠ **不能拿 `sys.executable` 去跑这段代码**：打包后它是那个 exe，`exe -c "..."`
      会去开界面，而不是跑我们给的字符串。所以用包里那份**便携 Python**
      （它本来就在），再把 `依赖库/` 挂到 `PYTHONPATH` 上给它找 llama_cpp 的
      Python 代码 —— 那份代码跟正在跑的这份是同一份。

    ⚠ 试的是"**能不能加载**"（DLL 依赖链通不通），不是"能不能出字" ——
      后者要加载几 GB 的模型、跑几十秒，不该发生在点"换装"的时候。

    ⚠ `CUDA_PATH` 要摘掉：目标机器多半没装 CUDA 工具包，别让打包机的工具包
      替它把 cublas 捞出来 —— 那样这个试加载就成了自欺（跟 打包.py 里
      `[8] 双后端自检` 要剥 CUDA_PATH 是同一个道理）。
    """
    包 = _包目录()
    if 包 is None or not os.path.isdir(库目录):
        return False, '没有可试的目录：%s' % 库目录
    py = os.path.join(环境目录() or '', 'Python', 'python.exe')
    if not os.path.isfile(py):
        # 没有便携 Python 就跳过这一关 —— 不能因为"试不了"就拦住换装。
        return True, '（包里没有便携 Python，跳过试加载这一步）'
    环境 = dict(os.environ)
    环境['PYTHONPATH'] = os.path.dirname(包)
    环境['LLAMA_CPP_LIB_PATH'] = os.path.abspath(库目录)
    环境['MTMD_CPP_LIB'] = os.path.abspath(库目录)
    环境['PYTHONIOENCODING'] = 'utf-8'
    环境.pop('CUDA_PATH', None)
    环境.pop('CUDA_HOME', None)
    码 = ('import importlib\n'
          'm = importlib.import_module("llama_cpp")\n'
          'print("OK", m.__version__)\n')
    try:
        果 = subprocess.run([py, '-c', 码], env=环境, cwd=os.path.dirname(包),
                            capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=秒)
    except Exception as 错:
        return False, '%s：%s' % (type(错).__name__, 错)
    出 = ((果.stdout or '') + (果.stderr or '')).strip()
    return (果.returncode == 0 and 'OK' in (果.stdout or '')), 出


def 装库(轮子径, 后端=默认后端):
    """
    把 wheel 里的 `llama_cpp/lib/*.dll` 换成 `依赖库/llama_cpp/lib/`。
    回一句人话（给界面直接显示）。

    `后端` 说这个 wheel 是哪个后端编出来的（`'cuda'` / `'vulkan'`）——
    校验"缺没缺柱子"、以及"要不要把外部依赖搬过来"，两件事都看它。

    四步，**顺序不能反**：

      1. **先校验产物**：里面必须有 `llama.dll`，还必须有那个后端的柱子
         （CUDA 是 `ggml-cuda.dll`，Vulkan 是 `ggml-vulkan*.dll`）。少了柱子就不是
         那个后端 —— 装上去等于把 GPU 能力弄丢，而且**看不出来**（程序照跑，只是
         一直蹲在 CPU 上）。宁可在这儿停。
      2. **先写到 `lib_新编/`**，不直接往 `lib/` 里写：正在被这个进程用的文件写不了，
         而且写坏了没有退路。
      3. **把外部依赖搬过来**（**只在后端没变时才搬**）：`lib/` 里那两个 cublas
         **不在 wheel 里**（它们来自 CUDA 工具包，见 打包.py 文件头那条依赖链），
         必须跟着走，不然新编的 `ggml-cuda.dll` 一加载就断在它身上。
         ⚠ **换后端时不搬**：CUDA → Vulkan 搬过去就是 492MB 的死重量；反过来
         Vulkan → CUDA 又缺 cublas，那条会被下面第 4 步当场拦住（不是静默坏）。
      4. **先试加载**（`试加载`）：干净子进程里 `import` 一次。这一步是"还没换
         就发现编出来是坏的"的唯一机会 —— 不试的话，坏的那份要等下次启动才现形。
      5. `lib/` → `lib_旧_<时间>/`，`lib_新编/` → `lib`。**全程改名、不拷贝**：
         492MB 的 cublas 拷一份要好几秒，改名是瞬间的。

    ⚠ 换完**必须重启程序**：正在跑的这个进程用的是已经加载进内存的旧 DLL。
      实测算过，Windows 允许在 DLL 加载状态下改整个目录的名（映像文件是
      FILE_SHARE_DELETE 打开的），所以**不用关程序也能换**，只是换完得重启才生效。
    """
    import zipfile
    if 后端 not in 后端开关:
        raise 编译错('不认识这个后端：%s' % 后端)
    lib = 库目录()
    if lib is None:
        raise 编译错('找不到 llama_cpp 包（依赖库/llama_cpp 不在）。')
    包 = os.path.dirname(lib)
    新 = os.path.join(包, 'lib_新编')
    旧 = os.path.join(包, 备份前缀 + time.strftime('%Y%m%d_%H%M%S'))
    旧后端 = 认后端(lib)              # 换之前 `lib/` 是哪一套 —— 决定第 3 步搬不搬

    if os.path.isdir(新):
        shutil.rmtree(新, ignore_errors=True)
    os.makedirs(新, exist_ok=True)
    try:
        with zipfile.ZipFile(轮子径) as z:
            要 = {os.path.basename(n): n for n in z.namelist()
                  if n.startswith('llama_cpp/lib/') and n.lower().endswith('.dll')}
            # 柱子那条用通配匹（Vulkan 的 DLL 名不敢写死，见 `后端柱子`）。
            少 = [n for n in ('llama.dll',) if n not in 要]
            if not any(fnmatch.fnmatch(n, 后端柱子[后端]) for n in 要):
                少.append(后端柱子[后端])
            if 少:
                死因 = ('这个 wheel 里没有 %s —— 它不是 %s 版（多半编的时候没吃上 '
                       '%s）。装上去本地模型就只能蹲 CPU 上跑。'
                       % ('、'.join(少), 后端话.get(后端, 后端),
                          'GGML_CUDA' if 后端 == 'cuda' else 'GGML_VULKAN'))
                shutil.rmtree(新, ignore_errors=True)
                raise 编译错(死因)
            for 名, 内 in 要.items():
                with open(os.path.join(新, 名), 'wb') as f:
                    f.write(z.read(内))
    except 编译错:
        raise
    except Exception as 错:
        shutil.rmtree(新, ignore_errors=True)
        raise 编译错('产物读不开（%s：%s）：%s' % (type(错).__name__, 错, 轮子径))

    跟着 = 0
    # ⚠ **只在后端没变时才搬。** 搬的是"新一轮里没有、旧 lib/ 里却有"的那些文件 ——
    #   对 CUDA 来说就是 cublas（它们不在 wheel 里，来自 CUDA 工具包）。
    #   换后端时不能搬：CUDA → Vulkan 搬过去就是 492MB 死重量；反过来
    #   Vulkan → CUDA 又缺 cublas —— 那条会在下面"试加载"那步当场被拦住（喧闹失败，
    #   不是静默坏），到那时候按提示重编一次就行。
    if 旧后端 and 旧后端 == 后端:
        for 名 in os.listdir(lib):
            目标 = os.path.join(新, 名)
            if not os.path.exists(目标):
                try:
                    os.rename(os.path.join(lib, 名), 目标)
                    跟着 += 1
                except OSError:
                    shutil.copy2(os.path.join(lib, 名), 目标)
                    跟着 += 1

    # 换之前先试一遍 —— **这是唯一能在"还没换"的时候发现"编出来的根本加载不了"
    # 的机会**。不试的话，坏的那份要等下次启动程序才现形，而那时用户已经在用
    # 一个新启动器都没有的包了（虽然还有"还原上一份"，但那得先能起来）。
    成, 出 = 试加载(新)
    if not 成:
        尾 = '\n'.join(出.splitlines()[-12:])
        # 把试加载搬过去的那两个 cublas 挪回旧 lib/ —— 它们是旧那份的东西，
        # 别留在废弃目录里，也别让旧那份缺胳膊少腿（还要靠它兜底）。
        if os.path.isdir(lib):
            for 名 in os.listdir(新):
                if not os.path.isfile(os.path.join(lib, 名)):
                    try:
                        os.rename(os.path.join(新, 名), os.path.join(lib, 名))
                    except OSError:
                        pass
        shutil.rmtree(新, ignore_errors=True)
        raise 编译错('新编的那套 DLL 加载不起来，**没有换装**（现在这份没动）。\n'
                    '输出：\n%s' % 尾)

    try:
        if os.path.isdir(lib):
            os.rename(lib, 旧)
        os.rename(新, lib)
    except Exception as 错:
        # 走到这儿说明改名失败（杀软占着、或者没有写权限）。**把现场留着** ——
        # 新编的那份还在 `lib_新编/`，别删，人还能手动换。
        raise 编译错('换装失败（%s：%s）。\n新编的那份留在 %s，可以关掉程序手动换。'
                    % (type(错).__name__, 错, 新))
    return ('换好了：%d 个新 DLL，另有 %d 个外部依赖（cublas 那些）原样跟着。\n'
            '旧的那份留作备份：%s\n**重启程序才生效** —— 现在跑着的还是旧的。'
            % (len(要), 跟着, os.path.basename(旧)))


def 还原(备份名):
    """
    把 `lib/` 换回某份备份（`备份们()` 给的名字）。回一句人话。

    走的是同一套"改名不拷贝"：现在的 `lib/` 也会被留成一份新备份，
    所以**还原本身也是可逆的**，不存在"越折腾越少"。
    """
    包 = _包目录()
    if 包 is None:
        raise 编译错('找不到 llama_cpp 包。')
    lib = os.path.join(包, 'lib')
    源 = os.path.join(包, 备份名)
    if not os.path.isdir(源):
        raise 编译错('这份备份不在了：%s' % 备份名)
    现在 = os.path.join(包, 备份前缀 + time.strftime('%Y%m%d_%H%M%S'))
    try:
        if os.path.isdir(lib):
            os.rename(lib, 现在)
        os.rename(源, lib)
    except Exception as 错:
        raise 编译错('还原失败（%s：%s）。' % (type(错).__name__, 错))
    return '已换回 %s。刚才那份留成了 %s。**重启程序才生效。**' % (
        时间戳说明(备份名[len(备份前缀):]), os.path.basename(现在))
