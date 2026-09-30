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
import random
import argparse
import logging
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlsplit, parse_qs
from xml.etree import ElementTree as etree
from Crypto.Cipher import DES
from Crypto.Util.Padding import pad


REQUIRED_CONFIG_KEYS = ['userid', 'key', 'stbid', 'mac', 'login_entry', 'egp_uri']  # 电信模式必填
REQUIRED_CMCC_KEYS = ['epg_base']  # 移动模式必填（服务器地址不入库，仅经配置提供）
DEFAULT_CATCHUP_FORMAT = 'playseek={utc:YmdHMS}-{utcend:YmdHMS}'
DEFAULT_CMCC_CATCHUP_FORMAT = '{utc:YmdHMS}/{utcend:YmdHMS}/1.m3u8'
DEFAULT_CMCC_EPG_DAYS = 8
VALID_FCC_TYPES = ('huawei', 'telecom')
VALID_ISP_TYPES = ('telecom', 'cmcc')


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
        user_id,
        key,
        stb_id,
        mac,
        login_entry,
        egp_uri,
    ):
        self.user_id = user_id
        self.key = key
        self.stb_id = stb_id
        self.mac = mac
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
        data = self._build_auth_data(auth_form)
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

    def _build_auth_data(self, auth_form):
        data = {
            inp.get('name'): inp.get('value', '')
            for inp in auth_form.find_all('input')
            if inp.get('name')
        }
        data['UserID'] = self.user_id
        data['STBID'] = self.stb_id
        data['mac'] = self.mac
        return data

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
        if not self._channel_list:
            self.logger.error(
                '[get_channel_list] 未获取到频道列表，保留已有输出 (url=%s, status=%s, len=%s, content-type=%s)',
                resp.url, resp.status_code, len(resp.text),
                resp.headers.get('Content-Type', ''))
            self.logger.error('[get_channel_list] 原始响应前 1000 字符: %r', resp.text[:1000])
            raise IPTVError('未获取到频道列表', label='get_channel_list')
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
                    # < > 转全角避免下游解析问题；& 由 ElementTree 序列化时自行转义，勿手工替换（会二次转义）
                    title = title.replace('<', '《').replace('>', '》')
                    programs.append({
                        'start': start, 'stop': stop,
                        'title': title, 'channel_id': channel_id,
                    })
        except Exception as e:
            self.logger.error('[get_channel_programs] EPG数据遍历异常 (ch=%s): %s', channel_id, e)
        return programs


