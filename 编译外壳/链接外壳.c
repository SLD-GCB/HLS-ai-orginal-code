/* 链接外壳.c —— 顶替 MSVC 的 link.exe，只为一件小事：让它认中文路径。
 *
 * 背景（2026-09-22 挖到底的那条）：
 *   CMake / Ninja 把过长的链接命令写进 `.rsp` 参数文件，用的是 **UTF-8**；
 *   而 link.exe 读它默认按**系统 ANSI 码页**（中文 Windows 上是 936 / GBK）。
 *   两边码页不一致，路径里的中文就变成乱码，报出来是
 *
 *       LINK : fatal error LNK1181: 无法打开输入文件“...\缂栬瘧鐜\xaf澧僜CUDA\lib\x64\cudart.lib”
 *
 *   —— 文件明明就在那儿。所以这是"写和读用了两套码"，**不是中文有罪**。
 *
 * 实测（三种编码各跑一次，不是推测）：
 *
 *     rsp 编码                  link 的反应
 *     ───────────────────────  ──────────────────────────────
 *     UTF-8，无 BOM            LNK1181，路径显示成乱码
 *     UTF-8，**带 BOM**        成功     ← 它认，只是要你明说
 *     GBK                      成功
 *     英文路径 + UTF-8         成功（ASCII 在两套码里字节相同，是交集区）
 *
 * 所以这个外壳只干一件事：**给每个 `@xxx.rsp` 补上 UTF-8 BOM**，然后原样转交给
 * 同目录下改名叫 `link-real.exe` 的真身。
 *
 * 为什么是"就地补 BOM"，而不是"转码成 GBK 再写个临时文件"：
 *   · 不用新建临时文件、不用管清理；
 *   · 不用重写参数（`@a` → `@临时文件`），也就避开了引号转义那一堆坑；
 *   · rsp 里再套 rsp —— **压根不存在这回事**：实测 link 自己就不认 rsp 里的 `@`，
 *     它会把 `@xxx.rsp` 当成一个文件名去找（报"无法打开输入文件 @xxx.rsp"）。
 *     所以这里不用管递归，那不是"没做"，是"没有这东西"。
 *   ninja 每跑一次这条边都会重写 rsp，所以我们补的 BOM 不会攒下来，也不影响
 *   增量判断（ninja 不拿 rsp 的 mtime 当依赖）。
 *
 * 三条铁律：
 *   1. **绝不改变 link 的行为。** 命令行里除 argv[0] 之外一个字符都不动；工作目录、
 *      三条标准流全部继承；退出码原样回传。
 *   2. **尽力而为，绝不搅局。** 读不了、写不了、不是合法 UTF-8 —— 一律静默跳过，
 *      让真身照旧跑。最坏也只是回到"中文报打不开"那个老毛病，不能因此编不了。
 *   3. **只在该动手时动手。** 文件已经有 BOM、是纯 ASCII、或者根本不是合法 UTF-8
 *      —— 一律不碰（纯 ASCII 的 rsp 两套码读出来一样，没必要标）。
 *
 * ⚠ 真身那个文件名用纯 ASCII（`link-real.exe`）：今天刚学到的教训 —— 凡是会进
 *   路径的东西，就别给自己找麻烦。
 * ⚠ 编译它时用 `/MT`（静态链 CRT）：这样它不依赖 vcruntime140.dll 之类的旁边文件，
 *   自己一个 exe 就能跑。少一个环节就少一种坏法。
 *
 * ⚠⚠ **编它的时候必须带 `/utf-8`。** 这个源文件是 UTF-8，而 cl.exe 默认按系统 ANSI
 *   码页（936）读源文件 —— 下面那些中文标识符会被打成乱码，一屏 `error C3872`。
 *   第一次就栽在这儿。命令行在 `打包.py` 的 `装链接外壳()` 里，别把那个开关删了。
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <locale.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

/* ── 判据 ─────────────────────────────────────────────────────── */

static int 有BOM(const unsigned char *p, DWORD n)
{
    return n >= 3 && p[0] == 0xEF && p[1] == 0xBB && p[2] == 0xBF;
}

static int 有非ASCII(const unsigned char *p, DWORD n)
{
    DWORD i;
    for (i = 0; i < n; i++)
        if (p[i] >= 0x80)
            return 1;
    return 0;
}

/* 交给系统判：字节流里有非法 UTF-8 序列时它会失败。 */
static int 是合法UTF8(const unsigned char *p, DWORD n)
{
    return MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                               (const char *)p, (int)n, NULL, 0) != 0;
}

/* ── 补 BOM ───────────────────────────────────────────────────── */

/*
 * 给一个 rsp 补 UTF-8 BOM。**成不成都不出声**（铁律 2）。
 *
 * ⚠ 不直接往原文件里"写 BOM + 原内容"：万一写到一半失败，那个 rsp 就成了
 *   半截垃圾，反而把一次本来能过的构建搞砸。所以先写同目录的临时文件，再用
 *   `MoveFileEx(..., MOVEFILE_REPLACE_EXISTING)` 一次性换过去 —— 换过去是原子的。
 */
