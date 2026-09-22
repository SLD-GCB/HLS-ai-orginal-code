#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证GPU.py — 编译 GPU 版 llama-cpp-python 之后的自动验收

干三件事：
    1. 记下生成前的显存占用
    2. 拿 3B 模型、GPU 层数拉满（-1）真跑一轮
    3. 对比显存：明显上涨 = 模型真上显卡了；没涨 = 还在 CPU 跑

退出码 0 = 验收通过，1 = 有问题（编译脚本靠它决定要不要回退 CPU 版）。
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import 酒馆大脑
import 酒馆模型

NSMI = r'C:\Windows\System32\nvidia-smi.exe'


def 显存():
    """当前显存占用（MB）。查不到回 -1。"""
    try:
        出 = subprocess.run(
            [NSMI, '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=10).stdout
        return int(出.strip().split('\n')[0])
    except Exception:
        return -1


def 主():
    前 = 显存()
    if 前 < 0:
        print('!! 查不到 nvidia-smi，这次只看速度不看显存')
    else:
        print('生成前显存: %d MB' % 前)

    配置 = 酒馆模型.接口配置(
        协议='local', 模型='qwen2.5-3b-instruct-q4_k_m.gguf',
        采样参数='{"n_ctx": 2048, "n_gpu_layers": -1, "max_tokens": 80}')
    缺 = 酒馆大脑.校验配置(配置)
    if 缺:
        print('配置不齐：%s' % '、'.join(缺))
        return 1

    print('开始生成（GPU 层数 -1 = 全上显卡）…')
    t = time.time()
    首 = None
    全 = ''
    try:
        for 段 in 酒馆大脑.流式(配置, '你是一只叫汤圆的猫，说话带喵。',
                               [{'role': 'user', 'content': '用三句话介绍你自己'}]):
            if 首 is None:
                首 = time.time() - t
            全 += 段
    except Exception as 错:
        print('生成失败：%s：%s' % (type(错).__name__, 错))
        return 1

    后 = 显存()
    print('首字: %.1fs   总耗时: %.1fs' % (首 or -1, time.time() - t))
    if 后 >= 0:
        print('显存: %d MB -> %d MB' % (前, 后))
    print('回复:', 全)

    if 后 >= 0 and 后 > 前 + 500:
        print('==> 显存明显上涨，模型真上 GPU 了。验收通过！')
        return 0
    if 后 >= 0:
        print('==> 显存没怎么涨——多半还在 CPU 上跑（GPU 没生效）。'
              '把这份输出发给 CodeBuddy 分析。')
        return 1
    print('==> 显存监测不可用，请对照首字速度自行判断（GPU 版 3B 首字'
          '应明显快于 CPU 版的 1.8s）')
    return 0


if __name__ == '__main__':
    sys.exit(主())