class CMCCSetTopBox:
    """移动 ysten/taipan EPG 数据源（免认证）。

    chlist/pglist 为前置 Nginx 缓存接口，不校验会话，认证链（iptvepg / ysten）
    与频道、节目单获取无关，因此无需模拟机顶盒认证。
    返回的频道结构与 IPTVSetTopBox 保持一致，供 M3U/EPG 输出管线直接复用。
    """

    def __init__(self, settings=None):
        settings = settings or {}
        self.epg_base = str(settings.get('epg_base') or '').strip().rstrip('/')
        if not self.epg_base:
            raise IPTVError('缺少必填配置: cmcc.epg_base（chlist/pglist 服务基地址）',
                            label='cmcc_config')
        try:
            self.epg_days = int(settings.get('epg_days') or DEFAULT_CMCC_EPG_DAYS)
        except (TypeError, ValueError):
            self.epg_days = DEFAULT_CMCC_EPG_DAYS
        self.ability_string = self._build_ability_string(settings.get('ability_string'))
        self.yauth = str(settings.get('yauth') or '').strip()
        # 回看网关：配置优先；缺省在拿到 chlist 后自动探测
        self.lookback_base = str(settings.get('lookback_base') or '').strip().rstrip('/')
        self.session = requests.Session()
        headers = {'User-Agent': 'gefo8(ysten) vfa5'}
        if self.yauth:
            headers['YAUTH'] = self.yauth
        self.headers = headers
        self._channel_list = []
        self.logger = logging.getLogger(f'{__name__}.{self.__class__.__name__}')
        self.logger.info('CMCC STB 初始化: epg_base=%s epg_days=%s lookback=%s',
                         self.epg_base, self.epg_days, self.lookback_base or '待探测')

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_ability_string(ability):
        if isinstance(ability, dict):
            return json.dumps(ability, ensure_ascii=False, separators=(',', ':'))
        return str(ability or '').strip()

    @staticmethod
    def _random():
        return f'0.{random.randint(10 ** 8, 10 ** 9 - 1)}'

    # ------------------------------------------------------------------
    # 回看网关探测
    # ------------------------------------------------------------------

    def _resolve_lookback_base(self, content):
        """回看网关前缀：配置优先；缺省用首个开时移频道的 pglist 探测一次。"""
        if self.lookback_base:
            return self.lookback_base
        for item in (content.get('channels') or []):
            if str(item.get('isShowBack') or '0') == '0':
                continue
            uuid = str(item.get('uuid') or '').strip()
            if not uuid:
                continue
            prefix = self._probe_lookback_base(uuid)
            if prefix:
                self.logger.info('回看网关探测成功: %s', prefix)
            else:
                self.logger.warning('回看网关探测失败，M3U 将不含回看地址；'
                                    '可在配置中显式指定 cmcc.lookback_base')
            return prefix
        return ''

    def _probe_lookback_base(self, channel_id):
        """拉一次最小日期范围的 pglist，从 backPlayUrl 按 /<uuid>/ 切出网关前缀。"""
        today = datetime.now(timezone(timedelta(hours=8)))
        try:
            data = self._request_json('/pglist', {
                'uuid': channel_id,
                'startDate': f'{today.year}-{today.month}-{today.day}',
                'endDate': f'{today.year}-{today.month}-{today.day}',
                'templateId': '',
                'abilityString': self.ability_string,
                'random': self._random(),
            }, f'CMCC-lookback-probe(ch={channel_id})')
        except IPTVError as e:
            self.logger.warning('[lookback探测] pglist 请求失败: %s', e)
            return ''
        marker = f'/{channel_id}/'
        for day in (data.get('content') or []):
            for prog in (day.get('programs') or []):
                back_url = str(prog.get('backPlayUrl') or '')
                if not back_url:
                    for res in (prog.get('resolution') or []):
                        back_url = str(res.get('backPlayUrl') or '')
                        if back_url:
                            break
                if not back_url:
                    continue
                head, sep, _tail = back_url.partition(marker)
                if sep and head.startswith('http'):
                    return head.rstrip('/')
                self.logger.warning('[lookback探测] backPlayUrl 结构异常: %s', back_url)
                return ''
        self.logger.warning('[lookback探测] pglist 无节目或无 backPlayUrl (ch=%s)', channel_id)
        return ''

    def _request_json(self, path, params, log_label):
        resp = _safe_http('GET', f'{self.epg_base}{path}', session=self.session,
                          log_label=log_label, params=params, headers=self.headers)
        try:
            return resp.json()
        except ValueError as e:
            self.logger.error('[%s] JSON解析失败: %s (url=%s)', log_label, e, resp.url)
            raise IPTVError(f'JSON解析失败 -> {resp.url}', label=log_label) from e

    @staticmethod
    def _fmt_time(ts):
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            return ''
        # 固定按北京时间转换，避免依赖宿主机时区
        return datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).strftime('%Y%m%d%H%M%S')

    def _epg_date_range(self):
        """返回 (startDate, endDate)，接口日期格式为 YYYY-M-D，月/日不补零。"""
        today = datetime.now(timezone(timedelta(hours=8)))
        start = today - timedelta(days=max(1, self.epg_days) - 2)
        end = today + timedelta(days=1)
        return (f'{start.year}-{start.month}-{start.day}',
                f'{end.year}-{end.month}-{end.day}')

    # ------------------------------------------------------------------
    # public API（与 IPTVSetTopBox 对齐）
    # ------------------------------------------------------------------

    def get_channel_list(self):
        """GET chlist 并映射为与电信模式相同的频道结构。"""
        if self._channel_list:
            return self._channel_list

        data = self._request_json('/chlist', {
            'templateId': '',
            'abilityString': self.ability_string,
            'random': self._random(),
        }, 'CMCC-chlist')
        if str(data.get('resultCode')) != '000':
            raise IPTVError(
                f'chlist 返回错误: {data.get("resultCode")} {data.get("resultMessage")}',
                label='cmcc_chlist')

        content = data.get('content') or {}
        self.lookback_base = self._resolve_lookback_base(content)
        for item in (content.get('channels') or []):
            channel = self._map_channel(item)
            if channel:
                self._channel_list.append(channel)
        if not self._channel_list:
            self.logger.error('[get_channel_list] chlist 无频道数据 (resultCode=%s)',
                              data.get('resultCode'))
            raise IPTVError('未获取到频道列表', label='cmcc_chlist')
        self.logger.info('频道列表获取完成: %s 个频道', len(self._channel_list))
        return self._channel_list

    def _map_channel(self, item):
        resolutions = item.get('resolution') or []
        resolution = resolutions[0] if resolutions else {}
        url = item.get('livePlayUrl') or resolution.get('url') or ''

        igmp_addr = ''
        fcc_ip = fcc_port = fec_port = ''
        for candidate in (url, item.get('multicastUrl') or ''):
            if not candidate:
                continue
            parts = urlsplit(candidate)
            if not parts.netloc:
                continue
            igmp_addr = parts.netloc.lstrip('@')  # 个别频道为 rtp://@239.x.x.x:5140
            query = parse_qs(parts.query)
            fcc_ip = (query.get('ChannelFCCIP') or [''])[0]
            fcc_port = (query.get('ChannelFCCPort') or [''])[0]
            fec_port = (query.get('channelFECPort') or [''])[0]
            break

        channel_id = str(item.get('uuid') or '').strip()
        if not channel_id or not igmp_addr:
            self.logger.warning('[_map_channel] 跳过无法解析的频道: %s (uuid=%s url=%s)',
                                item.get('name'), channel_id, url)
            return None
        timeshift = '0' if str(item.get('isShowBack') or '0') == '0' else '1'
        return {
            'id': channel_id,
            'name': str(item.get('name') or channel_id),
            'user_channel_id': str(item.get('num') or ''),
            'igmp_addr': igmp_addr,
            'timeshift': timeshift,
            'timeshift_len': '7',
            'timeshift_url': str(resolution.get('timeShiftAlias') or ''),
            'fcc_enable': '1' if (fcc_ip and fcc_port) else '0',
            'fcc_ip': fcc_ip,
            'fcc_port': fcc_port,
            'fec_port': fec_port,
            'catchup_prefix': (f'{self.lookback_base}/{channel_id}'
                               if timeshift != '0' and self.lookback_base else ''),
        }

    def get_channel_programs(self, channel_id):
        """GET pglist，返回 start/stop 为北京时间 yyyyMMddHHmmss 的节目列表。"""
        start_date, end_date = self._epg_date_range()
        data = self._request_json('/pglist', {
            'uuid': channel_id,
            'startDate': start_date,
            'endDate': end_date,
            'templateId': '',
            'abilityString': self.ability_string,
            'random': self._random(),
        }, f'EPG(ch={channel_id})')
        if str(data.get('resultCode')) != '000':
            self.logger.warning('[get_channel_programs] pglist 返回错误 (ch=%s): %s %s',
                                channel_id, data.get('resultCode'), data.get('resultMessage'))
            return []

        programs = []
        try:
            for day in data.get('content') or []:
                for prog in day.get('programs') or []:
                    start = self._fmt_time(prog.get('startTime'))
                    stop = self._fmt_time(prog.get('endTime'))
                    if not start or not stop:
                        continue
                    title = str(prog.get('programName') or '未知节目')
                    # < > 转全角避免下游解析问题；& 由 ElementTree 序列化时自行转义，勿手工替换（会二次转义）
                    title = title.replace('<', '《').replace('>', '》')
                    programs.append({
                        'start': start, 'stop': stop,
                        'title': title, 'channel_id': channel_id,
                    })
        except AttributeError as e:
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


