# -*- coding: UTF-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
"""离线冒烟测试：mock 全部 HTTP，跑通电信/移动两个 ISP 的完整输出管线。

用途:
    python tests/test_smoke.py --update-golden   # 生成/更新 tests/golden 基线
    python tests/test_smoke.py                   # 对比当前代码输出与基线

数据源:
    电信频道表  docs/channellistHWCTC.jsp        (真实抓包)
    移动频道表  docs/cmcc_chlist-20260929.json   (真实抓包)
    其余(认证页/EPG/合并源) 为脚本内联合成的最小样本。

说明:
    本脚本通过 monkeypatch iptv._safe_http 拦截网络，按 URL 路由到 fixture；
    输出中的生成时间戳统一归一化为 @ TIMESTAMP 后再比较。
"""

import argparse
import gzip
import json
import os
import re
import shutil
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, ROOT)
import iptv  # noqa: E402

GOLDEN_DIR = os.path.join(SCRIPT_DIR, 'golden')

TIMESTAMP_RE = re.compile(r'@\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')

# ---------------------------------------------------------------------------
# 内联 fixtures
# ---------------------------------------------------------------------------

TELECOM_LOGIN_PAGE = """<html><body>
<form action="/EDS/jsps/Login.jsp" method="post">
<input type="hidden" name="Telecont" value="8001">
<input type="hidden" name="userID" value="">
</form>
</body></html>"""

TELECOM_AUTH_PAGE = """<html><head><script>
var TokenLen = 16;
    EncryptToken = "ABCDEF0123456789";
function submit() {}
</script></head><body>
<form name="authform" action="/EDS/jsps/Auth.jsp" method="post">
<input type="hidden" name="UserID" value="">
<input type="hidden" name="STBID" value="">
<input type="hidden" name="mac" value="">
<input type="hidden" name="userToken" value="">
<input type="hidden" name="Authenticator" value="">
</form>
</body></html>"""

TELECOM_EPG_PROGRAMS = json.dumps(
    [{}, [[
        {"beginTimeFormat": "20260101000000", "endTimeFormat": "20260101010000", "programName": "晚间新闻<直播>"},
        {"beginTimeFormat": "20260101010000", "endTimeFormat": "20260101020000", "programName": "天气预报&出行提示"},
        {"beginTimeFormat": "20260101020000", "endTimeFormat": "20260101030000", "programName": "精品剧场"},
    ]], "meta"], ensure_ascii=False, separators=(',', ':'))

TELECOM_EPG_PAGE = (
    '<html><body><script>\n'
    'parent.jsonBackLookStr = ' + TELECOM_EPG_PROGRAMS + ';\n'
    '</script></body></html>'
)

CMCC_PGLIST = {
    "resultCode": "000",
    "resultMessage": "ok",
    "content": [
        {"date": "2026-10-21", "programs": [
            {"programName": "晨报", "startTime": 1761000000, "endTime": 1761003600,
             "backPlayUrl": "http://198.18.0.3:8080/lookback/cctv-1/20261021000000/20261021010000/709001.m3u8",
             "resolution": []},
            {"programName": "剧场", "startTime": 1761003600, "endTime": 1761007200,
             "backPlayUrl": "",
             "resolution": [{"backPlayUrl": "http://198.18.0.3:8080/lookback/cctv-1/20261021010000/20261021020000/709002.m3u8"}]},
        ]},
        {"date": "2026-10-22", "programs": [
            {"programName": "午间新闻", "startTime": 1761080000, "endTime": 1761083600,
             "backPlayUrl": "http://198.18.0.3:8080/lookback/cctv-1/20261022000000/20261022010000/709003.m3u8",
             "resolution": []},
        ]},
    ],
}

