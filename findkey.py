# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of IPTV Set-Top Box Simulator.
#
# IPTV Set-Top Box Simulator is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from Crypto.Cipher import DES

def find_key(authenticator):
    """暴力破解 DES 密钥（从 Authenticator 反向尝试）。"""
    try:
        cipher = bytes.fromhex(authenticator)
    except ValueError as e:
        print('[find_key] Authenticator 解析失败: %s', e)
        return []

    total = 100000000
    progress_step = total // 20
    progress_count = 0
    print('progress: ', end='', flush=True)
    for n in range(total):
        if n and n % progress_step == 0:
            print('-', end='', flush=True)
            progress_count += 1
        key_candidate = f'{n:0>8d}'
        des = DES.new(key_candidate.encode(), DES.MODE_ECB)
        decrypt = des.decrypt(cipher)
        try:
            decrypt_text = decrypt.decode()
            print('-' * (20 - progress_count), '100%', sep='', flush=True)
            return [key_candidate, decrypt_text]
        except UnicodeDecodeError:
            continue
    print(' 100%', flush=True)
    return []


if __name__ == '__main__':
    cipher = 'Your Authenticatorr'
    result = find_key(cipher)
    if result:
        key, decrypt = result
        print(f'key = {key}\ntext = {decrypt}')