def _m3u_header(x_tvg_url="", isp_label="电信"):
    line = (
        f'#EXTM3U name="{isp_label}IPTV '
        f'{os.path.basename(__file__)} @ {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())}"'
    )
    if x_tvg_url:
        line += f" x-tvg-url=\"{x_tvg_url}\""
    return line


def _append_query(url, query):
    if not query:
        return url
    if '?' not in url:
        return f'{url}?{query}'
    if url.endswith('?') or url.endswith('&'):
        return f'{url}{query}'
    return f'{url}&{query}'


def _proxy_base_url(proxy, source_url):
    source = urlparse(source_url)
    proxy_parts = urlparse(proxy)
    source_path = f'{source.scheme}/{source.netloc}{source.path}'
    proxy_path = proxy_parts.path.rstrip('/')
    base = f'{proxy_parts.scheme}://{proxy_parts.netloc}{proxy_path}/{source_path}'
    return _append_query(base, proxy_parts.query)


def _m3u_channel_url(ch, options):
    if ch.get('url'):
        return _merge_channel_url(ch['url'], options)

    source_url = f'rtp://{ch["igmp_addr"]}'
    proxy = options.get('proxy', '')
    if not proxy:
        return source_url

    url = _proxy_base_url(proxy, source_url)
    fcc_value = options.get('fcc')
    fcc_type = str(options.get('fcc-type') or (fcc_value if isinstance(fcc_value, str) else '')).lower()
    if fcc_value and not fcc_type and options.get('isp') == 'cmcc':
        fcc_type = 'telecom'  # fcc: true — 移动侧仅附加 fcc= 参数（取自频道 URL 自带参数）
    if fcc_value and fcc_type in VALID_FCC_TYPES:
        fcc_addr = f'{ch.get("fcc_ip", "")}:{ch.get("fcc_port", "")}'
        if ch.get('fcc_enable') != '0' and ch.get('fcc_ip') and ch.get('fcc_port'):
            url = _append_query(url, f'fcc={fcc_addr}')
            if fcc_type != 'telecom':
                url = _append_query(url, f'fcc-type={fcc_type}')
    return url