MERGE_M3U = """#EXTM3U url-tvg="http://192.0.2.9:8000/epg/merge.xml"
#KODIPROP:inputstream=inputstream.ffmpegdirect
#EXTINF:-1 tvg-id="MergeSat" tvg-name="MergeSat" group-title="各省卫视" catchup="default" catchup-days="7" catchup-source="rtsp://198.51.100.9/mergesat?playseek={utc:YmdHMS}-{utcend:YmdHMS}", MergeSat频道
http://198.51.100.9/mergesat/index.m3u8
#EXTINF:-1 tvg-id="MergeRtp" tvg-name="MergeRtp" group-title="数字频道", MergeRtp
rtp://198.51.100.10:6000
#EXTINF:-1 tvg-id="IPTV剧集" tvg-name="IPTV剧集" group-title="数字频道", IPTV剧集
rtp://198.51.100.11:6000
#EXTINF:-1 tvg-id="求索记录" tvg-name="求索记录" group-title="数字频道" catchup="default" catchup-days="5" catchup-source="rtsp://198.51.100.12/qiusuo?playseek={utc:YmdHMS}-{utcend:YmdHMS}", 求索记录
rtp://198.51.100.12:6000
#EXTINF:-1 tvg-id="MergeUdpxy" tvg-name="MergeUdpxy" group-title="数字频道", MergeUdpxy
http://198.51.100.13/udp/239.10.0.200:5140
#EXTINF:-1 tvg-id="MergeHttps" tvg-name="MergeHttps" group-title="数字频道", MergeHttps
https://198.51.100.14/rtp/239.10.0.201:5140
"""

MERGE_EPG_XML = """<?xml version="1.0" encoding="utf-8"?>
<tv>
  <channel id="CCTV-1">
    <display-name lang="zh">CCTV-1</display-name>
  </channel>
  <programme start="20260101000000 +0800" stop="20260101010000 +0800" channel="CCTV-1">
    <title>合并节目甲</title>
  </programme>
  <programme start="20260101000000 +0800" stop="20260101010000 +0800" channel="CCTV-1">
    <title>合并节目甲</title>
  </programme>
  <channel id="MergeOnly">
    <display-name lang="zh">MergeOnly</display-name>
  </channel>
  <programme start="20260102000000 +0800" stop="20260102010000 +0800" channel="MergeOnly">
    <title>合并独有频道节目</title>
  </programme>
</tv>
"""

TELECOM_CONFIG = {
    "userid": "TESTUSER@ITV",
    "key": "12345678",
    "stbid": "0000000000000000",
    "mac": "00:11:22:33:44:55",
    "login_entry": "http://mock-telecom/EDS/jsp/AuthenticationURL",
    "egp_uri": "/EPG/jsp/liveplay_30/en/getTvodData.jsp",
    "x-tvg-url": "http://192.0.2.9:8000/epg/telecom-epg.xml",
    "log": "run.log",
    "rules": {"rename": {"CCTV-少儿": "CCTV-14"}},
    "m3u": [
        {"telecom-full.m3u": {"selected": False}},
        {"telecom-sel.m3u": {
            "catchup-format": "playseek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}",
            "proxy": "http://198.51.100.7:4022?token=abc",
            "fcc": "telecom",
        }},
        {"telecom-sel-cp.m3u": {
            "catchup-format": "playseek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}",
            "catchup-proxy": True,
            "proxy": "http://198.51.100.7:4022?token=abc",
            "fcc": True,
        }},
    ],
    "epg": ["telecom-epg.xml"],
    "merge": {"m3u": ["merge-extra.m3u"], "epg": ["merge-epg.xml"]},
    "selected": {"channels": ["CCTV-1", "CCTV-14", "CCTV-4K", "CCTV-16 4K", "MergeSat", "MergeRtp"]},
}

CMCC_CONFIG = {
    "isp": "cmcc",
    "x-tvg-url": "http://192.0.2.9:8000/epg/cmcc-epg.xml",
    "log": "run.log",
    "rules": {"rename": {"求索记录": "求索纪录"}},
    "cmcc": {
        "epg_base": "http://mock-cmcc:7070/ysten-wtv-epg/epg/wtv",
        "ability_string": {"userGroupIds": ["xxxx"]},
    },
    "m3u": [
        {"cmcc-full.m3u": {"selected": False}},
        {"cmcc-sel.m3u": {
            "proxy": "http://198.51.100.8:4022?token=xyz",
            "fcc": True,
            "catchup-proxy": True,
        }},
    ],
    "epg": ["cmcc-epg.xml"],
    "merge": {"m3u": ["merge-extra.m3u"], "epg": ["merge-epg.xml.gz"]},
    "selected": {"channels": ["CCTV-1", "CCTV-5", "MergeSat", "剧集", "求索纪录", "MergeUdpxy"]},
}

