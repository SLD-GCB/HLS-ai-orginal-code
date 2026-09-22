#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆本地.py — 本地 GGUF 引擎（llama-cpp-python）+ 模型文件的下载/管理

**这个文件不 import Qt。** 跟 `酒馆大脑` 一个道理：引擎和下载是最需要
脱离界面单测的两块，绑上 Qt 就没法痛快测了。

两件事：

    引擎      Llama 实例的懒加载 / 换模型释放 / 逐 token 流式
    模型文件  列本地 GGUF、从三个源（modelscope / hf-mirror / huggingface）
              下载、搜别人的仓库、删除

**llama_cpp 是可选依赖。** 没装（或者 CUDA wheel 起不来）的时候这个文件
照样能 import——`可用()` 回 False，所有真碰引擎的入口先问它。界面靠
`不可用原因()` 把"怎么装"写给用户看。

**全局同时只有一个 Llama 实例。** 7B Q4 就要 4-5G 内存/显存，这台机器
装不下两个；而且 Llama 对象**不是线程安全的**，并发生成必须串行。
所以一把 `_锁` 既管"换模型"也管"生成"：生成整条跑完才放锁，换模型
（卸载旧实例）在外面等锁——界面上靠 `在生成()` 把"生成中不给切模型/
不给删模型"做在按钮上，而不是真去等锁把界面卡死。

**`流式块` 不管停旗。** 停旗是 `酒馆大脑` 的东西（`已打断` 定义在那边，
这个文件 import 它就循环了）。这里只负责一块一块吐字，查旗在
`酒馆大脑.流式` 的 local 分支里做——跟 HTTP 那条路"取块循环每 0.1 秒
看一次旗"是同一个形状。

⚠ **prefill（吃下整段提示词）期间查不了旗。** llama.cpp 的 Python 绑定
没法在 prompt 处理中途取消，提示词越长首字越慢、打断越钝。这是绑定的
硬约束，不是设计选择——`默认上下文` 别开太大就是为这个。

**下载的进度不在这一层报。** 两个源的下载都没有干净的应用层进度回调
（modelscope 的 `snapshot_download` 自己往终端画 tqdm；hf 那条我们干脆
自己下，见 `_hf直下`）。这一层只管"下完把 .gguf 挪进模型目录"；进度由
界面那边起个定时器去数**这一次下载专用的那个目录**的字节——见
`暂存目录`，它按 `(源, 仓库, 模式)` 三级分开。版本无关，断点续传时起始
进度还自动是对的，也不会把别的源、别的量化的残留算进来。

⚠ **半成品目录必须剪掉，否则会挪出一个坏模型。** `_挪进模型目录` 里
`os.walk` 一定要跳过 `._____temp` 和 `.cache`：那两个目录里躺着**没下完
的文件，而它们照样叫 `xxx.gguf`**。不剪的话，上次中断留下的半截文件会被
当成"下完了"挪进模型目录，得到一个加载不了的模型，而且大小看着还挺大，
最会骗人。两个源的半成品位置不一样：

    modelscope    <cache_dir>/._____temp/<owner>/<name>   ← 换了 local_dir 才在树下
    hf 系         <暂存>/<文件名>.part                     ← 我们自己的，见 `_hf直下`

⚠ **`hf_hub_download` 用不了 hf-mirror。** 镜像返的是弱 ETag（`W/"…"`），
而 huggingface_hub 1.x 只认强 ETag，抛 `FileMetadataError` 再被包成
`LocalEntryNotFoundError`——报出来的话是"找不到文件"，实际网络是通的
（纯 requests 一把就下下来了）。实测确认过，`endpoint=` 参数和
`HF_ENDPOINT` 环境变量两条路都救不了。所以 hf 那条路是 `_hf直下` 自己
拿 requests 下的；`huggingface_hub` 只剩 `HfApi` 的列清单 / 搜索还在用。