def _merge_channel_url(source_url, options):
    proxy = options.get('proxy', '')
    if not proxy or urlparse(source_url).scheme.lower() != 'rtp':
        return source_url
    return _proxy_base_url(proxy, source_url)


def _catchup_attr(ch, options):
    if ch.get('timeshift') == '0':
        return ''
    if 'catchup_prefix' in ch and not ch.get('catchup_prefix'):
        # 移动频道但回看网关不可用（探测失败）：优雅降级，不输出回看属性
        return ''
    catchup_prefix = ch.get('catchup_prefix')
    if catchup_prefix:
        # 移动: 路径式回看模板 .../lookback/<uuid>/<开始>/<结束>/1.m3u8
        catchup_format = str(options.get('catchup-format', DEFAULT_CMCC_CATCHUP_FORMAT)).strip('/')
        source = f'{catchup_prefix}/{catchup_format}'
    else:
        # 电信: 查询串式回看 ...?playseek=<开始>-<结束>
        catchup_format = str(options.get('catchup-format', DEFAULT_CATCHUP_FORMAT)).lstrip('?&')
        source = f'{ch["timeshift_url"]}?{catchup_format}'
    return (
        f' catchup="default" catchup-days="7"'
        f' catchup-source="{source}"'
    )


def _write_m3u_channels(filepath, channels, options=None):
    options = options or {}
    with open(filepath, 'w', encoding='utf-8') as fp:
        print(_m3u_header(options.get("x-tvg-url", ""), options.get("isp-label", "电信")), file=fp)
        for ch in channels:
            group_title = ch.get('group_title') or classify(ch['name'])
            catchup = _catchup_attr(ch, options) if 'timeshift' in ch else ''
            display_name = ch.get('display_name') or ch['name']
            tvg_id = ch.get('tvg_id') or ch['name']
            tvg_name = ch.get('tvg_name') or ch['name']
            for prop in ch.get('props', []):
                fp.write(f'{prop}\n')
            if catchup:
                fp.write('#KODIPROP:inputstream=inputstream.ffmpegdirect\n')
            fp.write(
                f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{tvg_name}"'
                f' group-title="{group_title}"{catchup}, {display_name}\n'
            )
            fp.write(f'{_m3u_channel_url(ch, options)}\n')


