@echo off
setlocal EnableExtensions

rem ============ 先把系统目录补回 PATH ============
rem 这台机器的 cmd 窗口 PATH 里没有 C:\Windows\System32，后果是一连串的：
rem   · xcopy / where 这些系统自带命令都"不是内部或外部命令"
rem   · vcvars64.bat 内部依赖 System32 里的工具，静默失败，不设 SDK 路径
rem   · rc.exe / mt.exe 找不到
rem   · mt.exe 报 0xc0000135（缺 DLL）——它要的 msvcp140.dll 就在 System32 里
rem 所以第一件事就是把 System32 放回最前面，后面才谈得上编译。
set PATH=C:\Windows\System32;C:\Windows;C:\Windows\System32\Wbem;C:\Windows\System32\WindowsPowerShell\v1.0;%PATH%

title 编译 GPU 版 llama-cpp-python（RTX 5050 / sm_120）
echo ==========================================================
echo   为你的 RTX 5050 (sm_120) 编译 llama-cpp-python CUDA 版
echo   预计 20~60 分钟。别关这个窗口，可以正常用电脑。
echo ==========================================================

rem ================= 路径全部写死在这里 =================

set VSDIR=C:\Program Files\Microsoft Visual Studio\2022\Community
set MSVC=%VSDIR%\VC\Tools\MSVC\14.44.35207
set CRT=%VSDIR%\VC\Redist\MSVC\14.44.35112\x64\Microsoft.VC143.CRT
set KITS=C:\Program Files (x86)\Windows Kits\10
set SDKVER=10.0.26100.0

set SDKRT=C:\btmp2\sdk
set NINJAEXE=D:\python13.3\Scripts\ninja.exe
set TMPDIR=C:\btmp2
set PYEXE=D:\python13.3\python.exe
rem =====================================================

if not exist "%TMPDIR%" mkdir "%TMPDIR%"
set TMP=%TMPDIR%
set TEMP=%TMPDIR%

echo [0/3] 环境准备...

rem ---- 头文件搜索路径 ----
set INCLUDE=%MSVC%\include;%KITS%\Include\%SDKVER%\ucrt;%KITS%\Include\%SDKVER%\um;%KITS%\Include\%SDKVER%\shared;%KITS%\Include\%SDKVER%\winrt;%KITS%\Include\%SDKVER%\cppwinrt

rem ---- 库搜索路径（kernel32.lib / ucrt.lib 都在这几个目录里）----
set LIB=%MSVC%\lib\x64;%KITS%\Lib\%SDKVER%\ucrt\x64;%KITS%\Lib\%SDKVER%\um\x64

rem ---- SDK 工具整目录搬到没空格没括号的地方，每次都重搬 ----
rem mt.exe 要和旁边那一堆依赖 DLL 待在一起，光搬 rc/mt 会报 0xc0000135。
echo     搬运 SDK 工具目录（约 71MB，几秒钟）...
if exist "%SDKRT%" rmdir /s /q "%SDKRT%"
mkdir "%SDKRT%"
xcopy /y /e /i /q "%KITS%\bin\%SDKVER%\x64" "%SDKRT%" >nul
if not exist "%SDKRT%\rc.exe" goto 环境失败
if not exist "%SDKRT%\mt.exe" goto 环境失败

set PATH=%SDKRT%;%CRT%;%MSVC%\bin\Hostx64\x64;%PATH%
echo     INCLUDE / LIB / PATH 已铺好
echo     cl      : %MSVC%\bin\Hostx64\x64\cl.exe
echo     rc/mt   : %SDKRT%

if not exist "%NINJAEXE%" goto 环境失败
echo     ninja   : %NINJAEXE%

rem ---- Ninja 生成器：绕开 CUDA 的 MSBuild 集成（那条会报 No CUDA toolset found）----
set CMAKE_GENERATOR=Ninja
set CMAKE_MAKE_PROGRAM=%NINJAEXE%
set CMAKE_ARGS=-DGGML_CUDA=on -DCMAKE_CUDA_ARCHITECTURES=120 -DCMAKE_RC_COMPILER=C:/btmp2/sdk/rc.exe -DCMAKE_MT=C:/btmp2/sdk/mt.exe

echo.
echo [1/3] 开始编译（下面会刷大量编译输出，属正常）...
echo.
pip install llama-cpp-python==0.3.35 --no-binary :all: --force-reinstall --no-deps --no-build-isolation
if errorlevel 1 goto 编译失败

echo.
echo [2/3] 编译完成，开始 GPU 验收（拿 3B 模型真跑一轮）...
echo.
cd /d "%~dp0"
"%PYEXE%" 验证GPU.py
if errorlevel 1 goto 验收失败

echo.
echo [3/3] 全部完成！
echo   到软件「接口配置」里把这套 local 配置的 GPU 层数设成 -1，
echo   就吃上你的 RTX 5050 了。
echo   （这份 CUDA 版是按本机显卡架构编的（sm_120 / RTX 50 系）。别的显卡
echo     拿了它会在「加载模型」那一步报 no kernel image，把 GPU 层数改成 0
echo     就会走 CPU；想让它真吃上那块卡，得在那台机器上重跑一次这个脚本。
echo     另外：重新打包时记得双击项目根目录的 打包.bat —— 它会把这份 CUDA 版
echo     缺的 cublas、以及 CPU 兜底那套 DLL 一起塞进 依赖库/，见 使用说明.txt）
pause
exit /b 0

:环境失败
echo.
echo [失败] 环境准备没通过（上面哪一行报错就发哪一行给 CodeBuddy）。
pause
exit /b 1

:编译失败
echo.
echo [失败] 编译没成。自动回退 CPU 版，保证软件还能用...
pip install llama-cpp-python==0.3.35 --only-binary=:all: --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu --force-reinstall --no-deps
echo.
echo 已回退 CPU 版。把本窗口上面的报错复制给 CodeBuddy 分析。
pause
exit /b 1

:验收失败
echo.
echo [失败] GPU 验收没过。自动回退 CPU 版...
pip install llama-cpp-python==0.3.35 --only-binary=:all: --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu --force-reinstall --no-deps
echo.
echo 已回退 CPU 版。把本窗口上面的输出发给 CodeBuddy 分析。
pause
exit /b 1
