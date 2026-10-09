#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆互联.py — 局域网互联服务端（**鸿胪寺隧道协议 honglu-tunnel v1**）

把本机跑模型的能力开放给同一个局域网里的安卓端「鸿胪寺」。**不走 HTTP** ——
通信是 `酒馆隧道.py` 那条 **TCP + TLS1.3 的加密隧道**，帧里跑这些"方法"：

    ping        握手：报上名号 + 本机模型清单 + 后端信息
    推荐        电脑那份**精选清单**（手机直接照这个下）
    chat        一次生成，逐字回吐（一串 CHUNK）
    下载        手机遥控电脑把某个 GGUF 下进 `酒馆数据/模型/`（CHUNK 报进度）
    下载取消    取消上一次远程下载（尽力而为）
    上传状态    问「这个文件电脑收到多少了」（续传 / 去重用）
    上传        手机把本地 GGUF 推给电脑（其后的 CHUNK 就是文件字节）

## 加密与身份（都在 `酒馆隧道` 里做）

**密码学一行不自己造**：加密是标准 TLS1.3。服务端证书私钥由**口令**派生，
手机用同一口令推出同一把公钥、只认它 —— **不知道口令的人连不上，也冒充不了**。

## PC 当大脑

手机发的是**原始的角色卡 + 消息 + 会话设定 + 记忆**；电脑用 `酒馆大脑.拼提示`
+ `酒馆协议.取控制` + `酒馆本地.流式块` 全干完。手机是纯前端。

## 两条搬模型的通道

落点都是 `酒馆数据/模型/`：**电脑自己下**（`下载`，手机只发仓库/文件/源）或
**手机推上来**（`上传`）。半成品都是 `<名>.part`，收满才改名成 `.gguf`。

## 线程

