"""复现昨晚在软件里死掉的那一步：CMake configure（enable_language(CUDA)）。

只做 configure，不编译 —— 失败点在 configure，真编一轮要几十分钟没必要。
"""
import os
import subprocess
import sys
import tarfile

根 = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
sys.path.insert(0, 根)
import 酒馆编译                                   # noqa: E402

假包 = os.path.join(根, '构建缓存', '_假包')
编 = os.path.join(假包, '编译环境')
解 = os.path.join(假包, 'src')
SDIST = os.path.join(根, '构建缓存', 'llama_cpp_python-0.3.35.tar.gz')

if not os.path.isdir(解):
    os.makedirs(解)
    print('解包 sdist…')
    with tarfile.open(SDIST) as t:
        t.extractall(解)

# 找 vendor/llama.cpp
源 = None
for 子 in os.listdir(解):
    候 = os.path.join(解, 子, 'vendor', 'llama.cpp')
    if os.path.isdir(候):
        源 = 候
        break
print('llama.cpp 源:', 源)
assert 源

os.environ['酒馆_编译环境'] = 编
环 = 酒馆编译.铺环境('120')

构建 = os.path.join(假包, 'build')
cmake = os.path.join(编, 'Python', 'Lib', 'site-packages', 'cmake', 'data', 'bin', 'cmake.exe')
print('cmake:', cmake, os.path.isfile(cmake))

命令 = [cmake, '-S', 源, '-B', 构建, '-G', 'Ninja',
        '-DGGML_CUDA=on', '-DCMAKE_CUDA_ARCHITECTURES=120']
for 名, 相 in (('CMAKE_RC_COMPILER', 'WinSDK/bin/rc.exe'), ('CMAKE_MT', 'WinSDK/bin/mt.exe')):
    命令.append('-D%s=%s' % (名, os.path.join(编, *相.split('/')).replace('\\', '/')))

p = subprocess.run(命令, env=环, capture_output=True, timeout=1800)
纹 = (p.stdout + p.stderr).decode('gbk', 'replace')
print('=== cmake configure 退出码 %d ===' % p.returncode)
关键 = [x for x in 纹.splitlines()
       if any(t in x for t in ('CUDA', 'cuda', 'error', 'Error', 'fatal', 'unsupported OS',
                               'compiler', 'Configuring', 'Generating'))]
print('\n'.join(关键[-25:]))