def _prepare_channels(channels):
    exclude_pattern = re.compile(
        '|'.join([
            '画中画', '直播', '组播', '中国体育', '导视', '指南', '推荐', '宣传', '云看',
            '红原', '乐山', '熊猫', '大爱', 'i成都',
        ])
    )
    clean_pattern = re.compile(r'(?:专区|高清|超清|频道)+$')
    clean_channels = []
    for ch in channels:
        if exclude_pattern.search(ch['name']):
            continue
        item = ch.copy()
        item['name'] = clean_pattern.sub('', item['name'])
        item['name'] = re.sub(r'[-_－—\s]+$', '', item['name'])
        if item['name'] == 'CCTV-少儿':
            item['name'] = 'CCTV-14'
        clean_channels.append(item)
    return clean_channels


def _channel_key(ch):
    return ch.get('name') or ch.get('url') or ''


def _dedupe_channels(channels):
    result = []
    used_keys = set()
    for ch in channels:
        key = _channel_key(ch)
        if not key:
            continue
        if key in used_keys:
            continue
        result.append(ch)
        used_keys.add(key)
    return result


def _dedupe_epg_root(root):
    channel_ids = set()
    programme_keys = set()
    for elem in list(root):
        if elem.tag == 'channel':
            channel_id = elem.get('id', '')
            if channel_id in channel_ids:
                root.remove(elem)
                continue
            channel_ids.add(channel_id)
        elif elem.tag == 'programme':
            title_elem = elem.find('title')
            title = title_elem.text if title_elem is not None else ''
            key = (elem.get('channel', ''), elem.get('start', ''), elem.get('stop', ''), title)
            if key in programme_keys:
                root.remove(elem)
                continue
            programme_keys.add(key)
    return channel_ids, programme_keys


def _select_channels(channels, selected_channels):
    if not selected_channels:
        return channels
    result = []
    used_indexes = set()
    for selected_name in selected_channels:
        selected_name = str(selected_name).strip()
        if not selected_name:
            continue
        match_index = None
        for index, ch in enumerate(channels):
            if index not in used_indexes and ch['name'] == selected_name:
                match_index = index
                break
        if match_index is None:
            for index, ch in enumerate(channels):
                if index in used_indexes:
                    continue
                if selected_name in ch['name'] or ch['name'] in selected_name:
                    match_index = index
                    break
        if match_index is None:
            continue
        item = channels[match_index].copy()
        item['display_name'] = selected_name
        result.append(item)
        used_indexes.add(match_index)
    return result


def generate_m3u(channels, filepath='iptv-full.m3u', options=None, selected_channels=None, merge_channels=None):
    """Generate an M3U playlist and return channels written to it."""
    logger = logging.getLogger(__name__)
    options = options or {}
    output_channels = _dedupe_channels(list(channels) + list(merge_channels or []))
    if options.get('selected'):
        output_channels = _select_channels(output_channels, selected_channels or [])
        if not output_channels:
            logger.warning('[generate_m3u] 未匹配到精选频道，跳过写入: %s', filepath)
            return []
    else:
        output_channels = _dedupe_channels(output_channels)

    try:
        _write_m3u_channels(filepath, output_channels, options)
        logger.info('M3U 文件已生成: %s (%s 个频道)', filepath, len(output_channels))
    except OSError as e:
        logger.error('[generate_m3u] 文件写入失败 (%s): %s', filepath, e)
    return output_channels


def _write_epg_gzip(filepath):
    with open(filepath, 'rb') as fp, gzip.open(filepath + '.gz', 'wb') as gz:
        gz.writelines(fp)


