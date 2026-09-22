"""最小复现：CMake+Ninja+MSVC 链接一个「路径带中文」的 .lib。

对照 A：链真身 CUDA 的 cudart.lib（全 ASCII 路径）
对照 B：链包里那份 cudart.lib（路径带中文）
除路径外，环境、工具、代码全都一样。
"""
import os
import shutil
import subprocess
import sys

根 = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
sys.path.insert(0, 根)
import 酒馆编译                                   # noqa: E402

包 = os.path.join(根, 'dist', '编译环境')
测根 = os.path.join(根, '构建缓存', '_路径测')
项目 = os.path.join(测根, '项目')
os.makedirs(项目, exist_ok=True)
open(os.path.join(项目, 'main.c'), 'w').write('int main(void){return 0;}\n')

CM = ('cmake_minimum_required(VERSION 3.20)\n'
      'project(pathtest C)\n'
      'add_executable(pathtest main.c)\n'
      'target_link_libraries(pathtest PRIVATE "%s")\n')

库们 = (
    ('A_真身_全ASCII',
     'C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v13.3/lib/x64/cudart.lib'),
    ('B_包内_带中文',
     os.path.join(包, 'CUDA', 'lib', 'x64', 'cudart.lib').replace(os.sep, '/')),
)

os.environ['酒馆_编译环境'] = 包
环 = 酒馆编译.铺环境('120')
cmake = os.path.join(包, 'Python', 'Lib', 'site-packages', 'cmake', 'data', 'bin', 'cmake.exe')

for 名, 路 in 库们:
    构 = os.path.join(测根, 'build_' + 名)
    if os.path.isdir(构):
        shutil.rmtree(构)
    open(os.path.join(项目, 'CMakeLists.txt'), 'w', encoding='utf-8').write(CM % 路)
    p = subprocess.run([cmake, '-S', 项目, '-B', 构, '-G', 'Ninja',
                        '-DCMAKE_NINJA_FORCE_RESPONSE_FILE=ON'],
                       env=环, capture_output=True, timeout=600)
    q = subprocess.run([cmake, '--build', 构], env=环, capture_output=True, timeout=600)
    print('=== %s ===' % 名)
    print('   库文件在吗:', os.path.isfile(路))
    print('   configure=%d  build=%d  产物=%s'
          % (p.returncode, q.returncode, os.path.isfile(os.path.join(构, 'pathtest.exe'))))
    if q.returncode:
        纹 = (q.stdout + q.stderr).decode('gbk', 'replace')
        for 行 in 纹.splitlines():
            if 'LNK' in 行 or 'ninja' in 行 or 'FAILED' in 行:
                print('   ', 行[:220])
