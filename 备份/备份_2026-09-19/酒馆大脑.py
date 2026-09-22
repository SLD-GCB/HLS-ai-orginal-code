#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆大脑.py — 把角色设定拼成提示词，再跟模型流式说话

**这个文件不 import Qt。** 它是纯 Python + `requests`，可以脱离界面直接单测
——协议解析这种地方是最需要反复单测的，绑上 Qt 就没法痛快测了。

三件事，从上到下：

    拼提示()   角色卡 + 会话设定 + 最近的消息 → (系统提示, 消息数组)
    规整角色() 把消息数组压成三家 API 都认的合法序列（**必踩的坑在这儿**）
    流式()     挑一个协议、发出去、逐段吐出文本，随时可打断

`拼提示` 在 `local` 协议下会**在系统提示最末尾**多补一段扮演规矩（见
`本地规矩`）——本地小模型没人帮忙兜着，人格保持全靠提示词里这几条。

**三家协议共用的只有 SSE 解析**（`_读SSE`）。请求体是各写各的——Anthropic
要顶层 `system` 且必须有 `max_tokens`，Gemini 的 role 是 `user`/`model` 且
参数名是 `topP`/`maxOutputTokens`。别图省事想着"一套 body 打三家"。
"""

import json
import os
import queue
import threading

import requests

import 酒馆本地

__all__ = ['已打断', '错配置', '错模型', '停旗', '拼提示', '规整角色',
           '从尾部截', '流式', '跑一轮', '发工具', '拼工具碎片', '收碎片',
           '原生工具协议',
           '列模型', '校验配置', '协议们',
           '默认上下文条数', '默认字数上限', '默认超时', '列模型超时',
           '上下文设置', '本地协议',
           '开场占位']

协议们 = ('openai', 'anthropic', 'gemini', 'local')

默认上下文条数 = 20
#: 上下文按**字符数**再封一次顶。只按条数封是不够的：一条超长消息就能顶爆
#: 上下文窗口，而条数看不出来这件事。
默认字数上限 = 24000
#: `(连接超时, 读超时)`。**读超时要给足**——本地小模型首字等个几十秒很正常，
#: 给短了会在模型还没开口的时候就把连接掐了，报出来的错还完全看不出真因。
默认超时 = (10, 300)
#: 生成器等下一块时最多等多久，到点醒一次看一眼停旗。
#:
#: ⚠ **这个数跟"模型多久吐下一个字"没有任何关系。** 等不到块不算错，
#: 回去接着等就是了——它只有一个作用：**决定用户点了停止之后，最长多久
#: 生效**。0.1 秒是人感觉不到、又不会把 CPU 转出火星的数。
等块秒 = 0.1

#: 首条是 `角色` 时补在前面那条 user 消息的内容。
#:
#: 为什么要补这么一条：**Anthropic 要求消息数组的第一条必须是 user**，
#: Gemini 也不认 assistant 打头。而我们的数据天然会这样——**开场白**就是一条
#: `说话人='角色'` 的消息，序号 1。
#:
#: ⚠ **开场白不能删。** 删掉的话模型不知道自己已经说过那句话，人格当场就崩。
#: 补一条占位 user 让它变成第二句，是三家通吃又不丢信息的做法。
开场占位 = '……'


class 已打断(Exception):
    """用户点了停止。**这是正常收场，不是故障**——界面不该为它弹红字。"""


class 错配置(Exception):
    """接口配置本身有问题（没地址、没模型、或者一套都没建）。"""


class 错模型(Exception):
    """请求发出去了，但对面不认（非 200、连接不上、超时）。"""


class 停旗(object):
    """
    停止信号。**就一个 `threading.Event`，不干别的。**

    ⚠ **真正让"停止"立刻生效的是旗子本身。** 这一点我一开始搞反过，以为
    "置旗没用，得把连接关掉才行"，实测才掰正：

      · `旗.is_set()` 是被 `流式` 的**取块循环**每 `等块秒` 看一次的，
        看一眼就走，**跟 socket 卡在哪儿完全无关**；
      · `响应.close()` **关不掉一次已经阻塞住的读**。Windows 上 `shutdown()`
        都唤醒不了另一条线程里阻塞的 `recv`（实测，见 `_读客`），何况
        `close()`。

    ⚠ **`set()` 只置旗子，绝不碰那条连接。** 曾经这里顺手调了一下
    `响应.close()`，理由是"帮那条被放弃的读线程早点结束"。去掉了，因为：

      · 它对打断**一点用都没有**（上面刚说过）；
      · 而它是在**调用方那条线程**上执行的。去掉它之后，`生成线` 那条
        原本要卡 30 秒的打断路径立刻变成 0.1 秒级——症状对得上，很像是
        `close()` 把调用方一起拖住了。**这条我没有单独验到底**（改完就好了，
        没有再做反证），但一笔"没用还可能有害"的账，没有留下的理由。

    被放弃的那条读线程不需要别人帮忙收尾：它认 `别塞` 事件，自己会随
    `with 响应:` 结束（见 `_读客`）。
    """

    __slots__ = ('_事',)

    def __init__(self):
        self._事 = threading.Event()

    def set(self):
        self._事.set()

    def is_set(self):
        return self._事.is_set()


# ── 拼提示 ──────────────────────────────────────────────────────────

def 上下文设置(设定):
    """
    会话的 `预设` → `(条数, 字数上限)`。

    ⚠ **抽出来是因为它有两个用户。** 聊天那边（`酒馆对话页._拼`）按它截，
    「上下文」那个窗口（`酒馆上下文页`）**拿它把哪几条会发出去画给用户看**。
    两处各写一份的话，界面说会发 20 条、实际发了 19 条——用户拿着界面去对，
    对不上；而这种不一致跑起来**一点症状都没有**，只能靠读代码发现。

    ⚠ **顺便把类型收干净。** `预设` 是手写 JSON，`上下文条数` 写成 `'20'`
    或者 `'abc'` 都是可能的。`从尾部截` 里要做 `总 + n > 字数上限` 这种
    比较，一个字符串进来就是 `TypeError`——而这条路上的异常会一路冒到
    "拼提示失败"，用户完全看不出是自己的预设写坏了。这儿统一成 int，
    写坏了就当没写（退回默认），和 `读JSON` 那条"坏配置不该让角色打不开"
    是一个道理。
    """
    设定 = 设定 if isinstance(设定, dict) else {}
    条数 = 默认上下文条数
    try:
        条数 = max(1, int(设定.get('上下文条数') or 条数))
    except (TypeError, ValueError):
        条数 = 默认上下文条数
    上限 = 默认字数上限
    try:
        上限 = max(1, int(设定.get('字数上限') or 上限))
    except (TypeError, ValueError):
        上限 = 默认字数上限
    return 条数, 上限


def 从尾部截(消息们, 字数上限=默认字数上限):
    """
    从**最新**的往回攒，攒到超过上限为止，返回正序（老 → 新）的一段。

    只按条数封顶是不够的：一条几万字的设定楼就能把上下文顶爆，而条数上
    完全看不出来。所以条数之外再按字符数封一道。

    攒的时候**至少留一条**——消息再长也得有一条，全截没了等于没上下文。
    """
    攒, 总 = [], 0
    for m in reversed(消息们):
        n = len(m.内容 or '')
        if 攒 and 总 + n > 字数上限:
            break
        攒.append(m)
        总 += n
    return list(reversed(攒))


def 规整角色(提示):
    """
    `[{'role':..., 'content':...}]` → 三家 API 都认的合法序列。

    ⚠ **这一步不能省。** Anthropic 和 Gemini 都要求 user / assistant
    **严格交替**，且 Anthropic 要求第一条必须是 `user`。而我们的数据天然
    会违反：开场白是 `角色` 说的（首条就是 assistant），重新生成之后也会
    连着两条 assistant。**不规整就是 400，而报错信息完全不会告诉你为什么。**

    顺序是有讲究的，**不能换**：

        1. 先滤空 —— 空内容三家都不认（Anthropic 会报
           `text content blocks must be non-empty`）
        2. 再合并相邻同角色 —— **滤空必须在合并前面**。反过来的话，滤掉一条
           空串会把它两侧同角色的两条变成相邻，凭空造出一对违规
        3. 最后补首条 —— 首条不是 user 就在前面插一条 `开场占位`

    第 3 步是**插一条，不是删原来的**。删掉的话开场白就没了，模型不知道自己
    已经说过那句话。插一条占位 user 让它退到第二句，信息一点不丢。
    """
    压 = []
    for m in 提示:
        角色 = m.get('role')
        if 角色 not in ('user', 'assistant'):
            continue
        文 = (m.get('content') or '').strip()
        if not 文:
            continue                       # ① 滤空
        if 压 and 压[-1]['role'] == 角色:
            压[-1]['content'] += '\n\n' + 文    # ② 合并相邻同角色
        else:
            压.append({'role': 角色, 'content': 文})

    if not 压:
        # 历史里一条能用的都没有（比如新会话、开场白也是空的）。
        # 给一条占位，让模型有话说——比发一个空数组出去换一个 400 强。
        return [{'role': 'user', 'content': 开场占位}]
    if 压[0]['role'] != 'user':
        压.insert(0, {'role': 'user', 'content': 开场占位})   # ③ 补首条
    return 压


#: 本地小模型的扮演规矩。`%s` 是角色名。
#:
#: ⚠ **故意不用 markdown 加粗。** `**` 对模型是纯噪音——实测过，小模型会
#: 把它当成"要输出的格式"，或者干脆吞掉。
#:
#: ⚠ **故意不提"本地""模型"这些词。** 第 3 条正在禁止它自称 AI，前文再提
#: 一句"本地模型"等于自相矛盾、勾着它往那条路上跑。
#:
#: 旁白两种写法都认（全角圆括号 + 星号），**不在提示里强行统一**——用户
#: 怎么写都行，模型看多了自己会跟。
本地规矩 = (
    '扮演规则（必须一直遵守）：\n'
    '1. 始终以%s的身份和口吻说话，不要跳出角色。\n'
    '2. 不要替用户写他说的话，也不要替用户做决定、描写用户的行为。\n'
    '3. 不要自称 AI、语言模型、助手或程序。\n'
    '4. 动作、神态、场景这类旁白，用圆括号包起来，比如（她转过头）'
    '或 *她转过头*；说的话直接写。\n'
    '5. 按角色的性格和当前情景来回应，别写客套话，也别总结或复述前文。\n'
    '6. 一次回复别太长，几句话把意思说到就行。'
)


def 本地协议(套):
    """
    这套接口配置是不是走本地 GGUF。`None`（一套都没配）当成不是。

    ⚠ **抽出来是因为它有两个调用方**（`酒馆对话页`、`酒馆上下文页`）。
    两处各写一遍 `套 is not None and 套.协议 == 'local'` 的话，迟早有一处
    漏掉 `None` 判断——`酒馆接口.取或默认()` 在一套接口都没建的时候回的就是
    `None`，漏了当场 AttributeError，而且报错点离这个判断隔着好几层。
    """
    return 套 is not None and (套.协议 or '') == 'local'


def 拼提示(角色, 会话, 消息们, 设定=None, 本地=False):
    """
    角色卡 + 会话设定 + 最近的消息 → `(系统提示, 消息数组)`。

    回来的消息数组**已经规整过**，四个协议直接能用。

    `说话人='系统'` 的消息**折进系统提示**，不当成 message —— 三家 HTTP
    协议里只有 OpenAI 有 `system` 这个 role，另外两家会把一条 role 叫
    "system" 的消息当成非法输入。

    `本地=True`（这套接口的协议是 `local`）时才在**系统提示最末尾**补一段
    扮演规则，见 `本地规矩`。云端那几家自带较强的人格保持，不掺这一脚。

    ══ 系统块的前后顺序，以及为什么这么排 ══════════════════════════════

        ① 身份     「你就是<名字>。」「角色简介：…」  ← 先立人格，名字永远在
        ② 人设     性格、说话方式                    ← 人格主体
        ③ 示例对话 带「这是谁说的」的抬头             ← 风格示范
        ④ 系统消息 `说话人='系统'` 折进来的            ← 位置原样不动
        ⑤ 会话系统提示 `设定['系统提示']`              ← 位置原样不动
        ⑥ 开场白说明 「你已经说过这句：…」
        ⑦ 扮演规则（**仅 local**）                    ← 压在整个提示的最末尾

    ①②③ 是「这个角色是谁」，放最前，让模型先建立人格再谈别的。

    ④⑤ 是用户自己写的东西，**位置一个字都不挪**：`酒馆上下文页` 那个
    label 的承诺就是"拼在角色设定后面"（见那边的文案），挪了就改了用户
    原来的意思。

    ⑥ 紧贴生成前重申"你已经说过这句"，防模型把开场白再复述一遍。

    ⑦ 放**整个系统提示的最末尾**：小模型对**末尾**指令的服从度最高
    （recency），把最容易翻车的几条（替用户说话 / 自称 AI / 没旁白）
    压到最后，命中率最好。

    ⚠ ⑥ 和 ⑦ 不打架：⑥ 说的是**事实**（你已说过这句，而且它是角色真实
    说过的话），⑦ 说的是**行为约束**（怎么说话）。⑦ 里"不要照抄"针对的
    是 ③ 那批**示例**，开场白不属于示例。

    ⚠ **①②的修复对云端协议也生效，是有意的。** 老代码是
    `if 人设: … elif 简介: …`——人设一非空，名字和简介双双消失，用户喊
    「乌玛」模型根本不知道那是谁。这是 bug，不是"只给本地做的风格偏好"。
    """
    设定 = 设定 or {}
    系统块 = []
    名字 = (角色.名字 or '').strip() if 角色 is not None else ''

    if 角色 is not None:
        简介 = (角色.简介 or '').strip()
        人设 = (角色.人设 or '').strip()

        # ① 身份：名字永远在，简介不再被"人设非空"挤掉。
        身份 = ['你就是%s。' % 名字 if 名字 else '你正在扮演一个角色。']
        if 简介:
            身份.append('角色简介：%s' % 简介)
        系统块.append('\n'.join(身份))

        # ② 人设：紧跟在身份后面，是人格主体。
        if 人设:
            系统块.append(人设)

        # ③ 示例对话：抬头里点明"这些是谁说的"。老版本只丢一句原样示例
        # 加一段 `**…**` 元指令，小模型既不知道谁说的、又容易被那层加粗
        # 绕晕或者直接照抄。
        示例 = (角色.示例对话 or '').strip()
        if 示例:
            系统块.append(
                '以下是「%s」的说话示例（只用来体会语气和用词，'
                '不要照抄，也别当成已经发生过的对话）：\n%s'
                % (名字 or '这个角色', 示例))

    # ④ `说话人='系统'` 的消息折进系统提示。按序号顺序排，保持时间感。
    临时 = []
    普通 = []
    for m in 消息们:
        if (m.说话人 or '') == '系统':
            文 = (m.内容 or '').strip()
            if 文:
                临时.append(文)
        else:
            普通.append(m)
    系统块.extend(临时)

    # ⑤ 会话级系统提示：用户自己写的，位置保持在角色设定后面。
    额外 = (设定.get('系统提示') or '').strip()
    if 额外:
        系统块.append(额外)

    提示 = [{'role': 'user' if m.说话人 == '用户' else 'assistant',
             'content': m.内容 or ''}
            for m in 普通 if m.说话人 in ('用户', '角色')]

    # ⑥ 首条是 assistant 的话，把它挪进系统提示里说明"你已经说过这句"，
    # 再从消息数组里去掉 —— 这样补占位那条 user 就不是凭空多出来的。
    # （`规整角色` 也能处理，但那样子系统提示里就没有角色自己说过的话。）
    if 提示 and 提示[0]['role'] == 'assistant':
        头 = 提示.pop(0)['content'].strip()
        if 头:
            系统块.append('对话是由你先开口的。你已经说过这句：\n' + 头)

    # ⑦ 本地扮演规矩：压在整个系统提示的最末尾，见 docstring。
    # ⚠ 角色卡不在了（`角色 is None`）也照样加：卡片可能被删，但历史里
    # 还有角色说过的话，本地模型不拴着就会突然变回"通用助手"。
    if 本地:
        系统块.append(本地规矩 % (名字 or '你扮演的角色'))

    return '\n\n'.join(系统块), 规整角色(提示)


# ── 参数 ────────────────────────────────────────────────────────────

#: 会话 `预设` 里能覆盖的键。**存的是协议线上的原键名**（`temperature`
#: 之类），不存中文——省一层翻译表，也就省掉一类"词对不上"的 bug。
#: `top_k` / `repeat_penalty` 主要是给 local 用的，但对 HTTP 协议它们
#: 本来也是合法参数（OpenAI 兼容服务大多认），放进来没有副作用。
可覆盖参数 = ('temperature', 'top_p', 'max_tokens', 'top_k', 'repeat_penalty')

#: local 协议专属、**绝不能进 HTTP 请求体**的键。`_备openai` 是
#: `体.update(参数)` 整份塞的——一套配置从 local 改回 openai 时这些键
#: 还在 `采样参数` JSON 里，不滤掉就发给对端了：轻则 400，重则对面
#: 静默按错的参数跑。
本地专属参数 = ('n_ctx', 'n_gpu_layers')


def 合并参数(配置, 设定):
    """接口配置的 `采样参数`，被会话 `预设` 里的同名键覆盖。"""
    出 = dict(配置.参数())
    for 键 in 可覆盖参数:
        if 设定 and 设定.get(键) is not None:
            出[键] = 设定[键]
    return 出


def 校验配置(配置):
    """
    这一套配置能不能发得出去。返回**缺什么**（空列表 = 齐了）。

    界面在保存时和发送前都调一下——总比发出了收到一个 400 强。
    """
    缺 = []
    if 配置 is None:
        return ['一套接口配置都没建']
    if (配置.协议 or '') not in 协议们:
        缺.append('协议（只能是 %s）' % '、'.join(协议们))
        return 缺
    if 配置.协议 == 'local':
        # local：地址/密钥没意义，「模型」是 GGUF 文件名（或绝对路径）
        if not (配置.模型 or '').strip():
            缺.append('模型（GGUF 文件名）')
        elif not os.path.isfile(酒馆本地.定位(配置.模型)):
            缺.append('模型文件（%s 不在，去「本地模型」下载）' % 配置.模型.strip())
        if not 酒馆本地.可用():
            缺.append('本地引擎（pip install llama-cpp-python）')
        return 缺
    if not (配置.地址 or '').strip():
        缺.append('地址')
    if not (配置.模型 or '').strip():
        缺.append('模型')
    return 缺


# ── SSE 读取 ────────────────────────────────────────────────────────

def _切SSE(响应):
    """
    从一条流式响应里逐条吐出 `data:` 后面的正文。**只管切，不管停。**

    ⚠ **故意不用 `响应.iter_lines(decode_unicode=True)`。**

    那个是按 `响应.encoding` 解码的，而流式响应**通常不带 charset**——
    requests 猜不到就去猜 ISO-8859-1，**中文整个变乱码**。实测（假端点，
    `Content-Type: text/event-stream` 不带 charset，吐「你好，世界🎭」）：

        iter_lines(decode_unicode=True)  →  'ä½ å¥½ï¼ä¸çð®'
        自己切字节 + 显式 utf-8            →  '你好，世界🎭'

    三家协议的流式响应都不带 charset，所以这条不是洁癖，是**必须**的。
    不这么写，全项目的模型输出就全是乱码，而且是那种"看起来像模型抽风"的乱码。

    ⚠ **`chunk_size` 必须是 `1`。** 这一条也是实测出来的：

        chunk_size=None   四块到达时刻：1.60                 ← 憋到 EOF 才一起给
        chunk_size=1024   四块到达时刻：1.60                 ← 一样，read(1024) 也等满
        chunk_size=1      四块到达时刻：0.00 0.40 0.80 1.20  ← 真流式

    `None` 看着像「来多少读多少」，实际落进 urllib3 是 `fp.read(None)`，
    那是**读到 EOF**——整段生成跑完了才一次性给你，打字机效果全没了。
    1024 也一样，`read(1024)` 会一直等到攒够 1024 字节或者流结束。

    ⚠ **也故意不用 `iter_lines`。** 它内部拿 `bytes.splitlines()` 切行，而
    `bytes.splitlines` 会把 `\\x85` 当换行符——`\\x85` 完全可以是 UTF-8 的
    续字节（续字节范围是 0x80–0xBF），**中文会被从中间劈开**。
    自己按 `\\n` 切就没这个问题：0x0A 在 UTF-8 里只可能是真的换行。
    """
    缓 = b''
    for 块 in 响应.iter_content(chunk_size=1):
        缓 += 块
        while b'\n' in 缓:
            行, 缓 = 缓.split(b'\n', 1)
            行 = 行.strip()
            # 空行是事件分隔符，`event:` / `id:` / `:` 开头的注释行和心跳都跳过
            if not 行.startswith(b'data:'):
                continue
            正文 = 行[5:].strip().decode('utf-8', 'replace')
            if 正文 == '[DONE]':
                return
            if 正文:
                yield 正文


class _读客(threading.Thread):
    """
    专门读流的一条线程。**它从不决定停，只往队列里塞。**

        str  → 一条 SSE 载荷
        None → 流正常读完（`[DONE]` 或对端关流）
        异常 → 读的时候炸了

    取的那头（`流式`）只认这三种，别的不管。

    ══ 为什么非得另开一条线程来读 ══════════════════════════════════════

    **这不是设计偏好，是 Windows 的硬约束。** 三条路我都实测堵死了：

      甲、裸 socket。A 线程阻塞在 `recv` 上，B 线程 `shutdown(SHUT_RDWR)`：
          **A 永远不醒**（`join(3)` 直接超时）。Linux 上会醒，**Windows 上
          不会**——这条是根子。

      乙、顺着 `响应` 往里掏套接字，`shutdown` / `响应.close()` 一起上：
          **没用**，实测硬等了 30 秒，等对端发下一坨数据才回来。

      丙、把套接字超时调短，指望超时回来看看旗子：
          urllib3 把读超时当**致命错**（`Read timed out.`），连接当场作废。
          **超时之后续读不成立**。

    三条全堵死，而且**任何同步 HTTP 客户端在 Windows 上都这样**（httpx 也
    一样，这是操作系统的 `recv` 不可取消，不是 requests 的锅）。

    所以"取消"只能做在**我们自己这一层**：读留在它那条线程里自己阻塞着，
    生成那边用**带超时的队列取**——超时不是错误，只是"回去看一眼旗子"的
    机会。用户一点停止，最长 `等块秒` 就抛出来，**跟 socket 卡在哪儿无关**。

    代价：被放弃的那条读线程要等到对端再发数据、或者读超时（300 秒）到顶，
    才自己结束，中途认 `别塞` 事件喊停。它不发信号、不碰存储、是 daemon
    ——留着也无害。这是这场交易里唯一付得起的那份钱。
    """

    def __init__(self, 响应, 队, 别塞):
        super().__init__(daemon=True, name='酒馆读流')
        self._响应 = 响应
        self._队 = 队
        self._别塞 = 别塞

    def run(self):
        try:
            for 载荷 in _切SSE(self._响应):
                if self._别塞.is_set():
                    return          # 这趟已经被放弃了，别再往队里堆东西
                self._队.put(载荷)
        except Exception as 错:
            self._队.put(错)         # 异常也是结果，让取的那头去判
        else:
            self._队.put(None)       # 正常读完


def _查头(头):
    """
    请求头只认 latin-1，检查一遍再发。

    **密钥或者附加头里混进一个中文字符**（从网页上复制密钥的时候经常带上
    全角字符、中文引号或者全角空格）的话，`requests` 抛的是

        UnicodeEncodeError: 'latin-1' codec can't encode character '\\u6d4b'

    这句话完全看不出「你密钥里有个中文」。在这儿当场换成一句人话，
    并且把**是哪个头、第几个字符、那个字符是什么**都点出来。
    """
    for 名, 值 in (头 or {}).items():
        for i, 字 in enumerate(str(值)):
            if ord(字) > 255:
                raise 错配置(
                    '请求头「%s」的第 %d 个字符是 %r —— HTTP 头只认 latin-1，'
                    '装不下中文。多半是从网页上复制密钥时带进来的全角字符、'
                    '中文引号或者全角空格。去「接口」里把那套配置的密钥/'
                    '附加头打开，把那个字符删掉。'
                    % (名, i + 1, 字))


def _查状态(响应):
    """
    非 200 就把**响应体一起塞进异常**。

    这是排错时唯一有用的东西。「HTTP 401」和
    「HTTP 401: {'error': {'message': 'invalid api key'}}」差着十万八千里，
    而后者对面其实已经告诉我们了，不读出来纯属浪费。
    """
    if 响应.status_code == 200:
        return
    身 = ''
    try:
        身 = (响应.text or '').strip()[:800]
    except Exception:
        身 = '(响应体读不出来)'
    raise 错模型('HTTP %d %s\n%s' % (响应.status_code, 响应.reason, 身))


# ── 三个协议 ────────────────────────────────────────────────────────
#
# 每个 `_备*` 只做一件事：把 (配置, 系统, 消息们, 参数, 地址) 拼成
# `{'地址':…, '头':…, '体':…, '取段':…}`。发起和读流是共用的（`流式`）。
# `取段` 负责从一帧里挖出文本，挖不到就返回 None（比如只带 usage 的收尾帧）。

def _头们(配置):
    """
    一套配置该带哪些请求头。**聊天和「发现模型」都从这一个口取。**

    密钥按协议放在**不同的头**上：OpenAI 系是 `Authorization: Bearer …`，
    Anthropic 是 `x-api-key`，Gemini 是 `x-goog-api-key`；只有 Anthropic
    还额外要一个 `anthropic-version`。`附加头` 最后合并，优先级最高。

    ⚠ **归拢到一处不是洁癖。** 这三种写法原来各散在 `_备*` 里，写「发现模型」
    的时候照抄一遍就是**四处**各自记着「密钥放哪个头」。哪天某家改了头名，
    改三处漏一处，症状是**聊天好好的、就是发现不了模型**——两件事在界面上
    隔着一个对话框，那种最难往这儿想。
    """
    头 = {'Content-Type': 'application/json'}
    if 配置.密钥:
        if 配置.协议 == 'anthropic':
            头['x-api-key'] = 配置.密钥
        elif 配置.协议 == 'gemini':
            头['x-goog-api-key'] = 配置.密钥
        else:
            头['Authorization'] = 'Bearer ' + 配置.密钥
    if 配置.协议 == 'anthropic':
        头['anthropic-version'] = '2023-06-01'
    头.update(配置.头们())
    return 头


def _取openai(载):
    try:
        return 载['choices'][0]['delta'].get('content') or None
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def _备openai(配置, 系统, 消息们, 参数, 地址):
    头 = _头们(配置)
    消息 = ([{'role': 'system', 'content': 系统}] if 系统 else []) + list(消息们)
    体 = {'model': 配置.模型, 'messages': 消息, 'stream': True}
    体.update(参数)
    # ⚠ 地址不猜。用户填 `http://127.0.0.1:11434/v1` 就拼成
    # `…/v1/chat/completions`，填到主机就拼成 `…/chat/completions`。
    # 猜（比如自动补 /v1）会让"我明明填对了"变成最难查的那种问题。
    return {'地址': 地址.rstrip('/') + '/chat/completions',
            '头': 头, '体': 体, '取段': _取openai}


def _取anthropic(载):
    if 载.get('type') == 'content_block_delta':
        段 = 载.get('delta') or {}
        # 只有 text_delta 带字；thinking_delta 之类的不往界面上放
        return 段.get('text') or None
    return None


def _备anthropic(配置, 系统, 消息们, 参数, 地址):
    头 = _头们(配置)
    参数 = dict(参数)
    体 = {'model': 配置.模型, 'messages': list(消息们), 'stream': True,
          # ⚠ `max_tokens` 在 Anthropic 是**必填**的，不给直接 400。
          'max_tokens': int(参数.pop('max_tokens', 4096) or 4096)}
    # ⚠ 系统提示走**顶层 `system`**，不是一条 role=system 的消息。
    # 而且**空字符串会被拒**（`text content blocks must be non-empty`），
    # 所以只有非空才放这个字段。
    if 系统:
        体['system'] = 系统
    体.update(参数)
    return {'地址': 地址.rstrip('/') + '/messages',
            '头': 头, '体': 体, '取段': _取anthropic}


def _取gemini(载):
    try:
        return 载['candidates'][0]['content']['parts'][0]['text'] or None
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def _备gemini(配置, 系统, 消息们, 参数, 地址):
    头 = _头们(配置)
    参数 = dict(参数)
    配置段 = {}
    # Gemini 的参数名跟另外两家不一样，这里转一次。
    if 参数.get('temperature') is not None:
        配置段['temperature'] = 参数.pop('temperature')
    if 参数.get('top_p') is not None:
        配置段['topP'] = 参数.pop('top_p')
    if 参数.get('max_tokens') is not None:
        配置段['maxOutputTokens'] = 参数.pop('max_tokens')
    体 = {
        # ⚠ role 是 `user` / `model`，**不是 `assistant`**。
        'contents': [{'role': 'model' if m['role'] == 'assistant' else 'user',
                      'parts': [{'text': m['content']}]} for m in 消息们],
        'generationConfig': 配置段,
    }
    if 系统:
        体['systemInstruction'] = {'parts': [{'text': 系统}]}
    return {'地址': '%s/models/%s:streamGenerateContent?alt=sse'
                   % (地址.rstrip('/'), 配置.模型),
            '头': 头, '体': 体, '取段': _取gemini}


_备 = {'openai': _备openai, 'anthropic': _备anthropic, 'gemini': _备gemini}


# ── 看它有哪些模型 ──────────────────────────────────────────────────
#
# 「发现模型」用：`GET {地址}/models`。
#
# 三家的**地址拼法是一样的**——各家 `_备*` 里的聊天地址都是在用户填的那层
# 地址后面接东西（见 `_备openai` 那条「地址不猜」），所以清单也在同一层后面
# 接 `/models`（那一层没有才退站点根，见 `_要清单`）。差别只在**回来的形状**，
# 所以一家一个 `_名*`。

def _名openai(载):
    """OpenAI 系：`{"data": [{"id": "qwen2.5:7b"}, …]}`。"""
    return [str(条['id']) for 条 in (载.get('data') or [])
            if isinstance(条, dict) and 条.get('id')]


def _名anthropic(载):
    """
    Anthropic：`{"data": [{"id": "claude-sonnet-5", …}, …]}`。

    **跟 OpenAI 一个形状**，所以直接借它的。哪天不一样了在这儿改，
    外面不用动。
    """
    return _名openai(载)


def _名gemini(载):
    """
    Gemini：`{"models": [{"name": "models/gemini-2.5-pro", …}, …]}`。

    两处跟别家不一样：

      · 名字带 `models/` 前缀，**得切掉**——`_备gemini` 拼聊天地址时是自己
        再接一个 `models/` 的，带着前缀填进「模型」那格会拼成
        `…/models/models/gemini-…`；
      · 它会把自己能干的活列在 `supportedGenerationMethods` 里，
        **只有带 `generateContent` 的才是拿来聊天的**。embedding 那些
        列出来，用户选中了照样发不出去。
    """
    出 = []
    for 条 in (载.get('models') or []):
        if not isinstance(条, dict):
            continue
        会 = 条.get('supportedGenerationMethods')
        if 会 and 'generateContent' not in 会:
            continue
        名 = str(条.get('name') or '')
        if 名.startswith('models/'):
            名 = 名[len('models/'):]
        if 名:
            出.append(名)
    return 出


_取名 = {'openai': _名openai, 'anthropic': _名anthropic, 'gemini': _名gemini}


def _站根(地址):
    """`https://api.deepseek.com/anthropic` → `https://api.deepseek.com`。"""
    尾 = 地址.find('://')
    起 = 尾 + 3 if 尾 >= 0 else 0
    斜 = 地址.find('/', 起)
    return 地址[:斜] if 斜 >= 0 else 地址

#: 问模型清单的超时。**比 `默认超时` 短得多。** 这是一次点按钮的小请求：
#: 读超时也给 300 秒的话，地址填错时用户要干等五分钟才看到报错。
#: 本地服务列个清单是毫秒级的，30 秒已经宽得离谱。
列模型超时 = (10, 30)


def _要清单(地址, 头):
    """
    去要一份模型清单，返回 `(回来的JSON, 真正管用的那个网址)`。

    ⚠ **原网址 404 就退到站点根再问一次。** 这一退不是拍脑袋，是因为各家
    「地址」填到哪一层并不统一，而模型清单**只有一个地方有**：

      · OpenAI 系填的是带版本的那层（`https://api.deepseek.com/v1`），
        清单就在同一层，`{地址}/models` 一次就中；
      · Anthropic 系填的是**不带版本的那层**——DeepSeek 官方文档给的
        `base_url` 就是 `https://api.deepseek.com/anthropic`（SDK 自己往后面
        补 `/v1/messages`）。可这一层底下**没有清单接口**：实测
        `…/anthropic/models`、`…/anthropic/v1/models` 都是 404，而同一台
        机器根上的 `https://api.deepseek.com/models` 是 200。

    ⚠ **聊天那条路一个字符都没动。** 用户填的地址该怎么拼还怎么拼（见
    `_备anthropic` 那条「地址不猜」），这儿退的只是「问清单」这一下。
    所以它不会把「我明明填对了」变成最难查的那种问题——恰恰相反，
    「我地址填对了、聊天是通的，就是按钮点了报 404」那才是。

    **只在 404 时退**（别的错码是真错，换个路径问也没用），**而且只退一次**。
    """
    基 = 地址.rstrip('/')
    候选 = [基 + '/models']
    根 = _站根(基).rstrip('/')
    if 根 != 基:
        候选.append(根 + '/models')

    for i, 网 in enumerate(候选):
        try:
            响应 = requests.get(网, headers=头, timeout=列模型超时)
        except requests.exceptions.RequestException as 错:
            raise 错模型('连不上 %s：%s' % (网, 错))

        with 响应:
            if 响应.status_code == 404 and i + 1 < len(候选):
                continue            # 这一层没有，退一级再问
            if 响应.status_code == 404:
                raise 错模型(
                    '%s 回了 404——这个地址底下没有模型清单接口。%s\n\n'
                    '**这不影响聊天**，两边不是同一条路径，发消息照发。\n'
                    '模型名那格直接手打就行，服务商文档里「模型 & 价格」'
                    '那一页写着叫什么。'
                    % (候选[0],
                       ('\n（退到站点根 %s 又问了一次，一样是 404。）' % 候选[-1])
                       if len(候选) > 1 else ''))
            _查状态(响应)
            try:
                载 = 响应.json()
            except ValueError as 错:
                raise 错模型('%s 答上了，但回来的不是 JSON：%s\n'
                            '（多半是地址填到了带网页的根目录，或者中间隔了代理）'
                            % (网, 错))

        return (载 if isinstance(载, dict) else {}), 网

    raise 错模型('没问出清单来。')      # 上面每种情况都 raise 过了，走不到这儿


def 列模型(配置):
    """
    问这套接口「你有哪些模型」，返回名字列表（**去过重、排过序**）。

    ⚠ **不走 `校验配置`。** 那个会连 `模型` 一起要求——可这个功能恰恰是
    「模型那格还空着，我不知道该填什么」的时候用的。拿它挡在前面，等于把
    唯一有用的那个场合挡掉了。这儿只要求协议认得、地址非空。

    ⚠ **不要在主线程里调。** 它是一条阻塞的 `requests.get`，地址不通要
    等满超时。界面那边是丢给一条后台线程去跑的，见 `酒馆接口页.找模型的`。
    """
    协议 = (配置.协议 or '').strip()
    if 协议 not in 协议们:
        raise 错配置('协议得是 %s 里的一个，这套现在写的是 `%s`。'
                   % ('、'.join(协议们), 协议 or '空'))
    if 协议 == 'local':
        # 本地没有"问一问"这回事——就是数一遍模型目录。磁盘读，毫秒级；
        # 调用方本来就在后台线程里跑（`酒馆接口页.找模型的`），不用特殊照顾。
        return [名 for 名, _大 in 酒馆本地.列本地()]

    地址 = (配置.地址 or '').strip()
    if not 地址:
        raise 错配置('地址还空着——得先知道该问谁。')

    头 = _头们(配置)
    _查头(头)                       # 密钥里混进中文在这儿就报，跟聊天同一条路
    载, 网址 = _要清单(地址, 头)

    名们 = _取名[协议](载)
    if not 名们:
        raise 错模型('%s 通了，但里头一个模型都没列出来。\n'
                    '如果是本地服务，先把模型拉下来再看；\n'
                    '如果是 `/models` 这个路径不对，那把「地址」改成'
                    '服务商文档里给的那个前缀。' % 网址)
    # 去重 + 排序：有些服务会把同一个模型列好几遍，界面上就是一串重影
    return sorted(set(名们), key=lambda 名: 名.lower())


# ── 出门（带工具的那条：给编程助手用）────────────────────────────────
#
# ⚠ **`流式` 一个字都没动。** 它是聊天链路天天在跑的东西，而这一层新增的
# 东西只服务编程助手 —— 跟"新行为限制在目标链路上"一个道理。
#
# 两个函数的区别只有一处，但很关键：
#
#   流式    逐段 yield 文本，**只读 `delta.content`** —— 工具调用会被丢掉
#   跑一轮  收完再回，**正文和工具碎片一起收**（三家碎片长得都不一样，见下）
#
# ⚠ **工具调用不是以文本回来的**（原生模式下），而且**三家回来的样子完全
# 不一样**，所以底下有三个各自独立的收集器，谁认得哪一帧谁收：
#
#   openai     `delta.tool_calls` 里一连串**碎片**，按 `index` 拼：
#                {"delta":{"tool_calls":[{"index":0,"id":"call_1",
#                              "function":{"name":"read_file","arguments":""}}]}}
#                {"delta":{"tool_calls":[{"index":0,
#                              "function":{"arguments":"{\"path\":"}}]}}
#                {"delta":{"tool_calls":[{"index":0,
#                              "function":{"arguments":"\"a.py\"}"}}]}}
#                {"delta":{},"finish_reason":"tool_calls"}
#              不拼就是拿到一串半截 JSON。有些服务（vLLM 之类）可能一次给全，
#              但**碎片才是规范**，按碎片拼的实现两种都能吃。
#
#   anthropic  **分两段**：`content_block_start` 里给名字和 id，
#              `content_block_delta` 的 `input_json_delta` 一段段给参数：
#                {"type":"content_block_start","index":2,
#                 "content_block":{"type":"tool_use","id":"call_00_x",
#                                  "name":"list_dir","input":{}}}
#                {"type":"content_block_delta","index":2,
#                 "delta":{"type":"input_json_delta","partial_json":"{\"path\":"}}
#              ⚠ 它的 `index` 是**内容块号**（思维链块、正文块各占一个号），
#              所以工具块常常不从 0 起 —— 拿它当槽号没问题，但**别假设下标
#              是连续的**。
#
#   gemini     `candidates[0].content.parts[*].functionCall`，**一次给全、
#              本来就是 dict**，没有碎片这回事。
#
# `发工具` 是反方向的那一半：把上面这套（OpenAI 形状的）定义转成各家线上
# 要的形状。**收和发必须成对改** —— 只改一边的症状是"能发出去、收不回来"，
# 或者反过来，两种都很难从界面上看出来。

def _函体(条):
    """从一条 OpenAI 形状的定义里抠出 `{name, description, parameters}`。抠不出回 `None`。"""
    try:
        函 = 条.get('function') or {}
        名 = str(函.get('name') or '').strip()
        if not 名:
            return None
        参 = 函.get('parameters')
        return {'name': 名,
                'description': str(函.get('description') or ''),
                'parameters': 参 if isinstance(参, dict)
                              else {'type': 'object', 'properties': {}}}
    except (AttributeError, TypeError):
        return None


def 发工具(协议, 工具):
    """
    把我们自己的工具定义转成**这套协议线上要的形状**。转不出来回 `None`。

    ⚠ **三家形状各不相同，一家一套。** 我们内部（`酒馆工具.定义们`）用的是
    OpenAI 那一套，另外两家只是把同一个东西**换个壳**：

        openai / local  [{'type':'function','function':{name,description,parameters}}]
        anthropic       [{'name','description','input_schema'}]
                                       ↑ 连键名都不一样：`parameters` → `input_schema`
        gemini          [{'functionDeclarations': [{name,description,parameters}]}]
                                       ↑ 整个清单**套在一层里**，不是一家一条

    转不动（形状不对 / 没名字）的那一条**跳过**，不抛异常 —— 少一个工具是
    能跑的任务，抛异常是整个请求发不出去。

    ⚠ **这个函数和 `收碎片` 是成对的。** 只改一边的症状是"模型收到了工具
    却永远调不回来"（或者反过来），两种都不好从界面上看出来。
    """
    if not 工具:
        return None
    if 协议 == 'anthropic':
        出 = []
        for 条 in 工具:
            函 = _函体(条)
            if 函:
                出.append({'name': 函['name'],
                          'description': 函['description'],
                          'input_schema': 函['parameters']})
        return 出 or None
    if 协议 == 'gemini':
        声 = []
        for 条 in 工具:
            函 = _函体(条)
            if 函:
                声.append(函)
        return [{'functionDeclarations': 声}] if 声 else None
    try:
        return list(工具)                 # openai / local：本来就是这套形状
    except TypeError:
        return None


def _停因(帧):
    """
    这一帧带没带"为什么停"。三家写法不一样，没有就回 `''`。

    ⚠ **这个值必须往上传。** 它是"模型一个字都没输出"时**唯一能分辨原因**的
    证据：`max_tokens`/`length` = 被输出上限截断（思维链吃光了额度）；
    `end_turn`/`stop` = 模型自己决定不说了。两种情况该让用户做的事完全不同
    （一个去调「单步输出」，一个去换模型），而 `正文` 是空的时候，除了它就
    再没有别的线索了。早先是直接丢掉的。
    """
    try:
        if 帧.get('type') == 'message_delta':
            return str((帧.get('delta') or {}).get('stop_reason') or '')
    except AttributeError:
        pass
    try:
        return str(帧['choices'][0].get('finish_reason') or '')
    except (KeyError, IndexError, TypeError, AttributeError):
        return ''


def _思维长(帧):
    """
    这一帧带多少字的思维链。**只数长度，不留内容。**

    ⚠ 留内容是有代价的：思维链动辄几千字，塞进转录本就是把预算白白吃掉
    —— 而它本来就**不该进 `正文`**（那是给人看的）。但对"什么都没输出"这种
    故障来说，"它想了多久"恰恰是关键线索，所以只把长度带上去，够用了。
    """
    try:
        if 帧.get('type') == 'content_block_delta':
            段 = 帧.get('delta') or {}
            if 段.get('type') != 'thinking_delta':
                return 0
            return len(段.get('thinking') or '')
    except AttributeError:
        return 0
    # OpenAI 系（含 DeepSeek 的 openai 兼容口）思维链在 `reasoning_content`
    try:
        return len(帧['choices'][0]['delta'].get('reasoning_content') or '')
    except (KeyError, IndexError, TypeError, AttributeError):
        return 0


def 拼工具碎片(碎片):
    """
    `{槽: {...}}` 的累积状态 → `解工具调用` 认的那种 `tool_calls` 列表。

    `arguments` 留下来的是**拼好的 JSON 字符串**，交给 `酒馆本地.解工具调用`
    去 parse —— 那边已经有三层修复（去双括号 / 补括号 / 救换行），
    那些坑对云端模型一样存在，没必要在这儿再写一遍。

    ⚠ **槽号可能是 `int` 也可能是 `str`**（openai / anthropic 用数字下标，
    gemini 用 `'g0'` 这种）。Python 3 里 `sorted` 拿混合类型比大小**直接抛
    `TypeError`** —— 那会把"模型明明调对了工具"变成一句看不懂的报错。所以
    排序键自己给：数字在前、按数值排，字符串在后、按字面排。
    """
    def 序(槽):
        return (0, 槽, '') if isinstance(槽, int) else (1, 0, str(槽))

    出 = []
    for 槽 in sorted(碎片, key=序):
        条 = 碎片[槽]
        名 = 条.get('name') or ''
        参 = 条.get('arguments') or ''
        if not 名 and not 参:
            continue
        出.append({'id': 条.get('id') or '',
                   'type': 'function',
                   'function': {'name': 名, 'arguments': 参}})
    return 出


def 收碎片(碎片, 帧):
    """
    把一帧里的工具碎片并进累积状态。回**这一帧有没有工具碎片**。

    三个收集器各认各的形状，互不干扰（见上面那段"三家样子不一样"）。

    ⚠ **返回 `True` 会让 `跑一轮` `continue` 掉这一帧的正文提取。** 所以三个
    收集器都必须是"不是我的形状就回假"，**一个字符的正文都不能吞** ——
    anthropic 的正文（`text_delta`）和思维链（`thinking_delta`）跟工具碎片
    共用同一个事件名，这是最容易吞掉正文的地方。
    """
    有 = _收openai(碎片, 帧)
    有 = _收anthropic(碎片, 帧) or 有
    有 = _收gemini(碎片, 帧) or 有
    return 有


def _槽(碎片, 槽):
    """按槽号取（没有就建）一个累积位。"""
    return 碎片.setdefault(槽, {'id': '', 'name': '', 'arguments': ''})


def _收openai(碎片, 帧):
    """
    OpenAI 系：`delta.tool_calls`。

    ⚠ **按 `index` 分槽，不是按顺序追加。** 模型可能并行调多个工具，
    碎片是交错的：第 0 个的第二个碎片和第 1 个的第一个碎片会前后脚来。
    按顺序追加的话两个调用的参数会串成一团。
    """
    try:
        条们 = 帧['choices'][0]['delta'].get('tool_calls')
    except (KeyError, IndexError, TypeError, AttributeError):
        条们 = None
    有 = False
    for 条 in (条们 or []):
        if not isinstance(条, dict):
            continue
        槽 = 条.get('index')
        try:
            槽 = 0 if 槽 is None else int(槽)
        except (TypeError, ValueError):
            槽 = 0
        现 = _槽(碎片, 槽)
        if 条.get('id'):
            现['id'] = 条['id']
        函 = 条.get('function') or {}
        if 函.get('name'):
            现['name'] += 函['name']      # 名字也可能是碎片（少见，但规范允许）
        if 函.get('arguments'):
            现['arguments'] += 函['arguments']
        有 = True
    return 有


def _收anthropic(碎片, 帧):
    """
    Anthropic 系：`content_block_start` 给名字，`input_json_delta` 给参数。

    ⚠ **只对 `tool_use` 和 `input_json_delta` 回真。** 同一个
    `content_block_delta` 底下还挂着 `text_delta`（正文）和 `thinking_delta`
    （思维链）—— 一律回真的话 `跑一轮` 会把正文 `continue` 掉，界面上就是
    "模型一个字都没说"。
    """
    类 = 帧.get('type')
    try:
        if 类 == 'content_block_start':
            块 = 帧.get('content_block') or {}
            if 块.get('type') != 'tool_use':
                return False
            现 = _槽(碎片, int(帧.get('index') or 0))
            if 块.get('id'):
                现['id'] = 块['id']
            if 块.get('name'):
                现['name'] += 块['name']
            # 起始帧里的 `input` 一般是空 dict —— 参数在后面的碎片里。
            # 但**有些服务会一次给全**，非空就顺手收下。
            起 = 块.get('input')
            if isinstance(起, dict) and 起:
                现['arguments'] += json.dumps(起, ensure_ascii=False)
            return True
        if 类 == 'content_block_delta':
            段 = 帧.get('delta') or {}
            if 段.get('type') != 'input_json_delta':
                return False
            现 = _槽(碎片, int(帧.get('index') or 0))
            现['arguments'] += 段.get('partial_json') or ''
            return True
    except (TypeError, ValueError, AttributeError):
        return False
    return False


def _收gemini(碎片, 帧):
    """
    Gemini 系：`candidates[0].content.parts[*].functionCall`。

    ⚠ **它一次给全，没有碎片这回事。** `args` 本来就是 dict，序列化成 JSON
    字符串再交给 `解工具调用` —— 那边对**字符串**有补括号 / 救换行那一套，
    对 dict 没有，统一走字符串这条路两条都吃得住。

    ⚠ 只认带 `functionCall` 的 part。同一个 `parts` 里还有正文 part，
    `取段`（`_取gemini`）读的是 `parts[0].text`，两边的读法不一样但**不能
    互相顶掉** —— 所以这里只挑 `functionCall`，其余原样留给 `取段`。
    """
    try:
        部们 = 帧['candidates'][0]['content']['parts']
    except (KeyError, IndexError, TypeError, AttributeError):
        return False
    if not isinstance(部们, list):
        return False
    有 = False
    for i, 部 in enumerate(部们):
        if not isinstance(部, dict):
            continue
        呼 = 部.get('functionCall')
        if not isinstance(呼, dict) or not 呼.get('name'):
            continue
        现 = _槽(碎片, 'g%d' % i)
        现['name'] = str(呼.get('name'))
        参 = 呼.get('args')
        if isinstance(参, dict):
            现['arguments'] = json.dumps(参, ensure_ascii=False)
        else:
            现['arguments'] = 参 or ''
        有 = True
    return 有


#: 哪些协议发得出"原生工具"。**四家现在都能。**
#:
#: 形状各不相同，一家一套（收发成对，见 `发工具` / `收碎片`）：
#:
#:     openai     `tools:[{type,function}]`          ← 回 `delta.tool_calls`
#:     anthropic  `tools:[{name,description,input_schema}]`
#:                                                 ← 回 `input_json_delta`
#:     gemini     `tools:[{functionDeclarations}]`   ← 回 `functionCall`
#:     local      走 llama.cpp，形状就是 OpenAI 那套
#:
#: ⚠ **`local` 必须在这个名单里。** 它先前不在，于是 `跑一轮` 无论设置成
#: 什么都判"不发工具"；而 `系统提示` 那边按"原生"只给了一行精简协议、**不给
#: 工具清单**，两边一凑就是模型压根不知道有哪些工具，表现是完全无视工具、
#: 自顾自地思考。要不要真发由 `默认工具协议` 看聊天模板决定（见那个函数）。
原生工具协议 = ('openai', 'anthropic', 'gemini', 'local')


def _要原生(协议, 要工具):
    """
    这次要不要发原生工具。`要工具` 是 `'auto'` / `'原生'` / `'从不'`。

    ⚠ **`'从不'` 之外一律看协议在不在 `原生工具协议` 里。** 也就是说
    `'auto'` 和 `'原生'` 在这儿是同一个意思 —— "自动判定"那一步（看聊天
    模板、看协议）在 `酒馆助手线.默认工具协议` 就做完了，传到这儿已经是
    结论。**别在这儿再判一次**，两处判据会打架，而且是"界面显示原生、实际
    没发"那种最难查的打架。
    """
    if 要工具 == '从不':
        return False
    return 协议 in 原生工具协议


def 跑一轮(配置, 系统, 消息们, 工具=None, 要工具='auto',
           停=None, 段回=None, 最大输出=1024):
    """
    跑一次生成，**收完再回**。给编程助手用（`流式` 是聊天用的）。

    回的形状跟 `酒馆本地.跑一轮` **完全一样**：

        {'正文', '调用们', '原生', '提示词token', '停过', '解析失败',
         '停因', '思维'}

    这样上层（`酒馆助手`）不用管底下是本地还是云端。

    `停因` 是对面的停止原因（`max_tokens` / `end_turn` / …）、`思维` 是思维链
    的**字数**（不留内容）。这两个只有在一个字都没输出时才用得上，但那时它们
    是唯一的线索 —— 见 `酒馆助手._空话`。

    协议分派：
        local            交给 `酒馆本地.跑一轮`（那边自己拼体、自己收 `tool_calls`）
        openai           原生 `tools=`，`delta.tool_calls` 碎片拼接
        anthropic        原生 `tools=`，`input_json_delta` 碎片拼接
        gemini           原生 `tools=`，`functionCall` 一次给全

    ⚠ **发不发**由 `要工具` + `原生工具协议` 定，**发什么形状**由 `发工具` 定，
    **怎么收**由 `收碎片` 定。三处是配套的，改一处要回头看另外两处。

    ⚠ **不管走不走原生，最后都过一遍 `解工具调用`。** 模型在原生模式下照样
    可能把 `<tool_call>{…}</tool_call>` 当**文本**吐出来（实测有），不兜这一下
    就是"白白丢掉一轮"。

    `停` 是带 `is_set()` 的停旗；`段回` 收到一块文本调一次。
    """
    缺 = 校验配置(配置)
    if 缺:
        raise 错配置('这套接口配置发不出去，缺：%s' % '、'.join(缺))
    参数 = 合并参数(配置, None)

    if 配置.协议 == 'local':
        # 本地那条路自己会 pop n_ctx / n_gpu_layers，原样透传
        return 酒馆本地.跑一轮(系统, 消息们, 参数, 酒馆本地.定位(配置.模型),
                            工具=工具 if _要原生('local', 要工具) else None,
                            要工具=要工具, 停=停, 段回=段回, 最大输出=最大输出)

    参数 = {键: 值 for 键, 值 in 参数.items() if 键 not in 本地专属参数}
    参数.pop('tools', None)              # 防用户手写在采样参数里，见下面 update 的写法
    参数.pop('tool_choice', None)
    参数.pop('stream', None)
    参数.pop('messages', None)
    参数.pop('max_tokens', None)
    # 写代码要稳，不要天马行空。**配置里写了就听配置的。**
    参数.setdefault('temperature', 0.2)
    地址 = (配置.地址 or '').strip()
    规格 = _备[配置.协议](配置, 系统, 消息们, 参数, 地址)
    体 = 规格['体']
    体['stream'] = True
    try:
        体['max_tokens'] = int(最大输出 or 1024)
    except (TypeError, ValueError):
        体['max_tokens'] = 1024
    if bool(工具) and _要原生(配置.协议, 要工具):
        形 = 发工具(配置.协议, 工具)          # 逐协议换壳，见 `发工具`
        if 形:
            体['tools'] = 形
            # ⚠ `tool_choice` 的写法**每家不一样**：OpenAI 收字符串 `'auto'`，
            # Anthropic 收对象 `{'type':'auto'}`，写错是 400。两家的默认本来
            # 就是 auto，所以只给 OpenAI 显式写 —— 少一处能写错的地方。
            if 配置.协议 == 'openai':
                体['tool_choice'] = 'auto'

    _查头(规格['头'])
    try:
        响应 = requests.post(规格['地址'], headers=规格['头'],
                            json=体, stream=True, timeout=默认超时)
    except requests.exceptions.RequestException as 错:
        raise 错模型('连不上 %s：%s' % (规格['地址'], 错))

    罐, 碎片, 提示, 停过 = [], {}, 0, False
    停因, 思维 = '', 0
    with 响应:
        _查状态(响应)
        队 = queue.Queue()
        别塞 = threading.Event()
        _读客(响应, 队, 别塞).start()
        try:
            while True:
                try:
                    件 = 队.get(timeout=等块秒)
                except queue.Empty:
                    if 停 is not None and 停.is_set():
                        raise 已打断()
                    continue
                if 件 is None:
                    break
                if isinstance(件, Exception):
                    raise 件
                if 停 is not None and 停.is_set():
                    raise 已打断()
                try:
                    帧 = json.loads(件)
                except ValueError:
                    continue                       # 心跳之类，跳过
                if not 提示:
                    # 有的服务在收尾帧里带 usage；没有就拿不到，回 0
                    try:
                        提示 = int((帧.get('usage') or {}).get('prompt_tokens') or 0)
                    except (TypeError, ValueError):
                        提示 = 0
                # ⚠ **"怎么停的"和"想了多久"要一路带到 `酒馆助手._空话`。**
                # 模型什么都没输出的时候，这两样是唯一能分辨原因的东西：
                # 被输出上限截断 vs 模型自己不想说 —— 两种要用户做的事完全不同。
                停因 = _停因(帧) or 停因
                思维 += _思维长(帧)
                if 收碎片(碎片, 帧):
                    continue                       # 工具碎片不算正文
                段 = 规格['取段'](帧)
                if 段:
                    罐.append(段)
                    if 段回:
                        段回(段)
        except 已打断:
            停过 = True
        except Exception as 错:
            if 停 is not None and 停.is_set():
                停过 = True
            elif isinstance(错, requests.exceptions.RequestException):
                raise 错模型('读流断了：%s：%s' % (type(错).__name__, 错))
            else:
                raise
        finally:
            别塞.set()

    完整 = ''.join(罐)
    原生 = 拼工具碎片(碎片)
    认得 = set()
    for d in (工具 or []):
        try:
            认得.add(酒馆本地.规整工具名(d['function']['name']))
        except (KeyError, TypeError):
            continue
    调用们, 残, 失败 = 酒馆本地.解工具调用(
        完整, 原生=原生, 给过工具=bool(工具), 认得的=认得)
    return {'正文': 残, '调用们': 调用们, '原生': 完整,
            '提示词token': 提示, '停过': 停过, '解析失败': 失败,
            '停因': 停因, '思维': 思维}


# ── 出门 ────────────────────────────────────────────────────────────

def 流式(配置, 系统, 消息们, 旗=None, 设定=None):
    """
    跑一次生成，**逐段吐出文本**。是个生成器，边收边吐。

    `旗` 是 `停旗`；给了就能随时打断。被打断时抛 `已打断`——**那是正常
    收场，不是故障**，调用方该单独接住它。

    三家协议共用这一条出门的路，差别全在 `_备*` 里。
    """
    缺 = 校验配置(配置)
    if 缺:
        raise 错配置('这套接口配置发不出去，缺：%s' % '、'.join(缺))

    参数 = 合并参数(配置, 设定)
    if 配置.协议 == 'local':
        # 本地这条道没有 HTTP、没有 SSE、没有读线程——`酒馆本地.流式块`
        # 直接一块一块吐字。查旗在这儿做，跟下面 HTTP 那条路同一个形状：
        # 每收一块看一次旗，置了旗抛 `已打断`（正常收场，不是故障）。
        # ⚠ prefill 期间查不了旗，见 `酒馆本地` 文件头。
        try:
            for 段 in 酒馆本地.流式块(系统, 消息们, 参数,
                                     酒馆本地.定位(配置.模型)):
                if 旗 is not None and 旗.is_set():
                    raise 已打断()
                yield 段
        except 已打断:
            raise
        except 酒馆本地.本地错 as 错:
            raise 错模型(str(错))
        return

    参数 = {键: 值 for 键, 值 in 参数.items() if 键 not in 本地专属参数}
    规格 = _备[配置.协议](配置, 系统, 消息们,
                          参数, (配置.地址 or '').strip())
    _查头(规格['头'])
    try:
        响应 = requests.post(规格['地址'], headers=规格['头'],
                             json=规格['体'], stream=True,
                             timeout=默认超时)
    except requests.exceptions.RequestException as 错:
        # 连接不上 / 超时 / DNS 挂了。包一层，让界面上显示的是人话。
        raise 错模型('连不上 %s：%s' % (规格['地址'], 错))

    with 响应:
        _查状态(响应)
        队 = queue.Queue()
        别塞 = threading.Event()
        _读客(响应, 队, 别塞).start()      # 读留在它那条线程里阻塞，见 `_读客`
        try:
            while True:
                try:
                    件 = 队.get(timeout=等块秒)
                except queue.Empty:
                    # ⚠ **这一条不是错误。** 等不到块只是说"这么久没新字"，
                    # 模型首字等几十秒很正常，接着等就是。它的唯一作用是
                    # 给我们一个**能醒过来看旗子**的机会——这是整个可打断
                    # 机制的支点，见 `_读客` 那段为什么非得这么写。
                    if 旗 is not None and 旗.is_set():
                        raise 已打断()
                    continue
                if 件 is None:
                    return                              # 流正常读完
                if isinstance(件, Exception):
                    raise 件                            # 读那头炸了，在这儿炸
                if 旗 is not None and 旗.is_set():
                    raise 已打断()
                try:
                    帧 = json.loads(件)
                except ValueError:
                    # 有些服务会插非 JSON 的心跳行。跳过，不因为一行坏了
                    # 就把整段生成扔掉。
                    continue
                段 = 规格['取段'](帧)
                if 段:
                    yield 段
        except 已打断:
            raise
        except Exception as 错:
            # 用户点了停止之后，`旗.set()` 会在**另一条线程**上把这条连接
            # `close()` 掉，而读线程可能正卡在这次读里。它会以五花八门的
            # 样子炸出来：
            #
            #   · requests 报 `RequestException`
            #   · 还有一条 **CPython `http.client` 自己的竞态**：它在
            #     `_close_conn` 里写 `fp, self.fp = self.fp, None`，
            #     而我们那一下 `close()` 已经把 `fp` 置成 `None` 了，
            #     紧接着 `fp.close()` 就抛
            #     `AttributeError: 'NoneType' object has no attribute 'close'`
            #
            # 那条竞态在 CPython 里修不掉（我们改不了 stdlib），但它的
            # **效果正是我们想要的**——连接断了、读返回了。所以规则简单点：
            # **旗子置了，就先当打断；没置，照旧往上抛。**
            # 这样正常跑的时候一个错都不会被吞掉，只有"用户确实喊了停"
            # 这一种情况下才把收尾时的杂音盖掉。
            if 旗 is not None and 旗.is_set():
                raise 已打断()
            if isinstance(错, requests.exceptions.RequestException):
                raise 错模型('读流断了：%s：%s' % (type(错).__name__, 错))
            raise
        finally:
            # 不管是读完了、炸了、还是被打断，都让读线程别再往队里堆东西。
            # 被放弃的那条读线程靠这个才停得下来（见 `_读客`）。
            别塞.set()
