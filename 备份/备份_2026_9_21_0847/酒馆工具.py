#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆工具.py — 编程助手能用的那几个工具，外加把它们关在工作目录里的沙箱

**这个文件不 import Qt。** 跟 `酒馆大脑` / `酒馆本地` 一个道理：路径判定、
截断、JSON 参数校验是**最需要反复单测**的三块，绑上 Qt 就没法痛快测了。
而这三块恰好是小模型不听话时出问题最多的地方。

三件事：

    定界()     把模型给的路径钉死在工作目录里（**这是这一层的命门**）
    工具们     读 / 写 / 改 / 列 / 搜 / 跑命令，每个都自带截断
    定义们     上面那些工具的 JSON Schema，喂给模型看

════════════════════════════════════════════════════════════════════
两件必须一直记着的事
════════════════════════════════════════════════════════════════════

**一、每个工具的返回都必须截断，这是常态不是极端情况。** agent 干的第一件
事就是读文件，而一个两千行的文件塞进 8K 上下文就是当场爆掉。所以阈值是
写死的常量，不是"防御性代码"。

**二、模型给的路径一律不信。** 小模型会写绝对路径、会写 `..\\..`、会写
`C:foo` 这种盘符相对路径（`os.path.isabs` 对它判 **False**，只判 isabs
会漏），会写 `NUL`（Windows 上 `open('NUL','w')` **静默成功**，写进黑洞，
模型以为写好了）。每一条都有对策，见 `定界` 那张表。
"""

import os
import re
import subprocess
import threading

__all__ = ['工具错', '定界', '估token', '定义们', '工具名们',
           '执行', '要审批吗', '硬黑名单', '根目录说明', '语法提示',
           '单行上限', '读上限行', '读上限字', '列上限条', '搜上限条', '命令上限字']


class 工具错(Exception):
    """
    工具这一层出的错。**文案写给人看，而且会被原样喂回模型**。

    这一点跟别处的异常不一样：别处的错是给用户看的，这里的错**同时也是
    给模型看的提示**。所以文案要写"你该怎么做"，不能只写"出错了"：

        差：'路径不合法'
        好：'只接受相对工作目录的路径（不要给绝对路径）：C:\\x'

    模型看到第二种，多半下一轮就改对了。
    """


# ── 阈值（都是一次性常量，改这儿就行）──────────────────────────────

#: 单行最多显示多少字符。压日志、压压缩过的一行文件用。
单行上限 = 500
#: `读文件` 一次最多几行。**硬上限，模型要再多也不给。**
读上限行 = 400
#: `读文件` 返回文本的字符上限。
读上限字 = 16000
#: `列目录` 最多几条 + 字符上限。
列上限条 = 200
列上限字 = 8000
#: `找文件` 最多几条。
搜上限条 = 100
搜上限字 = 8000
#: `跑命令` 输出字符上限（头尾各留一半）。
命令上限字 = 8000
#: `跑命令` 一次最多收多少字节进内存。**超了就杀进程** —— 不设这个的话
#: `dir /s C:\\` 或者一次完整构建日志能把程序吃爆。
命令原始上限 = 256 * 1024
#: 命令超时的夹逼范围（秒）。
命令超时下限, 命令超时上限 = 1, 120

#: 列目录时默认跳过的目录。这些里面动辄几万个文件，列出来除了烧上下文
#: 没有任何用处。
跳过目录 = frozenset(('.git', 'node_modules', '__pycache__', '.venv',
                   'venv', '.idea', '.vscode', 'dist', 'build',
                   '.mypy_cache', '.pytest_cache', '.tox', 'target'))

#: 二进制扩展名。`读文件` 读到这些直接拒绝 —— 读进来一堆乱码既烧上下文
#: 又完全没用，还不如明确告诉模型"这是二进制，别读"。
二进制尾巴 = frozenset((
    '.exe', '.dll', '.so', '.dylib', '.bin', '.dat', '.pyd', '.pyc', '.pyo',
    '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar', '.whl',
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp', '.svg',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.mp3', '.mp4', '.avi', '.mkv', '.wav', '.flac', '.mov',
    '.gguf', '.safetensors', '.pt', '.pth', '.onnx', '.h5', '.pb',
    '.woff', '.woff2', '.ttf', '.otf', '.eot', '.db', '.sqlite', '.sqlite3',
))


# ── 路径沙箱 ────────────────────────────────────────────────────────

#: Windows 保留设备名。`open('NUL','w')` 会**静默成功**（写进黑洞），
#: 模型会以为文件写好了 —— 这种"成功了的失败"最难查，所以直接拒。
保留名 = frozenset((
    'con', 'prn', 'aux', 'nul',
    'com1', 'com2', 'com3', 'com4', 'com5', 'com6', 'com7', 'com8', 'com9',
    'lpt1', 'lpt2', 'lpt3', 'lpt4', 'lpt5', 'lpt6', 'lpt7', 'lpt8', 'lpt9',
))


def 定界(根, 目标, 要文件=True):
    """
    把模型给的路径钉死在 `根` 里面。回绝对路径；出界就抛 `工具错`。

    `要文件=False` 是给 `列目录` / `找文件` 用的——那两个的合法目标是目录，
    `.` 和 `sub` 都该放行。判目录/文件这件事不能混在一起，见下面末尾那段。

    ⚠ **用 `realpath` + `commonpath`，不是字符串前缀匹配。** 前缀匹配挡不住
    符号链接，也会被 `C:\\Proj` 和 `c:\\proj` 这种大小写差异骗过去。

    要挡的东西，以及各自挡在哪：

        要挡的                     挡在哪                        为什么要单独管
        ─────────────────────────────────────────────────────────────────────
        `..\\..\\Windows\\x`       commonpath 归一后前缀对不上   最基本的一种
        `sub/../a`（**合法的**）   —— 放行 ——                    见 `..` 就拒会误伤
        符号链接指到外面           realpath 解出真实路径          软链接是藏路径的常见手法
        Windows 大小写             normcase 两边                 C:\\Proj vs c:\\proj
        跨盘 `D:\\x`               commonpath 抛 ValueError       接住给人话，别让它裸奔
        绝对路径 `C:\\x` / `/etc`  isabs 直接拒                  模型最常犯；直接拒比
                                                                 "解析后发现在里面才放行"清楚
        **盘符相对 `C:foo`        splitdrive 拒                 ⚠ os.path.isabs('C:foo')
          **                                                     判 **False**！只判 isabs
                                                                 会漏，这个坑必须单测
        UNC `\\\\srv\\share`        isabs 判 True → 拒            兜住了
        `NUL` / `CON` / `COM1`     保留名表拒                    open('NUL','w') 静默成功
        结尾的点 / 空格           拒绝，让模型改名              Windows 会**静默吃掉**结尾
                                                                 的点，`X.` 变成 `X`
                                                                 （`酒馆本地._净名` 那条
                                                                 注释记的就是这个坑）

    ⚠ **TOCTOU 不防**（上面判定完到真正 open 之间被换成符号链接）。本机
    单用户、自己给自己当 agent，这一段不在威胁模型内 —— 写在注释里，
    不假装防住了。
    """
    if not 目标 or not str(目标).strip():
        raise 工具错('路径是空的。要给一个相对工作目录的路径，比如 src/main.py')
    目标 = str(目标).strip().strip('"').strip("'").strip()   # 小模型爱加引号
    if not 目标:
        raise 工具错('路径是空的。要给一个相对工作目录的路径，比如 src/main.py')

    # 归一斜杠，免得 Windows 上 `a/b` 和 `a\b` 两种写法各自出岔子
    目标 = 目标.replace('/', os.sep).replace('\\', os.sep)

    if os.path.isabs(目标):
        raise 工具错('只接受相对工作目录的路径，不要给绝对路径：%s\n'
                    '（工作目录已经是根了，直接写 src/main.py 这样就行）' % 目标)
    盘, _尾 = os.path.splitdrive(目标)
    if 盘:
        raise 工具错('路径不能带盘符：%s\n'
                    '（`C:foo` 这种写法在 Windows 上会被当前盘符解释，'
                    '我们一律不接受，请写相对路径）' % 目标)

    根真 = os.path.realpath(根)
    试 = os.path.join(根真, 目标)
    # 写新文件时文件还不存在，realpath 一个不存在的路径会把最后一段原样
    # 留着；真正要判的是**它所在的目录**有没有出界。
    上级 = os.path.dirname(试) or 根真
    上级真 = os.path.realpath(上级)
    try:
        共同 = os.path.commonpath([os.path.normcase(根真), os.path.normcase(上级真)])
    except ValueError:
        # 两边不在同一个盘上，commonpath 会抛
        raise 工具错('这个路径跨到了别的盘：%s\n工作目录是 %s' % (目标, 根真))
    if 共同 != os.path.normcase(根真):
        raise 工具错('这个路径跑到工作目录外面去了：%s\n'
                    '（工作目录是 %s，只能动它里面的东西）' % (目标, 根真))

    末 = os.path.basename(试)
    if not 末:
        raise 工具错('路径最后是空的：%s' % 目标)
    if 要文件:
        # 只对"要一个具体文件"的操作管这个。列目录/搜索的合法目标就是目录，
        # `.` 和 `sub` 都该放行 —— 这两件事混在一起判就会误伤。
        if 末 in ('.', '..'):
            raise 工具错('这是个目录，不是文件：%s' % 目标)
    if 末 not in ('.', '..'):
        if 末.split('.')[0].lower() in 保留名:
            raise 工具错('`%s` 是 Windows 的保留设备名，不能拿来当文件名。'
                        '换一个名字。' % 末)
        if 末 != 末.rstrip('. '):
            raise 工具错('名字不能以点或空格结尾（Windows 会**静默**把它们吃掉，'
                        '你写的是 `%s` 但盘上会变成 `%s`）。改个名字。'
                        % (末, 末.rstrip('. ')))
    # 中间层目录也过一遍同样的检查
    for 段 in os.path.relpath(上级真, 根真).split(os.sep):
        if 段 in ('.', ''):
            continue
        if 段 != 段.rstrip('. ') or 段.split('.')[0].lower() in 保留名:
            raise 工具错('路径里有不能用的目录名：%r（结尾的点/空格会被 '
                        'Windows 静默吃掉，保留设备名不能用）' % 段)
    return os.path.join(上级真, 末)


def 相对(根, 绝对):
    """绝对路径 → 相对工作目录的显示形式。给模型看的路径一律用这种。"""
    try:
        return os.path.relpath(绝对, os.path.realpath(根)).replace(os.sep, '/')
    except ValueError:
        return 绝对


def 根目录说明(根):
    """第一条系统提示里要写死的东西 —— 模型最常见的错就是不知道根在哪。"""
    return ('工作目录（一切相对路径都以它为根）：%s' % os.path.realpath(根))


# ── 估 token ────────────────────────────────────────────────────────

def 估token(文):
    """
    粗估一段文本多少 token。**不用真 tokenizer** —— 那要拿到模板渲染后的
    整段文本才准，我们手上只有消息数组，渲染是 llama.cpp 内部干的。

    ⚠ **系数 2026-09-19 重标过一次，原来是 1.87 / 3.5，两个都太乐观。**

    怎么发现的：`n_ctx=4096` 的本地模型跑着跑着报
    `Requested tokens (4359) exceed context window of 4096` —— 我们那道
    "还装不装得下"的闸是按这个函数估的，估低了 1.5 倍。

    拿 `Qwen3.5-9B` **真正的 tokenizer** 量了本项目 25.7 万字
    （10 个 `.py` + 那份倒计时网页的 3 个分片）：

        文件            字数     真token   字/token   中文占比
        酒馆助手.py    23098     11116      2.08      33.2%   ← 最密
        酒馆本地.py    52770     24642      2.14      24.7%
        酒馆锁.py      14414      6624      2.18      34.1%
        01.txt          1210       529      2.29       1.0%
        02.txt          1057       396      2.67       0.0%
        合计          257450    112217      2.29

    ⚠ **旧的 3.5（ASCII）错得最狠 —— 那是"自然英语散文"的数，不是代码的数。**
    这个 tokenizer 上代码/HTML 只有 2.1~2.9 字/token。旧的 1.87（中文）也偏高。

    新系数按"**最密的那一档还要再留一截**"取：纯中文推到约 1.4、其余 2.2。
    量下来整体高估 12~32%，**永远不会低估** —— 而这里只有一个方向是安全的：

    ⚠ **估高了只是保守**（提前折叠、多付一次 prefill，难受但不出错）；
    **估低了会让提示词真的越过 `n_ctx`**，llama.cpp 直接抛错把任务打炸
    （不是截断，是抛错 —— 这条也跟旧注释写的不一样，实测撞到了）。
    所以宁可估高。
    """
    文 = 文 or ''
    中 = 0
    for c in 文:
        o = ord(c)
        if 0x4E00 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF:
            中 += 1
    他 = len(文) - 中
    return int(中 / 1.4 + 他 / 2.2) + 2


# ── 截断 ────────────────────────────────────────────────────────────

def _切单行(行):
    """单行太长就中间截。给模型看的是"这行有多长"，不是这一行的全部内容。"""
    if len(行) <= 单行上限:
        return 行
    return '%s…（这一行还有 %d 字符）' % (行[:单行上限], len(行) - 单行上限)


def _尾截(文, 上限字, 标记):
    """从尾部截，并把标记附在后面。**标记必须让模型看见** —— 不然它会把
    半截内容当成全部，然后基于残缺信息做判断。"""
    if len(文) <= 上限字:
        return 文
    return 文[:上限字] + '\n' + 标记


def _头尾截(文, 上限字, 尾巴保留=None):
    """
    头尾各留一半，中间省略。

    **为什么命令输出要头尾都留**：报错的 traceback 在开头（"哪个文件哪一行"），
    而"失败了/几个测试没过"在结尾。只留头会丢掉结论，只留尾会丢掉位置。

    ⚠ **没超限就原样返回，不加省略号。** 漏了这个判断的话，一段二十个字的
    输出后面也会挂一句"（中间省略 -7988 字符）"—— 负数，因为减反了。
    """
    文 = 文 or ''
    if len(文) <= 上限字:
        return 文
    尾 = 尾巴保留 if 尾巴保留 is not None else 上限字 // 2
    首 = 上限字 - 尾
    省略 = len(文) - 首 - 尾
    return (文[:首] + '\n…（中间省略 %d 字符）…\n' % 省略 + 文[-尾:])


# ── 工具定义（喂给模型看的 JSON Schema）──────────────────────────────
#
# ⚠ 给小模型的 schema 有几条经验，都体现在下面：
#
#   · **只用一层 object**，不嵌套、不用 $ref、不用 oneOf。
#   · **所有参数都进 required**，连有默认值的也进 —— 小模型对"可省略"
#     处理得很差，省了它就不给。
#   · description 一句话 + **一个具体例子**。例子的作用大于所有形容词。
#   · 枚举用 `enum`，比散文里写"用 A 或 B"有效得多。
#   · **参数名用 ASCII**（path/start/count…）。这是线上协议，不是我们的
#     代码标识符 —— "全中文命名"的约定管的是代码，不管协议。

def _函(名, 说明, 参数):
    """一条工具定义。`参数` 是 `[(名, 类型, 说明), …]`，全部自动进 required。"""
    属性 = {}
    for 键, 型, 描 in 参数:
        属性[键] = {'type': 型, 'description': 描}
    return {'type': 'function', 'function': {
        'name': 名, 'description': 说明,
        'parameters': {'type': 'object', 'properties': 属性,
                       'required': [k for k, _, _ in 参数]}}}


定义们 = (
    _函('read_file',
        '读一个文本文件里的若干行。返回带行号的内容。例如 path="src/main.py"',
        [('path', 'string', '相对工作目录的文件路径，例如 src/main.py'),
         ('start', 'integer', '从第几行开始（从 1 数），例如 1'),
         ('count', 'integer', '最多读几行，例如 100')]),
    _函('list_dir',
        '列一个目录里的文件和子目录。例如 path="."',
        [('path', 'string', '相对工作目录的目录路径，用 "." 表示工作目录本身'),
         ('depth', 'integer', '往下看几层，一般用 1')]),
    _函('search',
        '在文件内容里找一段文字，或者在文件名里找。例如 pattern="def main"',
        [('pattern', 'string', '要找的文字或文件名的一部分'),
         ('path', 'string', '从哪个目录开始找，用 "." 表示工作目录'),
         ('what', 'string', '找什么：填 "内容" 搜文件内容，填 "文件名" 搜文件名'),
         ('limit', 'integer', '最多回几条，例如 50')]),
    _函('write_file',
        '写一个文件（整个覆盖）。**路径不存在会新建，已存在会整个替换掉。**'
        '只改一点点内容请用 edit_file，不要用这个。',
        [('path', 'string', '相对工作目录的文件路径，例如 src/new.py'),
         ('content', 'string', '要写进去的完整文件内容')]),
    _函('edit_file',
        '把一个文件里的某一段文字换成另一段。**old 必须在文件里唯一出现**，'
        '不唯一就多给几行上下文。改一点内容用这个，别用 write_file。',
        [('path', 'string', '相对工作目录的文件路径，例如 src/main.py'),
         ('old', 'string', '要被替换掉的原文（要跟文件里的**逐字一致**，含缩进）'),
         ('new', 'string', '替换成的新文字'),
         ('all', 'string', '是否替换全部出现的地方，填 "否" 或 "是"')]),
    _函('run_cmd',
        '在工作目录里跑一条命令，回它的输出和退出码。例如 cmd="python main.py"',
        [('cmd', 'string', '要跑的命令，例如 python -m pytest -q'),
         ('timeout', 'integer', '最多等几秒，例如 30')]),
)

工具名们 = tuple(工['function']['name'] for 工 in 定义们)

#: 哪些工具会改动磁盘 / 跑外部程序 —— 这些要用户点头。只读的不问，
#: 否则每读一个文件弹一次窗，agent 根本没法用。
要审批的 = frozenset(('write_file', 'edit_file', 'run_cmd'))


def 要审批吗(名):
    return 名 in 要审批的


# ── 命令黑名单 ──────────────────────────────────────────────────────

#: 灾难性、且跟"写代码"毫无关系的命令。**不可审批，直接拒。**
#:
#: 为什么不做白名单：代码 agent 要"改代码 → 跑测试 → 看日志"，`python` /
#: `pytest` / `git` / `npm` / `pip` 都得放，放完的白名单跟没有差不多，
#: 而维护它是一份持续成本。真正需要无条件挡住的是下面这些 —— 它们跟
#: 写代码没有任何关系，一旦跑错是整机级别的损失。
硬黑名单 = (
    (re.compile(r'(^|[\s&|;])format\s+[a-z]:', re.I), '格式化磁盘'),
    (re.compile(r'\bdiskpart\b', re.I), '分区操作'),
    (re.compile(r'\bbcdedit\b', re.I), '改启动配置'),
    (re.compile(r'\bvssadmin\b', re.I), '卷影/备份删除'),
    (re.compile(r'\breg\s+delete\b', re.I), '删注册表'),
    (re.compile(r'\bnet\s+user\b', re.I), '改用户账户'),
    (re.compile(r'\bshutdown\b|\breboot\b', re.I), '关机/重启'),
    (re.compile(r'\bdel\s+/[fsq]', re.I), '强制删除'),
    (re.compile(r'\brd\s+/s|\brmdir\s+/s', re.I), '递归删目录'),
    (re.compile(r'\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r', re.I), '强制递归删'),
    (re.compile(r'>\s*\\\\\.\\PhysicalDrive', re.I), '直接写物理磁盘'),
    (re.compile(r'\bmkfs\b', re.I), '格式化'),
)


def 查黑名单(命令):
    """命中硬黑名单就回一句人话，否则回空串。"""
    文 = 命令 or ''
    for 规, 叫 in 硬黑名单:
        if 规.search(文):
            return ('这条命令被拦下了（%s）。它跟写代码没关系，'
                    '而且一旦跑错是整机级别的损失，所以不允许执行。' % 叫)
    return ''


# ── 工具实现 ────────────────────────────────────────────────────────
#
# 每个工具签名统一 `(根, 参数)`，回一段**给模型看的文本**。
# 出错就抛 `工具错`，文案也是给模型看的（见那个类的 docstring）。

def _数字(参, 键, 默认, 低, 高):
    """
    从参数里取整数，带夹逼。模型给字符串、给负数、给离谱大的都兜住。

    ⚠ **必须挡 `Infinity` / `NaN`。** JSON 规范里没这俩，但 Python 的
    `json.loads` **认**（`parse_constant` 默认就吃），所以模型写
    `{"count": Infinity}` 能被解析成 `float('inf')`，然后
    `int(float('inf'))` 直接抛 `OverflowError` —— 而那是**内置异常**，
    不是 `工具错`，会把整个任务从根上打炸（实测撞到过）。
    """
    值 = 参.get(键)
    if 值 is None or (isinstance(值, str) and not 值.strip()):
        return 默认
    try:
        数 = float(str(值).strip())
    except (TypeError, ValueError):
        raise 工具错('参数 `%s` 得是个整数，你给的是 %r' % (键, 参.get(键)))
    if 数 != 数 or 数 in (float('inf'), float('-inf')):
        # NaN（数 != 数）和正负无穷 —— 当成没给
        return 默认
    return max(低, min(高, int(数)))


def 读文件(根, 参):
    """读一段，带行号。回 `相对路径:行号→ 内容`。"""
    径 = 定界(根, 参.get('path'))
    起 = _数字(参, 'start', 1, 1, 10 ** 9)
    数 = _数字(参, 'count', 200, 1, 读上限行)
    if not os.path.exists(径):
        raise 工具错('文件不存在：%s\n先用 list_dir 看看目录里有什么。'
                    % 相对(根, 径))
    if os.path.isdir(径):
        raise 工具错('%s 是个目录，不是文件。用 list_dir 列它。' % 相对(根, 径))
    尾 = os.path.splitext(径)[1].lower()
    if 尾 in 二进制尾巴:
        raise 工具错('%s 是二进制文件（%s），读进来是乱码。别读它。'
                    % (相对(根, 径), 尾))
    try:
        with open(径, 'r', encoding='utf-8', errors='replace') as f:
            行们 = f.readlines()
    except OSError as 错:
        raise 工具错('读不了 %s：%s' % (相对(根, 径), 错))

    总 = len(行们)
    if 起 > 总:
        raise 工具错('文件只有 %d 行，你要从第 %d 行开始读。' % (总, 起))
    取 = 行们[起 - 1: 起 - 1 + 数]
    文 = []
    for i, 行 in enumerate(取, 起):
        文.append('%5d→%s' % (i, _切单行(行.rstrip('\n'))))
    出 = '\n'.join(文)
    到 = 起 + len(取) - 1
    脚 = ''
    if 到 < 总:
        脚 = ('\n…（只显示了第 %d-%d 行，共 %d 行；要继续读就用 '
              'start=%d）' % (起, 到, 总, 到 + 1))
    if len(出) > 读上限字:
        出 = 出[:读上限字]
        脚 = ('\n…（内容太长被截断了；这一段在 %d 行以内，'
              '减小 count 或换 start 分段读）' % 读上限行)
    return '%s:%d-%d\n%s%s' % (相对(根, 径), 起, 到, 出, 脚)


def 列目录(根, 参):
    """列一层或几层。目录名后面带 `/`。"""
    径 = 定界(根, 参.get('path') or '.', 要文件=False)
    if not os.path.isdir(径):
        raise 工具错('%s 不是目录（或者不存在）。用 "." 表示工作目录。'
                    % 相对(根, 径))
    深 = _数字(参, 'depth', 1, 1, 4)
    条, 跳 = [], 0
    根基 = os.path.realpath(根)
    for 现根, 目们, 件们 in os.walk(径):
        相对深 = os.path.relpath(现根, 径).count(os.sep)
        if os.path.relpath(现根, 径) == '.':
            相对深 = -1
        if 相对深 >= 深 - 1:
            目们[:] = []
        else:
            目们[:] = sorted(d for d in 目们 if d not in 跳过目录 and not d.startswith('.'))
        # ⚠ 指向外面的符号链接要跳掉 —— 否则一个链接就能让列目录把
        # C:\Windows 列出来
        件们 = sorted(件们)
        名 = os.path.basename(现根) if 相对深 >= 0 else '.'
        前 = (相对(根, 现根) + '/') if 相对深 >= 0 else ''
        for d in 目们:
            条.append('%s%s/' % (前, d))
        for f in 件们:
            p = os.path.join(现根, f)
            try:
                if os.path.islink(p) and not os.path.realpath(p).startswith(根基):
                    跳 += 1
                    continue
                大 = os.path.getsize(p)
            except OSError:
                continue
            条.append('%s%s  (%s)' % (前, f, _大小(大)))
    if not 条:
        return '（%s 里是空的，或者全是跳过的目录）' % 相对(根, 径)
    总 = len(条)
    出 = '\n'.join(_切单行(x) for x in 条[:列上限条])
    脚 = ''
    if 总 > 列上限条:
        脚 += '\n…（还有 %d 项没列出来；用 path 缩小范围）' % (总 - 列上限条)
    if 跳:
        脚 += '\n（跳过了 %d 个指向工作目录外面的链接）' % 跳
    if len(出) > 列上限字:
        出 = 出[:列上限字]
        脚 += '\n…（内容太长被截断了）'
    return '%s（%d 项）\n%s%s' % (相对(根, 径), 总, 出, 脚)


def 找文件(根, 参):
    """按内容或文件名找。内容命中回 `路径:行号: 原文`。"""
    模 = str(参.get('pattern') or '').strip()
    if not 模:
        raise 工具错('pattern 是空的。要给出要找的文字。')
    起 = 定界(根, 参.get('path') or '.', 要文件=False)
    找什么 = str(参.get('what') or '内容').strip()
    找名 = 找什么 in ('文件名', 'name', 'filename', '文件')
    限 = _数字(参, 'limit', 50, 1, 搜上限条)
    根基 = os.path.realpath(根)
    条, 满 = [], False
    try:
        规 = re.compile(模) if 找名 else re.compile(re.escape(模))
    except re.error:
        规 = re.compile(re.escape(模))

    for 现根, 目们, 件们 in os.walk(起):
        目们[:] = sorted(d for d in 目们 if d not in 跳过目录 and not d.startswith('.'))
        for f in sorted(件们):
            if 满:
                break
            p = os.path.join(现根, f)
            try:
                if os.path.islink(p) and not os.path.realpath(p).startswith(根基):
                    continue
            except OSError:
                continue
            if 找名:
                if 规.search(f):
                    条.append(相对(根, p))
                    if len(条) >= 限:
                        满 = True
                continue
            if os.path.splitext(f)[1].lower() in 二进制尾巴:
                continue
            try:
                with open(p, 'r', encoding='utf-8', errors='replace') as fh:
                    for i, 行 in enumerate(fh, 1):
                        if 规.search(行):
                            条.append('%s:%d: %s' % (相对(根, p), i, _切单行(行.strip())))
                            if len(条) >= 限:
                                满 = True
                                break
            except OSError:
                continue
    if not 条:
        return '没找到 %r。' % 模
    出 = '\n'.join(条)
    脚 = ''
    if 满:
        脚 = '\n…（到上限就停了，可能还有更多匹配）'
    if len(出) > 搜上限字:
        出 = 出[:搜上限字]
        脚 += '\n…（内容太长被截断了）'
    return '找到 %d 处%r：\n%s%s' % (len(条), 模, 出, 脚)


#: 写完 / 改完之后**顺手做语法检查**的后缀。**只做能确定的** ——
#: Python 有 `compile()`，一调用就知道对不对；HTML / JS 没有这种东西，
#: 硬塞一个检查器只会造出假警报。
查语法后缀 = ('.py',)


def 语法提示(径, 文):
    """
    刚落盘的文件如果语法不对，回一段**给模型看的**提示；没问题回空串。

    ⚠ **这是补上现在最大的一个缺口：写出来的文件是坏的，而没人发现。**
    实测撞到过：模型在**段落交界处偶发漏掉换行**，把
    `import time` 和 `def countdown(...)` 粘成一行 —— 写出来的 `.py`
    根本跑不起来（`实验/countdown.py` 就是这么坏的），而系统提示第 7 条
    那句"写完要自己跑一遍验证"是句软话，模型经常不听。

    这里**确定性地**查一遍，把语法错**直接塞进工具结果** —— 它下一轮必然
    看到。零额外模型调用，也不用改提示词。

    ⚠ **为什么不在解析器里"把换行补回来"。** 查过了：同一条提示，流式和非
    流式**逐字相等**、`compile()` 也过 —— 解析链路**一个换行都没丢**。粘连
    是**模型自己吐的**（同一条提示跑两次，一次好一次粘）。在解析层补换行
    只能靠**猜**，猜错就是**静默写一个坏文件** —— 那比"读不到"糟糕一个
    数量级（见 `改文件` 那段注释）。所以这儿**只报告、不改内容**，让模型
    自己重写。
    """
    if not str(径 or '').lower().endswith(查语法后缀):
        return ''
    try:
        compile(文, os.path.basename(径), 'exec')
    except SyntaxError as 错:
        坏行 = (错.text or '').rstrip()[:120]
        return ('\n\n⚠ **这个文件语法不对，现在跑不起来，必须重写。**\n'
                '  第 %s 行：%s\n'
                '  那一行是：%s\n'
                '多半是**段落交界处的换行漏了**（本该分行的地方粘成了一行）。'
                '重新 write_file 一遍完整内容，注意**每个语句各占一行**，'
                '函数之间留空行。'
                % (错.lineno, 错.msg, 坏行 if 坏行 else '（在文件末尾）'))
    except (ValueError, TypeError):
        # 内容里带 NUL、或者压根不是字符串 —— 那不是"语法错"，
        # 别的工具会给出更贴切的报错，这儿不抢话
        return ''
    return ''


def 写文件(根, 参):
    """整个覆盖。**要审批**，调用前上层已经问过用户了。"""
    径 = 定界(根, 参.get('path'))
    文 = 参.get('content')
    if 文 is None:
        raise 工具错('content 是空的。要写整个文件的内容。想新建空文件就写 ""')
    文 = str(文)
    if os.path.isdir(径):
        raise 工具错('%s 是个目录，不能用 write_file 写。' % 相对(根, 径))
    有 = os.path.isfile(径)
    try:
        os.makedirs(os.path.dirname(径), exist_ok=True)
        with open(径, 'w', encoding='utf-8', newline='') as f:
            f.write(文)
    except OSError as 错:
        raise 工具错('写不了 %s：%s' % (相对(根, 径), 错))
    行 = 文.count('\n') + (1 if 文 and not 文.endswith('\n') else 0)
    return ('已%s %s（%d 行，%d 字符）%s'
            % ('覆盖' if 有 else '新建', 相对(根, 径), 行, len(文),
               语法提示(径, 文)))


def 改文件(根, 参):
    """
    把 `old` 换成 `new`。**`old` 必须在文件里精确唯一匹配。**

    ⚠ **默认绝不做模糊匹配。** 忽略空白、相似度阈值那一类，在"差不多就
    对上"的时候会**静默改到错的地方** —— 比"读不到"糟糕一个数量级：
    读不到模型会重试，改错了没有任何人知道。

    匹配到 0 处 / 多处都当**失败**报回去，让模型重新 read_file 拿准原文。
    """
    径 = 定界(根, 参.get('path'))
    旧 = 参.get('old')
    新 = 参.get('new')
    if 旧 is None or 新 is None:
        raise 工具错('old 和 new 都要给。old 是要被替换掉的原文，new 是新的。')
    旧, 新 = str(旧), str(新)
    if 旧 == '':
        raise 工具错('old 是空的。要写出要被替换掉的那段原文（含缩进，逐字一致）。')
    if not os.path.isfile(径):
        raise 工具错('文件不存在：%s\n新建文件用 write_file。' % 相对(根, 径))
    try:
        with open(径, 'r', encoding='utf-8', errors='replace') as f:
            原文 = f.read()
    except OSError as 错:
        raise 工具错('读不了 %s：%s' % (相对(根, 径), 错))

    全部 = str(参.get('all') or '否').strip() in ('是', 'true', 'True', 'yes', 'all')
    处 = 原文.count(旧)
    if 处 == 0:
        # 给模型一点线索：它多半是缩进或空白没对上
        起 = 原文.find(旧.split('\n')[0].strip()[:40]) if 旧.strip() else -1
        提 = ''
        if 起 >= 0:
            提 = ('\n文件里有一行开头跟你的 old 很像（第 %d 行左右），'
                 '多半是缩进或空白对不上。先 read_file 把原文抄准。'
                 % (原文[:起].count('\n') + 1))
        raise 工具错('old 在 %s 里没找到，逐字对不上。%s' % (相对(根, 径), 提))
    if 处 > 1 and not 全部:
        raise 工具错('old 在 %s 里出现了 %d 次，没法确定改哪一处。\n'
                    '要么多给几行上下文把它变得唯一，要么把 all 填 "是" 全改。'
                    % (相对(根, 径), 处))
    换过 = 原文.replace(旧, 新) if 全部 else 原文.replace(旧, 新, 1)
    行号 = 原文[:原文.find(旧)].count('\n') + 1
    try:
        with open(径, 'w', encoding='utf-8', newline='') as f:
            f.write(换过)
    except OSError as 错:
        raise 工具错('写不了 %s：%s' % (相对(根, 径), 错))
    加 = 新.count('\n') + 1
    减 = 旧.count('\n') + 1
    return ('已改 %s（第 %d 行起，%s %d 行，共 %d 处）%s'
            % (相对(根, 径), 行号, '+%d/-%d' % (加, 减), 处 if 全部 else 1,
               处 if 全部 else 1, 语法提示(径, 换过)))


def _大小(字节):
    if 字节 >= 1024 * 1024:
        return '%.1f MB' % (字节 / 1024 / 1024)
    if 字节 >= 1024:
        return '%.0f KB' % (字节 / 1024)
    return '%d B' % 字节


#: 剥 ANSI 转义（颜色码）。`git` / `npm` / `pytest` 带色输出时那些
#: `\x1b[32m` 对模型是纯噪音，而且白占 token。
_ANSI = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07')


def 跑命令(根, 参, 停=None):
    """
    在工作目录里跑一条命令。回 `退出码 / 输出`。

    ⚠ **不用 `subprocess.run(capture_output=True)`** —— 它把输出**全部**
    读进内存，`dir /s C:\\` 或者一次完整构建日志能吃掉几个 G，直接把程序
    打爆。这里自己 Popen + 限量读，超了杀进程。

    几个细节都是有代价的，别随手改：

      `stdin=DEVNULL`  命令一旦交互式提问（`git commit` 忘了 `-m`、裸跑
                       `python` 进 REPL），就会挂到超时为止。**这是
                       "agent 卡死"最常见的来源**，一行就免掉。
      超时要杀进程树   `subprocess.run(timeout=)` 只杀 shell，它下面的
                       子进程在 Windows 上**会活着继续跑**（比如
                       `cmd /c python server.py`）。得用 `taskkill /T`。
      编码级联解码     `text=True` 会用 `locale.getpreferredencoding()`，
                       中文 Windows 上是 cp936，而 Python/git/node 输出
                       的是 UTF-8 → 满屏乱码；反过来 `dir`/`type` 输出
                       GBK，用 UTF-8 解也乱。所以**收 bytes，先 UTF-8
                       严格、失败再 GBK**，两头都兜住。
    """
    命令 = str(参.get('cmd') or '').strip()
    if not 命令:
        raise 工具错('cmd 是空的。要给一条要跑的命令，比如 python main.py')
    拦 = 查黑名单(命令)
    if 拦:
        raise 工具错(拦)
    超 = _数字(参, 'timeout', 30, 命令超时下限, 命令超时上限)

    环 = dict(os.environ)
    环.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1', NO_COLOR='1',
              TERM='dumb', GIT_PAGER='cat')
    try:
        进 = subprocess.Popen(
            命令, shell=True, cwd=根, env=环,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as 错:
        raise 工具错('起不了这条命令：%s' % 错)

    罐 = bytearray()
    够 = threading.Event()

    def 收():
        try:
            while True:
                块 = 进.stdout.read(4096)
                if not 块:
                    break
                if len(罐) < 命令原始上限:
                    罐.extend(块[:命令原始上限 - len(罐)])
                else:
                    够.set()          # 收够了，但**不 break** —— 让管子继续
                                      # 被读空，否则子进程写满管道会卡住
        except (OSError, ValueError):
            pass

    线 = threading.Thread(target=收, daemon=True)
    线.start()
    超时 = False
    try:
        进.wait(timeout=超)
    except subprocess.TimeoutExpired:
        超时 = True
        _杀树(进)
        try:
            进.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    线.join(timeout=3)
    码 = 进.returncode

    原文 = bytes(罐)
    文 = _解码(原文)
    文 = _ANSI.sub('', 文).replace('\r\n', '\n').replace('\r', '\n')
    被切 = 够.is_set() or len(原文) >= 命令原始上限
    出 = _头尾截(文, 命令上限字)

    头 = '退出码 %s%s' % (码, '（超时 %d 秒，已强行结束）' % 超 if 超时 else '')
    脚 = []
    if 被切:
        脚.append('（输出太多，只留了开头和结尾）')
    if 超时:
        脚.append('（命令没有在 %d 秒内结束，已经把它连同子进程一起结束了）' % 超)
    if 码 not in (0, None) and not 文.strip():
        脚.append('（命令没有输出）')
    return '%s\n%s%s' % (头, 出 or '（没有输出）',
                       ('\n' + '\n'.join(脚)) if 脚 else '')


def _杀树(进):
    """连同子进程一起杀。见 `跑命令` 里那条注释。"""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(进.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
        else:
            import signal
            os.killpg(os.getpgid(进.pid), signal.SIGKILL)
    except Exception:
        try:
            进.kill()
        except Exception:
            pass


def _解码(原):
    """UTF-8 严格优先，失败退 GBK。见 `跑命令` 里那条注释。"""
    if not 原:
        return ''
    try:
        return 原.decode('utf-8')
    except UnicodeDecodeError:
        return 原.decode('gbk', errors='replace')


#: 工具名 → 实现。`跑命令` 多一个 `停` 参数（要传给子进程收尾），
#: `执行` 里单独走。
实现们 = {
    'read_file': 读文件,
    'list_dir': 列目录,
    'search': 找文件,
    'write_file': 写文件,
    'edit_file': 改文件,
}


def 执行(名, 根, 参, 停=None):
    """
    跑一个工具，回**给模型看的文本**。工具名不认识 / 参数不对都抛 `工具错`，
    文案也是给模型看的。

    这是这一层唯一的入口 —— 上层（`酒馆助手`）不直接调具体工具。

    ⚠ **工具里蹦出来的任何意外异常都在这儿拦成 `工具错`。** 一个工具实现的
    bug（比如实测撞到的 `count: Infinity` 抛 `OverflowError`）不该把**整个
    任务**从根上打炸 —— 那会让用户看到一堆 Python traceback，而且已经干完的
    几步也白干了。拦成工具错之后，它就跟"路径不对""文件不存在"一样被喂回
    模型，模型多半能自己绕过去。**带 `本地错` 前缀的那句是给用户看的**，
    让它一眼看出"这不是模型的错，是工具自己的锅"。
    """
    名 = (名 or '').strip()
    if not isinstance(参, dict):
        raise 工具错('参数得是一个 JSON 对象，你给的是 %r' % (参,))
    if 名 == 'run_cmd':
        if 停 is not None and 停.is_set():
            raise 工具错('用户喊了停，这条命令不跑了。')
        return 跑命令(根, 参, 停=停)
    if 名 not in 实现们:
        raise 工具错('没有 `%s` 这个工具。能用的只有：%s'
                    % (名, '、'.join(工具名们)))
    try:
        return 实现们[名](根, 参)
    except 工具错:
        raise
    except OverflowError as 错:
        raise 工具错('参数的数字太大了，超出了能表示的范围（%s）。'
                    '改用一个小一点的整数。' % 错)
    except Exception as 错:
        raise 工具错('（工具内部出错，不是你的问题）%s：%s\n'
                    '换个参数或者换个做法再试。'
                    % (type(错).__name__, 错))