⚠ **`HF_HUB_DISABLE_XET` 必须在任何 `import huggingface_hub` 之前设。**
hf 的 Xet 传输把块缓存写到 `~/.cache/huggingface/xet/`——在我们的暂存
目录**外面**，界面那个"数字节算进度"会永远是 0。而这个开关是
`huggingface_hub.constants` 在 **import 时**读的，import 之后再设 `os.environ`
一点用都没有。本文件对 hf 只在函数里懒 import，所以下面模块顶层这一句
一定是最早的一次。
"""

import gc
import os
import threading

import requests

os.environ.setdefault('HF_HUB_DISABLE_XET', '1')      # 见文件头，必须早于 hf 的 import

try:
    from llama_cpp import Llama
    _装上了 = True
    _装不上原因 = ''
except Exception as 错:                      # 没装 / DLL 缺 / CUDA 起不来
    Llama = None
    _装上了 = False
    _装不上原因 = '%s：%s' % (type(错).__name__, 错)

__all__ = ['可用', '不可用原因', '本地错', '模型目录', '列本地', '定位',
           '删本地', '在生成', '卸载全部', '流式块',
           '列远程文件', '下载', '推荐模型', '搜模型', '暂存目录',
           '下载源', '源们', '默认源', '源上有', '源名', '首选源', '精选脚注',
           '跑一轮', '解工具调用', '本地停', '规整工具名', '有工具模板',
           '开始任务', '结束任务', '默认上下文']


class 本地错(Exception):
    """本地引擎这一层出的所有错。文案写给人看，界面直接显示。"""


def 可用():
    """llama_cpp 装上了没。没装/起不来时所有碰引擎的入口都该先问这个。"""
    return _装上了


def 不可用原因():
    """装不上的原因（给人看）。装上了就是空串。"""
    return _装不上原因


# ── 模型文件 ────────────────────────────────────────────────────────

def 模型目录():
    """
    `酒馆数据/模型/`。**用的时候立，不在 import 时立**——import 不该有
    副作用，而且 `酒馆存储` 的数据目录要等程序定位完才知道在哪。
    """
    import 酒馆存储                       # 函数内 import，避免模块级环
    目 = os.path.join(酒馆存储.默认数据目录(), '模型')
    os.makedirs(目, exist_ok=True)
    return 目


def 定位(名):
    """
    配置里「模型」那格 → 真实路径。

    绝对路径原样用（用户自己放在别处的 GGUF）；否则按模型目录里的
    文件名拼——配置里存的通常只是文件名，数据目录整个搬了家也认得。
    """
    名 = (名 or '').strip()
    if os.path.isabs(名):
        return 名
    return os.path.join(模型目录(), 名)


def 列本地():
    """模型目录下的 GGUF 清单：`[(文件名, 字节数), …]`，按名字排。"""
    try:
        名们 = [f for f in os.listdir(模型目录()) if f.lower().endswith('.gguf')]
    except OSError:
        return []
    return sorted(((f, os.path.getsize(os.path.join(模型目录(), f)))
                   for f in 名们), key=lambda 项: 项[0].lower())


def 删本地(名):
    """
    删一个已下载的 GGUF。**只许删模型目录里的。**

    `os.path.basename` 是守口：配置里的「模型」格是用户能手打的，不挡
    的话塞个绝对路径进来就把别处的文件删了。
    """
    径 = os.path.join(模型目录(), os.path.basename(名))
    if os.path.isfile(径):
        os.remove(径)
        return True
    return False


# ── 引擎 ────────────────────────────────────────────────────────────

锁 = threading.RLock()
_实例 = None
_键 = None            # (路径, n_ctx, n_gpu_layers)：换任何一个都重建
_在生成 = False
_任务中 = False       # 一次「任务」期间为真，见 `开始任务`

#: 上下文窗口默认开多大。别开太大：KV 缓存跟着涨，而且 prefill 期间打断
#: 不生效（见文件头），窗口越大"点了停止没反应"的时间越长。R1 这类推理
#: 模型思考链很吃上下文，界面上建议给 8192。
默认上下文 = 4096


def 在生成():
    """
    这会儿引擎忙不忙。界面拿它禁用切模型/删模型/下载。

    ⚠ **`_任务中` 那一半是给编程助手用的。** agent 跑一个任务会连着发起
    十几次模型调用，**两次调用之间 `_在生成` 是放开的**——不挡的话用户能
    正好在这中间切模型，`_取实例` 当场把 Llama 重建一遍（7B 重载几十秒），
    任务跑在半途换了底座，报出来的错还完全看不出真因。
    `开始任务()` / `结束任务()` 把整段盖住，现有那几个闸门不用改一处。
    聊天链路从不设 `_任务中`，它的行为**一个字节都没变**。
    """
    return _在生成 or _任务中


def 开始任务():
    """一次多轮任务开始。见 `在生成` 那条为什么需要它。"""
    global _任务中
    _任务中 = True


def 结束任务():
    """任务收场。**必须和 `开始任务` 配对，放 finally 里。**"""
    global _任务中
    _任务中 = False


def _取实例(路径, 参数):
    """
    拿到该用的 Llama 实例。键（路径/上下文/GPU 层数）不变就复用，
    变了**先放掉旧的再建新的**——先断引用再 `gc.collect()`，mmap 占的
    内存才放得掉，不然两棵模型同时在内存，7B 的机器当场爆满。
    """
    global _实例, _键
    if not _装上了:
        raise 本地错('本地引擎没装上（%s）。\n'
                    'CPU 版：pip install llama-cpp-python\n'
                    '装完重启程序。' % (_装不上原因 or '没装 llama-cpp-python'))
    n_ctx = int(参数.pop('n_ctx', 0) or 0) or 默认上下文
    n_gpu = int(参数.pop('n_gpu_layers', 0) or 0)
    if n_gpu == -1:
        n_gpu = 9999                    # llama.cpp：层数给超 = 能上的全上 GPU
    键 = (路径, n_ctx, n_gpu)
    if _实例 is not None and _键 != 键:
        _实例 = None
        gc.collect()
    if _实例 is None:
        if not os.path.isfile(路径):
            raise 本地错('模型文件不在：%s\n去「本地模型」里下载，或者检查'
                        '「模型」那格填的文件名。' % 路径)
        try:
            _实例 = Llama(model_path=路径, n_ctx=n_ctx,
                          n_gpu_layers=n_gpu, verbose=False)
        except Exception as 错:
            raise 本地错('模型加载失败：%s：%s\n（文件没下完整、内存/显存不够、'
                        '这个 GGUF 太新当前 llama.cpp 不认、或者这个引擎构建'
                        '跟你机器的 CPU 指令集不兼容——0xc000001d 就是后者，'
                        '回 CPU 版能跑）'
                        % (type(错).__name__, 错))
        _键 = 键
    return _实例


def 流式块(系统, 消息们, 参数, 路径):
    """
    本地生成，**逐块吐字符串**。是个生成器。

    ⚠ **整条生成都握着 `锁`**——Llama 不是线程安全的，这期间谁要换
    模型都得等。`_在生成` 是给界面的提示位，让它在"生成中"把切换/删除
    按钮按掉，而不是真去等锁把界面卡死。

    采样参数**原键名直通**：temperature / top_p / top_k / max_tokens /
    repeat_penalty，`create_chat_completion` 都认。`n_ctx` / `n_gpu_layers`
    在 `_取实例` 里 pop 掉了，不会漏进生成调用。
    """
    global _在生成
    参数 = dict(参数)                    # _取实例 要 pop，别动调用方那份
    with 锁:
        实例 = _取实例(路径, 参数)
        _在生成 = True
        try:
            消息 = ([{'role': 'system', 'content': 系统}] if 系统 else []) \
                  + list(消息们)
            # ⚠ **铺完再盖，不是 `create_chat_completion(messages=…, stream=True, **参数)`。**
            # 后一种写法在 `参数` 里已经有 `messages` / `stream` 时会抛
            # `TypeError: got multiple values for keyword argument`。这两条路
            # （`跑一轮` 和这儿）是同一个坑，一起按同一种写法处理。
            体 = dict(参数)
            体['messages'] = 消息
            体['stream'] = True
            流 = 实例.create_chat_completion(**体)
            for 帧 in 流:
                try:
                    段 = 帧['choices'][0]['delta'].get('content')
                except (KeyError, IndexError, TypeError, AttributeError):
                    段 = None
                if 段:
                    yield 段
        finally:
            _在生成 = False


def 有工具模板(路径):
    """
    这个模型的聊天模板里到底认不认 `tools`。认就给 `tools=`，不认就给文本协议。

    ⚠ **判据是模板里有没有 `tools` 这个变量，不是 `tool_call`。** 这两个
    容易搞混，实测踩过：

        qwen2.5-3b      有 `{%- if tools %}` 那段，把工具定义渲染进提示词   → 认
        R1-Distill-Qwen 模板里**没有** `tools`，但**有** `tool_call`
                        （它只是渲染 role='tool' 的历史消息，不渲染工具定义）→ 不认

    按 `tool_call` 判的话 R1 会被判成"认"，于是我们传了 `tools=` 但模板
    根本没把它渲染进去 —— 模型压根不知道有哪些工具，表现是**完全无视工具、
    自顾自地思考**（实测就是这样）。按 `tools` 判才对得上。

    ⚠ **这只是启发式。** 所以 `酒馆助手` 留了「工具协议」开关（自动/原生/文本），
    判错的场合用户能绕过。

    读的是 `实例.metadata`，加载时就拿到了；模型没加载就先加载一次。
    """
    try:
        实例 = _取实例(路径, {})
    except 本地错:
        return False
    try:
        模板 = (实例.metadata or {}).get('tokenizer.chat_template') or ''
    except Exception:
        return False
    return 'tools' in 模板


# ── 工具调用解析 ────────────────────────────────────────────────────
#
# ⚠ **这一段必须自己写。** 实测 `llama-cpp-python` 0.3.35 不会把模型吐出来的
# 工具调用解析回 `tool_calls` 字段 —— 连 `tool_choice='required'` 都不解析，
# 原始文本原样躺在 `content` 里、`tool_calls` 是 None。模型那边是对的（它
# 按模板教的格式吐了），坏的只是绑定这一层。
#
# 顺带一个好处：既然无论如何都要自己解析 `content`，那就没有理由不用
# `stream=True` —— 而流式是**唯一能在解码阶段提供打断点**的方式。

def _扫平衡(文, 起):
    """
    从 `文[起]` 的 `{` 开始，找与它配对的那个 `}` 的下标（含）。找不到回 -1。

    ⚠ **不能用「第一个 `{` 到最后一个 `}`」那种切法。** 工具参数里经常
    整个塞着一份代码（`write_file` 的 `content` 就是），里面 `{` `}` 满地
    都是，还有字符串里的引号和转义。所以这里是一个**字符串/转义感知**的
    深度计数器：

        "…"  和 '…' 里的括号不算数（要处理 \\ 转义）
        裸括号才计数

    小模型的 JSON 本来就不严谨（单引号、没转义），所以这里只求**切得准**，
    切出来能不能 parse 交给下一次判断。
    """
    深 = 0
    i = 起
    串 = None           # 当前在哪个引号里：'"' / "'" / None
    while i < len(文):
        c = 文[i]
        if 串:
            if c == '\\':
                i += 2
                continue
            if c == 串:
                串 = None
        elif c in '"\'':
            串 = c
        elif c == '{':
            深 += 1
        elif c == '}':
            深 -= 1
            if 深 == 0:
                return i
        i += 1
    return -1


def _去双括号(文):
    """
    `{{"name": …}}` → `{"name": …}`。**只在字符串字面量之外动。**

    Qwen 系的模板教的是 `<tool_call>{{…}}</tool_call>`（双大括号），实测
    qwen2.5-3b 就是照这个吐的。

    ⚠ **绝不能无脑 `文.replace('{{', '{')`。** 工具参数里整个塞着一份代码是
    常态（`write_file` 的 `content`），代码里出现 `{{`（格式化字符串、
    嵌套字面量、字典推导）太正常了。无脑替换会**静默改坏要写进文件的
    内容** —— 不报错、写进去的是错的，是最坏的一类 bug。
    """
    出 = []
    i = 0
    串 = None
    while i < len(文):
        c = 文[i]
        if 串:
            if c == '\\' and i + 1 < len(文):
                出.append(文[i:i + 2]); i += 2; continue
            if c == 串:
                串 = None
            出.append(c); i += 1; continue
        if c in '"\'':
            串 = c; 出.append(c); i += 1; continue
        # 字符串外才动双括号
        if c == '{' and 文[i:i + 2] == '{{':
            出.append('{'); i += 2; continue
        if c == '}' and 文[i:i + 2] == '}}':
            出.append('}'); i += 2; continue
        出.append(c); i += 1
    return ''.join(出)


def _救换行(文):
    """
    把 JSON 字符串字面量里**没转义的真换行**换成转义写法，再交给 `json.loads`。

    ⚠ **这不是锦上添花，是小模型写多行代码时的常态。** 实测：让它写一个
    三十行的 `todo.py`，它把 `content` 的值直接写成一串**真的换行符**：

        {"name":"write_file","arguments":{"path":"todo.py",
         "content":"from todo import add   ← 这儿是真的 0x0A，不是 \\n
           ..."}}

    JSON 规范里字符串里不许有裸控制字符，`json.loads` 当场报
    `Invalid control character`。而内容是**完全正确**的代码 —— 只是没转义。
    不救的话每次让它写多行文件都会失败，或者更糟：被别的候选解析出半截内容，
    **静默写出一个坏文件**（实测撞到过，写出来的文件头几行粘成了一行）。

    只动字符串**里面**的控制字符；结构部分的空白照旧（那是合法的，不用管）。
    """
    出 = []
    i = 0
    串 = None
    while i < len(文):
        c = 文[i]
        if 串:
            if c == '\\' and i + 1 < len(文):
                出.append(文[i:i + 2]); i += 2; continue
            if c == 串:
                串 = None
                出.append(c); i += 1; continue
            if c == '\n':
                出.append('\\n')
            elif c == '\r':
                出.append('\\r')
            elif c == '\t':
                出.append('\\t')
            else:
                出.append(c)
            i += 1
            continue
        if c in '"\'':
            串 = c
        出.append(c); i += 1
    return ''.join(出)


def _补括号(文):
    """
    把没闭合的 `{` `[` 补上。**字符串/转义感知**，跟 `_扫平衡` 同一套逻辑。

    ⚠ **这不是锦上添花，是必需的。** 小模型写工具调用时**漏掉最后一个右
    大括号是常态**，实测就撞上了：

        {{"name": "edit_file", "arguments": { ... "all": "否"}}
          ↑ 开了 3 个 {，只闭了 2 个 —— 少一个

    这种 JSON 用任何标准解析器都过不了，但内容是**完全正确**的，只是少个
    收尾。不补的话每一轮都会判"格式不合法"然后中止任务，而模型其实做对了。

    只补不删：多出来的右括号不归这里管（那是模型真的写错了，应该报错）。
    """
    栈 = []
    i = 0
    串 = None
    while i < len(文):
        c = 文[i]
        if 串:
            if c == '\\':
                i += 2
                continue
            if c == 串:
                串 = None
        elif c in '"\'':
            串 = c
        elif c in '{[':
            栈.append(c)
        elif c in '}]':
            if 栈:
                栈.pop()
        i += 1
    if not 栈:
        return 文
    补 = ''.join('}' if x == '{' else ']' for x in reversed(栈))
    return 文 + 补


def _取调用(块):
    """
    从一段 `<tool_call>` 的内脏里抠出 `(名字, 参数字典)`。抠不出回 `None`。

    候选按"干净程度"降序试，全试不过才放弃：

        ① 切出平衡的 JSON 区间（`_扫平衡`）—— 最干净，能甩掉后面跟着的废话
        ② 整段原样
        ③ 上面前两个各去一次双大括号（Qwen 教的是 `{{…}}`，实测它就是照这个吐）
        ④ 再各补一次缺失的收尾括号（小模型漏最后一个 `}` 是常态）
        ⑤ 再各救一次没转义的真换行（写多行代码时的常态，见 `_救换行`）

    每一轮都在上一轮的候选上继续加工，所以最后那一批是"去了双括号 + 补了
    括号 + 救了换行"的全套处理 —— 那是小模型写多行文件时最常见的样子。

    ⚠ 切区间用的是 `_扫平衡`，**不是「第一个 `{` 到最后一个 `}`」** ——
    参数里塞着一整份代码时大括号满地都是，那种切法必错。
    """
    import json
    候 = [块.strip()]
    头 = 块.find('{')
    if 头 >= 0:
        尾 = _扫平衡(块, 头)
        if 尾 > 头:
            候.insert(0, 块[头:尾 + 1])
    for 步 in (_去双括号, _补括号, _救换行):
        候 += [步(t) for t in list(候)]
    for 试文 in 候:
        try:
            载 = json.loads(试文.strip())
        except ValueError:
            continue
        if not isinstance(载, dict):
            continue
        名 = 载.get('name') or 载.get('tool') or 载.get('function')
        参 = 载.get('arguments')
        if 参 is None:
            参 = 载.get('parameters') or 载.get('args') or {}
        if isinstance(参, str):
            try:
                参 = json.loads(_救换行(_补括号(_去双括号(参))))
            except ValueError:
                参 = {'_原样': 参}          # 留着给模型看它自己写了什么
        if 名:
            return str(名), (参 if isinstance(参, dict) else {'_原样': 参})
    return None


#: 模型爱用的别名 → 我们的规范名。归一不到就当一个不认识的工具报错，
#: **不是崩** —— 把"没有这个工具"喂回模型，它多半会换个名字重试。
工具别名 = {
    'read': 'read_file', 'readfile': 'read_file', 'cat': 'read_file',
    'ls': 'list_dir', 'list': 'list_dir', 'listdir': 'list_dir', 'dir': 'list_dir',
    'grep': 'search', 'find': 'search', 'search_file': 'search',
    'write': 'write_file', 'writefile': 'write_file', 'create_file': 'write_file',
    'edit': 'edit_file', 'replace': 'edit_file', 'patch': 'edit_file',
    'run': 'run_cmd', 'bash': 'run_cmd', 'shell': 'run_cmd',
    'cmd': 'run_cmd', 'execute': 'run_cmd', 'terminal': 'run_cmd',
}


def 规整工具名(名):
    """模型给的工具名 → 我们的规范名。认不出来就原样回（让上层报"没这个工具"）。"""
    净 = str(名 or '').strip().strip('`').strip()
    小 = 净.lower().replace('-', '_').replace(' ', '_')
    if 小 in 工具别名:
        return 工具别名[小]
    return 小 or 净


def 解工具调用(文, 原生=None, 给过工具=False, 认得的=()):
    """
    从模型输出里抠出工具调用。回 `(调用们, 剩下的正文, 解析失败吗)`。

    `调用们` 每项是 `{'名': str, '参数': dict, '原始': str}`。

    分级，从强到弱：

      ① **原生字段**（`原生` 参数）：某些 chat_format 会回填 `tool_calls`。
         这条路是**将来升级绑定之后白赚的**，现在实测拿不到。
      ② **抠 `<tool_call>…</tool_call>`**（大小写不敏感，容忍缺闭合）。
      ③ **markdown 围栏里的 JSON** —— 见下面那条注释，要过一道名字校验。
      ④ 整段就是个 JSON 对象且有 `name` 字段 —— **只在这次确实给模型看过
         工具定义时才走**（`给过工具`）。不给这个条件的话，模型正常回答里
         写一段带 `name` 键的示例 JSON 就会被当成工具调用。

    ⚠ **解析失败必须能被上层看出来**（第三个返回值）。把"模型写了
    `<tool_call>` 但格式崩了"静默当成最终回答的话，用户看到的是模型
    答非所问，完全不知道是格式问题。
    """
    import re
    文 = 文 or ''
    出 = []

    # ① 原生字段
    for 条 in (原生 or []):
        try:
            函 = 条.get('function') or {}
            名 = 函.get('name') or 条.get('name')
            参 = 函.get('arguments')
            if 参 is None:
                参 = 条.get('arguments') or {}
            if isinstance(参, str):
                try:
                    import json
                    参 = json.loads(参)
                except ValueError:
                    参 = {'_原样': 参}
            if 名:
                出.append({'名': 规整工具名(名), '参数': 参 or {}, '原始': ''})
        except (AttributeError, TypeError):
            continue

    def 收(块, 原始):
        取 = _取调用(块)
        if not 取:
            return False
        名 = 规整工具名(取[0])
        if 认得的 and 名 not in 认得的:
            return False                 # 见 ③ 那条，名字对不上就不要
        出.append({'名': 名, '参数': 取[1], '原始': 原始})
        return True

    # ② `<tool_call>` 块
    #
    # ⚠ **① 已经收到东西了就别再抠文本。** 原生模式下工具调用走 `tool_calls`
    # 通道，可正文里**很可能又写了一遍** —— `酒馆助手.系统提示` 教的写法就是
    # `<tool_call>{…}</tool_call>`，原生和文本两边都给是模型的常态。不拦的话
    # 同一个调用会被收两遍、执行两遍：`write_file` 看不出来，`run_cmd` 是真会
    # 把命令跑两次。
    捞过 = 0
    块们 = []
    if not 出:
        块们 = re.findall(r'<tool_call>\s*(.*?)\s*</tool_call>', 文,
                         flags=re.S | re.I)
        if not 块们:
            # 缺闭合（生成被截断）也要捞 —— 从最后一个开标签到末尾
            位 = 文.lower().rfind('<tool_call>')
            if 位 >= 0:
                块们 = [文[位 + len('<tool_call>'):]]
    for 块 in 块们:
        块 = 块.strip()
        if 块.startswith('```'):
            块 = re.sub(r'^```[a-zA-Z_]*\s*', '', 块)
            块 = re.sub(r'\s*```$', '', 块).strip()
        if 收(块, 块):
            捞过 += 1

    # ③ markdown 围栏里的 JSON
    #
    # ⚠ 有模型**会用围栏而不是 `<tool_call>` 标签**。实测 R1-Distill-Qwen-7B
    # 在文本协议下就是这么吐的：
    #
    #     好的，我来帮你列出当前目录下的所有文件。
    #     ```json
    #     {"name": "list_dir", "arguments": {"path": ".", "depth": 1}}
    #     ```
    #
    # 内容完全正确，只是包装形式不对。早先只认 ```` ```tool_call ````，
    # 这种全被判成"没调工具"，任务当场结束。
    #
    # ⚠ **但不能见围栏就收。** 模型在正文里展示一段带 `name` 键的示例 JSON
    # 太常见了，收了就是"它只是举个例子，我们却真去改文件"。
    # 所以加一道硬门槛：**解析出来的工具名必须在下发过的工具清单里**
    # （`认得的`）。示例 JSON 里的名字几乎不可能正好撞上我们的工具名。
    if not 出 and 认得的:
        for 块 in re.findall(r'```[a-zA-Z_]*\s*(.*?)\s*```', 文, flags=re.S):
            块 = 块.strip()
            if 收(块, 块):
                捞过 += 1

    # ③ 裸 JSON 兜底（只在这次确实给模型看过工具定义时才敢用）
    if not 出 and 给过工具:
        净 = 文.strip()
        if 净.startswith('{') and 净.endswith('}'):
            取 = _取调用(净)
            if 取:
                出.append({'名': 规整工具名(取[0]), '参数': 取[1], '原始': 净})

    # 剩下的正文：把工具调用那几段挖掉，其余的留给用户看
    残 = re.sub(r'<tool_call>\s*.*?\s*</tool_call>', '', 文, flags=re.S | re.I)
    开 = 残.lower().find('<tool_call>')          # 缺闭合的尾巴也要挖掉
    if 开 >= 0:
        残 = 残[:开]
    if 捞过:
        # 围栏里的内容已经当成工具调用收走了，别在正文里再露一遍
        残 = re.sub(r'```[a-zA-Z_]*\s*.*?\s*```', '', 残, flags=re.S)
    残 = 残.strip()

    失败 = bool(出 == [] and ('<tool_call' in 文.lower()))
    return 出, 残, 失败


class 本地停(Exception):
    """
    用户喊了停，`跑一轮` 从流里退出来。

    **正常收场，不是故障** —— 跟 `酒馆大脑.已打断` 一个意思。分开定义只是
    为了不让引擎这一层 import 上层。
    """


def 跑一轮(系统, 消息们, 参数, 路径, 工具=None, 要工具='auto',
           停=None, 段回=None, 最大输出=1024):
    """
    本地生成，**一次拿完整条**。给编程助手用（`流式块` 是聊天用的，两者分开）。

    ⚠ **对外是"一次拿完"，内部是"流式累加"。** 为什么不真用 `stream=False`：
    那在 llama.cpp 里是一口气 decode 完再返回，这期间既没有解码期的打断点，
    也没有首字回调——用户在整个几十秒里只看到转圈。而**prefill 期间两者
    都掐不断**（绑定的硬约束，见文件头），所以非流式在这一点上没有任何
    优势，白白丢掉可打断和首字可见。

    回 `{'正文', '调用们', '原生', '提示词token', '停过', '解析失败', '停因',
    '思维'}`。后两个跟 `酒馆大脑.跑一轮` 对齐（那边是思维链**字数**）——
    本地这条路上思维链不单独给，所以恒为 0，但**键必须在**，不然
    `酒馆助手._空话` 读它会拿不到。

    `提示词token` 是**这次提示词的 token 数**（不是 dict）—— 上下文仪表靠它，
    取法和为什么这么取见下面那段注释。拿不到就是 0。

    `段回` 给了的话，每收到一块就调一次 `段回(段)` —— 界面拿它做"模型在写"
    的实时感。`停` 是 `酒馆大脑.停旗` 那样带 `is_set()` 的东西，每收一块
    查一次，置了旗就抛 `本地停`。

    ⚠ **`流式块` 一个字都没动。** 那 6 行样板（`dict(参数)` → `with 锁` →
    `_取实例` → `_在生成=True` → `finally`）这儿重写了一遍，换的是聊天
    链路零风险——它天天在跑，不值得为省几行去动它。
    """
    global _在生成
    参数 = dict(参数)                    # _取实例 要 pop，别动调用方那份
    # ⚠ **别在这儿 pop `n_ctx` / `n_gpu_layers`** —— `_取实例` 正是从
    # `参数` 里读这两个来决定"开多大上下文、多少层上显卡"。提前吃掉的话
    # 它会以为没配，回头按默认值（纯 CPU、4096）建实例，而且一声不响。
    with 锁:
        实例 = _取实例(路径, 参数)
        _在生成 = True
        try:
            消息 = ([{'role': 'system', 'content': 系统}] if 系统 else []) \
                  + list(消息们)
            # ⚠ **先铺 `参数`，这几个显式的键最后盖上去 —— 不是 `dict(键=值, **参数)`。**
            #
            # 后一种写法在 `参数` 里已经有同名键时会直接抛
            # `TypeError: dict() got multiple values for keyword argument 'max_tokens'`。
            # 而 `参数` 就是从接口配置的「采样参数」JSON 来的 —— 那里面
            # **本来就有 `max_tokens`**（接口页上那个格子），所以这条路必炸。
            # 用户手写 `messages` / `stream` 也会一样炸。
            #
            # 铺完再盖：显式的永远赢，而且要哪个键都不会撞。
            体 = dict(参数)
            体['messages'] = 消息
            体['stream'] = True
            try:
                体['max_tokens'] = int(最大输出 or 体.get('max_tokens') or 1024)
            except (TypeError, ValueError):
                体['max_tokens'] = 1024      # 配置里写坏了，退回默认，别崩
            # `要工具`：'auto' 交给 `有工具模板` 判；'从不' 就纯聊天
            if 工具 and 要工具 != '从不':
                体['tools'] = 工具
                体['tool_choice'] = 'auto'
            流 = 实例.create_chat_completion(**体)
            罐, 提示, 末帧, 停过 = [], 0, None, False
            for 帧 in 流:
                if 停 is not None and 停.is_set():
                    停过 = True
                    raise 本地停()
                末帧 = 帧
                # ⚠ **流式拿不到 `usage`**（那只在非流式的返回里）。但它有
                # 更好的东西：`n_tokens` 就是 KV 缓存当前的占用位置。第一帧
                # 到达时提示词已经 prefill 完、才生成了 1 个 token，所以
                # **此刻的 `n_tokens - 1` 就是这次提示词的 token 数**。
                # 上下文仪表靠它——不用自己估，是精确值。
                if 提示 == 0:
                    try:
                        提示 = max(1, int(实例.n_tokens) - 1)
                    except Exception:
                        提示 = 0
                try:
                    段 = 帧['choices'][0]['delta'].get('content')
                except (KeyError, IndexError, TypeError, AttributeError):
                    段 = None
                if 段:
                    罐.append(段)
                    if 段回:
                        段回(段)
        except 本地停:
            raise
        finally:
            _在生成 = False

    完整 = ''.join(罐)
    原生, 停因 = None, ''
    if isinstance(末帧, dict):
        try:
            原生 = 末帧['choices'][0]['message'].get('tool_calls') or None
        except (KeyError, IndexError, TypeError, AttributeError):
            原生 = None
        # ⚠ 本地的 `finish_reason` 是 `stop` / `length` —— 后者同样意味着
        # "被输出上限截断"（`max_tokens` 那一格）。**跟云端那条要给出一致的
        # 解释**，不然同一个病在两条路上说成两种话，`_空话` 就白写了。
        try:
            停因 = str(末帧['choices'][0].get('finish_reason') or '')
        except (KeyError, IndexError, TypeError, AttributeError):
            停因 = ''
    给了工具 = bool(工具 and 要工具 != '从不')
    # 下发过的工具名 —— 解析器拿它给 markdown 围栏那道兜底当门槛，见 `解工具调用`
    认得 = set()
    for d in (工具 or []):
        try:
            认得.add(规整工具名(d['function']['name']))
        except (KeyError, TypeError):
            continue
    调用们, 残, 失败 = 解工具调用(完整, 原生=原生, 给过工具=给了工具,
                                  认得的=认得)
    return {'正文': 残, '调用们': 调用们, '原生': 完整,
            '提示词token': 提示, '停过': 停过, '解析失败': 失败,
            '停因': 停因, '思维': 0}


def 卸载全部():
    """
    把实例放掉。**关窗时叫**——在生成线停干净之后（`酒馆窗口` 关窗流程），
    别在生成中途叫：锁会等到生成跑完才放，主线程就这么卡死了。
    """
    global _实例, _键
    with 锁:
        _实例 = None
        _键 = None
        gc.collect()


# ── 下载（三个源：modelscope / hf-mirror / huggingface）─────────────

#: 三个下载源。`标` 进下拉和日志，`系` 决定走哪套 SDK（`ms` = modelscope，
#: `hf` = huggingface_hub），`端` 一律**不带结尾斜杠**——hf 那边自己会
#: `rstrip('/')`，modelscope 那边是 f-string 直拼，多一个斜杠就出双斜杠路径。
#:
#: ⚠ **源是独立维度，不是仓库名的一部分。** 同一个仓库名
#: （`unsloth/DeepSeek-R1-Distill-Qwen-7B-GGUF`）在三个源上都有，只是
#: **覆盖不一样**：ModelScope 只镜像了 unsloth / bartowski 系，mradermacher、
#: HauhauCS 那些社区量化仓库它**没有**（实测）。所以 `列远程文件`/`下载`
#: 都要单独收一个源参数，而不是把源塞进仓库名里。
下载源 = (
    {'标': 'modelscope', '名': 'ModelScope（国内直连，最稳）',
     '端': 'https://www.modelscope.cn', '系': 'ms'},
    {'标': 'hf-mirror', '名': 'HF 镜像 hf-mirror.com（国内直连）',
     '端': 'https://hf-mirror.com', '系': 'hf'},
    {'标': 'huggingface', '名': 'HuggingFace 官方（要梯子）',
     '端': 'https://huggingface.co', '系': 'hf'},
)
源们 = {d['标']: d for d in 下载源}
默认源 = 'modelscope'


def _hf令牌(源标):
    """
    hf 两源的令牌策略。

    ⚠ **hf-mirror 强制 `token=False`。** 它是第三方镜像，把账号令牌发给它
    等于把家钥匙交给中介。`token=False` 在 huggingface_hub 里的含义是
    "明确一个令牌都不发"，正是我们要的。

    官方源给 `None`＝走默认（按 `HF_TOKEN` 环境变量 → `huggingface-cli login`
    的缓存这个顺序读），这样门禁仓库、私有仓库的用户登录后能下。

    **绝不要为了图省事让镜像源"走默认"** —— 那样本机但凡登录过 HF，
    令牌就跟着每个请求发出去了。
    """
    return False if 源标 == 'hf-mirror' else None


def _查源(源标):
    """源标识 → 源字典。不认识的当场报，别让它一路漂到网络层。"""
    源 = 源们.get(源标 or 默认源)
    if 源 is None:
        raise 本地错('没有这个下载源：%r（认得的只有 %s）'
                    % (源标, '、'.join(源们)))
    return 源


def 源名(源标):
    """源标识 → 给人看的名字。界面文案和错误提示都要用，别各写一份。"""
    return _查源(源标)['名']


#: 内置精选档位。分五组：
#:
#:   DeepSeek 系          官方 R1 蒸馏全系 + 去审查版（**全族模板都不带工具**，见下）
#:   千问·全系（1.5B–9B）  唯一"全系带原生工具调用"的一族 —— 做 agent 靠它
#:   国外·通用            Gemma-4 / Mistral / Phi / Llama / GLM-4 / LFM2.5
#:   国外·角色扮演        专为 RP 微调的（ArliAI RPMax 系、Stheno、Peach…）
#:   国外·去审查          abliterated / uncensored，通用创作向
#:
#: `模式` 是要下的那个量化文件名——两个源都按"精确文件名"匹配，只下这一个，
#: 整个仓库十几个 G 不会全拖下来。文件名和 `约字节` 都以 `列远程文件` 的
#: 实际结果核对过（2026-09）。
#:
#: ⚠ **同名底座的不同量化别重复列。** 比如 `gemma-4-E4B-it` 官方
#: （`ggml-org`）只发到 `Q4_0`（老式量化，同尺寸下明显差于 `Q4_K_M`），
#: 所以这里收的是 unsloth 那份 `Q4_K_M`。要官方版用「搜索」找得到。
#:
#: `有ms` 是**唯一的事实来源**：ModelScope 上有没有镜像。2026-09 逐条用
#: `get_model_files` 实测过——没有的只有 mradermacher 全系和 HauhauCS 那条
#: （社区做去审查的量化仓库），unsloth / bartowski / TheBloke 都有。
#: 档位不另存"该从哪个源下"这个字段，由它推出来（`首选源`），免得两处对不上。
#:
#: ⚠ **注意这不等于"只有那一个源"。** 三个源里 hf-mirror 和官方 HF 是同一个
#: 仓库群的两条路（镜像关系），所以**每条在 HF 系两个源上都下得到**；
#: ModelScope 是另一套，覆盖少一截。界面上只在"当前源确实没有"时把那条变灰，
#: 见 `酒馆模型页._按来源变`。
#:
#: ⚠ **全部 ≤ 6GB，保证 8G 显存能 `n_gpu_layers=-1` 全量上卡。**
#: 12B 以上的（DeepSeek-V2-Lite 10.4GB、Rocinante-X-12B / Lumimaid-Magnum-12B
#: 各 7.5GB）**故意不列**：列了会诱导用户白下 8~15GB 才发现只能往内存卸载，
#: 首字速度断崖下跌，是最坏的一种体验。要下那几档用「搜索」，不设门槛。
#:
#: ⚠ **这里一条 NSFW/色情指向的仓库名都不放。** "Uncensored / Abliterated"
#: 属于通用去审查，保留；明确指向色情的那些（搜索时见得着）不进预置列表。
#: 搜索是不做过滤的——搜到什么算用户自己的选择，但预置清单要保持干净。
推荐模型 = (
    # ── DeepSeek 系 ──
    {'组': 'DeepSeek 系',
     '名字': 'DeepSeek-R1-Distill-Qwen-1.5B（Q4_K_M，1.1GB，轻快会思考）',
     '仓库': 'unsloth/DeepSeek-R1-Distill-Qwen-1.5B-GGUF',
     '模式': 'DeepSeek-R1-Distill-Qwen-1.5B-Q4_K_M.gguf',
     '约字节': int(1.12e9), '有ms': True},
    {'组': 'DeepSeek 系',
     '名字': 'DeepSeek-R1-Distill-Qwen-7B（Q4_K_M，4.7GB，主力档）',
     '仓库': 'unsloth/DeepSeek-R1-Distill-Qwen-7B-GGUF',
     '模式': 'DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf',
     '约字节': int(4.68e9), '有ms': True},
    {'组': 'DeepSeek 系',
     '名字': 'DeepSeek-R1-Distill-Llama-8B（Q4_K_M，4.9GB，换 Llama 底座）',
     '仓库': 'unsloth/DeepSeek-R1-Distill-Llama-8B-GGUF',
     '模式': 'DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf',
     '约字节': int(4.92e9), '有ms': True},
    {'组': 'DeepSeek 系',
     '名字': 'DeepSeek-R1-0528-Qwen3-8B（Q4_K_M，5.0GB，新版蒸馏）',
     '仓库': 'unsloth/DeepSeek-R1-0528-Qwen3-8B-GGUF',
     '模式': 'DeepSeek-R1-0528-Qwen3-8B-Q4_K_M.gguf',
     '约字节': int(5.03e9), '有ms': True},
    {'组': 'DeepSeek 系',
     '名字': 'DeepSeek-R1-Distill-Qwen-7B 去审查版（Q4_K_M，4.7GB）',
     '仓库': 'mradermacher/DeepSeek-R1-Distill-Qwen-7B-Uncensored-i1-GGUF',
     '模式': 'DeepSeek-R1-Distill-Qwen-7B-Uncensored.i1-Q4_K_M.gguf',
     '约字节': int(4.68e9), '有ms': False},
    {'组': 'DeepSeek 系',
     '名字': 'DeepSeek-R1-Distill-Llama-8B 去审查版（Q4_K_M，4.9GB）',
     '仓库': 'mradermacher/DeepSeek-R1-Distill-Llama-8B-Abliterated-i1-GGUF',
     '模式': 'DeepSeek-R1-Distill-Llama-8B-Abliterated.i1-Q4_K_M.gguf',
     '约字节': int(4.92e9), '有ms': False},
    {'组': 'DeepSeek 系',
     '名字': 'DeepSeek-R1-Distill-Qwen-1.5B 去审查版（Q4_K_M，1.1GB）',
     '仓库': 'mradermacher/DeepSeek-R1-Distill-Qwen-1.5B-uncensored-GGUF',
     '模式': 'DeepSeek-R1-Distill-Qwen-1.5B-uncensored.Q4_K_M.gguf',
     '约字节': int(1.12e9), '有ms': False},
    {'组': 'DeepSeek 系',
     '名字': 'deepseek-llm-7b-chat（Q4_K_M，4.2GB，老一代，不思考）',
     '仓库': 'TheBloke/deepseek-llm-7B-chat-GGUF',
     '模式': 'deepseek-llm-7b-chat.Q4_K_M.gguf',
     '约字节': int(4.22e9), '有ms': True},

    # ── 千问·全系（1.5B–9B）──
    #
    # ⚠ **这一组是唯一"全系带原生工具调用"的族**。2026-09 逐条查过它们的
    # 源模型 `tokenizer_config.json`：Qwen3.5-9B 的模板 7756 字、Qwen3-8B
    # 4168 字，全都有 `{%- if tools %}` 分支和 `role='tool'` 渲染。
    # **DeepSeek 官方全系一个都没有**（详见那次对比），所以做 agent 只能靠
    # 这一族。
    #
    # 各代的关系（同一档位优先用新的）：
    #
    #   Qwen3.5   2026 年那代，模板最完整，同尺寸最强
    #   Qwen3     上一代；`Qwen3-4B-Instruct-2507` 是它的非思考版，快
    #   Qwen2.5   更早，但 `-Instruct` 那份工具支持很成熟，中文也好
    #   Coder     代码向，写代码/agent 的活可以优先试——但角色扮演别用
    {'组': '千问·全系（1.5B–9B）',
     '名字': 'Qwen3.5-9B（Q4_K_M，5.7GB，8G 档最强基线，模板最全）',
     '仓库': 'unsloth/Qwen3.5-9B-GGUF',
     '模式': 'Qwen3.5-9B-Q4_K_M.gguf',
     '约字节': int(5.68e9), '有ms': True},
    {'组': '千问·全系（1.5B–9B）',
     '名字': 'Qwen3.5-4B（Q4_K_M，2.7GB，快得多）',
     '仓库': 'unsloth/Qwen3.5-4B-GGUF',
     '模式': 'Qwen3.5-4B-Q4_K_M.gguf',
     '约字节': int(2.74e9), '有ms': True},
    {'组': '千问·全系（1.5B–9B）',
     '名字': 'Qwen3.5-2B（Q4_K_M，1.3GB，轻快）',
     '仓库': 'unsloth/Qwen3.5-2B-GGUF',
     '模式': 'Qwen3.5-2B-Q4_K_M.gguf',
     '约字节': int(1.28e9), '有ms': True},
    {'组': '千问·全系（1.5B–9B）',
     '名字': 'Qwen3.5-0.8B（Q4_K_M，0.5GB，极小，试水用）',
     '仓库': 'unsloth/Qwen3.5-0.8B-GGUF',
     '模式': 'Qwen3.5-0.8B-Q4_K_M.gguf',
     '约字节': int(0.53e9), '有ms': True},
    {'组': '千问·全系（1.5B–9B）',
     '名字': 'Qwen3-8B（Q4_K_M，5.0GB，上一代旗舰）',
     '仓库': 'unsloth/Qwen3-8B-GGUF',
     '模式': 'Qwen3-8B-Q4_K_M.gguf',
     '约字节': int(5.03e9), '有ms': True},
    {'组': '千问·全系（1.5B–9B）',
     '名字': 'Qwen3-4B（Q4_K_M，2.5GB）',
     '仓库': 'unsloth/Qwen3-4B-GGUF',
     '模式': 'Qwen3-4B-Q4_K_M.gguf',
     '约字节': int(2.50e9), '有ms': True},
    {'组': '千问·全系（1.5B–9B）',
     '名字': 'Qwen3-4B-Instruct-2507（Q4_K_M，2.5GB，不思考，快）',
     '仓库': 'unsloth/Qwen3-4B-Instruct-2507-GGUF',
     '模式': 'Qwen3-4B-Instruct-2507-Q4_K_M.gguf',
     '约字节': int(2.50e9), '有ms': True},
    {'组': '千问·全系（1.5B–9B）',
     '名字': 'Qwen3-1.7B（Q4_K_M，1.1GB）',
     '仓库': 'unsloth/Qwen3-1.7B-GGUF',
     '模式': 'Qwen3-1.7B-Q4_K_M.gguf',
     '约字节': int(1.11e9), '有ms': True},
    {'组': '千问·全系（1.5B–9B）',
     '名字': 'Qwen3-0.6B（Q4_K_M，0.4GB，极小）',
     '仓库': 'unsloth/Qwen3-0.6B-GGUF',
     '模式': 'Qwen3-0.6B-Q4_K_M.gguf',
     '约字节': int(0.40e9), '有ms': True},
    {'组': '千问·全系（1.5B–9B）',
     '名字': 'Qwen2.5-7B-Instruct（Q4_K_M，4.7GB，中文很稳）',
     # ⚠ 不用 `Qwen/Qwen2.5-7B-Instruct-GGUF`：那个仓库**只发了 fp16 分片，
     # 没有 Q4_K_M**（3B / 1.5B 那两个官方仓库反而有，同一家的命名不统一）。
     # 写进去就是死条目 —— 实测核对文件名时才发现的，见 `推荐模型` 那条注释。
     '仓库': 'bartowski/Qwen2.5-7B-Instruct-GGUF',
     '模式': 'Qwen2.5-7B-Instruct-Q4_K_M.gguf',
     '约字节': int(4.68e9), '有ms': True},
    {'组': '千问·全系（1.5B–9B）',
     '名字': 'Qwen2.5-3B-Instruct（Q4_K_M，2.1GB，你本地已经有了）',
     '仓库': 'Qwen/Qwen2.5-3B-Instruct-GGUF',
     '模式': 'qwen2.5-3b-instruct-q4_k_m.gguf',
     '约字节': int(2.10e9), '有ms': True},
    {'组': '千问·全系（1.5B–9B）',
     '名字': 'Qwen2.5-1.5B-Instruct（Q4_K_M，1.1GB）',
     '仓库': 'Qwen/Qwen2.5-1.5B-Instruct-GGUF',
     '模式': 'qwen2.5-1.5b-instruct-q4_k_m.gguf',
     '约字节': int(1.12e9), '有ms': True},
    {'组': '千问·全系（1.5B–9B）',
     '名字': 'Qwen2.5-Coder-7B-Instruct（Q4_K_M，4.7GB，写代码优先）',
     '仓库': 'Qwen/Qwen2.5-Coder-7B-Instruct-GGUF',
     '模式': 'qwen2.5-coder-7b-instruct-q4_k_m.gguf',
     '约字节': int(4.68e9), '有ms': True},
    {'组': '千问·全系（1.5B–9B）',
     '名字': 'Qwen2.5-Coder-3B-Instruct（Q4_K_M，2.1GB，写代码·快）',
     '仓库': 'Qwen/Qwen2.5-Coder-3B-Instruct-GGUF',
     '模式': 'qwen2.5-coder-3b-instruct-q4_k_m.gguf',
     '约字节': int(2.10e9), '有ms': True},
    {'组': '千问·全系（1.5B–9B）',
     '名字': 'Qwen2.5-Coder-1.5B-Instruct（Q4_K_M，1.1GB，写代码·最小）',
     '仓库': 'Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF',
     '模式': 'qwen2.5-coder-1.5b-instruct-q4_k_m.gguf',
     '约字节': int(1.12e9), '有ms': True},

    # ── 国外·通用 ──
    {'组': '国外·通用',
     '名字': 'Gemma-4-12B-it-qat（UD-Q4_K_XL，6.7GB，12B 但 8G 还塞得下）',
     '仓库': 'unsloth/gemma-4-12B-it-qat-GGUF',
     '模式': 'gemma-4-12B-it-qat-UD-Q4_K_XL.gguf',
     '约字节': int(6.72e9), '有ms': True},
    {'组': '国外·通用',
     '名字': 'Gemma-4-E4B-it（Q4_K_M，5.0GB）',
     '仓库': 'unsloth/gemma-4-E4B-it-GGUF',
     '模式': 'gemma-4-E4B-it-Q4_K_M.gguf',
     '约字节': int(4.98e9), '有ms': True},
    {'组': '国外·通用',
     '名字': 'Gemma-4-E2B-it（Q4_K_M，3.1GB，小而快）',
     '仓库': 'unsloth/gemma-4-E2B-it-GGUF',
     '模式': 'gemma-4-E2B-it-Q4_K_M.gguf',
     '约字节': int(3.11e9), '有ms': True},
    {'组': '国外·通用',
     '名字': 'GLM-4-9B-Chat（IQ4_XS，5.3GB，智谱）',
     '仓库': 'legraphista/glm-4-9b-chat-IMat-GGUF',
     '模式': 'glm-4-9b-chat.IQ4_XS.gguf',
     '约字节': int(5.25e9), '有ms': False},
    {'组': '国外·通用',
     '名字': 'LFM2.5-2.6B（Q4_K_M，1.7GB，极小，官方标支持中文）',
     '仓库': 'LiquidAI/LFM2.5-2.6B-GGUF',
     '模式': 'LFM2.5-2.6B-Q4_K_M.gguf',
     '约字节': int(1.67e9), '有ms': True},
    {'组': '国外·通用',
     '名字': 'Mistral-7B-Instruct-v0.3（Q4_K_M，4.4GB）',
     '仓库': 'bartowski/Mistral-7B-Instruct-v0.3-GGUF',
     '模式': 'Mistral-7B-Instruct-v0.3-Q4_K_M.gguf',
     '约字节': int(4.37e9), '有ms': True},
    {'组': '国外·通用',
     '名字': 'Phi-3.5-mini-instruct（Q4_K_M，2.4GB，小快）',
     '仓库': 'bartowski/Phi-3.5-mini-instruct-GGUF',
     '模式': 'Phi-3.5-mini-instruct-Q4_K_M.gguf',
     '约字节': int(2.39e9), '有ms': True},
    {'组': '国外·通用',
     '名字': 'Llama-3.1-8B-Instruct（Q4_K_M，4.9GB）',
     '仓库': 'unsloth/Llama-3.1-8B-Instruct-GGUF',
     '模式': 'Llama-3.1-8B-Instruct-Q4_K_M.gguf',
     '约字节': int(4.92e9), '有ms': True},

    # ── 国外·角色扮演 ──
    # ArliAI 的 RPMax 是长期口碑系列，专门为角色扮演调的，不是通用模型改名
    {'组': '国外·角色扮演',
     '名字': 'Llama-3.1-8B-ArliAI-RPMax-v1.3（Q4_K_M，4.9GB，RP 调优）',
     '仓库': 'bartowski/Llama-3.1-8B-ArliAI-RPMax-v1.3-GGUF',
     '模式': 'Llama-3.1-8B-ArliAI-RPMax-v1.3-Q4_K_M.gguf',
     '约字节': int(4.92e9), '有ms': True},
    {'组': '国外·角色扮演',
     '名字': 'Gemma-2-9B-ArliAI-RPMax-v1.1（Q4_K_M，5.8GB，RP 调优）',
     '仓库': 'bartowski/Gemma-2-9B-ArliAI-RPMax-v1.1-GGUF',
     '模式': 'Gemma-2-9B-ArliAI-RPMax-v1.1-Q4_K_M.gguf',
     '约字节': int(5.76e9), '有ms': True},
    {'组': '国外·角色扮演',
     '名字': 'Phi-3.5-mini-3.8B-ArliAI-RPMax-v1.1（Q4_K_M，2.4GB，RP 调优·小）',
     '仓库': 'bartowski/Phi-3.5-mini-3.8B-ArliAI-RPMax-v1.1-GGUF',
     '模式': 'Phi-3.5-mini-3.8B-ArliAI-RPMax-v1.1-Q4_K_M.gguf',
     '约字节': int(2.39e9), '有ms': True},
    {'组': '国外·角色扮演',
     '名字': 'rpDungeon-Gemma-4-E4B-Luchador（Q4_K_M，5.4GB）',
     '仓库': 'bartowski/rpDungeon_Gemma-4-E4B-Luchador-GGUF',
     '模式': 'rpDungeon_Gemma-4-E4B-Luchador-Q4_K_M.gguf',
     '约字节': int(5.41e9), '有ms': True},
    {'组': '国外·角色扮演',
     '名字': 'Nyx-RP-9B-Instruct-2608（Q4_K_M，5.8GB，2026-08 新）',
     '仓库': 'Indexnusrefather/Nyx-RP-9B-Instruct-2608-v1',
     '模式': 'Nyx-RP-9B-Instruct-2608-v1.Q4_K_M.gguf',
     '约字节': int(5.78e9), '有ms': False},
    {'组': '国外·角色扮演',
     '名字': 'L3-8B-Stheno-v3.2（Q4_K_M，4.9GB，经典 RP）',
     '仓库': 'bartowski/L3-8B-Stheno-v3.2-GGUF',
     '模式': 'L3-8B-Stheno-v3.2-Q4_K_M.gguf',
     '约字节': int(4.92e9), '有ms': True},
    {'组': '国外·角色扮演',
     '名字': 'Humanish-Roleplay-Llama-3.1-8B（Q4_K_M，4.9GB）',
     '仓库': 'mradermacher/Humanish-Roleplay-Llama-3.1-8B-i1-GGUF',
     '模式': 'Humanish-Roleplay-Llama-3.1-8B.i1-Q4_K_M.gguf',
     '约字节': int(4.92e9), '有ms': False},
    {'组': '国外·角色扮演',
     '名字': 'Peach-9B-8k-Roleplay（Q4_K_M，5.3GB）',
     '仓库': 'bartowski/Peach-9B-8k-Roleplay-GGUF',
     '模式': 'Peach-9B-8k-Roleplay-Q4_K_M.gguf',
     '约字节': int(5.33e9), '有ms': True},

    # ── 国外·去审查 ──
    # ⚠ "Uncensored / Abliterated" 只是**定向削弱了某些方向的拒答倾向**，
    # 副作用常是逻辑连贯性和指令跟随一起掉。表现就是"肯说了，但说得颠三倒四"。
    # 所以别看着名字就信——拿同一个底座的三份（官方 / uncensored / abliterated）
    # 跑同一个提示词对比，才知道这份到底值不值。
    {'组': '国外·去审查',
     '名字': 'Gemma-4-E4B-Uncensored-Aggressive（Q4_K_M，5.3GB，去审查）',
     '仓库': 'HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive',
     '模式': 'Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf',
     '约字节': int(5.34e9), '有ms': False},
    {'组': '国外·去审查',
     '名字': 'Qwen3.5-9B-Uncensored-Aggressive（Q4_K_M，5.6GB，去审查）',
     '仓库': 'HauhauCS/Qwen3.5-9B-Uncensored-HauhauCS-Aggressive',
     '模式': 'Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf',
     '约字节': int(5.63e9), '有ms': False},
    {'组': '国外·去审查',
     '名字': 'Huihui-Qwen3.5-9B-abliterated（Q4_K_M，5.6GB，去审查）',
     '仓库': 'mradermacher/Huihui-Qwen3.5-9B-abliterated-GGUF',
     '模式': 'Huihui-Qwen3.5-9B-abliterated.Q4_K_M.gguf',
     '约字节': int(5.63e9), '有ms': False},
    {'组': '国外·去审查',
     '名字': 'Huihui-Ornith-1.5-9B-abliterated（Q4_K_M，5.6GB，去审查）',
     '仓库': 'mradermacher/Huihui-Ornith-1.5-9B-abliterated-i1-GGUF',
     '模式': 'Huihui-Ornith-1.5-9B-abliterated.i1-Q4_K_M.gguf',
     '约字节': int(5.63e9), '有ms': False},
    {'组': '国外·去审查',
     '名字': 'Qwen3.5-9B-Defiant-Fable-Heretic（Q4_K_M，7.0GB，去审查·吃紧）',
     '仓库': 'DavidAU/Qwen3.5-9B-The-Defiant-Fable-Uncensored-Heretic-NEO-IMATRIX-MAX-MTP-GGUF',
     '模式': 'Qwen3.5-9B-The-Defiant-Fable-Uncnr-Heretic-NEO-MAX-MTP-Q4_K_M.gguf',
     '约字节': int(6.98e9), '有ms': False},
)


def 首选源(档):
    """
    这一档首选从哪个源下：ModelScope 有镜像就用它（国内最快），没有就 hf-mirror。

    ⚠ **名字不能叫 `默认源`** —— 那个是模块级的字符串常量（`默认源 =
    'modelscope'`），重名的话这个函数会把它整个盖掉。后果极其隐蔽：
    `列远程文件(仓库)` 这种"不传源用默认"的调用，默认值会变成一个函数对象，
    一路漂到 `源们.get()` 才变成"没有这个下载源"——而报错信息里印出来的是
    `<function 默认源 at 0x...>`，跟"源名写错了"看着一模一样。

    不把源写死在档位里、而是从 `有ms` 推——两处各存一份的话，迟早有一处
    忘了跟着改，症状同样是"点了下载报一个源上没这个仓库"。
    """
    return 'modelscope' if 档.get('有ms') else 'hf-mirror'


def 源上有(档, 源标):
    """
    这一档在当前源上下不下得动。

    **hf 系两个源一律算有**：hf-mirror 就是 huggingface.co 的镜像，同一批
    仓库两条路。只有 ModelScope 是另一套、覆盖少一截（见 `推荐模型` 那段）。
    """
    if 源标 != 'modelscope':
        return True
    return bool(档.get('有ms'))

#: 精选清单底部那行灰字。**不可选、不可下载**，只是把"为什么没收录"说清楚，
#: 免得用户以为列表坏了。见 `推荐模型` 上面对 12B+ 那段的说明。
精选脚注 = ('12B 以上的（DeepSeek-V2-Lite 10.4GB、Rocinante-X-12B / '
          'Lumimaid-Magnum-12B 各 7.5GB）8G 显存全量上不了卡，没进这个清单；'
          '要下用上面的「搜索」。')


def _净名(段):
    """
    把用户能打的东西洗成安全的路径段。

    仓库名是能手打的（「自己填仓库」那格），`..\\..\\` 能顺着模型目录爬出去
    写文件。跟 `删本地` 里那个 `os.path.basename` 守口是同一个道理：
    **别信任何会进路径的用户输入。** 只留 `[A-Za-z0-9._-]`，其余一律换成
    下划线。

    ⚠ **光靠白名单还不够，点和尾巴要单独挡。** `.` 和 `..` 在白名单里是合法
    字符，但它们同时是路径语义：

        '.'   → 这一层整个消失（`.../hf-mirror/./x.gguf` 落在 hf-mirror 上）
        '..'  → 真的往上跳一层（`.../hf-mirror/../x.gguf` 落在 _暂存 根）

    实测就是这么翻的车。所以洗完还要过两道：

      ① **去掉结尾的点**（`rstrip('.')`）。Windows 会**静默吃掉路径段结尾
         的点**——`mkdir('..__..')` 建出来实际叫 `..__`，紧接着建它的子目录
         就 `WinError 3 系统找不到指定的路径`。这个是实测撞出来的，不是理论。
      ② **整段只剩点就换成下划线**（`.`、`..`、`...` 都是）。空串同理——
         空段会让 `os.path.join` 少一层，仓库那级的隔离就白做了。
    """
    净 = ''.join(c if (c.isalnum() or c in '._-') else '_' for c in (段 or ''))
    净 = 净.rstrip('.')
    if not 净.strip('.'):
        return '_'
    return 净


def 暂存目录(源标='', 仓库='', 模式=''):
    """
    这一次下载专用的暂存目录，三级：`源 / 仓库 / 模式`。不带参数时回根。

    为什么要分这么细：

      · **断点续传只认"同一个目录里的上次残留"**。混在一起的话，两个源的
        半成品格式根本不同（ms 是 `._____temp/<owner>/<name>`，hf 是
        `.cache/huggingface/download/<哈希>.<etag>.incomplete`），谁也认不出
        谁的分片，白重下。
      · **进度只要"这一次"的量**。共用一个大会数到别人的字节——先下了
        modelscope 的一半再切 hf 重下同一个文件，进度条开局就 30%、没下完
        先顶到 99%，全是另一个源的残留。
      · **同一个仓库里有多个量化档位**（7B 的 Q4/Q5/Q6 在同一个仓库），
        分到"模式"这一级，Q4 下了一半改下 Q5 时不会互相算进对方进度。
      · 同一个仓库在不同源上的**缓存布局不同**，混用会把缓存元数据对错。

    仓库名里的 `/` 换成 `__` 再洗——`owner/name` 直接当路径会多出一层目录。
    """
    段 = [模型目录(), '_暂存']
    if 源标:
        段.append(_净名(源标))
        if 仓库:
            段.append(_净名(仓库.replace('/', '__')))
            if 模式:
                段.append(_净名(模式))
    目 = os.path.join(*段)
    os.makedirs(目, exist_ok=True)
    return 目


def _挪进模型目录(地, 去向=None):
    """
    把 `地` 里**已经下完**的 `.gguf` 挪进 `去向`（默认模型目录），回文件名表。

    ⚠ **必须整棵剪掉半成品目录**，见文件头那条。用 `os.walk` 的原地剪枝
    （改 `目录们[:]`）——比"先全走一遍再挑干净的挪"稳，半成品目录整个不进，
    连误判的机会都没有。

    **不往子目录里下探也不行**：hf 传 `filename` 带子目录时文件就在子目录里，
    所以照常下探，只剪那两个已知的半成品目录名。

    `去向` 只是给无网单测用的（指到临时目录，不碰真模型目录）。
    """
    去向 = 去向 or 模型目录()
    剪 = frozenset(('._____temp', '.cache'))
    落了 = []
    for 根, 目录们, 件们 in os.walk(地):
        目录们[:] = [d for d in 目录们 if d not in 剪]
        for 件 in 件们:
            if not 件.lower().endswith('.gguf'):
                continue
            os.replace(os.path.join(根, 件),
                       os.path.join(去向, os.path.basename(件)))
            落了.append(os.path.basename(件))
    return 落了


def _ms是人话(仓库, 错):
    """
    modelscope 的错 → 人话。

    ⚠ 最要紧的是**仓库不存在**这一种：ModelScope 只镜像了一部分 HF 仓库，
    用户点了一条 mradermacher 的档位、或者自己填了个 HF 系仓库名，拿到的
    原始报错是 `HTTPError: The request model: xxx does not exist!`——这句
    完全没告诉他"该换个源"。这儿当场换成一句能照着做的。

    别的错原样带上类型名和消息，不吞。
    """
    文 = '%s' % (错,)
    if 'does not exist' in 文 or 'Not Found' in 文 or '404' in 文:
        return ('ModelScope 上没有这个仓库（%s）。它多半是 HF 系的仓库——'
                '把「来源」切到 hf-mirror.com 或 HuggingFace 官方再试。'
                % 仓库)
    return '%s：%s' % (type(错).__name__, 错)


def _hf是人话(仓库, 错):
    """
    hf 系那条路出的错 → 人话。

    三种最常见的：

      **401/403**   门禁仓库（要先去网页上接受一次许可）
      **404**       仓库名或文件名不对
      **连不上**    官方源要梯子；hf-mirror 不用

    ⚠ **`LocalEntryNotFoundError` 不能当成"文件不在"。** 那个名字骗人：
    huggingface_hub 把"连不上 / 元数据对不上"也套成它（实测 hf-mirror
    就是这条——它返的是弱 ETag，`W/"…"`，而 hf 只认强 ETag）。所以我们
    干脆不用它的下载函数了，见 `_hf直下`；这个函数只留着给
    `list_repo_tree` / `list_models` 那两条还在用 HfApi 的路。
    """
    文 = '%s' % (错,)
    名 = type(错).__name__
    if 'GatedRepo' in 名 or '401' in 文 or '403' in 文:
        return ('这个仓库要授权才能下（%s）。去 HuggingFace 网页上接受一次'
                '许可，然后：把「来源」切到 HuggingFace 官方、并在命令行跑一次 '
                'huggingface-cli login。' % 仓库)
    if 'RepositoryNotFound' in 名 or '404' in 文:
        return ('这个源上没有这个仓库（%s）。换一个「来源」再试，'
                '或者检查仓库名拼错了没有。' % 仓库)
    if 'LocalEntryNotFound' in 名 or 'Connect' in 名 or 'Timeout' in 名 \
            or 'timed out' in 文:
        return ('连不上 %s。官方源要梯子，hf-mirror.com 不用——'
                '把「来源」切成 hf-mirror.com 试试。' % 仓库)
    return '%s：%s' % (名, 错)


def _hf直下(仓库, 模式, 源, 暂):
    """
    从 hf 系（hf-mirror.com / huggingface.co）下一个文件，**阻塞**。

    ⚠ **为什么不用 `hf_hub_download`。** 它跟 hf-mirror 根本不兼容：镜像站
    返回的是**弱 ETag**（`W/"…"`），而 huggingface_hub 1.x 的
    `get_hf_file_metadata` 只认强 ETag，当场抛 `FileMetadataError`——外面
    又被包成 `LocalEntryNotFoundError`，报出来的话是"找不到文件"，而实际上
    网络是通的、文件也拿得到（纯 requests 一把就下下来了）。实测确认过。
    它家那个 `endpoint=` 参数和 `HF_ENDPOINT` 环境变量两条路都救不了。

    自己下反而干净：**续传、落点、进度、尺寸校验全在明面上**，也不用管
    Xet 把块缓存写到别处、更不用管 `.cache` 那套目录形状。

    续传：半成品放 `<暂存>/<文件名>.part`，下次同一个调用看到它就带
    `Range: bytes=<已有字节>-` 接着要。对面不认 Range（回 200 而不是 206）
    的话就把已下的丢掉从头写——**不能盲目 append**，那会拼出一个坏文件。

    ⚠ **下完要比对字节数。** 尺寸对不上说明断了（服务器提前关流、代理截断），
    这时候 `.part` 留着继续续、但**绝不能改名成正式文件**——一个 truncated
    的 GGUF 加载时报的错跟"这个模型不支持"长得一模一样，最难查。
    """
    网 = '%s/%s/resolve/main/%s' % (源['端'], 仓库, 模式)
    头 = {'User-Agent': 'Mozilla/5.0'}      # 缺 UA 有些 CDN 直接 403
    牌 = _hf令牌(源['标'])
    if 牌:
        头['Authorization'] = 'Bearer ' + 牌

    终 = os.path.join(暂, os.path.basename(模式))
    半 = 终 + '.part'
    已有 = os.path.getsize(半) if os.path.isfile(半) else 0
    if 已有:
        头['Range'] = 'bytes=%d-' % 已有

    try:
        响应 = requests.get(网, headers=头, stream=True, timeout=(10, 120))
    except requests.exceptions.RequestException as 错:
        raise 本地错('连不上 %s：%s（官方 HuggingFace 要梯子，'
                    'hf-mirror.com 不用）' % (网, 错))

    with 响应:
        if 响应.status_code == 416:
            # 本地那份已经够长（多半是上次下完没改名就断了），当成下完
            os.replace(半, 终)
            return 终
        if 响应.status_code in (401, 403):
            raise 本地错('这个仓库要授权才能下（%s）。去 HuggingFace 网页上'
                        '接受一次许可，再把「来源」切到 HuggingFace 官方、'
                        '跑一次 huggingface-cli login。' % 仓库)
        if 响应.status_code == 404:
            raise 本地错('这个源上没有 %s 里的 %s。检查一下仓库名和文件名，'
                        '或者换个「来源」。' % (仓库, 模式))
        if 响应.status_code not in (200, 206):
            raise 本地错('下 %s 的时候对面回了 HTTP %d。'
                        % (仓库, 响应.status_code))

        if 响应.status_code == 200:
            # 对面不理 Range，把整个文件从头给了 —— 丢掉已下的重写，
            # **不能 append**，否则前半段是旧的、后半段是新的，拼出坏文件
            已有 = 0
        长 = int(响应.headers.get('Content-Length') or 0)
        总 = (已有 + 长) if 响应.status_code == 206 else (长 or 0)

        with open(半, 'ab' if 已有 else 'wb') as 出:
            for 块 in 响应.iter_content(chunk_size=1 << 20):
                if 块:
                    出.write(块)

    实 = os.path.getsize(半)
    if 总 and 实 != 总:
        raise 本地错('下到一半断了：%s 只拿到 %d / %d 字节（差 %d）。'
                    '已经下的部分留着，再点一次「开始下载」会接着下。'
                    % (模式, 实, 总, 总 - 实))
    os.replace(半, 终)
    return 终


def 列远程文件(仓库, 源标=默认源):
    """
    列一个仓库里的 GGUF 文件：`[(仓库内路径, 字节数), …]`，按路径排。

    **阻塞网络调用，别在主线程里调**——跟 `酒馆大脑.列模型` 同一条纪律，
    界面那边起一次性线程跑。

    `源标` 是 `下载源` 里的标识；默认 modelscope，老调用点不改也能跑。
    """
    仓库 = (仓库 or '').strip()
    if not 仓库:
        raise 本地错('仓库名是空的——得先知道去哪个仓库里翻。')
    源 = _查源(源标)

    if 源['系'] == 'hf':
        from huggingface_hub import HfApi, RepoFile
        try:
            # ⚠ 生成器的异常是在**迭代时**才抛的，所以 list() 得在 try 里。
            # ⚠ 必须用 list_repo_tree：list_repo_files 只给文件名、**没有大小**，
            #   而下拉框要显示"多大"、进度条要估算，都需要 size。
            节点们 = list(HfApi(endpoint=源['端'],
                              token=_hf令牌(源['标'])).list_repo_tree(
                仓库, recursive=True))
        except Exception as 错:
            raise 本地错(_hf是人话(仓库, 错))
        出 = []
        for n in 节点们:
            if not isinstance(n, RepoFile):
                continue                  # RepoFolder，跳过
            if not n.path.lower().endswith('.gguf'):
                continue
            # LFS 大文件的大小有时只在 `.lfs.size` 里，两个都兜一下
            大 = n.size or ((n.lfs or {}).get('size') if n.lfs else 0) or 0
            出.append((n.path, int(大)))
        return sorted(出, key=lambda 项: 项[0])

    from modelscope.hub.api import HubApi
    try:
        件们 = HubApi().get_model_files(仓库, recursive=True)
    except Exception as 错:
        raise 本地错(_ms是人话(仓库, 错))
    return sorted(((f['Path'], f.get('Size') or 0) for f in 件们
                   if f.get('Path', '').lower().endswith('.gguf')),
                  key=lambda 项: 项[0])


def 搜模型(关键词, 源标=默认源, 限=12):
    """
    按关键词搜别人的仓库，回**仓库级**的列表：

        [{'仓库': 'owner/name', '下载': int, '喜欢': int, '源': 源标}, …]

    **阻塞网络调用，别在主线程里调。** 搜出来的只是仓库名，还得
    `列远程文件` 才能挑具体那个 .gguf——界面那边是"选中结果 → 自动列文件"。

    ⚠ **搜索结果不做任何过滤。** 搜到什么算用户自己的选择；预置清单
    （`推荐模型`）才是要保证干净的地方。别顺手给这儿加白名单。
    """
    关键词 = (关键词 or '').strip().replace('\r', ' ').replace('\n', ' ')[:64]
    if not 关键词:
        raise 本地错('搜索词是空的。')
    源 = _查源(源标)
    限 = max(1, int(限 or 12))

    if 源['系'] == 'hf':
        from huggingface_hub import HfApi
        try:
            # ⚠ `list_models` 返回的是**生成器**，直接 len() 会 TypeError。
            # ⚠ 这个版本也没有 `direction` 参数，排序只能靠 sort=。
            # `filter='gguf'` 写死——只回真带 GGUF 的仓库，关键字是用户输入
            # 但走的是 SDK 的 search= 参数（SDK 自己 urlencode），不手工拼 URL。
            批 = list(HfApi(endpoint=源['端'],
                          token=_hf令牌(源['标'])).list_models(
                search=关键词, filter='gguf', sort='downloads', limit=限))
        except Exception as 错:
            raise 本地错(_hf是人话('(搜索)', 错))
        return [{'仓库': m.id, '下载': int(m.downloads or 0),
                 '喜欢': int(m.likes or 0), '源': 源['标']} for m in 批]

    # ── modelscope ──
    # ⚠ SDK 搜不了：`HubApi.list_models` 第一个参数是 owner/group，只能列
    # "某个人/组织下的模型"，**没有关键词参数**（`list_datasets` 才有）。
    # 这里用的是它官网前端自己的那个接口。好处是三源都能搜，坏处是它是
    # **非官方接口、没有版本保证**——所以挂了的时候文案要给出退路。
    try:
        响应 = requests.put(源['端'] + '/api/v1/dolphin/models',
                           json={'PageSize': 限, 'PageNumber': 1,
                                 'SortBy': 'Default', 'Target': '',
                                 'SingleCriterion': [], 'Name': 关键词},
                           timeout=20)
        响应.raise_for_status()
        模型段 = ((响应.json().get('Data') or {}).get('Model') or {})
        条们 = 模型段.get('Models') or []
    except Exception as 错:
        raise 本地错(
            'ModelScope 搜索接口没响应（%s：%s）。它是官网接口，不保证稳定——'
            '可以先切到 hf-mirror.com 搜，或者用「自己填仓库」直接填仓库名。'
            % (type(错).__name__, 错))

    出 = []
    for m in 条们:
        if not isinstance(m, dict):
            continue
        名 = '%s/%s' % (m.get('Path') or '', m.get('Name') or '')
        if 名 == '/':
            continue
        出.append({'仓库': 名, '下载': int(m.get('Downloads') or 0),
                   '喜欢': int(m.get('Stars') or 0), '源': 源['标']})
    return 出


def 下载(仓库, 模式, 源标=默认源):
    """
    把一个（或一组）GGUF 下进 `模型目录/`。**阻塞，给后台线程用。**

    两个源都下到**只属于这一次下载的临时目录**
    （`暂存目录(源, 仓库, 模式)`），下完由 `_挪进模型目录` 把 `.gguf` 挪进
    模型目录——挪走之后「已下载列表」才看得见它。断点续传是各自带的：
    modelscope 靠 `._____temp` 分片 + `.msc` 索引，hf 系靠 `_hf直下` 自己
    维护的 `.part`，都是"下次同一个调用自动接着下"，我们不用管。

    ⚠ **两条路都不看 SDK 的返回值。** modelscope 的 `snapshot_download` 回的
    是 cache 根目录、`_hf直下` 回的是文件路径，形状不一样；统一从我们指定的
    暂存目录里挪，出口只有一个。

    ⚠ 中途掐不断：两条都是一次阻塞调用，取消只能等它这次回来之后才生效
    （界面那边置旗，回来后不挪文件）。跟 `_读客` 在 `酒馆大脑` 里记录的
    Windows 阻塞读是同一个道理。
    """
    仓库 = (仓库 or '').strip()
    模式 = (模式 or '').strip()
    if not 仓库 or not 模式:
        raise 本地错('仓库和文件都得有——现在是仓库 %r、文件 %r。'
                    % (仓库, 模式))
    源 = _查源(源标)
    暂 = 暂存目录(源['标'], 仓库, 模式)

    if 源['系'] == 'hf':
        _hf直下(仓库, 模式, 源, 暂)
    else:
        from modelscope import snapshot_download
        try:
            # ⚠ 继续用 cache_dir、**不换成 local_dir**：这条现在是通的，
            # 而 cache_dir 模式下半截文件在 `._____temp`（snapshot_download
            # 返回目录的兄弟），本来就不在返回树下。换了 local_dir 反而
            # 把半截文件搬进树里，白白多一个要防的东西。
            snapshot_download(仓库, cache_dir=暂, allow_patterns=[模式])
        except Exception as 错:
            raise 本地错(_ms是人话(仓库, 错))

    落了 = _挪进模型目录(暂)
    if not 落了:
        raise 本地错('下完了但没找到 GGUF 文件——模式 %r 可能没匹配上，'
                    '或者这个文件不在当前下载源上（换个「来源」再试）。'
                    % 模式)
    return 落了
