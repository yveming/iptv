# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of IPTV Set-Top Box Simulator.
#
# IPTV Set-Top Box Simulator is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import argparse
import multiprocessing
import os
import sys

from Crypto.Cipher import DES

# 可打印 ASCII 集合（0x20-0x7E），用于 translate 预筛（C 级单次调用）
_PRINTABLE = bytes(range(0x20, 0x7F))
TOTAL = 100000000          # 密钥空间：00000000-99999999
CHUNK_COUNT = 50           # 分块数（每块 TOTAL/CHUNK_COUNT 个候选）


def _scan_range(args):
    """扫描 [start, end) 密钥区间（工作进程调用）。

    优化：ECB 各块独立，验钥只需解密第一个 8 字节块；先用"全可打印 ASCII"
    预筛（随机 8 字节全落在 0x20-0x7E 的概率约 4.6e-9），通过预筛的极少数
    候选再做全量解密 + UTF-8 解码验证。
    返回 (key, 明文) 或 None。
    """
    try:
        import signal
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C 由主进程处理，避免子进程打印异常栈
    except (ImportError, ValueError):
        pass
    start, end, cipher_hex = args
    cipher = bytes.fromhex(cipher_hex)
    first_block = cipher[:8]
    for n in range(start, end):
        key_candidate = f'{n:0>8d}'
        des = DES.new(key_candidate.encode(), DES.MODE_ECB)
        head = des.decrypt(first_block)
        if head.translate(None, _PRINTABLE):
            continue
        try:
            decrypt_text = des.decrypt(cipher).decode()
        except UnicodeDecodeError:
            continue
        return key_candidate, decrypt_text
    return None


def find_key(authenticator, workers=None):
    """暴力破解 DES 密钥（从 Authenticator 反向尝试，密钥空间为 8 位纯数字）。

    多进程并行扫描，命中即终止；workers 缺省为 CPU 核数。
    """
    try:
        cipher = bytes.fromhex(authenticator)
    except ValueError as e:
        print(f'[find_key] Authenticator 解析失败: {e}')
        return []
    if len(cipher) < 8 or len(cipher) % 8:
        print('[find_key] Authenticator 长度非法（须为 8 的倍数字节）')
        return []

    if workers is None:
        workers = os.cpu_count() or 1
    chunk_size = TOTAL // CHUNK_COUNT
    tasks = [(i * chunk_size, (i + 1) * chunk_size, authenticator)
             for i in range(CHUNK_COUNT)]

    print(f'progress({workers}p): ', end='', flush=True)
    pool = multiprocessing.Pool(workers)
    try:
        # 带超时轮询结果：主线程周期性醒来，保证 Windows 下 Ctrl+C 能及时中断
        results = pool.imap_unordered(_scan_range, tasks)
        while True:
            try:
                result = results.next(timeout=0.5)
            except StopIteration:
                break
            except multiprocessing.TimeoutError:
                continue
            print('-', end='', flush=True)
            if result:
                print(' 100%', flush=True)
                return [result[0], result[1]]
        print(' 100%', flush=True)
        return []
    except KeyboardInterrupt:
        print('\n[find_key] 用户中断', flush=True)
        return []
    finally:
        pool.terminate()
        pool.join()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='DES 密钥暴力破解（8 位纯数字，多进程）')
    parser.add_argument('authenticator', nargs='?', default='Your Authenticator',
                        help='Authenticator hex 串')
    parser.add_argument('-t', type=int, default=None, dest='threads',
                        help='进程数（默认 CPU 核数）')
    args = parser.parse_args()

    result = find_key(args.authenticator, workers=args.threads)
    if result:
        key, decrypt = result
        print(f'key = {key}\ntext = {decrypt}')