COMPARABLE = [
    'telecom-full.m3u', 'telecom-sel.m3u', 'telecom-sel-cp.m3u', 'telecom-epg.xml',
    'cmcc-full.m3u', 'cmcc-sel.m3u', 'cmcc-epg.xml',
]


# ---------------------------------------------------------------------------
# HTTP mock
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, url, content, content_type='text/html'):
        self.url = url
        self.status_code = 200
        self.headers = {'Content-Type': content_type}
        self._content = content

    @property
    def text(self):
        return self._content.decode('utf-8', errors='replace')

    @property
    def content(self):
        return self._content

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(self._content.decode('utf-8'))


def _route(url):
    if 'AuthenticationURL' in url:
        return TELECOM_LOGIN_PAGE.encode('utf-8')
    if 'jsps/Login' in url:
        return TELECOM_AUTH_PAGE.encode('utf-8')
    if 'jsps/Auth' in url or 'ServicesEntryHWCTC' in url:
        with open(os.path.join(ROOT, 'docs', 'channellistHWCTC.jsp'), 'rb') as fp:
            return fp.read()
    if 'getTvodData' in url:
        return TELECOM_EPG_PAGE.encode('utf-8')
    if '/chlist' in url:
        with open(os.path.join(ROOT, 'docs', 'cmcc_chlist-20260929.json'), 'rb') as fp:
            return fp.read()
    if '/pglist' in url:
        return json.dumps(CMCC_PGLIST, ensure_ascii=False).encode('utf-8')
    raise AssertionError('mock 未路由的 URL: %s' % url)


def fake_safe_http(method, url, session=None, log_label='', **kwargs):
    return FakeResponse(url, _route(url))


# ---------------------------------------------------------------------------
# 流水线驱动（与 iptv.py __main__ 块保持同序）
# ---------------------------------------------------------------------------

def run_pipeline(run_dir, config, builder):
    config_path = os.path.join(run_dir, 'config.json')
    with open(config_path, 'w', encoding='utf-8') as fp:
        json.dump(config, fp, ensure_ascii=False, indent=2)
    with open(os.path.join(run_dir, 'merge-extra.m3u'), 'w', encoding='utf-8') as fp:
        fp.write(MERGE_M3U)
    with open(os.path.join(run_dir, 'merge-epg.xml'), 'wb') as fp:
        fp.write(MERGE_EPG_XML.encode('utf-8'))
    with open(os.path.join(run_dir, 'merge-epg.xml.gz'), 'wb') as fp:
        with gzip.GzipFile(fileobj=fp, mode='wb', mtime=0) as gz:
            gz.write(MERGE_EPG_XML.encode('utf-8'))

    config = iptv.load_config(config_path)
    config_dir = run_dir
    merge_config = config.get('merge', {})
    merge_m3u_sources = iptv.resolve_merge_sources(merge_config.get('m3u'), config_dir)
    merge_epg_sources = iptv.resolve_merge_sources(merge_config.get('epg'), config_dir)
    selected_channels = iptv._as_list(config.get('selected', {}).get('channels'))

    isp = str(config.get('isp') or 'telecom').lower()
    isp_label = '移动' if isp == 'cmcc' else '电信'
    stb = builder(config)

    rules = iptv._resolve_rules(config)
    channels = iptv._prepare_channels(stb.get_channel_list(), rules)
    m3u_targets = iptv.parse_m3u_targets(config, config_dir, config.get("x-tvg-url", ""))
    merge_m3u_channels = iptv.load_merge_m3u_channels(merge_m3u_sources, rules)

    for epg_path in iptv.parse_epg_paths(config, config_dir):
        iptv.generate_epg(stb, channels, epg_path, source_info_name=f'{isp_label}IPTV')
        iptv.merge_epg(epg_path, merge_epg_sources)
    finalize = getattr(stb, 'finalize_catchup', None)
    if finalize:
        finalize(channels)

    for target in m3u_targets:
        target['options'].setdefault('isp-label', isp_label)
        target['options'].setdefault('isp', isp)
        target_merge_channels = merge_m3u_channels if target['options'].get('merge') else []
        iptv.generate_m3u(channels, target['path'], target['options'], selected_channels, target_merge_channels)


