#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆存储.py — ai酒馆 唯一的存储出口，**落地是本机 JSON 文件**

上面所有模块都不直接碰文件，一律从这里的 `库` 走。

    酒馆数据/
      角色卡.json           {"<编号>": {每一列}}
      接口配置.json         {"<编号>": {每一列}}
      会话.json             {"<编号>": {每一列}}
      助手设置.json         {"<编号>": {每一列}}      ← 永远只有编号 1
      助手任务.json         {"<编号>": {每一列}}      ← 编程助手的任务清单
      记忆.json             {"<编号>": {每一列}}      ← 长期记忆的**索引**（类型/键）
      头像/<角色编号>.json   {"类型": "image/png", "图": "<base64>"}
      消息/<会话编号>.json   [{每一列}, …]  ← 按 `序号` 升序
      助手转录/<任务编号>.json  {每一步的助手输出 / 工具调用 / 结果}
      记忆/<记忆编号>.json     [{一条记忆}, …]

**为什么这么分文件。** 小表（角色卡 / 接口配置 / 会话 / 助手设置 / 助手任务 /
记忆的索引）行数就那么点，一张表一个文件、读进来摆在内存里最省事。而消息、
头像、助手转录和记忆条目是**会长大的**：一段会话能攒几千条消息、一张头像
几百 KB、一次任务的工具结果里可能塞着整个文件、记忆条目会一条条攒起来 ——
跟别的挤在一个文件里，就是"改一条重写整个库"——所以这几样各自分文件。

⚠ **记忆是"索引在小表、条目在大件"两截的**（跟 `助手任务`+`助手转录` 同一种
分法）：小表那行只有 `类型/键`，用来按 `(类型, 键)` 找到编号；条目本体在
`记忆/<编号>.json`。**别把条目塞进小表** —— 小表是整张读进内存的，条目一多
每次开软件都要把所有人的记忆全读一遍。

⚠ **写是原子的**（先写 `.tmp` 再 `os.replace`）。`os.replace` 在同一个卷上
是原子的，所以**中途停电/被杀进程**只会看到"旧的完整版本"或者"新的完整
版本"，不会留下半个 JSON。这一点比什么都重要：JSON 一旦被截断，下次就
整个读不出来了。

