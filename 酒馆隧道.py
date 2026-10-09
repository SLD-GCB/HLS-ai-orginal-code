#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酒馆隧道.py — **honglu-tunnel v1** 传输层（加密隧道，不含业务）

两端不再走 HTTP。这一层是一条 **TCP + TLS 1.3 的隧道**：

  · **加密**：标准 TLS（OpenSSL）。**密码学一行不自己造。**
  · **身份**：服务端证书私钥由「口令」派生（P-256）；客户端用同一口令推出
    同一把公钥，**只认它** —— 不知道口令的人连不上，也冒充不了。
  · **分帧**：TLS 之上是自有的二进制帧（见下），一条隧道可跑多路（stream）。
  · **并发**：大文件开多条隧道（连接）分段并发 —— 见 `酒馆互联`。

帧格式（TLS 之上）：
    连接建立后服务端先发魔数 b"HLS1"
    每帧：[len:4 大端][type:1][stream:4 大端][payload: len-5 字节]
    type: 1 REQ / 2 RES / 3 CHUNK / 4 END / 5 ERR / 7 HELLO
        REQ.payload = JSON {"方法":..,"参数":..}
        RES/END/ERR.payload = JSON(UTF-8)；CHUNK.payload = 原始字节

握手（TLS 之上再补一道口令双向认证）：
    服务端 → HELLO{nonce}
    客户端 → REQ(auth,{mac=HMAC(口令,"client"|nonce)})    口令空则免
    服务端 → RES{mac2=HMAC(口令,"server"|nonce)}；客户端校验

**取消**：客户端**直接断开连接**即可（服务端下次写帧会失败 → 收尾）。
需要"让电脑别下这个大模型"另有 `下载取消` 方法（另开一条连接发）。

⚠ 一个连接**同一时刻只跑一个方法**（顺序处理）。要并发就开多条连接 ——
  这正是"多连接提速"的来源，也省掉了连接内多路的状态管理。
