"""拿一个「假包」把新写的两半串起来验一次：打包.py 的摆法 + 酒馆编译.py 的铺环境()。

假包用目录联接搭（拷贝 2.5GB 没必要，验的是**形状和 env**，不是拷贝）。
"""
import os
import subprocess
import sys

根 = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
sys.path.insert(0, 根)
import 打包                                    # noqa: E402
import 酒馆编译                                 # noqa: E402

CUDA = r'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.3'
VSDIR = r'C:\Program Files\Microsoft Visual Studio\2022\Community'
TOOLS = VSDIR + r'\VC\Tools\MSVC\14.44.35207'
CRT = VSDIR + r'\VC\Redist\MSVC\14.44.35112\x64\Microsoft.VC143.CRT'
KITS = r'C:\Program Files (x86)\Windows Kits\10'
SDKVER = '10.0.26100.0'
PY = r'D:\python13.3'

假包 = os.path.join(根, '构建缓存', '_假包')
编 = os.path.join(假包, '编译环境')


def 联(目标, 真):
    if os.path.isdir(目标):
        return
    os.makedirs(os.path.dirname(目标), exist_ok=True)
    r = subprocess.run(['cmd', '/c', 'mklink', '/J', 目标, 真], capture_output=True)
    if r.returncode:
        print('联接失败:', 目标, r.stderr.decode('gbk', 'replace')[:200])


联(os.path.join(编, 'CUDA'), CUDA)
联(os.path.join(编, 'Python'), PY)
联(os.path.join(编, 'VS', 'VC', 'Tools', 'MSVC', '14.44.35207'), TOOLS)
联(os.path.join(编, 'VS', 'VC', 'Redist', 'CRT'), CRT)
联(os.path.join(编, 'WinSDK', 'bin'), os.path.join(KITS, 'bin', SDKVER, 'x64'))
for 子 in ('ucrt', 'um', 'shared'):
    联(os.path.join(编, 'WinSDK', 'Include', 子),
        os.path.join(KITS, 'Include', SDKVER, 子))
for 子 in ('ucrt', 'um'):
    联(os.path.join(编, 'WinSDK', 'Lib', 子, 'x64'),
        os.path.join(KITS, 'Lib', SDKVER, 子, 'x64'))

件数 = 打包.铺vs骨架(编)
print('铺vs骨架 写了 %d 个文件' % 件数)

os.environ['酒馆_编译环境'] = 编
print('环境目录 :', 酒馆编译.环境目录())
print('工具集   :', 酒馆编译.工具集())
print('齐不齐   :', 酒馆编译.齐不齐())

环 = 酒馆编译.铺环境('120')
print('VSCMD_VER:', 环.get('VSCMD_VER'))
print('CMAKE_ARGS:', 环.get('CMAKE_ARGS'))
print('PATH 前 6 段:')
for 段 in 环['PATH'].split(os.pathsep)[:6]:
    print('   ', 段)

源 = os.path.join(根, '构建缓存', '_nvcc测', 'a.cu')
出 = os.path.join(根, '构建缓存', '_nvcc测', '_假包产物.obj')
if os.path.isfile(出):
    os.remove(出)
p = subprocess.run([os.path.join(编, 'CUDA', 'bin', 'nvcc.exe'), '-c', 源, '-o', 出],
                   env=环, capture_output=True, timeout=900)
print()
print('=== 用假包里那套环境真编一次 ===')
print('退出码=%d  产物=%s' % (p.returncode, os.path.isfile(出) and os.path.getsize(出) > 0))
if p.returncode:
    纹 = (p.stdout + p.stderr).decode('gbk', 'replace')
    print('\n'.join(x for x in 纹.splitlines() if 'fatal' in x or 'error' in x.lower())[:600])