def _write_epg_tree(filepath, tv_root):
    tree = etree.ElementTree(tv_root)
    etree.indent(tree, space='  ')
    with open(filepath, 'wb') as fp:
        tree.write(fp, encoding='utf-8', xml_declaration=True)
    _write_epg_gzip(filepath)


def generate_epg(box, channels, filepath='iptv-epg.xml', source_info_name='电信IPTV'):
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
        tv_root.set('source-info-name', source_info_name)
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

    isp = str(config.get('isp') or 'telecom').strip().lower()
    if isp not in VALID_ISP_TYPES:
        raise IPTVError(f'不支持的 isp: {isp} (可选 {", ".join(VALID_ISP_TYPES)})')
    config['isp'] = isp
    if isp == 'cmcc':
        missing = [f'cmcc.{key}' for key in REQUIRED_CMCC_KEYS
                   if not (config.get('cmcc') or {}).get(key)]
    else:
        missing = [key for key in REQUIRED_CONFIG_KEYS if not config.get(key)]
    if missing:
        raise IPTVError(f'配置文件缺少必填项: {", ".join(missing)}')
    logger.info('配置文件已加载: %s (isp=%s)', filepath, isp)
    return config


def resolve_output_path(path, base_dir):
    if not path:
        return ''
    if os.path.isabs(path):
        return path
    return os.path.join(base_dir, path)



def _is_url(source):
    return urlparse(source).scheme in ('http', 'https')


def _as_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


def resolve_merge_sources(sources, base_dir):
    resolved_sources = []
    for source in _as_list(sources):
        source = str(source).strip()
        if not source:
            continue
        if _is_url(source) or os.path.isabs(source):
            resolved_sources.append(source)
        else:
            resolved_sources.append(os.path.join(base_dir, source))
    return resolved_sources


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


def _parse_extinf_attrs(extinf):
    return dict(re.findall(r'([\w-]+)="([^"]*)"', extinf))


def _parse_m3u_channels(text):
    channels = []
    pending_props = []
    current_attrs = None
    current_display = ''
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#EXTM3U'):
            continue
        if line.startswith('#EXTINF'):
            current_attrs = _parse_extinf_attrs(line)
            current_display = line.split(',', 1)[1].strip() if ',' in line else ''
            continue
        if line.startswith('#'):
            pending_props.append(line)
            continue
        if current_attrs is None:
            pending_props = []
            continue

        tvg_id = current_attrs.get('tvg-id', '').strip()
        tvg_name = current_attrs.get('tvg-name', '').strip()
        display_name = current_display or tvg_name or tvg_id or line
        name = tvg_name or tvg_id or display_name
        channels.append({
            'name': name,
            'display_name': display_name,
            'tvg_id': tvg_id or name,
            'tvg_name': tvg_name or name,
            'group_title': current_attrs.get('group-title', ''),
            'url': line,
            'props': pending_props,
        })
        pending_props = []
        current_attrs = None
        current_display = ''
    return channels


def load_merge_m3u_channels(sources):
    logger = logging.getLogger(__name__)
    channels = []
    for source in _as_list(sources):
        data = _read_merge_source(source)
        if not data:
            continue
        text = data.decode('utf-8-sig', errors='ignore')
        source_channels = _parse_m3u_channels(text)
        channels.extend(source_channels)
        logger.info('已读取合并 M3U: %s (%s 个频道)', source, len(source_channels))
    return channels


def merge_epg(filepath, sources):
    logger = logging.getLogger(__name__)
    sources = _as_list(sources)
    if not sources:
        return

    try:
        tree = etree.parse(filepath)
        root = tree.getroot()
        channel_ids, programme_keys = _dedupe_epg_root(root)
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
            merged_count = 0
            for elem in list(merge_root):
                if elem.tag == 'channel':
                    channel_id = elem.get('id', '')
                    if channel_id in channel_ids:
                        continue
                    channel_ids.add(channel_id)
                elif elem.tag == 'programme':
                    title_elem = elem.find('title')
                    title = title_elem.text if title_elem is not None else ''
                    key = (elem.get('channel', ''), elem.get('start', ''), elem.get('stop', ''), title)
                    if key in programme_keys:
                        continue
                    programme_keys.add(key)
                root.append(elem)
                merged_count += 1
            logger.info('已合并 EPG: %s -> %s (%s 个节点)', source, filepath, merged_count)
        _write_epg_tree(filepath, root)
        logger.info('EPG 合并文件已生成: %s.gz', filepath)
    except OSError as e:
        logger.error('[merge_epg] 文件处理失败 (%s): %s', filepath, e)
    except etree.ParseError as e:
        logger.error('[merge_epg] 目标 XML 解析失败 (%s): %s', filepath, e)


