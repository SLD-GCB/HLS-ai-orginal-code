"""照软件里那条失败的命令行，单独复现那两个文件的编译。

命令行是从用户贴的日志里抄的（文件、宏、开关、顺序都一样），只把 pip 的临时路径
换成我解包出来的这份。
"""
import os
import subprocess
import sys
import tarfile

根 = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
sys.path.insert(0, 根)
import 酒馆编译                                   # noqa: E402

包 = os.path.join(根, 'dist', '编译环境')
解 = os.path.join(根, '构建缓存', '_复现')
SDIST = os.path.join(根, '构建缓存', 'llama_cpp_python-0.3.35.tar.gz')

if not os.path.isdir(解):
    os.makedirs(解)
    print('解包 sdist…')
    with tarfile.open(SDIST) as t:
        t.extractall(解)

LL = None
for 子 in os.listdir(解):
    候 = os.path.join(解, 子, 'vendor', 'llama.cpp')
    if os.path.isdir(候):
        LL = 候
assert LL, '没找到 vendor/llama.cpp'
print('llama.cpp:', LL)

os.environ['酒馆_编译环境'] = 包
环 = 酒馆编译.铺环境('120')
cl = os.path.join(包, 'VS', 'VC', 'Tools', 'MSVC', '14.44.35207', 'bin', 'Hostx64', 'x64', 'cl.exe')
print('cl.exe 存在:', os.path.isfile(cl))
print('VSCMD_VER =', 环.get('VSCMD_VER'))

出目 = os.path.join(解, 'obj')
os.makedirs(出目, exist_ok=True)
SRC = os.path.join(LL, 'src')
GGML = os.path.join(LL, 'ggml', 'src')

命令头 = [cl, '/nologo', '/TP',
        '-DGGML_BACKEND_SHARED', '-DGGML_SHARED', '-DGGML_USE_CPU', '-DGGML_USE_CUDA',
        '-DLLAMA_BUILD', '-DLLAMA_COMMIT="4df29be"', '-DLLAMA_SHARED',
        '-DLLAMA_VERSION="0.1.0-dev"', '-D_CRT_SECURE_NO_WARNINGS', '-Dllama_EXPORTS',
        '-I' + SRC, '-I' + os.path.join(SRC, '..', 'include'),
        '-I' + os.path.join(GGML, '..', 'include'),
        '/DWIN32', '/D_WINDOWS', '/EHsc', '/O2', '/Ob2', '/DNDEBUG',
        '-std:c++17', '-MD', '/utf-8', '/bigobj',
        '/Fo' + 出目 + os.sep, '/Fd' + 出目 + os.sep, '/FS']

for 相 in ('src/models/mistral4.cpp', 'src/unicode-data.cpp', 'src/llama-graph.cpp'):
    源 = os.path.join(LL, *相.split('/'))
    if not os.path.isfile(源):
        print('[跳过] 没有这个文件：', 相)
        continue
    备 = subprocess.run(命令头 + ['-c', 源], env=环, capture_output=True, timeout=1800)
    纹 = (备.stdout + 备.stderr).decode('gbk', 'replace')
    print('=' * 70)
    print('%s  →  cl 退出码 %d' % (相, 备.returncode))
    留 = [行 for 行 in 纹.splitlines()
         if not 行.startswith('注意: 包含文件') and not 行.startswith('Note: including file')]
    print('\n'.join(留[:25]) or '（没有输出）')