"""
import hashlib
import hmac
import json
import os
import socket
import socketserver
import ssl
import struct
import tempfile
import threading

__all__ = ['盐', '迭代', '魔数', 'T_REQ', 'T_RES', 'T_CHUNK', 'T_END', 'T_ERR', 'T_HELLO',
           '最大帧', '派生', '原公钥', '公钥指纹', '生成身份', '连接', '服务']

盐 = b'honglu-tunnel-v1'
迭代 = 200000
魔数 = b'HLS1'

T_REQ, T_RES, T_CHUNK, T_END, T_ERR, T_HELLO = 1, 2, 3, 4, 5, 7
最大帧 = 8 * 1024 * 1024            # 单帧上限（CHUNK 也在这个以内）

#: P-256 的阶 n（派生私钥标量时用它取模）。
_P256阶 = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def 派生(口令):
    """口令 → 32 字节密钥材料（PBKDF2-HMAC-SHA256）。**两端必须一致。**"""
    return hashlib.pbkdf2_hmac('sha256', (口令 or '').encode('utf-8'), 盐, 迭代, 32)


def 原公钥(钥):
    """从私钥对象拿公钥对象（cryptography）。"""
    return 钥.public_key()


def 公钥指纹(钥):
    """
    公钥（SubjectPublicKeyInfo DER）的 SHA-256，小写十六进制。

    ⚠ **这是给手机"钉"的那串。** 别去钉"整张证书的 SHA-256"——证书里有随机
    序列号和有效期，每次生成都不一样；只有**公钥**才是"由口令唯一决定、稳定"的。
    """
    from cryptography.hazmat.primitives import serialization
    spki = 钥.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(spki).hexdigest()


def 生成身份(口令):
    """口令 → (私钥对象, 证书PEM, 私钥PEM, 公钥指纹)。P-256 自签。"""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    k = 派生(口令)
    d = (int.from_bytes(k, 'big') % (_P256阶 - 1)) + 1
    钥 = ec.derive_private_key(d, ec.SECP256R1())

    名 = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'honglu-tunnel')])
    现在 = datetime.datetime.now(datetime.timezone.utc)
    证 = (x509.CertificateBuilder()
          .subject_name(名).issuer_name(名)
          .public_key(钥.public_key())
          .serial_number(x509.random_serial_number())
          .not_valid_before(现在 - datetime.timedelta(days=1))
          .not_valid_after(现在 + datetime.timedelta(days=3650))
          .sign(钥, hashes.SHA256()))
    证PEM = 证.public_bytes(serialization.Encoding.PEM)
    钥PEM = 钥.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    return 钥, 证PEM, 钥PEM, 公钥指纹(钥)


# ── 连接 ────────────────────────────────────────────────────────────

class 连接(object):
    """
    一条隧道上的一个连接。读帧 / 写帧 + 收发语义。

    ⚠ **不是线程安全**：一个连接由一条线程处理（同一时刻一个方法）。
    """

    def __init__(self, 套, 口令=''):
        self._套 = 套
        self._口令 = 口令 or ''
        self._写锁 = threading.Lock()
        self.当前流 = 0
        self.对端 = ''
        try:
            self.对端 = '%s:%s' % 套.getpeername()
        except Exception:
            pass

    # ── 底层 ──
    def _收全(self, n):
        缓 = bytearray()
        while len(缓) < n:
            块 = self._套.recv(n - len(缓))
            if not 块:
                raise ConnectionError('连接断了')
            缓 += 块
        return bytes(缓)

    def 读帧(self):
        """回 `(type, stream, payload)`；连接正常结束回 `None`。"""
        try:
            头 = self._收全(4)
        except (ConnectionError, OSError):
            return None
        (长,) = struct.unpack('>I', 头)
        if 长 < 5 or 长 > 最大帧:
            raise ValueError('帧长度不合法：%d' % 长)
        体 = self._收全(长)
        return 体[0], struct.unpack('>I', 体[1:5])[0], 体[5:]

    def _写帧(self, typ, payload=b'', stream=None):
        if isinstance(payload, str):
            payload = payload.encode('utf-8')
        流 = self.当前流 if stream is None else stream
        包 = struct.pack('>I', 5 + len(payload)) + bytes([typ]) + struct.pack('>I', 流) + payload
        with self._写锁:
            self._套.sendall(包)

    # ── 收发语义 ──
    def 发魔数(self):
        self._套.sendall(魔数)

    def 发HELLO(self, nonce):
        self._写帧(T_HELLO, json.dumps({'nonce': nonce}))

    def 发RES(self, 对象):
        self._写帧(T_RES, json.dumps(对象 or {}, ensure_ascii=False))

    def 发CHUNK(self, 数据):
        self._写帧(T_CHUNK, 数据)

    def 发END(self, 对象=None):
        self._写帧(T_END, json.dumps(对象 or {}, ensure_ascii=False))

    def 发ERR(self, 文):
        self._写帧(T_ERR, json.dumps({'错': str(文)}, ensure_ascii=False))

    def 关(self):
        try:
            self._套.close()
        except Exception:
            pass


# ── 服务端 ──────────────────────────────────────────────────────────

class _TCP(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False      # Windows 上 SO_REUSEADDR 会"抢绑"，学乖了


class _处理(socketserver.BaseRequestHandler):
    def handle(self):
        服 = self.server.隧道对象
        try:
            tls = 服._上下文.wrap_socket(self.request, server_side=True)
        except Exception as 错:
            服._记('TLS 握手失败（%s）：%s' % (self.client_address, 错))
            return
        try:
            服._接待(tls)
        except Exception as 错:
            服._记('连接异常：%s' % 错)
        finally:
            try:
                tls.close()
            except Exception:
                pass


class 服务(object):
    """
    一条 honglu 隧道服务。`处理(连接, 方法, 参数)` 由业务层注入。

    线程模型：每个连接一条线程（`ThreadingTCPServer`），连接内顺序处理。
    """

    def __init__(self, 端口, 口令, 处理, 记=None):
        self.端口 = int(端口)
        self.口令 = 口令 or ''
        self.处理函数 = 处理
        self.记 = 记 or (lambda 文: None)
        self.指纹 = ''
        self._服务器 = None
        self._线程 = None
        self._上下文 = None
        self._证书目录 = None

    def _记(self, 文):
        try:
            self.记(文)
        except Exception:
            pass

    def 在跑(self):
        return self._服务器 is not None

    def 启动(self):
        """起隧道。端口被占之类的错**原样抛给界面**。"""
        if self._服务器 is not None:
            return
        _钥, 证PEM, 钥PEM, 指纹 = 生成身份(self.口令)
        self.指纹 = 指纹
        目 = tempfile.mkdtemp(prefix='honglu-cert-')      # load_cert_chain 要文件路径
        self._证书目录 = 目
        证路 = os.path.join(目, 'cert.pem')
        钥路 = os.path.join(目, 'key.pem')
        with open(证路, 'wb') as f:
            f.write(证PEM)
        with open(钥路, 'wb') as f:
            f.write(钥PEM)
        上下文 = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            上下文.minimum_version = ssl.TLSVersion.TLSv1_3
        except Exception:
            pass
        上下文.load_cert_chain(证路, 钥路)
        self._上下文 = 上下文
        服务器 = _TCP(('0.0.0.0', self.端口), _处理)
        服务器.隧道对象 = self
        线程 = threading.Thread(target=服务器.serve_forever, name='鸿胪隧道', daemon=True)
        线程.start()
        self._服务器, self._线程 = 服务器, 线程
        self._记('隧道已启动，端口 %d' % self.端口)
        self._记('公钥指纹：%s' % self.指纹)

    def 停止(self):
        服务器, 线程 = self._服务器, self._线程
        self._服务器 = self._线程 = None
        if 服务器 is not None:
            try:
                服务器.shutdown()
            except Exception:
                pass
            try:
                服务器.server_close()
            except Exception:
                pass
        if 线程 is not None:
            try:
                线程.join(timeout=3)
            except Exception:
                pass
        if self._证书目录:
            try:
                import shutil
                shutil.rmtree(self._证书目录, ignore_errors=True)
            except Exception:
                pass
            self._证书目录 = None
        self._记('隧道已停止')

    # ── 一个连接的一生 ──
    def _接待(self, tls):
        连 = 连接(tls, self.口令)
        self._记('来了：%s' % (连.对端 or '?'))
        try:
            连.发魔数()
            nonce = os.urandom(16).hex()
            连.发HELLO(nonce)
            已认证 = not self.口令
            while True:
                帧 = 连.读帧()
                if 帧 is None:
                    break
                typ, stream, payload = 帧
                if typ != T_REQ:
                    continue
                try:
                    头 = json.loads(payload.decode('utf-8'))
                except Exception:
                    连.当前流 = stream
                    连.发ERR('REQ 不是合法 JSON')
                    continue
                方法 = str(头.get('方法') or '')
                参数 = 头.get('参数') or {}
                连.当前流 = stream
                if 方法 == 'auth':
                    已认证 = self._认证(连, nonce, 参数)
                    if not 已认证:
                        return
                    continue
                if not 已认证:
                    连.发ERR('先认证')
                    return
                try:
                    self.处理函数(连, 方法, 参数)
                except Exception as 错:
                    self._记('方法「%s」出错：%s' % (方法, 错))
                    try:
                        连.发ERR(str(错))
                    except Exception:
                        pass
        finally:
            连.关()
            self._记('走了：%s' % (连.对端 or '?'))

    def _认证(self, 连, nonce, 参数):
        """校验客户端；回 True/False。口令空则免检。"""
        if not self.口令:
            连.发RES({'mac2': ''})
            return True
        mac = str(参数.get('mac') or '')
        预期 = hmac.new(self.口令.encode('utf-8'), ('client' + nonce).encode('utf-8'),
                       hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, 预期):
            连.发ERR('口令不对')
            return False
        mac2 = hmac.new(self.口令.encode('utf-8'), ('server' + nonce).encode('utf-8'),
                       hashlib.sha256).hexdigest()
        连.发RES({'mac2': mac2})
        return True