⚠ **坏掉的文件不会让程序起不来。** 读不出来就当空的（`_读` 里那句话），
宁可丢一个文件也不要把整个程序挡在门外。
"""

import base64
import contextlib
import json
import os
import shutil
import sys
import threading
import time

__all__ = ['酒馆错误', '库', '打开', '毫秒', '全部表', '默认数据目录']


class 酒馆错误(Exception):
    """读写这一层出的岔子。带上说明了在干哪件事。"""

    def __init__(self, 消息, 在哪=None):
        Exception.__init__(self, 消息)
        self.在哪 = 在哪


def 毫秒():
    """
    现在，epoch 毫秒。**全项目只从这一个地方取时间。**

    时间一律按毫秒的 int 存，不存任何格式化过的字符串——省掉一类
    "解析日期"的错，也不丢精度。要给人看的时候在下游格式化。
    """
    return int(time.time() * 1000)


# ── 数据放哪 ────────────────────────────────────────────────────────

#: 全部"表"的名字。名字照旧，只是底下从数据库换成了文件。
#:
#: ⚠ 这张单子只是**文档**（外面没有任何地方读它，`小表` 才是白名单），
#: 但加了新表要跟着加，不然它就成了假的。
全部表 = ('角色卡', '角色头像', '接口配置', '会话', '消息', '助手设置',
        '助手任务', '记忆')

#: 这几张是"一张表一个文件、整张读进内存"的小表。
#:
#: `助手设置` 永远只有一行（编号 1）—— 存编程助手的工作目录、用哪套接口
#: 这些。加进这张名单就自动有 `发号/存/取/全都/数/删` 可用，不用另写存储。
#:
#: `助手任务` 是编程助手的任务清单（每次任务一行）。**转录本本身不在这儿**
#: ——它可能几 MB，进小表就是"列一次清单把全部任务的正文读进内存"，
#: 所以按编号分文件放在 `助手转录/` 下，跟 `会话`+`消息` 是同一种分法。
#:
#: `记忆` **只放索引**（`类型` + `键` → `编号`），条目本体在 `记忆/<编号>.json`。
#: 同样的理由：条目是会长大的。
小表 = ('角色卡', '接口配置', '会话', '助手设置', '助手任务', '记忆')

#: 每张小表的主键列。
主键列 = {'角色卡': '编号', '接口配置': '编号', '会话': '编号',
        '助手设置': '编号', '助手任务': '编号', '记忆': '编号'}


def 默认数据目录():
    """
    `酒馆数据/` 在哪。跟本文件同目录（打包之后就是 exe 旁边）。

    **不能放在用户目录或者临时目录里**：那是"换台机器/换个目录就找不着自己
    存过什么"的经典坏法。数据得跟着程序走。
    """
    if getattr(sys, 'frozen', False):
        这里 = os.path.dirname(os.path.abspath(sys.executable))
    else:
        这里 = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(这里, '酒馆数据')


# ── 落盘 ────────────────────────────────────────────────────────────

def _读(路径, 默认):
    """
    读一个 JSON。**读不出来就给默认值，绝不抛。**

    文件不在、内容坏了、被截断了——三种情况都回默认值。理由：这是"存过
    的东西打不开"，不是"库坏了"。为了一个坏文件让整个程序起不来，是拿
    小毛病换大毛病；坏掉的内容还在盘上，事后能捞。
    """
    try:
        with open(路径, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return 默认


def _写(路径, 对象):
    """
    原子地写一个 JSON。

    ⚠ **先写 `.tmp` 再 `os.replace`，不直接往目标文件上写。**
    直接写的话，写到一半被杀进程 / 断电，留下的是**半个 JSON**——下次
    整个文件都读不出来，等于把之前存的全丢了。`os.replace` 在同一个卷上
    是原子的，所以外面只会看到完整的旧版本或者完整的新版本。

    `ensure_ascii=False`：中文按原样写。存的是给人看的数据文件，不是要
    塞进网络里的小包。
    """
    目录 = os.path.dirname(路径)
    if 目录:
        os.makedirs(目录, exist_ok=True)
    临时 = 路径 + '.tmp'
    with open(临时, 'w', encoding='utf-8') as f:
        json.dump(对象, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(临时, 路径)


class 库(object):
    """
    ai酒馆 的存储出口。**落地是本机 JSON 文件。**

    跟数据库那一版比，这里有几个方法**故意没有**了：没有 `执行` / `查` /
    `批量` / `改写`。那些是"把 SQL 字符串递给引擎"的形状，JSON 底下没有
    SQL 这回事，留着只会逼上层继续拼字符串。
    """

    def __init__(self, 目录=None):
        self.目录 = os.path.abspath(目录 or 默认数据目录())
        self._锁 = threading.RLock()
        self._小表 = {}              # 名 → {主键值: 行}，整张读进内存
        self._开过 = False

    # ── 开 / 关 ─────────────────────────────────────────────────────

    def 开(self):
        """把数据目录立起来，把三张小表读进内存。**幂等。**"""
        with self._锁:
            if self._开过:
                return self
            try:
                os.makedirs(self.目录, exist_ok=True)
                os.makedirs(self._头像目录(), exist_ok=True)
                os.makedirs(self._消息目录(), exist_ok=True)
                os.makedirs(self._助手转录目录(), exist_ok=True)
                os.makedirs(self._记忆目录(), exist_ok=True)
            except OSError as 错:
                raise 酒馆错误('数据目录建不起来：%s（%s）'
                              % (self.目录, 错), '开')
            self._开过 = True
            for 名 in 小表:
                self._装(名)
            return self

    def 关(self):
        """**可以重复调。** JSON 没有连接要关，这一步只是把内存里的丢开。"""
        with self._锁:
            self._小表 = {}
            self._开过 = False

    @contextlib.contextmanager
    def 独占(self):
        """
        复合操作要原子的时候套这个：`with 库.独占():` 。

        **为什么非有不可** —— 「读一次盘 → 改 → 写回去」这种形状，每一步
        自己都是原子的，合起来不是。两条线程同时往一段会话里追加消息，两边
        都读到"现在有 7 条"，然后各自写出"8 条"的那一份 —— **先写的那条被
        后写的整份盖掉，静默丢一条消息。**

        套上这个之后中间不会被别的线程插进来。`RLock` 可重入，所以里头的
        `消息们` / `存消息们` / `取` / `存` 照样能调。

        ⚠ **只护同一个进程里的两条线程**（工人一条、生成一条）。两个进程
        同时开同一个数据目录，这个锁挡不住——那种用法一概不支持，也别去
        试：文件这一层没有跨进程的锁。
        """
        with self._锁:
            self._要开()
            yield self

    def __enter__(self):
        return self.开()

    def __exit__(self, *exc):
        self.关()

    def __repr__(self):
        return '<酒馆库 %s%s>' % (self.目录, '' if self._开过 else ' (没开)')

    def 说明(self):
        """一行「数据在哪」，给状态栏和排错看。"""
        return self.目录

    # ── 路径 ────────────────────────────────────────────────────────

    def _路径(self, 名):
        return os.path.join(self.目录, '%s.json' % 名)

    def _头像目录(self):
        return os.path.join(self.目录, '头像')

    def _消息目录(self):
        return os.path.join(self.目录, '消息')

    def _助手转录目录(self):
        return os.path.join(self.目录, '助手转录')

    def _记忆目录(self):
        return os.path.join(self.目录, '记忆')

    def _头像路径(self, 角色编号):
        return os.path.join(self._头像目录(), '%s.json' % int(角色编号))

    def _消息路径(self, 会话编号):
        return os.path.join(self._消息目录(), '%s.json' % int(会话编号))

    def _转录路径(self, 任务编号):
        return os.path.join(self._助手转录目录(), '%s.json' % int(任务编号))

    def _记忆路径(self, 记忆编号):
        return os.path.join(self._记忆目录(), '%s.json' % int(记忆编号))

    def _要开(self):
        if not self._开过:
            raise 酒馆错误('库还没开。先 `开()`（或者用 `with 库(...)`）。')

    @staticmethod
    def _认表(名):
        """表名白名单。**路径是拼出来的，所以这是唯一的守口**——
        不挡的话 `名` 里塞个 `../` 就能写到数据目录外面去。"""
        if 名 not in 小表:
            raise 酒馆错误('不认识的表：%r（只认 %s）'
                          % (名, '、'.join(小表)))
        return 名

    # ── 小表：读 ────────────────────────────────────────────────────

    def _装(self, 名):
        """从盘上读一张小表进内存。键统一转成 `str`——JSON 的键就是字符串。"""
        原始 = _读(self._路径(名), {})
        if not isinstance(原始, dict):
            # 文件里是别的形状（手改坏了之类）。当空的，不炸。
            原始 = {}
        self._小表[名] = {str(k): dict(v) for k, v in 原始.items()
                         if isinstance(v, dict)}
        return self._小表[名]

    def _拿(self, 名):
        self._要开()
        名 = self._认表(名)
        if 名 not in self._小表:
            self._装(名)
        return self._小表[名]

    def 全都(self, 名):
        """
        一张小表的全部行，**按主键升序**。

        回来的是**副本**——调用方拿去改不会静悄悄地影响内存里那份。
        要改就走 `存()`。
        """
        with self._锁:
            表 = self._拿(名)
            return [dict(表[k]) for k in self._排好(表)]

    @staticmethod
    def _排好(表):
        """键排序。键是数字串就按数字排，不是就按字符串排。"""
        try:
            return sorted(表, key=lambda k: (0, int(k)))
        except (TypeError, ValueError):
            return sorted(表)

    def 取(self, 名, 键):
        """按主键取一行。没有就返回 `None`。"""
        with self._锁:
            return dict(self._拿(名).get(str(键)) or {}) or None

    def 数(self, 名):
        """一张小表有多少行。"""
        with self._锁:
            return len(self._拿(名))

    # ── 小表：写 ────────────────────────────────────────────────────

    def 存(self, 名, 行):
        """
        写一行。**有就整个盖掉，没有就加**（upsert，按主键）。

        落盘是"改完之后把整张小表写出去"。小表的行数就那么点，整张写比
        跟踪"改了哪几行"简单得多，也不会漏。
        """
        with self._锁:
            名 = self._认表(名)
            表 = self._拿(名)
            行 = dict(行 or {})
            键 = 主键列[名]
            if 行.get(键) is None:
                raise 酒馆错误('存 %s 的行没有主键 `%s`' % (名, 键), '存')
            表[str(行[键])] = 行
            _写(self._路径(名), 表)
            return dict(行)

    def 删(self, 名, 键):
        """按主键删一行。删到了返回 `True`。"""
        with self._锁:
            名 = self._认表(名)
            表 = self._拿(名)
            if 表.pop(str(键), None) is None:
                return False
            _写(self._路径(名), 表)
            return True

    def 发号(self, 名):
        """
        给一张小表发一个新编号 = **现有最大编号 + 1**。

        不发负数、不从 0 开始重来：编号是主键，重号就是把别人顶掉。
        表空的时候从 1 开始。
        """
        with self._锁:
            表 = self._拿(名)
            最大 = 0
            for k in 表:
                try:
                    最大 = max(最大, int(k))
                except (TypeError, ValueError):
                    continue
            return 最大 + 1

    # ── 消息（按会话分文件）──────────────────────────────────────────

    def 消息们(self, 会话编号):
        """一段会话的全部消息，**按 `序号` 升序**。"""
        with self._锁:
            self._要开()
            原始 = _读(self._消息路径(会话编号), [])
            if not isinstance(原始, list):
                原始 = []
            行们 = [dict(r) for r in 原始 if isinstance(r, dict)]
            行们.sort(key=lambda r: int(r.get('序号') or 0))
            return 行们

    def 存消息们(self, 会话编号, 行们):
        """
        把一段会话的消息整份写下去。**按 `序号` 排好再写。**

        整份写而不是"追加一行"：这段会话的消息本来就得整个读出来才知道
        写到哪了，而一次生成改的是"一条"，一段会话撑死几千行——整份写
        换来的是"盘上永远是自洽的一份"，不会有半条记录。
        """
        with self._锁:
            self._要开()
            行们 = [dict(r) for r in (行们 or [])]
            行们.sort(key=lambda r: int(r.get('序号') or 0))
            _写(self._消息路径(会话编号), 行们)
            return len(行们)

    def 删消息们(self, 会话编号):
        """把一段会话的消息整个删掉。返回删了几条。"""
        with self._锁:
            self._要开()
            路径 = self._消息路径(会话编号)
            条 = len(self.消息们(会话编号))
            try:
                os.remove(路径)
            except OSError:
                pass
            return 条

    # ── 头像（一个角色一个文件）─────────────────────────────────────

    def 存头像(self, 角色编号, 图, 类型=''):
        """
        存/换一个头像。`图` 是原始字节，写进文件时编成 base64。

        **一个角色一个文件。** 跟消息一个道理：头像几百 KB，全挤在一个
        `角色头像.json` 里的话，换一个人的头像就得把所有人的重写一遍。
        """
        with self._锁:
            self._要开()
            if not isinstance(图, (bytes, bytearray, memoryview)):
                raise 酒馆错误('头像得是 bytes（拿到的是 %s）'
                              % type(图).__name__, '存头像')
            _写(self._头像路径(角色编号),
                {'类型': 类型 or '', '图': base64.b64encode(bytes(图)).decode('ascii')})
            return True

    def 取头像(self, 角色编号):
        """取头像，返回 `(字节, 类型)`；没有就返回 `None`。"""
        with self._锁:
            self._要开()
            原始 = _读(self._头像路径(角色编号), None)
            if not isinstance(原始, dict) or not 原始.get('图'):
                return None
            try:
                图 = base64.b64decode(原始['图'])
            except (ValueError, TypeError):
                # 内容坏了。当没有头像，不炸——一个坏头像不该让左栏整个
                # 画不出来。
                return None
            return (图, 原始.get('类型') or '')

    def 删头像(self, 角色编号):
        with self._锁:
            self._要开()
            路径 = self._头像路径(角色编号)
            if not os.path.isfile(路径):
                return False
            try:
                os.remove(路径)
            except OSError:
                return False
            return True

    # ── 编程助手的转录（一次任务一个文件）───────────────────────────
    #
    # 跟消息/头像一个道理：一次任务的转录里可能有整个文件那么长的工具结果，
    # 塞进 `助手任务` 那张小表就是"列一次任务清单把全部转录读进内存"。
    #
    # ⚠ **`对象` 是调用方给的（`酒馆助手存`），这儿只管原样落盘。** 所以
    # 写之前必须能 `json.dump` 得下去 —— 转录里混进一个 tuple 或 bytes
    # 就会当场抛 `TypeError`，而 `_写` 是先写 `.tmp` 再 `os.replace` 的，
    # 抛在半路只会留下一个 `.tmp`，盘上原来的那份完好。

    def 存转录(self, 任务编号, 对象):
        """把一次任务的转录整个写下去。"""
        with self._锁:
            self._要开()
            _写(self._转录路径(任务编号), 对象)
            return True

    def 取转录(self, 任务编号):
        """
        取一次任务的转录，**没有就返回 `None`**。

        文件不在、内容坏了、形状不对（不是 dict）——三种都当没有。跟
        `取头像` 一个约定：读不出来是"这次任务的转录没了"，不是"库坏了"。
        """
        with self._锁:
            self._要开()
            原始 = _读(self._转录路径(任务编号), None)
            return 原始 if isinstance(原始, dict) else None

    def 删转录(self, 任务编号):
        """删一次任务的转录。删到了返回 `True`。没有这个文件也算成功过的
        ——调用方（`酒馆助手存.删任务`）删的是"这次任务"，转录不在不该拦住
        它把清单行也删掉，那样反而留一个点开是空的条目。"""
        with self._锁:
            self._要开()
            路径 = self._转录路径(任务编号)
            if not os.path.isfile(路径):
                return True
            try:
                os.remove(路径)
            except OSError:
                return False
            return True

    # ── 记忆条目（一套记忆一个文件）──────────────────────────────────
    #
    # 记忆也是"索引在小表、正文在大件"两截：`记忆` 小表那一行只有
    # `类型/键`，靠它找到编号；条目本体在这儿。见文件头那段。

    def 存记忆们(self, 记忆编号, 条目们):
        """把一套记忆的条目整个写下去。"""
        with self._锁:
            self._要开()
            _写(self._记忆路径(记忆编号), list(条目们 or []))
            return True

    def 取记忆们(self, 记忆编号):
        """
        取一套记忆的条目，**没有就返回 `None`**。

        ⚠ **形状不对时回 `[]` 而不是 `None`**：文件里是个 dict 或者被手改成了
        一行字符串，都算"这套记忆的正文是空的"，不该让调用方去分辨"没有"和
        "坏了" —— 对读记忆的人来说这两件事没差别（都是没记住东西）。
        """
        with self._锁:
            self._要开()
            原始 = _读(self._记忆路径(记忆编号), None)
            if 原始 is None:
                return None
            return 原始 if isinstance(原始, list) else []

    def 删记忆们(self, 记忆编号):
        """删一套记忆的条目。**文件不在也算删成功**（理由同 `删转录`）。"""
        with self._锁:
            self._要开()
            路径 = self._记忆路径(记忆编号)
            if not os.path.isfile(路径):
                return True
            try:
                os.remove(路径)
            except OSError:
                return False
            return True

    # ── 连根拔 ──────────────────────────────────────────────────────
    def 清空(self):
        """
        把整个数据目录删掉重来。**只在用户明确要这么干的时候调。**

        删的是 `self.目录` 整棵树，所以先确认它确实是我们要的那个目录
        ——这是个防手滑的兜底，不是防御性编程：`清空` 一旦指错地方，
        删的就是用户别的东西。
        """
        with self._锁:
            要删 = os.path.abspath(self.目录)
            if os.path.basename(要删) != '酒馆数据':
                raise 酒馆错误('不肯删 %s——这不是一个 `酒馆数据` 目录' % 要删,
                              '清空')
            self._小表 = {}
            self._开过 = False
            if os.path.isdir(要删):
                shutil.rmtree(要删)
            return True


def 打开(目录=None):
    """开一个库，返回 `库`。等价于 `库(目录).开()`。"""
    return 库(目录).开()
