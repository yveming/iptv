# IPTV Set-Top Box Simulator

电信 IPTV 机顶盒模拟工具，用于完成机顶盒认证、获取频道列表，并生成 M3U 播放列表和 XMLTV EPG 节目单。

## 功能

- 模拟 IPTV 机顶盒认证流程
- 生成 M3U 直播播放列表
- 支持一次运行输出多个 M3U 文件
- 支持按频道名关键词生成精选 M3U
- 精选频道支持模糊匹配，并按 `selected.channels` 的顺序输出
- 精选频道写入 M3U 时可替换显示名，且不改变 `tvg-id` / `tvg-name`
- 支持代理播放地址和 FCC 参数输出
- 生成 XMLTV EPG 节目单，并自动生成 `.gz` 压缩文件
- 通过 JSON 配置认证信息、认证入口、EPG 接口和输出路径
- 支持合并外部 M3U / EPG 到最终输出文件
- 外部合并源支持本地文件和 `http://` / `https://` 远程文件

## 安装依赖

```bash
pip install requests beautifulsoup4 pycryptodome
```

## 快速开始

按下面示例生成一个 `iptv.json` 配置文件。

```bash
python iptv.py -f iptv.json
```

如果不传 `-f`，默认读取脚本目录下的 `iptv.json`：

```bash
python iptv.py
```

运行后默认生成配置中指定的 M3U、EPG 和日志文件。

## 配置文件

配置文件为 JSON 格式。必填字段：

- `userid`
- `key`
- `stbid`
- `mac`
- `login_entry`
- `egp_uri`

示例：

```json
{
  "userid": "UserID@ITV",
  "key": "12345678",
  "stbid": "12345678890123456",
  "mac": "11:22:33:44:55:66",
  "login_entry": "http://182.138.3.142:8082/EDS/jsp/AuthenticationURL",
  "egp_uri": "/EPG/jsp/liveplay_30/en/getTvodData.jsp",

  "log": "iptv.log",

  "m3u": [
    {
      "iptv-full.m3u": {
        "catchup-format": "playseek={{utc:YmdHMS}}-{{utcend:YmdHMS}}",
        "selected": false
      }
    },
    "iptv精选.m3u",
    {
      "iptv精选-1.m3u": {
        "catchup-format": "playseek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}",
        "proxy": "http://192.168.1.1:4022?r2h-token=12345678",
        "fcc": "telecom"
      }
    }
  ],

  "epg": ["iptv-epg.xml"],

  "merge": {
    "m3u": [
      "extra.m3u",
      "https://example.com/live.m3u"
    ],
    "epg": [
      "extra.xml",
      "https://example.com/epg.xml",
      "https://example.com/epg.xml.gz"
    ]
  },

  "selected": {
    "channels": [
      "CCTV-1综合",
      "CCTV-5体育",
      "四川卫视"
    ]
  }
}
```

### 认证字段

程序会在 `IPTVSetTopBox` 内部生成认证表单数据。配置中的：

- `userid` 会映射到认证字段 `UserID`
- `stbid` 会映射到认证字段 `STBID`
- `mac` 会映射到认证字段 `mac`

其他认证表单字段未配置时会自动使用空字符串。

### 输出路径

- `m3u`：M3U 输出配置，可以是字符串或数组
- `epg`：XMLTV EPG 输出配置，可以是字符串或数组
- `log`：日志文件输出路径

相对路径会以配置文件所在目录为基准解析。

### M3U 输出配置

`m3u` 支持两种形式。

单个输出文件：

```json
{
  "m3u": "iptv-full.m3u"
}
```

多个输出文件：

```json
{
  "m3u": [
    "iptv精选.m3u",
    {
      "iptv-full.m3u": {
        "selected": false
      }
    }
  ]
}
```

数组元素可以是：

- 字符串：表示输出文件路径，使用默认选项
- 单键对象：键为输出文件路径，值为该 M3U 的选项

可用选项：

- `selected`：是否只输出精选频道
- `merge`：是否合并 `merge.m3u` 中的外部源
- `catchup-format`：回看 URL 的 `playseek` 参数格式
- `proxy`：代理地址
- `fcc`：FCC 协议类型，可为 `"telecom"` 或 `"huawei"`
- `fcc-type`：FCC 协议类型；也支持 `"fcc": true, "fcc-type": "telecom"` 的写法

默认规则：

- 配置了 `selected.channels` 时，各 M3U 的 `selected` 默认是 `true`
- 显式配置 `"selected": false` 时输出全量频道
- 配置了 `merge.m3u` 时，各 M3U 的 `merge` 默认是 `true`
- 显式配置 `"merge": false` 时不合并外部 M3U

### 精选频道

`selected.channels` 用于筛选精选频道：

```json
{
  "selected": {
    "channels": ["CCTV-1综合", "CCTV-5体育", "四川卫视"]
  }
}
```

匹配规则：

- 模糊匹配：频道真实名包含配置值，或配置值包含频道真实名，均视为命中
- 输出顺序：按 `selected.channels` 中的顺序输出
- 输出名称：命中后，M3U 逗号后的显示名会替换为 `selected.channels` 中的配置值
- EPG 匹配：`tvg-id` / `tvg-name` 保持频道真实名，不会因显示名替换而改变
- 同一真实频道只会输出一次

### 代理与 FCC

配置了 `proxy` 后，直播 URL 会转换为代理格式。例如：

```json
{
  "proxy": "http://192.168.100.10:4022?r2h-token=12345678",
  "fcc": "telecom"
}
```

输出示例：

```text
http://192.168.100.10:4022/rtp/239.94.0.31:5140?r2h-token=12345678&fcc=118.123.55.74:8027
```

`fcc` 支持：

- `"telecom"`：输出 `fcc=FCC服务器IP:端口`，省略 `fcc-type`
- `"huawei"`：输出 `fcc=FCC服务器IP:端口&fcc-type=huawei`

也可以写成：

```json
{
  "proxy": "http://192.168.100.10:4022?r2h-token=3580bdfe",
  "fcc": true,
  "fcc-type": "huawei"
}
```

只有频道开启 FCC 且存在 FCC IP/端口时，才会附加 FCC 参数。

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

外部 M3U 会先解析成频道，再与本机频道一起参与精选匹配、排序和去重；如果目标 M3U 配置了 `proxy`，外部 M3U 中的 `rtp://` 播放地址也会转换为代理地址，`http://` / `https://` 地址保持原样。

EPG 合并后会按频道 `id` 和节目 `(channel, start, stop, title)` 去重，重新写入目标 XML，并重新生成 `.gz` 文件。

## 辅助脚本

- [findkey.py](findkey.py)：用于尝试从 Authenticator 中查找 DES key。

## 授权协议

本项目所有 Python 源码文件使用 GNU Affero General Public License v3.0 or later 授权。

SPDX 标识：

```text
AGPL-3.0-or-later
```

如需完整协议文本，请参考：<https://www.gnu.org/licenses/agpl-3.0.html>