引擎（`酒馆本地`）靠一把全局 `锁` 串行化。服务端再拿一把 `生成锁` 把并发的生成
也串起来。隧道本身每个连接一条线程，连接内顺序处理一个方法。
"""
import json
import os
import shutil
import socket
import threading
import time
import urllib.parse
from collections import deque

import requests

import 酒馆存储
import 酒馆隧道

__all__ = ['默认端口', '设置名', '服务', '读配置', '写配置', '局域网地址们']

#: 手机端默认往这个端口连。
默认端口 = 8017

#: 配置落在 `酒馆数据/互联.json`。
设置名 = '互联'


# ── 配置（`酒馆数据/互联.json`）──────────────────────────────────────

_配置默认 = {'端口': 默认端口, '口令': '', 'gpu层': -1, '自动启动': False}


def 读配置():
    """读服务端配置。文件不在 / 坏了都回默认（跟全项目一个脾气：绝不抛）。"""
    存 = 酒馆存储.读设置(设置名, {}) or {}
    出 = dict(_配置默认)
    for 键 in 出:
        if 键 in 存:
            出[键] = 存[键]
    return 出


def 写配置(端口=None, 口令=None, gpu层=None, 自动启动=None):
    """把这几样落盘（只写传进来的；没传的保持原值）。"""
    出 = 读配置()
    if 端口 is not None:
        出['端口'] = int(端口)
    if 口令 is not None:
        出['口令'] = str(口令)
    if gpu层 is not None:
        出['gpu层'] = int(gpu层)
    if 自动启动 is not None:
        出['自动启动'] = bool(自动启动)
    酒馆存储.写设置(设置名, 出)
    return 出


# ── 局域网地址探测 ──────────────────────────────────────────────────

def 局域网地址们():
    """
    本机能被局域网访问的 IPv4 地址们。

    两条路一起用，取并集：
      · 按主机名解析一遍（能捞出多网卡）；
      · 那个经典的 UDP 小把戏——连一下外网地址（不发包），从内核问出本机
        被路由选中的那块网卡的地址。**没网也不会抛**，只是这条拿不到。
    """
    名们 = set()
    try:
        for 信 in socket.getaddrinfo(socket.gethostname(), None):
            地址 = 信[4][0]
            if ':' in 地址 or 地址.startswith('127.'):
                continue
            名们.add(地址)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            名们.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    return sorted(名们)


# ── 文件名 / 模型目录 / 源 ──────────────────────────────────────────

def _净名(名):
    """
    手机递过来的文件名 → 一个安全的、只会落在模型目录里的名字。

    **别信任何会进路径的输入**：只收裸文件名（去得掉路径才算数）、必须以
    `.gguf` 结尾、不许带分隔符。洗不出来就回空串，调用方当"没有"处理。
    """
    名 = (名 or '').strip()
    if not 名 or not 名.lower().endswith('.gguf'):
        return ''
    if os.path.basename(名) != 名 or 名 in ('.', '..'):
        return ''
    if ('/' in 名) or ('\\' in 名) or ('\x00' in 名):
        return ''
    return 名


def _件URL(仓库, 文件, 源):
    """一个仓库内文件在这套源上的**直下地址**（对齐安卓端已验证的两种形状）。"""
    if 源['系'] == 'ms':
        return '%s/api/v1/models/%s/repo?Revision=master&FilePath=%s' % (
            源['端'], 仓库, urllib.parse.quote(文件))
    return '%s/%s/resolve/main/%s' % (源['端'], 仓库, 文件)


#: 手机端「本地模型」页多一个 `aifasthub`（AI Fast Hub，一个 HF 镜像）。电脑端
#: 的源表里没有它 —— 但它在协议形状上就是 hf 系，这儿补个别名，省得动
#: `酒馆本地.py`（那是主干，能不动就不动）。仓库名跟 HuggingFace 同构，
#: 直下地址也同构。
_额外源 = {
    'aifasthub': {'标': 'aifasthub', '名': 'AI Fast Hub（HF 镜像）',
                  '端': 'https://aifasthub.com', '系': 'hf'},
}


def _查源(源标):
    """源标 → 源字典。先看别名表，再交给 `酒馆本地._查源`。"""
    import 酒馆本地
    标 = (源标 or '').strip() or 酒馆本地.默认源
    if 标 in _额外源:
        return _额外源[标]
    return 酒馆本地._查源(标)


class _信封错(Exception):
    """请求信封不合法（缺字段 / 类型不对 / 内容为空）。会被转成 ERR 帧回给客户端。"""


# ── 远程下载（电脑自己下）────────────────────────────────────────────

class 下载任务(object):
    """
    一次远程下载。**worker 线程跑，服务线程只读它的快照吐 CHUNK。**

    为什么自己写流式这一段、不复用 `酒馆本地.下载`：那个是"一次阻塞调用"，
    既报不出进度、也掐不断。落点、续传、尺寸校验、GGUF 魔数校验都照抄电脑端
    那条链的做法。
    """

    def __init__(self, 仓库, 文件, 源标):
        self.仓库 = 仓库
        self.文件 = 文件
        self.源标 = 源标
        self.名 = os.path.basename(文件)
        self.已 = 0
        self.总 = 0
        self.阶段 = '准备中'
        self.错 = ''
        self.完 = False
        self.取消过 = False
        self._停 = threading.Event()
        self._锁 = threading.Lock()
        self._线 = None

    def 快照(self):
        with self._锁:
            return {'已': self.已, '总': self.总, '阶段': self.阶段,
                    '错': self.错, '完': self.完, '取消': self.取消过}

    def 取消(self):
        self._停.set()

    def 开始(self):
        self._线 = threading.Thread(target=self._跑, name='鸿胪远程下载', daemon=True)
        self._线.start()

    def _置(self, **kw):
        with self._锁:
            for k, v in kw.items():
                setattr(self, k, v)

    def _跑(self):
        import 酒馆本地
        try:
            源 = _查源(self.源标)
        except Exception as 错:
            self._置(错=str(错), 阶段='失败')
            return
        目录 = 酒馆本地.模型目录()
        终 = os.path.join(目录, self.名)
        半 = 终 + '.part'
        if os.path.isfile(终):
            self._置(阶段='已在', 完=True, 已=os.path.getsize(终), 总=os.path.getsize(终))
            return

        网 = _件URL(self.仓库, self.文件, 源)
        头 = {'User-Agent': 'Mozilla/5.0'}
        牌 = 酒馆本地._hf令牌(源['标']) if 源['系'] == 'hf' else None
        if 牌:
            头['Authorization'] = 'Bearer ' + 牌
        已有 = os.path.getsize(半) if os.path.isfile(半) else 0
        if 已有:
            头['Range'] = 'bytes=%d-' % 已有
        self._置(阶段='连接中', 已=已有)
        try:
            应 = requests.get(网, headers=头, stream=True, timeout=(10, 60))
        except requests.exceptions.RequestException as 错:
            self._置(错='连不上 %s：%s' % (网, 错), 阶段='失败')
            return
        with 应:
            if 应.status_code == 416:
                self._收尾(半, 终)          # 本地那份已经够长，当完成
                return
            if 应.status_code in (401, 403):
                self._置(错='这个仓库要授权才能下（%s）。' % self.仓库, 阶段='失败'); return
            if 应.status_code == 404:
                self._置(错='这个源上没有 %s 里的 %s。' % (self.仓库, self.文件), 阶段='失败'); return
            if 应.status_code not in (200, 206):
                self._置(错='下 %s 时对面回 HTTP %d。' % (self.仓库, 应.status_code), 阶段='失败'); return
            if 应.status_code == 200:
                已有 = 0            # 对面不认 Range —— 从头写，**绝不能 append**
            长 = int(应.headers.get('Content-Length') or 0)
            总 = (已有 + 长) if 应.status_code == 206 else (长 or 0)
            self._置(总=总, 已=已有, 阶段='下载中')
            写 = 0
            取消了 = False
            with open(半, 'ab' if 已有 else 'wb') as 出:
                for 块 in 应.iter_content(chunk_size=1 << 20):
                    if self._停.is_set():
                        取消了 = True
                        break
                    if not 块:
                        continue
                    出.write(块); 写 += len(块)
                    self._置(已=已有 + 写)
            if 取消了:
                # 取消 = **连没下完的半成品一起删掉**（用户要的语义）。别留个 .part，
                # 否则下次"续传"会接到一个已经不想要的半截上。
                # ⚠ 删除得在 `with open(...)` **退出之后**做 —— Windows 上删一个还开着的文件会失败。
                self._置(取消过=True, 阶段='已取消')
                try:
                    if os.path.isfile(半):
                        os.remove(半)
                except OSError:
                    pass
                return
            实 = os.path.getsize(半)
            if 总 and 实 != 总:
                self._置(错='下到一半断了：只拿到 %d / %d 字节。再点一次会接着下。'
                          % (实, 总), 阶段='失败')
                return
        self._收尾(半, 终)

    def _收尾(self, 半, 终):
        """校验 GGUF 魔数 → 原子改名进模型目录 → 报完成。"""
        import 酒馆本地
        try:
            with open(半, 'rb') as f:
                头 = f.read(4)
        except OSError:
            头 = b''
        if 头 != b'GGUF':
            try:
                os.remove(半)
            except OSError:
                pass
            self._置(错='下下来的不是 GGUF（开头不是「GGUF」）——可能下到的是网页'
                      '或别的文件，已丢弃。', 阶段='失败')
            return
        try:
            os.replace(半, 终)
        except OSError as 错:
            self._置(错='改名失败：%s' % 错, 阶段='失败'); return
        大 = os.path.getsize(终)
        self._置(已=大, 总=大, 阶段='已下好', 完=True)
        try:
            酒馆本地.让路()            # 腾显存：接下来多半要加载这个新模型
        except Exception:
            pass


# ── 服务端 ──────────────────────────────────────────────────────────

class 服务(object):
    """
    一台互联服务，架在 `酒馆隧道` 上。**启动 / 停止都幂等。**

    `gpu层` 由**服务端自己**决定（默认 -1 = 能上的层全塞 GPU），手机不用操心。
    """

    def __init__(self, 端口=None, 口令=None, gpu层=None):
        存 = 读配置()
        self.端口 = int(存['端口'] if 端口 is None else 端口)
        self.口令 = str(存['口令'] if 口令 is None else 口令)
        self.gpu层 = int(存['gpu层'] if gpu层 is None else gpu层)
        self._隧道 = None
        self._生成锁 = threading.Lock()        # 一次只服务一个生成
        self._下载 = None                      # 当前的远程下载任务（`下载任务`）
        self._下载锁 = threading.Lock()
        self._上传 = None                      # 当前上传的进度（一个小字典）
        self._上传锁 = threading.Lock()
        self.日志 = deque(maxlen=200)          # 给界面看的一小段滚动日志
        self.指纹 = ''                         # 服务端公钥指纹（给手机钉）

    # ── 记一笔 ──
    def _记(self, 文):
        self.日志.append(str(文))

    # ── 远程下载：同一时刻只留一个 ──
    def 换下载(self, 任务):
        with self._下载锁:
            旧 = self._下载
            self._下载 = 任务
        if 旧 is not None:
            try:
                旧.取消()
            except Exception:
                pass

    def 取消下载(self):
        with self._下载锁:
            任务 = self._下载
        if 任务 is None:
            return False
        任务.取消()
        self._记('已请求取消下载：%s（半成品会删掉）' % 任务.名)
        # ⚠ 任务已经**跑完**了（失败 / 中断 / 取消过）时，它自己不会再回来删 `.part`
        #   —— 这儿补一刀，保证"取消"这个名字到哪儿都等于"连残件一起清掉"。
        #   正在下的话，文件这会儿还开着、Windows 删不掉，会失败 —— 交给 `_跑` 退出
        #   `with open(...)` 之后再删（见那段）。
        try:
            import 酒馆本地
            半 = os.path.join(酒馆本地.模型目录(), 任务.名 + '.part')
            if os.path.isfile(半):
                os.remove(半)
        except OSError:
            pass
        return True

    # ── 上传进度（给界面看）──
    def 置上传(self, 值):
        with self._上传锁:
            self._上传 = 值

    def 进度(self):
        """
        这会儿电脑在搬哪个模型、搬了多少。**给「局域网互联」那个框画的进度条用。**

        回一个字典：`{'在传': False}` 或
        `{'在传': True, '类型': '下载'/'上传', '名', '已', '总', '阶段'}`。
        """
        with self._下载锁:
            任务 = self._下载
        if 任务 is not None:
            快 = 任务.快照()
            if not (快['完'] or 快['错'] or 快['取消']):
                return {'在传': True, '类型': '下载', '名': 任务.名,
                        '已': 快['已'], '总': 快['总'], '阶段': 快['阶段']}
        with self._上传锁:
            上 = self._上传
        if 上:
            return dict(上, 在传=True, 类型='上传')
        return {'在传': False}

    # ── 开关 ──
    def 在跑(self):
        return self._隧道 is not None and self._隧道.在跑()

    def 启动(self):
        """起隧道。端口被占之类的错**原样抛给界面**。"""
        if self._隧道 is not None:
            return
        隧 = 酒馆隧道.服务(self.端口, self.口令, self._方法, 记=self._记)
        隧.启动()
        self._隧道 = 隧
        self.指纹 = 隧.指纹
        self._记('公钥指纹（手机要钉的）：%s' % self.指纹)

    def 停止(self):
        """停隧道。没在跑就当没听见。"""
        隧 = self._隧道
        self._隧道 = None
        if 隧 is not None:
            隧.停止()

    def 地址们(self):
        """手机端该填的地址们：`<局域网IP>:<端口>`（隧道不是 HTTP，别加 http://）。"""
        return ['%s:%d' % (地址, self.端口) for 地址 in 局域网地址们()]

    # ── 方法分发 ──
    def _方法(self, 连, 方法, 参数):
        if not isinstance(参数, dict):
            参数 = {}
        表 = {
            'ping': self._ping,
            '推荐': self._推荐,
            'chat': self._chat,
            '下载': self._下载_,
            '下载取消': self._下载取消,
            '下载状态': self._下载状态,
            '残件': self._残件,
            '删残件': self._删残件,
            '上传状态': self._上传状态,
            '上传': self._上传_,
            '上传备好': self._上传备好,
            '上传段': self._上传段,
            '上传收尾': self._上传收尾,
            '上传丢弃': self._上传丢弃,
        }
        f = 表.get(方法)
        if f is None:
            连.发ERR('没有这个方法：%s' % 方法)
            return
        f(连, 参数)

    # ── ping ──
    def _ping(self, 连, 参数):
        import 酒馆本地
        可用 = 酒馆本地.可用()
        本 = 酒馆本地.列本地()          # [(文件名, 字节数)]
        连.发RES({
            '应用': '鸿胪寺',
            '版本': 2,
            '协议': 'honglu-tunnel',
            '后端': 酒馆本地.后端说明(),
            '可用': bool(可用),
            '说明': '' if 可用 else 酒馆本地.不可用原因(),
            '模型': [名 for 名, _ in 本],
            '模型明细': [{'名': 名, '字节': 大} for 名, 大 in 本],
        })

    # ── 精选推荐 ──
    def _推荐(self, 连, 参数):
        import 酒馆本地
        出 = []
        for 档 in (getattr(酒馆本地, '推荐模型', ()) or ()):
            if not isinstance(档, dict):
                continue
            出.append({
                '组': str(档.get('组') or ''),
                '名字': str(档.get('名字') or ''),
                '用途': str(档.get('用途') or '通用'),
                '仓库': str(档.get('仓库') or ''),
                '模式': str(档.get('模式') or ''),
                '约字节': int(档.get('约字节') or 0),
                '源': 酒馆本地.首选源(档),
            })
        连.发RES({
            '脚注': str(getattr(酒馆本地, '精选脚注', '') or ''),
            '条目': 出,
        })

    # ── chat ──
    def _chat(self, 连, 参数):
        import 酒馆本地
        模型名 = str(参数.get('模型') or '').strip()
        清单 = [名 for 名, _ in 酒馆本地.列本地()]
        if not 模型名:
            连.发ERR('没给模型名。用 ping 列一下本机有哪些。'); return
        if 模型名 not in 清单:
            连.发ERR('本机没有这个模型：%s' % 模型名); return

        参 = 参数.get('参数') or {}
        if not isinstance(参, dict):
            参 = {}
        参 = dict(参)
        参.setdefault('n_gpu_layers', self.gpu层)
        径 = 酒馆本地.定位(模型名)

        try:
            if ('角色' in 参数) or ('设定' in 参数):
                系统, 消息数组, 控 = self._拼v2(参数, 模型名, 参)
            else:
                系统, 消息数组, 控 = self._拼v1(参数)
        except _信封错 as 错:
            连.发ERR(str(错)); return
        if not 消息数组:
            连.发ERR('「消息」里一条能用的都没有'); return

        with self._生成锁:
            流 = None
            try:
                流 = 酒馆本地.流式块(系统, 消息数组, 参, 径, 控)
                首 = next(流, None)          # 触发加载：失败在这儿现形
            except Exception as 错:
                if 流 is not None:
                    try:
                        流.close()
                    except Exception:
                        pass
                连.发ERR(str(错)); return
            try:
                self._记('开始生成：%s' % 模型名)
                if 首:
                    连.发CHUNK(首)
                for 段 in 流:
                    连.发CHUNK(段)
                连.发END({})
            except (BrokenPipeError, ConnectionError, OSError):
                self._记('客户端断开，生成中止')
            except Exception as 错:
                try:
                    连.发ERR(str(错))
                except Exception:
                    pass
            finally:
                # ⚠ **必须显式关掉生成器。** `酒馆本地.流式块` 整条生成都握着
                # 引擎那把 `锁`；客户端中途断开时这个 for 是被异常打断的，
                # 生成器还停在 `yield` 上、锁**没放** —— 下一次请求就干等。
                if 流 is not None:
                    try:
                        流.close()
                    except Exception:
                        pass

    def _拼v1(self, 参数):
        """老信封：系统 + 消息[role/content] + 控制。**原样收下。**"""
        系统 = str(参数.get('系统') or '')
        消息们 = 参数.get('消息') or []
        if not isinstance(消息们, list):
            raise _信封错('「消息」得是数组')
        清 = []
        for 条 in 消息们:
            if not isinstance(条, dict):
                continue
            role = str(条.get('role') or '')
            if role not in ('user', 'assistant'):
                continue
            清.append({'role': role, 'content': str(条.get('content') or '')})
        控 = 参数.get('控制') or {}
        if not isinstance(控, dict):
            控 = {}
        return 系统, 清, 控

    def _拼v2(self, 参数, 模型名, 参):
        """新信封：**原始** 角色卡 + 消息 + 设定 + 记忆 → 电脑自己拼提示 + 控制。"""
        import 酒馆模型
        import 酒馆大脑
        import 酒馆协议

        角色 = None
        r = 参数.get('角色')
        if isinstance(r, dict):
            try:
                角色 = 酒馆模型.角色卡.从行(r)
            except Exception:
                角色 = None

        消息们 = []
        for 条 in (参数.get('消息') or []):
            if not isinstance(条, dict):
                continue
            说话 = str(条.get('说话人') or '用户')
            if 说话 not in ('用户', '角色', '系统'):
                说话 = '用户'
            消息们.append(酒馆模型.消息(说话人=说话, 内容=str(条.get('内容') or '')))
        if not 消息们:
            raise _信封错('「消息」里一条能用的都没有（说话人只收 用户/角色/系统）')

        设定 = dict(参数.get('设定') or {})
        记忆 = 参数.get('记忆')
        if 记忆:
            设定['__记忆__'] = str(记忆)

        套 = 酒馆模型.接口配置(
            名称='互联', 协议='local', 模型=模型名,
            采样参数=json.dumps({k: 参[k] for k in ('n_ctx', 'n_gpu_layers') if k in 参},
                               ensure_ascii=False))
        _尾部, 系统, 数组 = 酒馆大脑.窗口内(角色, None, 消息们, 设定, 套)
        控 = 酒馆协议.取控制(设定)
        return 系统, 数组, 控

    # ── 远程下载：手机遥控电脑下 ──
    def _下载_(self, 连, 参数):
        import 酒馆本地
        仓库 = str(参数.get('仓库') or '').strip()
        文件 = str(参数.get('文件') or '').strip()
        源标 = str(参数.get('源') or 酒馆本地.默认源).strip()
        if not 仓库 or not 文件:
            连.发ERR('「仓库」和「文件」都得有'); return
        名 = _净名(os.path.basename(文件))
        if not 名:
            连.发ERR('文件名得是个 .gguf'); return
        try:
            _查源(源标)
        except Exception as 错:
            连.发ERR(str(错)); return

        任务 = 下载任务(仓库, 文件, 源标)
        self.换下载(任务)
        任务.开始()
        self._记('远程下载：%s @ %s' % (名, 源标))
        self._吐下载进度(连, 任务)

    def _下载取消(self, 连, 参数):
        self.取消下载()
        连.发RES({'已请求取消': True})

    def _下载状态(self, 连, 参数):
        """
        手机来问一句：那个远程下载这会儿到哪了。

        ⚠ **一次一问，不占长连接。** 这是「下载到：电脑」能在手机熄屏后照常的原因 ——
        手机不再守一条长连接看进度（守必然被熄屏弄断），改成隔一会儿短问一句：
        断了就下次再问，电脑那边**在自己的线程里该下还下**（见 `_吐下载进度` 的兜底）。
        """
        with self._下载锁:
            任务 = self._下载
        if 任务 is None:
            连.发RES({'有': False})
            return
        快 = 任务.快照()
        连.发RES({
            '有': True, '名': 任务.名, '已': 快['已'], '总': 快['总'], '阶段': 快['阶段'],
            '完': 快['完'], '错': 快['错'], '取消': 快['取消'],
        })

    # ── 残件：没下完的半成品（`.part`）──

    def _残件(self, 连, 参数):
        """
        列电脑上没下完的 `.part`（下载中途失败的**残留**）。

        手机拿去问用户"留不留"：留着下次下同一个能**接着下**（断点续传），删掉就从头再来。
        """
        import 酒馆本地
        目录 = 酒馆本地.模型目录()
        try:
            名们 = sorted(os.listdir(目录))
        except OSError:
            名们 = []
        项 = []
        for 名 in 名们:
            if not 名.endswith('.part'):
                continue
            径 = os.path.join(目录, 名)
            if os.path.isfile(径):
                项.append({'名': 名[:-5], '字节': os.path.getsize(径)})
        连.发RES({'项': 项})

    def _删残件(self, 连, 参数):
        """删掉某个没下完的半成品。"""
        import 酒馆本地
        名 = _净名(参数.get('名'))
        if not 名:
            连.发ERR('文件名得是个 .gguf'); return
        径 = os.path.join(酒馆本地.模型目录(), 名 + '.part')
        try:
            if os.path.isfile(径):
                os.remove(径)
        except OSError as 错:
            连.发ERR('删不掉：%s' % 错); return
        连.发RES({'删了': True})

    def _吐下载进度(self, 连, 任务):
        上次已 = -1
        try:
            连.发CHUNK(json.dumps({'阶段': '开始', '名': 任务.名, '总': 任务.总},
                                 ensure_ascii=False))
            while True:
                快 = 任务.快照()
                if 快['已'] != 上次已 or 快['完'] or 快['错'] or 快['取消']:
                    上次已 = 快['已']
                    连.发CHUNK(json.dumps({'已': 快['已'], '总': 快['总'],
                                          '阶段': 快['阶段'], '名': 任务.名},
                                         ensure_ascii=False))
                if 快['完']:
                    self._记('远程下载完成：%s（%.1f MB）' % (任务.名, (快['总'] or 0) / 1e6))
                    连.发END({'完': True, '文件': 任务.名}); return
                if 快['错']:
                    self._记('远程下载失败：%s —— %s' % (任务.名, 快['错']))
                    连.发ERR(快['错']); return
                if 快['取消']:
                    self._记('远程下载已取消：%s' % 任务.名)
                    连.发END({'取消': True, '阶段': '已取消'}); return
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionError, OSError):
            self._记('下载进度连接断开，下载继续在电脑上进行')

    # ── 上传：手机把本地 GGUF 推上来 ──
    def _上传状态(self, 连, 参数):
        import 酒馆本地
        名 = _净名(参数.get('名'))
        if not 名:
            连.发ERR('文件名得是个 .gguf'); return
        目录 = 酒馆本地.模型目录()
        终 = os.path.join(目录, 名)
        半 = 终 + '.part'
        已收 = os.path.getsize(半) if os.path.isfile(半) else 0
        已有 = os.path.getsize(终) if os.path.isfile(终) else 0
        try:
            空闲 = shutil.disk_usage(目录).free
        except OSError:
            空闲 = -1
        连.发RES({'名': 名, '已收': 已收, '已有': 已有, '空闲': 空闲})

    def _上传_(self, 连, 参数):
        import 酒馆本地
        名 = _净名(参数.get('名'))
        if not 名:
            连.发ERR('文件名得是个 .gguf'); return
        try:
            偏移 = int(参数.get('偏移') or 0)
            总大小 = int(参数.get('总大小') or 0)
        except (TypeError, ValueError):
            连.发ERR('偏移 / 总大小得是整数'); return
        if 偏移 < 0 or 总大小 < 0:
            连.发ERR('偏移 / 总大小不能为负'); return

        目录 = 酒馆本地.模型目录()
        终 = os.path.join(目录, 名)
        半 = 终 + '.part'
        try:
            空闲 = shutil.disk_usage(目录).free
            if 总大小 and 总大小 + 64 * 1024 * 1024 > 空闲:
                连.发ERR('电脑磁盘空间不够。'); return
        except OSError:
            pass

        if 偏移 == 0:
            模式 = 'wb'
        else:
            现 = os.path.getsize(半) if os.path.isfile(半) else 0
            if 现 != 偏移:
                连.发ERR('偏移对不上，按这个续传：已收=%d' % 现); return
            模式 = 'ab'

        self.置上传({'名': 名, '已': 偏移, '总': 总大小 or 0, '阶段': '接收中'})
        已写 = 偏移
        try:
            with open(半, 模式) as 出:
                while True:
                    帧 = 连.读帧()
                    if 帧 is None:
                        return
                    typ, _stream, payload = 帧
                    if typ == 酒馆隧道.T_CHUNK:
                        出.write(payload); 已写 += len(payload)
                        self.置上传({'名': 名, '已': 已写, '总': 总大小 or 已写,
                                    '阶段': '接收中'})
                    elif typ == 酒馆隧道.T_END:
                        break
        except OSError as 错:
            连.发ERR('写文件失败：%s' % 错); return
        finally:
            self.置上传(None)

        已收 = os.path.getsize(半)
        完 = 总大小 > 0 and 已收 >= 总大小
        if 完:
            try:
                with open(半, 'rb') as f:
                    头 = f.read(4)
            except OSError:
                头 = b''
            if 头 != b'GGUF':
                try:
                    os.remove(半)
                except OSError:
                    pass
                连.发ERR('收到的不是 GGUF（开头不是「GGUF」），已丢弃。'); return
            try:
                os.replace(半, 终)
            except OSError as 错:
                连.发ERR('改名失败：%s' % 错); return
            self._记('收到模型：%s（%.1f MB）' % (名, 已收 / 1e6))
        连.发RES({'已收': 已收, '完': 完})

    # ── 并行上传：切 K 段、K 条隧道同时传（大文件提速）──────────────────
    #
    # 思路借鉴 TUIC 的"多路"设计，落在 TCP 上就是**多条并发连接**：单条 TCP 在
    # WiFi 上常被"延迟×窗口"卡住，劈成几条并发能把带宽吃满（aria2 / hf_transfer 同理）。
    #
    # 三段式：`上传备好`（建一个空文件）→ K 条连接各自 `上传段`（seek 到自己那段写）
    # → `上传收尾`（校验 GGUF、改名）。**并行模式不做断点续传**（分片会留空洞）；
    # 断了就退回单连接那套 `上传`（它按前缀续传，慢但准）。

    def _上传备好(self, 连, 参数):
        import 酒馆本地
        名 = _净名(参数.get('名'))
        if not 名:
            连.发ERR('文件名得是个 .gguf'); return
        try:
            总大小 = int(参数.get('总大小') or 0)
        except (TypeError, ValueError):
            连.发ERR('总大小得是整数'); return
        if 总大小 <= 0:
            连.发ERR('总大小得大于 0'); return
        目录 = 酒馆本地.模型目录()
        终 = os.path.join(目录, 名)
        半 = 终 + '.part'
        if os.path.isfile(终) and os.path.getsize(终) == 总大小:
            连.发RES({'已有': True}); return
        try:
            空闲 = shutil.disk_usage(目录).free
            if 总大小 + 64 * 1024 * 1024 > 空闲:
                连.发ERR('电脑磁盘空间不够。'); return
        except OSError:
            pass
        try:
            # 建一个刚好这么大的空文件，供各段 seek 到自己的偏移去写。
            with open(半, 'wb') as f:
                f.truncate(总大小)
        except OSError as 错:
            连.发ERR('建文件失败：%s' % 错); return
        连.发RES({'备好': True, '总大小': 总大小})

    def _上传段(self, 连, 参数):
        import 酒馆本地
        名 = _净名(参数.get('名'))
        if not 名:
            连.发ERR('文件名得是个 .gguf'); return
        try:
            偏移 = int(参数.get('偏移') or 0)
            长度 = int(参数.get('长度') or 0)
        except (TypeError, ValueError):
            连.发ERR('偏移 / 长度得是整数'); return
        if 偏移 < 0 or 长度 <= 0:
            连.发ERR('偏移不能为负、长度得大于 0'); return
        半 = os.path.join(酒馆本地.模型目录(), 名 + '.part')
        if not os.path.isfile(半):
            连.发ERR('先调「上传备好」'); return
        self.置上传({'名': 名, '已': 偏移, '总': 偏移 + 长度, '阶段': '接收中'})
        写 = 0
        try:
            with open(半, 'r+b') as 出:
                出.seek(偏移)
                while 写 < 长度:
                    帧 = 连.读帧()
                    if 帧 is None:
                        return
                    typ, _s, payload = 帧
                    if typ == 酒馆隧道.T_CHUNK:
                        if payload:
                            出.write(payload); 写 += len(payload)
                    elif typ == 酒馆隧道.T_END:
                        break
        except OSError as 错:
            连.发ERR('写文件失败：%s' % 错); return
        finally:
            self.置上传(None)
        if 写 < 长度:
            连.发ERR('这一段没传完：收到 %d / %d 字节' % (写, 长度)); return
        连.发RES({'已收': 偏移 + 写})

    def _上传收尾(self, 连, 参数):
        import 酒馆本地
        名 = _净名(参数.get('名'))
        if not 名:
            连.发ERR('文件名得是个 .gguf'); return
        try:
            总大小 = int(参数.get('总大小') or 0)
        except (TypeError, ValueError):
            连.发ERR('总大小得是整数'); return
        目录 = 酒馆本地.模型目录()
        终 = os.path.join(目录, 名)
        半 = 终 + '.part'
        if not os.path.isfile(半):
            连.发ERR('没有半成品可收尾'); return
        实 = os.path.getsize(半)
        if 总大小 and 实 != 总大小:
            连.发ERR('尺寸对不上：%d / %d' % (实, 总大小)); return
        try:
            with open(半, 'rb') as f:
                头 = f.read(4)
        except OSError:
            头 = b''
        if 头 != b'GGUF':
            try:
                os.remove(半)
            except OSError:
                pass
            连.发ERR('收到的不是 GGUF（开头不是「GGUF」），已丢弃。'); return
        try:
            os.replace(半, 终)
        except OSError as 错:
            连.发ERR('改名失败：%s' % 错); return
        self._记('收到模型：%s（%.1f MB，并行）' % (名, 实 / 1e6))
        连.发RES({'已收': 实, '完': True})

    def _上传丢弃(self, 连, 参数):
        """
        把某个半成品删掉。

        ⚠ **并行上传失败后必须调它。** 并行分片是随机写下标的，中途失败会留下
        **空洞**（文件大小看着是够的、中间却是空的）；这时若下次走"前缀续传"，
        就会在空洞后面接着拼，**拼出一个坏文件**。删了重来最干净。
        """
        import 酒馆本地
        名 = _净名(参数.get('名'))
        if not 名:
            连.发ERR('文件名得是个 .gguf'); return
        半 = os.path.join(酒馆本地.模型目录(), 名 + '.part')
        删了 = False
        try:
            if os.path.isfile(半):
                os.remove(半); 删了 = True
        except OSError:
            pass
        连.发RES({'已删': 删了})


# ── 单文件自测：`python 酒馆互联.py` ────────────────────────────────
#
# 不起界面，直接把隧道跑起来（拿同口令的客户端连）。Ctrl+C 收工。

if __name__ == '__main__':
    import sys

    存 = 读配置()
    服 = 服务(端口=存['端口'], 口令=存['口令'])
    try:
        服.启动()
    except Exception as 错:
        print('起不来：%s：%s' % (type(错).__name__, 错))
        sys.exit(1)

    print('鸿胪寺隧道已启动，端口 %d' % 服.端口)
    print('公钥指纹：%s' % 服.指纹)
    if not 服.口令:
        print('⚠ 口令为空：任何知道地址的人都能连，也不校验身份。')
    for 址 in 服.地址们():
        print('  手机端地址：%s' % 址)
    print('按 Ctrl+C 停止。')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n收工。')
        服.停止()