def parse_m3u_targets(config, base_dir, global_x_tvg_url=""):
    logger = logging.getLogger(__name__)
    selected_channels = _as_list(config.get('selected', {}).get('channels'))
    has_merge = bool(_as_list(config.get('merge', {}).get('m3u')))
    m3u_config = config.get('m3u', 'iptv-full.m3u')

    legacy_scalar = not isinstance(m3u_config, list)
    items = m3u_config if isinstance(m3u_config, list) else [m3u_config]
    targets = []
    for item in items:
        path = ''
        options = {}
        if isinstance(item, str):
            path = item
        elif isinstance(item, dict) and item:
            path, options = next(iter(item.items()))
            if not isinstance(options, dict):
                options = {}
        else:
            logger.warning('[config] 跳过无效 m3u 配置: %s', item)
            continue

        options = options.copy()
        if 'selected' not in options:
            options['selected'] = bool(selected_channels) and not legacy_scalar
        if 'merge' not in options:
            options['merge'] = has_merge
        if 'x-tvg-url' not in options and global_x_tvg_url:
            options['x-tvg-url'] = global_x_tvg_url
        targets.append({'path': resolve_output_path(path, base_dir), 'options': options})
    return targets


def parse_epg_paths(config, base_dir):
    epg_config = config.get('epg', 'iptv-epg.xml')
    return [resolve_output_path(path, base_dir) for path in _as_list(epg_config) if path]


# ---------------------------------------------------------------------------
# 主程序入口：在此处集中配置 logger，然后注入到各个组件
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    file_path = os.path.dirname(os.path.abspath(__file__)) or '.'

    parser = argparse.ArgumentParser(description='电信/移动 IPTV M3U/EPG 生成器')
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

        merge_config = config.get('merge', {})
        merge_m3u_sources = resolve_merge_sources(merge_config.get('m3u'), config_dir)
        merge_epg_sources = resolve_merge_sources(merge_config.get('epg'), config_dir)
        selected_config = config.get('selected', {})
        selected_channels = _as_list(selected_config.get('channels'))

        isp = str(config.get('isp') or 'telecom').lower()
        isp_label = '移动' if isp == 'cmcc' else '电信'
        if isp == 'cmcc':
            stb = CMCCSetTopBox(config.get('cmcc') or {})
        else:
            stb = IPTVSetTopBox(
                user_id=str(config['userid']),
                key=str(config['key']),
                stb_id=str(config['stbid']),
                mac=str(config['mac']),
                login_entry=str(config.get('login_entry', '')),
                egp_uri=str(config.get('egp_uri', '')),
            )

        channels = _prepare_channels(stb.get_channel_list())
        m3u_targets = parse_m3u_targets(config, config_dir, config.get("x-tvg-url", ""))
        merge_m3u_channels = load_merge_m3u_channels(merge_m3u_sources)
        for target in m3u_targets:
            target['options'].setdefault('isp-label', isp_label)
            target['options'].setdefault('isp', isp)
            target_merge_channels = merge_m3u_channels if target['options'].get('merge') else []
            generate_m3u(channels, target['path'], target['options'], selected_channels, target_merge_channels)

        for epg_path in parse_epg_paths(config, config_dir):
            generate_epg(stb, channels, epg_path, source_info_name=f'{isp_label}IPTV')
            merge_epg(epg_path, merge_epg_sources)
    except IPTVError as e:
        logger.error('IPTV 错误 [%s]: %s', e.label, e)
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        logger.exception('程序异常退出: %s', e)
        sys.exit(1)