def telecom_builder(config):
    return iptv.IPTVSetTopBox(
        user_id=str(config['userid']), key=str(config['key']),
        stb_id=str(config['stbid']), mac=str(config['mac']),
        login_entry=str(config.get('login_entry', '')),
        egp_uri=str(config.get('egp_uri', '')),
    )


def cmcc_builder(config):
    return iptv.CMCCSetTopBox(config.get('cmcc') or {})


def run_all(out_dir):
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(os.path.join(out_dir, 'telecom'))
    os.makedirs(os.path.join(out_dir, 'cmcc'))
    original = iptv._safe_http
    iptv._safe_http = fake_safe_http
    try:
        run_pipeline(os.path.join(out_dir, 'telecom'), TELECOM_CONFIG, telecom_builder)
        run_pipeline(os.path.join(out_dir, 'cmcc'), CMCC_CONFIG, cmcc_builder)
    finally:
        iptv._safe_http = original


def normalize(path):
    with open(path, 'rb') as fp:
        text = fp.read().decode('utf-8')
    return TIMESTAMP_RE.sub('@ TIMESTAMP', text).encode('utf-8')


def main():
    parser = argparse.ArgumentParser(description='离线冒烟测试（golden 对比）')
    parser.add_argument('--update-golden', action='store_true', help='生成/更新基线')
    args = parser.parse_args()

    logging_ = iptv.logging.getLogger('iptv')
    logging_.addHandler(iptv.logging.NullHandler())
    logging_.setLevel(iptv.logging.WARNING)

    if args.update_golden:
        run_all(GOLDEN_DIR)
        gz_ok = all(
            os.path.exists(os.path.join(GOLDEN_DIR, isp, name + '.gz'))
            for isp in ('telecom', 'cmcc')
            for name in ('%s-epg.xml' % isp,)
        )
        print('golden updated at %s (gz files present: %s)' % (GOLDEN_DIR, gz_ok))
        return 0

    run_dir = os.path.join(tempfile.gettempdir(), 'opencode', 'smoke-run')
    run_all(run_dir)
    failed = 0
    for isp in ('telecom', 'cmcc'):
        for name in COMPARABLE:
            if not name.startswith(isp):
                continue
            actual = normalize(os.path.join(run_dir, isp, name))
            golden_path = os.path.join(GOLDEN_DIR, isp, name)
            if not os.path.exists(golden_path):
                print('MISSING GOLDEN: %s/%s' % (isp, name))
                failed += 1
                continue
            expected = normalize(golden_path)
            if actual == expected:
                print('PASS %s/%s' % (isp, name))
            else:
                failed += 1
                print('FAIL %s/%s' % (isp, name))
                exp_lines = expected.decode('utf-8', errors='replace').splitlines()
                act_lines = actual.decode('utf-8', errors='replace').splitlines()
                shown = 0
                for i, (e, a) in enumerate(zip(exp_lines, act_lines)):
                    if e != a:
                        print('  line %s' % (i + 1))
                        print('   golden: %s' % e[:160])
                        print('   actual: %s' % a[:160])
                        shown += 1
                        if shown >= 5:
                            break
                if len(exp_lines) != len(act_lines):
                    print('  line count: golden=%s actual=%s' % (len(exp_lines), len(act_lines)))
    print('%s' % ('ALL PASS' if failed == 0 else '%d FILE(S) FAILED' % failed))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