static void 补BOM(const wchar_t *径)
{
    static const unsigned char BOM[3] = { 0xEF, 0xBB, 0xBF };
    HANDLE h = INVALID_HANDLE_VALUE, 出 = INVALID_HANDLE_VALUE;
    DWORD 大小, 读 = 0, 写 = 0;
    unsigned char *原 = NULL, *新 = NULL;
    wchar_t 临时[MAX_PATH];

    /*
     * ① 读进来，**读完立刻关句柄**。
     *    ⚠ 这一条是拿一整轮调试换来的：后面要把新文件换到这个名字上，而自己
     *      还攥着这个文件不放（读句柄没带 FILE_SHARE_DELETE）的话，
     *      `MoveFileEx` 会以共享冲突失败 —— BOM 明明写好了，最后一步被自己挡了，
     *      而且它一声不吭（铁律 2 要求静默），表现就是"外壳装了跟没装一样"。
     */
    h = CreateFileW(径, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                    NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE)
        return;
    大小 = GetFileSize(h, NULL);
    if (大小 != INVALID_FILE_SIZE && 大小 > 0) {
        原 = (unsigned char *)malloc(大小);
        if (原 && (!ReadFile(h, 原, 大小, &读, NULL) || 读 != 大小)) {
            free(原);
            原 = NULL;
        }
    }
    CloseHandle(h);

    if (!原)
        return;
    if (有BOM(原, 读))          goto 收工;      /* 已经标过了 */
    if (!有非ASCII(原, 读))     goto 收工;      /* 纯 ASCII，两套码一样 */
    if (!是合法UTF8(原, 读))    goto 收工;      /* 本来就别标 */

    新 = (unsigned char *)malloc(3 + 读);
    if (!新)
        goto 收工;
    memcpy(新, BOM, 3);
    memcpy(新 + 3, 原, 读);

    if (_snwprintf_s(临时, MAX_PATH, _TRUNCATE, L"%s.hlbom", 径) < 0)
        goto 收工;
    出 = CreateFileW(临时, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                     FILE_ATTRIBUTE_NORMAL, NULL);
    if (出 == INVALID_HANDLE_VALUE)
        goto 收工;
    if (!WriteFile(出, 新, 3 + 读, &写, NULL) || 写 != 3 + 读) {
        CloseHandle(出);
        出 = INVALID_HANDLE_VALUE;
        DeleteFileW(临时);
        goto 收工;
    }
    CloseHandle(出);
    出 = INVALID_HANDLE_VALUE;
    if (!MoveFileExW(临时, 径, MOVEFILE_REPLACE_EXISTING))
        DeleteFileW(临时);

收工:
    if (出 != INVALID_HANDLE_VALUE)
        CloseHandle(出);
    free(原);
    free(新);
}

/* ── 命令行 ───────────────────────────────────────────────────── */

/*
 * 把 `GetCommandLineW()` 里的 argv[0] 换成真身路径，**其余一个字符都不动**。
 * 不做重新拼接、不做重新加引号 —— 那些地方最容易把参数改坏。
 */
static void 换头(wchar_t *出, size_t 容量, const wchar_t *真身)
{
    const wchar_t *p = GetCommandLineW();
    while (*p == L' ' || *p == L'\t')
        p++;
    if (*p == L'"') {                       /* 自己的路径带空格，是带引号的 */
        p++;
        while (*p && *p != L'"')
            p++;
        if (*p == L'"')
            p++;
    } else {                                /* 没引号：吃到第一个空白 */
        while (*p && *p != L' ' && *p != L'\t')
            p++;
    }
    _snwprintf_s(出, 容量, _TRUNCATE, L"\"%s\"%s", 真身, p);
}

/* ── 入口 ─────────────────────────────────────────────────────── */

int wmain(int argc, wchar_t **argv)
{
    wchar_t 自身[MAX_PATH], 真身[MAX_PATH];
    wchar_t *刀;
    wchar_t *命令行;
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    DWORD 码 = 0;
    int i;

    /*
     * ⚠ 不设这一句，下面 `fwprintf` 写中文会烂：C 运行时的默认区域是 "C"，只认
     *   ASCII，宽字符转多字节时非 ASCII 直接丢。表现是那行"找不到真身/起不了真身"
     *   打出来只剩一个 `[` —— 而**恰恰是杀毒软件把它拦下的时候最需要这行能看懂**，
     *   所以这句必须留着。
     */
    setlocale(LC_ALL, "");

    if (!GetModuleFileNameW(NULL, 自身, MAX_PATH))
        return 2;
    刀 = wcsrchr(自身, L'\\');
    if (!刀)
        return 2;
    *(刀 + 1) = 0;
    if (_snwprintf_s(真身, MAX_PATH, _TRUNCATE, L"%slink-real.exe", 自身) < 0)
        return 2;

    if (GetFileAttributesW(真身) == INVALID_FILE_ATTRIBUTES) {
        fwprintf(stderr, L"[链接外壳] 找不到真身，没法干活：%s\n", 真身);
        return 2;
    }

    /* 给每个 `@rsp` 补 BOM。`@"带空格.rsp"` 这种要把引号剥掉。 */
    for (i = 1; i < argc; i++) {
        wchar_t *参 = argv[i];
        size_t 长;
        if (参[0] != L'@' || !参[1])
            continue;
        参++;
        长 = wcslen(参);
        if (长 >= 2 && 参[0] == L'"' && 参[长 - 1] == L'"') {
            参[长 - 1] = 0;
            参++;
        }
        补BOM(参);
    }

    命令行 = (wchar_t *)malloc(32768 * sizeof(wchar_t));
    if (!命令行)
        return 2;
    换头(命令行, 32768, 真身);

    ZeroMemory(&si, sizeof(si));
    ZeroMemory(&pi, sizeof(pi));
    si.cb = sizeof(si);

    /* 工作目录传 NULL = 继承我们的；命令行不长在 argv 里时相对路径才能对上。
       句柄全继承 = ninja 看到的输出还是 link 的输出。 */
    if (!CreateProcessW(NULL, 命令行, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) {
        fwprintf(stderr, L"[链接外壳] 起不了真身（错误码 %lu）：%s\n",
                 GetLastError(), 真身);
        free(命令行);
        return 2;
    }
    free(命令行);

    WaitForSingleObject(pi.hProcess, INFINITE);
    GetExitCodeProcess(pi.hProcess, &码);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return (int)码;
}
