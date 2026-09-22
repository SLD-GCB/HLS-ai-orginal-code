"""验证解法：把「编译环境」用一个纯 ASCII 的目录联接暴露出来，再从那儿链。
对照 C：链接 <ASCII联接>/CUDA/lib/x64/cudart.lib（强制走 rsp）
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

ASCII根 = 'C:/ProgramData/HL_CompileEnv_Test'
if not os.path.isdir(ASCII根):
    os.makedirs(os.path.dirname(ASCII根), exist_ok=True)
    r = subprocess.run(['cmd', '/c', 'mklink', '/J',
                        ASCII根.replace('/', os.sep),
                        os.path.join(根, 'dist', '编译环境')],
                       capture_output=True)
    print('建联接:', r.returncode, r.stdout.decode('gbk', 'replace').strip()[:100])
print('联接里的 CUDA 在吗:', os.path.isdir(os.path.join(ASCII根, 'CUDA')))
print('联接里的 VS 骨架在吗:',
      os.path.isfile(os.path.join(ASCII根, 'VS', 'VC', 'Auxiliary', 'Build', 'vcvarsall.bat')))

CM = ('cmake_minimum_required(VERSION 3.20)\n'
      'project(pathtest C)\n'
      'add_executable(pathtest main.c)\n'
      'target_link_libraries(pathtest PRIVATE "%s")\n')
open(os.path.join(项目, 'main.c'), 'w').write('int main(void){return 0;}\n')

库 = ASCII根 + '/CUDA/lib/x64/cudart.lib'          # ⚠ 必须正斜杠：CMake 字符串里反斜杠是转义符
构 = os.path.join(测根, 'build_C_ASCII联接')
if os.path.isdir(构):
    shutil.rmtree(构)
open(os.path.join(项目, 'CMakeLists.txt'), 'w', encoding='utf-8').write(CM % 库)

# ⚠ 环境也整个改从 ASCII 联接走 —— 这是真正要验的形态
os.environ['酒馆_编译环境'] = ASCII根.replace('/', os.sep)
环 = 酒馆编译.铺环境('120')
cmake = os.path.join(ASCII根, 'Python', 'Lib', 'site-packages', 'cmake', 'data', 'bin', 'cmake.exe')
print('工具集:', 酒馆编译.工具集()[0])
print('齐不齐:', 酒馆编译.齐不齐())

p = subprocess.run([cmake, '-S', 项目, '-B', 构, '-G', 'Ninja',
                    '-DCMAKE_NINJA_FORCE_RESPONSE_FILE=ON'],
                   env=环, capture_output=True, timeout=600)
q = subprocess.run([cmake, '--build', 构], env=环, capture_output=True, timeout=600)
print('=== C 走 ASCII 联接 + 强制 rsp ===')
print('  库文件在吗:', os.path.isfile(库))
print('  configure=%d  build=%d  产物=%s'
      % (p.returncode, q.returncode, os.path.isfile(os.path.join(构, 'pathtest.exe'))))
if q.returncode:
    纹 = (q.stdout + q.stderr).decode('gbk', 'replace')
    for 行 in 纹.splitlines():
        if 'LNK' in 行 or 'ninja' in 行 or 'FAILED' in 行:
            print('   ', 行[:200])
