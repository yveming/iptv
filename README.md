# IPTV Set-Top Box Simulator

电信 IPTV 机顶盒模拟工具，用于完成机顶盒认证、获取频道列表，并生成 M3U 播放列表和 XMLTV EPG 节目单。

## 功能

- 模拟 IPTV 机顶盒认证流程
- 生成 M3U 直播播放列表
- 生成 XMLTV EPG 节目单，并自动生成 `.gz` 压缩文件
- 通过 JSON 配置认证信息、认证入口、EPG 接口和输出路径
- 支持合并外部 M3U / EPG 到最终输出文件
- 外部合并源支持本地文件和 `http://` / `https://` 远程文件
- 支持按频道名关键词生成精选 M3U

## 安装依赖

```bash
pip install requests beautifulsoup4 pycryptodome
```

## 快速开始

按下面实例生成一个iptv.json配置文件。

```bash
python iptv.py -f iptv.json
```

如果不传 `-f`，默认读取脚本目录下的 `iptv.json`：

```bash
python iptv.py
```

运行后默认生成：

- `iptv-full.m3u`
- `iptv-epg.xml`
- `iptv-epg.xml.gz`
- `iptv.log`

## 配置文件

配置文件为 JSON 格式。必填字段只有：

- `UserID`
- `key`
- `STBID`
- `login_entry`
- `egp_uri`

其他机顶盒认证字段都是可选项；没有配置的字段会自动赋值为空字符串 `""`。

示例：

```json
{
  "UserID": "UserID@ITV",
  "key": "12345678",
  "STBID": "12345678890123456",
  "mac": "11:22:33:44:55:66",
  "login_entry": "http://182.138.3.142:8082/EDS/jsp/AuthenticationURL",
  "egp_uri": "/EPG/jsp/liveplay_30/en/getTvodData.jsp",

  "m3u": "iptv-full.m3u",
  "epg": "iptv-epg.xml",
  "log": "iptv.log",

  "merge": {
    "m3u": [
      "extra.m3u",
      "https://example.com/live.m3u"
    ],
    "epg": [
      "extra.xml",
      "https://example.com/epg.xml"
    ]
  },

  "selected": {
    "path": "iptv-select.m3u",
    "channels": [
      "CCTV1",
      "CCTV5",
      "四川卫视"
    ]
  }
}
```

### 输出路径

- `m3u`：最终 M3U 输出路径
- `epg`：最终 XMLTV EPG 输出路径
- `log`：日志文件输出路径

相对路径会以配置文件所在目录为基准解析。

### 合并外部 M3U / EPG

`merge.m3u` 和 `merge.epg` 都是列表，可以配置多条来源：

```json
{
  "merge": {
    "m3u": [
      "local-extra.m3u",
      "https://example.com/extra.m3u"
    ],
    "epg": [
      "local-extra.xml",
      "https://example.com/extra.xml",
      "https://example.com/extra.xml.gz"
    ]
  }
}
```

程序会自动识别本地路径和 HTTP/HTTPS URL。

EPG 合并后会重新写入目标 XML，并重新生成 `.gz` 文件。

### 精选频道

`selected` 用于额外生成一个只包含指定频道的 M3U：

```json
{
  "selected": {
    "path": "iptv精选.m3u",
    "channels": ["CCTV1", "CCTV5", "四川卫视"]
  }
}
```

只要频道名包含 `channels` 列表中的任意字符串，就会输出到 `selected.path`。

## 辅助脚本

- [findkey.py](findkey.py)：用于尝试从 Authenticator 中查找 DES key。

## 授权协议

本项目所有 Python 源码文件使用 GNU Affero General Public License v3.0 or later 授权。

SPDX 标识：

```text
AGPL-3.0-or-later
```

如需完整协议文本，请参考：<https://www.gnu.org/licenses/agpl-3.0.html>
