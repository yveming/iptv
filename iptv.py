# -*- coding: UTF-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of IPTV Set-Top Box Simulator.
#
# IPTV Set-Top Box Simulator is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""
IPTV Set-Top Box Simulator
Simulates STB authentication, pulls channel list and EPG programs.

All network requests and error-prone operations are wrapped with logging;
connection failures include the failed URL.
"""

import re
import json
import gzip
import time
import socket
import sys
import os
import argparse
import logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as etree
from Crypto.Cipher import DES
from Crypto.Util.Padding import pad


REQUIRED_CONFIG_KEYS = ['UserID', 'key', 'STBID', 'mac', 'login_entry', 'egp_uri']
DEVICE_DESC_KEYS = [
    'UserID', 'Lang', 'SupportHD', 'NetUserID', 'Authenticator', 'STBType', 'STBVersion',
    'conntype', 'STBID', 'templateName', 'areaId', 'userToken', 'userGroupId',
    'productPackageId', 'mac', 'UserField', 'SoftwareVersion', 'IsSmartStb', 'desktopId',
    'stbmaker', 'XMPPCapability', 'ChipID', 'VIP',
]


class IPTVError(Exception):
    """IPTV 操作异常，携带 label 供调用方定位错误阶段。"""

    def __init__(self, message, label=''):
        super().__init__(message)
        self.label = label


# ---------------------------------------------------------------------------
# 底层 HTTP 工具（logger 由调用方注入）
# ---------------------------------------------------------------------------

def _safe_http(method, url, session=None, log_label='', **kwargs):
    """包装 requests 调用，失败时抛出 IPTVError（不再直接 exit）。"""
    logger = logging.getLogger(__name__)
    try:
        if session:
            resp = session.request(method, url, **kwargs)
        else:
            resp = requests.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp
    except requests.exceptions.ConnectionError as e:
        logger.error('[%s] 连接失败 -> %s\n  %s', log_label, url, e)
        raise IPTVError(f'连接失败 -> {url}', label=log_label) from e
    except requests.exceptions.Timeout as e:
        logger.error('[%s] 超时 -> %s\n  %s', log_label, url, e)
        raise IPTVError(f'超时 -> {url}', label=log_label) from e
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else '?'
        logger.error('[%s] HTTP错误 %s -> %s\n  %s', log_label, status, url, e)
        raise IPTVError(f'HTTP错误 {status} -> {url}', label=log_label) from e
    except requests.exceptions.RequestException as e:
        logger.error('[%s] 请求失败 -> %s\n  %s', log_label, url, e)
        raise IPTVError(f'请求失败 -> {url}', label=log_label) from e

# ---------------------------------------------------------------------------
# 核心类
# ---------------------------------------------------------------------------

class IPTVSetTopBox:
    """Simulates a set-top box: authenticates, retrieves channel list and EPG data."""

    def __init__(
        self,
        desc,
        user_id,
        key,
        login_entry,
        egp_uri,
    ):
        self.desc = desc
        self.desc['UserID'] = user_id
        self.user_id = user_id
        self.stb_id = desc['STBID']
        self.key = key
        self.mac = desc['mac']
        self.ip = socket.gethostbyname(socket.gethostname())
        self.login_entry = login_entry
        self.egp_uri = egp_uri
        self.reserved = ''
        self.session = requests.Session()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (X11; U; Linux i686; en-US) AppleWebKit/534.0 (KHTML, like Gecko)',
        }
        self._last_url = login_entry
        self._channel_list = []
        self.logger = logging.getLogger(f'{__name__}.{self.__class__.__name__}')
        self.logger.info('STB 初始化: user=...%s stb=...%s ip=%s', self.user_id[-8:], self.stb_id[-12:], self.ip)

    # ------------------------------------------------------------------
    # authentication pipeline
    # ------------------------------------------------------------------

    def _authenticate(self):
        """Run the multi-step STB authentication and return the final response."""
        self._last_url = self.login_entry
        resp = _safe_http('GET', self._last_url, session=self.session,
                          log_label='AUTH-1/4',
                          params={'UserID': self.user_id, 'Action': 'Login'},
                          headers=self.headers)
        self.logger.debug('[_entry] status=%s, url=%s',
                         resp.status_code, self.login_entry)        
        resp = self._login(resp)
        resp = self._auth(resp)
        resp = self._get_channel_list(resp)
        self.logger.info('认证流程完成')
        return resp

    def _login(self, resp):
        """If the response contains a <form>, submit it and return the next response."""
        try:
            soup = BeautifulSoup(resp.text, 'html.parser')
        except Exception as e:
            self.logger.error('[_login] HTML解析失败: %s', e)
            return resp
        form = soup.find('form')
        if form is None:
            self.logger.debug('[_login] 未找到 <form>, 跳过')
            return resp
        action = urljoin(resp.url, form.get('action', ''))
        data = {
            inp.get('name'): inp.get('value', '')
            for inp in form.find_all('input')
            if inp.get('name')
        }
        self.headers['Referer'] = resp.url
        next_resp = _safe_http('POST', action, session=self.session,
                               log_label='AUTH-2/4 (_login)',
                               data=data, headers=self.headers)
        self._last_url = next_resp.url
        self.logger.debug('[_login] status=%s, url=%s',
                         next_resp.status_code, action)
        return next_resp

    def _auth(self, resp):
        """Submit the authform (requires DES-encrypted Authenticator)."""
        try:
            soup = BeautifulSoup(resp.text, 'html.parser')
        except Exception as e:
            self.logger.error('[_auth] HTML解析失败: %s', e)
            return resp
        auth_form = soup.find('form', {'name': 'authform'})
        if auth_form is None:
            self.logger.error('[_auth] 未找到认证表单 (name=authform)')
            return resp
        action = urljoin(resp.url, auth_form.get('action', ''))
        token = self._extract_token(soup)
        if not token:
            self.logger.error('[_auth] EncryptToken 提取失败, 认证可能不完整')
        authenticator = self._build_authenticator(token)
        data = self.desc
        data['Authenticator'] = authenticator
        data['userToken'] = token
        self.headers['Referer'] = resp.url
        next_resp = _safe_http('POST', action, session=self.session,
                               log_label='AUTH-3/4 (_auth)',
                               data=data, headers=self.headers)
        self._last_url = next_resp.url
        self.logger.debug('[_auth] status=%s, url=%s',
                         next_resp.status_code, action)
        return next_resp

    def _get_channel_list(self, resp):
        """Submit the final hidden-input form after authform (if present)."""
        try:
            soup = BeautifulSoup(resp.text, 'html.parser')
        except Exception as e:
            self.logger.error('[_get_channel_list] HTML解析失败: %s', e)
            return resp
        form_tag = soup.find('form', {'name': 'authform'})
        if form_tag is None:
            self.logger.debug('[_get_channel_list] 无更多 authform, 认证结束')
            return resp
        action = urljoin(resp.url, form_tag.get('action', ''))
        try:
            data = {
                inp.get('name'): inp.get('value', '')
                for inp in soup.find_all('input', {'type': 'hidden'})
                if inp.get('name')
            }
        except Exception as e:
            self.logger.error('[_get_channel_list] 提取 hidden input 失败: %s', e)
            data = {}
        self.headers['Referer'] = resp.url
        next_resp = _safe_http('POST', action, session=self.session,
                               log_label='AUTH-4/4 (_get_channel_list)',
                               data=data, headers=self.headers, timeout=15)
        self._last_url = next_resp.url
        self.logger.debug('[_get_channel_list] status=%s, url=%s',
                         next_resp.status_code, action)
        return next_resp

    def _extract_token(self, soup):
        """Extract EncryptToken value from page scripts."""
        try:
            scripts = '\n'.join(script.get_text() for script in soup.find_all('script'))
        except Exception as e:
            self.logger.error('[_extract_token] 提取 <script> 文本失败: %s', e)
            return ''
        match = re.search(r'\s+EncryptToken\s*=\s*"([A-F0-9]+)";', scripts, re.IGNORECASE)
        if match:
            token = match.group(1)
            self.logger.debug('userToken: %s', token)
            return token
        self.logger.error('[_extract_token] 未匹配到 EncryptToken')
        return ''

    def _build_authenticator(self, token):
        """Build DES-encrypted Authenticator string."""
        try:
            padded_key = f'{self.key:0<8}'.encode()
            des = DES.new(padded_key, DES.MODE_ECB)

            plain_text = (
                '99999$' + token + '$' + self.user_id + '$'
                + self.stb_id + '$' + self.ip + '$' + self.mac + '$'
                + self.reserved + '$CTC'
            )
            result = des.encrypt(pad(plain_text.encode(), 8)).hex()
            self.logger.debug('Authenticator: %s..., len=%s,', result[:48], len(result))
            return result
        except Exception as e:
            self.logger.error('[_build_authenticator] DES加密失败: %s', e)
            raise IPTVError('DES加密失败') from e

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    @classmethod
    def find_key(this, Authenticator=None):
        """暴力破解 DES 密钥（从 desc['Authenticator'] 反向尝试）。"""
        result = []
        try:
            if Authenticator is None:
                cipher = bytes.fromhex(this.desc['Authenticator'])
            else:
                cipher = bytes.fromhex(Authenticator)
        except Exception as e:
            this.logger.error('[find_key] Authenticator 解析失败: %s', e)
            return result

        for n in range(100000000):
            key_candidate = f"{n:0>8d}"
            des = DES.new(key_candidate.encode(), DES.MODE_ECB)
            decrypt = des.decrypt(cipher)
            try:
                decrypt_text = decrypt.decode()
                this.key = key_candidate
                result = [key_candidate, decrypt_text]
                break
            except Exception:
                continue
        return result

    def get_channel_list(self):
        """Authenticate (if needed) and return the full channel list.

        Each entry: id, user_channel_id, igmp_addr, timeshift, timeshift_len,
        timeshift_url, fcc_enable, fcc_ip, fcc_port, fec_port, name, group_title.
        """
        CHANNEL_PATTERN = re.compile(
            r'ChannelID\=\"(\d+)\",'
            r'ChannelName\=\"(.+?)\",'
            r'UserChannelID\=\"(\d+)\",'
            r'ChannelURL=\"igmp://(.+?)\".+?'
            r'TimeShift\=\"(\d+)\",'
            r'TimeShiftLength\=\"(\d+)\".+?,'
            r'TimeShiftURL\=\"(.+?)\?.+?'
            r'FCCEnable\=\"(\d+)\",'
            r'ChannelFCCIP\=\"(.*?)\",'
            r'ChannelFCCPort\=\"(.*?)\",'
            r'ChannelFECPort\=\"(.*?)\"'
        )

        if self._channel_list:
            return self._channel_list

        resp = self._authenticate()
        try:
            for match in CHANNEL_PATTERN.finditer(resp.text):
                g = match.groups()
                self._channel_list.append({
                    'id': g[0], 'name': g[1], 'user_channel_id': g[2], 'igmp_addr': g[3],
                    'timeshift': g[4], 'timeshift_len': g[5], 'timeshift_url': g[6],
                    'fcc_enable': g[7], 'fcc_ip': g[8], 'fcc_port': g[9], 'fec_port': g[10]
                })
        except Exception as e:
            self.logger.error('[get_channel_list] 频道解析失败: %s', e)
        self.logger.info('频道列表获取完成: %s 个频道', len(self._channel_list))
        return self._channel_list

    def get_channel_programs(self, channel_id):
        """Fetch and return the EPG program list for a single channel.

        Returns list[dict] with: start, stop, title, channel_id.
        """
        if self._channel_list is None:
            self.get_channel_list()
        url = urljoin(self._last_url, f'{self.egp_uri}?channelId={channel_id}')
        self.headers['Referer'] = url
        resp = _safe_http('GET', url, session=self.session,
                          log_label=f'EPG(ch={channel_id})',
                          headers=self.headers)
        try:
            match = re.search(r'parent\.jsonBackLookStr\s*=\s*(\[.*?\]);', resp.text)
        except Exception as e:
            self.logger.error('[get_channel_programs] EPG正则匹配失败 (ch=%s): %s', channel_id, e)
            return []
        if not match:
            self.logger.warning('[get_channel_programs] 未匹配到 jsonBackLookStr (ch=%s)', channel_id)
            return []
        try:
            epg_data = json.loads(match.group(1))
        except json.JSONDecodeError as e:
            self.logger.error('[get_channel_programs] JSON解析失败 (ch=%s): %s', channel_id, e)
            return []
        if not epg_data or not isinstance(epg_data, list) or len(epg_data) < 2:
            self.logger.warning('[get_channel_programs] EPG数据为空或格式异常 (ch=%s)', channel_id)
            return []
        programs = []
        try:
            for programs_group in epg_data[1]:
                if not isinstance(programs_group, list):
                    continue
                for prog in programs_group:
                    if not isinstance(prog, dict):
                        continue
                    start = prog.get('beginTimeFormat', '')
                    stop = prog.get('endTimeFormat', '')
                    if not start or not stop:
                        continue
                    title = prog.get('programName', '未知节目')
                    title = title.replace('<', '《').replace('>', '》').replace('&', '&amp;')
                    programs.append({
                        'start': start, 'stop': stop,
                        'title': title, 'channel_id': channel_id,
                    })
        except Exception as e:
            self.logger.error('[get_channel_programs] EPG数据遍历异常 (ch=%s): %s', channel_id, e)
        return programs

def classify(name):
    """Return a group-title string for a channel name."""
    logger = logging.getLogger(__name__)

    IS_4K = ['4K']
    CCTV = ['CCTV', 'CGTN', '音乐现场']
    CETV = ['CETV']
    SCTV = ['四川', 'SCTV', '峨眉', '乐山', '康巴']
    CDTV = ['成都', 'CDTV', '蓉城']
    PROV = ['卫视']

    try:
        if re.search('|'.join(IS_4K), name):
            return '4K超高清'
        if re.search('|'.join(CCTV), name) or re.search('|'.join(CETV), name):
            return 'CCTV央视'
        if re.search('|'.join(SCTV), name) or re.search('|'.join(CDTV), name):
            return '四川成都'
        if re.search('|'.join(PROV), name):
            return '各省卫视'
    except re.error as e:
        logger.error('[classify] 正则异常 (name=%s): %s', name, e)
    return '数字频道'


def _m3u_header():
    return (
        f'#EXTM3U name="成都电信IPTV '
        f'{os.path.basename(__file__)} @ {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())}"'
    )


def _write_m3u_channels(filepath, channels):
    with open(filepath, 'w', encoding='utf-8') as fp:
        print(_m3u_header(), file=fp)
        for ch in channels:
            group_title = classify(ch['name'])
            catchup = ''
            if ch['timeshift'] != '0':
                fp.write('#KODIPROP:inputstream=inputstream.ffmpegdirect\n')
                catchup = (
                    f' catchup="default" catchup-days="7"'
                    f' catchup-source="{ch["timeshift_url"]}'
                    f'?playseek={{utc:YmdHMS}}-{{utcend:YmdHMS}}"'
                )
            fp.write(
                f'#EXTINF:-1 tvg-id="{ch["name"]}" tvg-name="{ch["name"]}"'
                f' group-title="{group_title}"{catchup}, {ch["name"]}\n'
            )
            fp.write(f'rtp://{ch["igmp_addr"]}\n')


def _prepare_channels(channels):
    exclude_pattern = re.compile(
        '|'.join([
            '画中画', '直播', '组播', '中国体育', '导视', '指南', '推荐', '宣传', '云看',
            '红原', '乐山', '熊猫', '大爱', 'i成都',
        ])
    )
    clean_pattern = re.compile(r'(?:专区|高清|超清)+$')
    clean_channels = []
    for ch in channels:
        if exclude_pattern.search(ch['name']):
            continue
        item = ch.copy()
        item['name'] = clean_pattern.sub('', item['name'])
        clean_channels.append(item)
    return clean_channels


def generate_m3u(box, filepath='iptv-full.m3u'):
    """Generate an M3U playlist and return prepared_channels.

    prepared_channels 可传给 generate_epg() 复用，避免重复拉取和过滤。
    """
    logger = logging.getLogger(__name__)

    try:
        channels = box.get_channel_list()
    except Exception as e:
        logger.error('[generate_m3u] 获取频道列表失败: %s', e)
        return []

    clean_channels = _prepare_channels(channels)
    try:
        _write_m3u_channels(filepath, clean_channels)
        logger.info('M3U 文件已生成: %s', filepath)
    except OSError as e:
        logger.error('[generate_m3u] 文件写入失败 (%s): %s', filepath, e)
    return clean_channels


def generate_selected_m3u(source_path, selected_config):
    """从完整 M3U 文件中按频道名关键词过滤出精选频道。"""
    logger = logging.getLogger(__name__)
    if not selected_config:
        return

    filepath = selected_config.get('path', '')
    names = selected_config.get('channels', [])
    if not filepath or not names:
        return
    if isinstance(names, str):
        names = [names]

    try:
        with open(source_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except OSError as e:
        logger.error('[generate_selected_m3u] 读取源文件失败 (%s): %s', source_path, e)
        return

    selected_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('#EXTINF:'):
            last_comma = line.rfind(',')
            if last_comma != -1:
                display_name = line[last_comma + 1:].strip()
                if any(name in display_name for name in names):
                    # 包含前面的 KODIPROP 行（如果有）
                    if i > 0 and lines[i - 1].startswith('#KODIPROP:'):
                        selected_lines.append(lines[i - 1])
                    selected_lines.append(lines[i])
                    # 包含下一行 URL
                    i += 1
                    while i < len(lines) and not lines[i].startswith('#') and lines[i].strip():
                        selected_lines.append(lines[i])
                        i += 1
                    continue
        i += 1

    if not selected_lines:
        logger.warning('[generate_selected_m3u] 未匹配到精选频道，跳过写入: %s', filepath)
        return

    try:
        with open(filepath, 'w', encoding='utf-8') as fp:
            fp.write(_m3u_header() + '\n')
            fp.writelines(selected_lines)
        logger.info('精选 M3U 文件已生成: %s (%s 条)', filepath, len(selected_lines) // 2)
    except OSError as e:
        logger.error('[generate_selected_m3u] 文件写入失败 (%s): %s', filepath, e)


def _write_epg_gzip(filepath):
    with open(filepath, 'rb') as fp, gzip.open(filepath + '.gz', 'wb') as gz:
        gz.writelines(fp)


def _write_epg_tree(filepath, tv_root):
    tree = etree.ElementTree(tv_root)
    etree.indent(tree, space='  ')
    with open(filepath, 'wb') as fp:
        tree.write(fp, encoding='utf-8', xml_declaration=True)
    _write_epg_gzip(filepath)


def generate_epg(box, channels, filepath='iptv-epg.xml'):
    """Generate an XMLTV EPG file from a STB instance and write it to *filepath*.
    传入 channels 可复用 generate_m3u() 的返回结果，省去重复的网络请求和过滤。
    """
    logger = logging.getLogger(__name__)

    if not channels:
        logger.warning('[generate_epg] 频道列表为空，跳过 EPG 生成，保留已有文件')
        return

    try:
        tv_root = etree.Element('tv')
        tv_root.set(
            'generator-info-name',
            f'{os.path.basename(__file__)} @ {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())}',
        )
        tv_root.set('source-info-name', '四川成都电信IPTV')
        for ch in channels:
            programs = box.get_channel_programs(ch['id'])
            if not programs:
                continue
            chan_elem = etree.SubElement(tv_root, 'channel', id=ch['name'])
            dn = etree.SubElement(chan_elem, 'display-name', lang='zh')
            dn.text = ch['name']
            for prog in programs:
                pe = etree.SubElement(tv_root, 'programme')
                pe.set('start', f"{prog['start']} +0800")
                pe.set('stop', f"{prog['stop']} +0800")
                pe.set('channel', ch['name'])
                title_elem = etree.SubElement(pe, 'title')
                title_elem.text = prog['title']
        if not len(tv_root):
            logger.warning('[generate_epg] 未获取到任何频道节目数据，跳过写入，保留已有文件')
            return
        _write_epg_tree(filepath, tv_root)
        logger.info('EPG 文件已生成: %s', filepath)
        logger.info('EPG 文件已生成: %s.gz', filepath)
    except OSError as e:
        logger.error('[generate_epg] 文件写入失败 (%s): %s', filepath, e)
    except Exception as e:
        logger.error('[generate_epg] 生成异常: %s', e)


def load_config(filepath):
    logger = logging.getLogger(__name__)
    try:
        with open(filepath, 'r', encoding='utf-8') as fp:
            config = json.load(fp)
    except OSError as e:
        raise IPTVError(f'配置文件读取失败: {filepath}') from e
    except json.JSONDecodeError as e:
        raise IPTVError(f'配置文件 JSON 解析失败: {filepath}') from e

    missing = [key for key in REQUIRED_CONFIG_KEYS if not config.get(key)]
    if missing:
        raise IPTVError(f'配置文件缺少必填项: {", ".join(missing)}')
    logger.info('配置文件已加载: %s', filepath)
    return config


def build_device_desc(config):
    return {key: str(config.get(key, '')) for key in DEVICE_DESC_KEYS}


def resolve_output_path(path, base_dir):
    if not path:
        return ''
    if os.path.isabs(path):
        return path
    return os.path.join(base_dir, path)


def resolve_selected_config(selected_config, base_dir):
    if not selected_config:
        return {}
    result = selected_config.copy()
    result['path'] = resolve_output_path(result.get('path', ''), base_dir)
    return result


def _is_url(source):
    return urlparse(source).scheme in ('http', 'https')


def _as_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _read_merge_source(source):
    logger = logging.getLogger(__name__)
    try:
        if _is_url(source):
            resp = _safe_http('GET', source, log_label='MERGE', timeout=30)
            return resp.content
        with open(source, 'rb') as fp:
            return fp.read()
    except OSError as e:
        logger.error('[merge] 读取本地文件失败 (%s): %s', source, e)
    except IPTVError:
        pass
    return b''


def merge_m3u(filepath, sources):
    logger = logging.getLogger(__name__)
    sources = _as_list(sources)
    if not sources:
        return

    try:
        with open(filepath, 'ab') as fp:
            for source in sources:
                data = _read_merge_source(source)
                if not data:
                    continue
                text = data.decode('utf-8-sig', errors='ignore').strip()
                lines = [line for line in text.splitlines() if line.strip() and not line.startswith('#EXTM3U')]
                if not lines:
                    continue
                fp.write(b'\n'.join(line.encode('utf-8') for line in lines) + b'\n')
                logger.info('已合并 M3U: %s -> %s', source, filepath)
    except OSError as e:
        logger.error('[merge_m3u] 文件写入失败 (%s): %s', filepath, e)


def merge_epg(filepath, sources):
    logger = logging.getLogger(__name__)
    sources = _as_list(sources)
    if not sources:
        return

    try:
        tree = etree.parse(filepath)
        root = tree.getroot()
        for source in sources:
            data = _read_merge_source(source)
            if not data:
                continue
            if source.endswith('.gz'):
                try:
                    data = gzip.decompress(data)
                except OSError as e:
                    logger.error('[merge_epg] gzip 解压失败 (%s): %s', source, e)
                    continue
            try:
                merge_root = etree.fromstring(data)
            except etree.ParseError as e:
                logger.error('[merge_epg] XML 解析失败 (%s): %s', source, e)
                continue
            for elem in list(merge_root):
                root.append(elem)
            logger.info('已合并 EPG: %s -> %s', source, filepath)
        _write_epg_tree(filepath, root)
        logger.info('EPG 合并文件已生成: %s.gz', filepath)
    except OSError as e:
        logger.error('[merge_epg] 文件处理失败 (%s): %s', filepath, e)
    except etree.ParseError as e:
        logger.error('[merge_epg] 目标 XML 解析失败 (%s): %s', filepath, e)


# ---------------------------------------------------------------------------
# 主程序入口：在此处集中配置 logger，然后注入到各个组件
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    file_path = os.path.dirname(os.path.abspath(__file__)) or '.'

    parser = argparse.ArgumentParser(description='成都电信 IPTV M3U/EPG 生成器')
    parser.add_argument('-f', '--config', default=os.path.join(file_path, 'iptv.json'), help='配置文件路径')
    args = parser.parse_args()

    # logger 是此处唯一配置点，不再有模块级全局 logger
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)

    try:
        config_path = os.path.abspath(args.config)
        config_dir = os.path.dirname(config_path) or file_path
        config = load_config(config_path)
        log_path = resolve_output_path(config.get('log', 'iptv.log'), config_dir)
        if not logger.handlers:
            logger_handler = logging.FileHandler(log_path, encoding='utf-8')
            logger_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
            logger.addHandler(logger_handler)
        logger.info('日志文件: %s', log_path)
        device_desc = build_device_desc(config)

        m3u_path = resolve_output_path(config.get('m3u', 'iptv-full.m3u'), config_dir)
        epg_path = resolve_output_path(config.get('epg', 'iptv-epg.xml'), config_dir)
        selected_config = resolve_selected_config(config.get('selected', {}), config_dir)
        merge_config = config.get('merge', {})

        stb = IPTVSetTopBox(
            desc=device_desc,
            user_id=str(config['UserID']),
            key=str(config['key']),
            login_entry=str(config.get('login_entry', '')),
            egp_uri=str(config.get('egp_uri', '')),
        )

        m3u_channels = generate_m3u(stb, m3u_path)
        merge_m3u(m3u_path, merge_config.get('m3u'))
        generate_selected_m3u(m3u_path, selected_config)

        generate_epg(stb, m3u_channels, epg_path)
        merge_epg(epg_path, merge_config.get('epg'))
    except IPTVError as e:
        logger.error('IPTV 错误 [%s]: %s', e.label, e)
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        logger.exception('程序异常退出: %s', e)
        sys.exit(1)
