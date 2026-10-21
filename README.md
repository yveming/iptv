# IPTV Set-Top Box Simulator

电信 / 移动 IPTV 机顶盒模拟工具，用于完成机顶盒认证（移动模式免认证）、获取频道列表，并生成 M3U 播放列表和 XMLTV EPG 节目单。

## 功能

- 模拟电信 IPTV 机顶盒认证流程
- 支持电信/移动 IPTV，自动生成 M3U 直播播放列表和 XMLTV EPG 节目单，包括 `.xml` 和 `.gz` 格式
- 支持一次运行输出多个 M3U 文件
- 支持按频道名关键词生成精选 M3U
- 精选频道支持模糊匹配，并按选定顺序输出
- 精选频道写入 M3U 时可替换显示名，且不改变 `tvg-id` / `tvg-name`
- 支持通过 `rules` 配置自定义频道过滤、名称前后缀清洗、改名与分组归类
- 支持代理播放地址和 FCC 参数配置
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

## 移动 IPTV

配置文件设置 `"isp": "cmcc"` 即启用移动模式。频道与节目单来自移动侧 ysten EPG 接口（`chlist` / `pglist`），无需认证，电信模式的必填字段（`userid`、`key`、`stbid`、`login_entry`、`egp_uri`）全部不再需要，移动模式仅要求 `epg_base` 和 `ability_string`：

```json
{
  "isp": "cmcc",
  "log": "iptv-cmcc.log",
  "x-tvg-url": "http://192.168.1.100/epg/iptv-cmcc-epg.xml",

  "cmcc": {
    "epg_base": "http://192.0.2.3:7070/ysten-wtv-epg/epg/wtv",
    "ability_string": {"userGroupIds": ["xxxx"]}
  },

  "m3u": ["iptv-cmcc.m3u"],
  "epg": ["iptv-cmcc-epg.xml"]
}
```

`epg_base` 为ysten服务基地址，各地市不同，需通过抓包获取。

`cmcc` 其余字段均可省略：

- `ability_string`：**实测必配**——空串返回 `COS-900 无可用结果`。最小可用形式为
  `{"userGroupIds": ["<组id>"]}`（组 id 来自抓包，如 `queryuserinfo` 下发的
  usergroupid）

移动模式的输出差异：

- 回看为路径式模板，默认
  `catchup-source=".../lookback/<回看id>/{utc:YmdHMS}/{utcend:YmdHMS}/1.m3u8"`，
  回看 id 取自 pglist `backPlayUrl`（可能与频道 uuid 不同）；
  仍可用 `catchup-format` 覆盖，例如 `${(b)yyyyMMddHHmmss}/${(e)yyyyMMddHHmmss}/1.m3u8`

注意：移动模式的 EPG 服务器仅在企业网内可达，需要在移动 IPTV 网络（或挂靠该网络的设备）上运行。

## 配置文件

配置文件为 JSON 格式。电信模式（`isp` 缺省或 `"telecom"`）必填字段：

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
  "stbid": "1234567890123456",
  "mac": "11:22:33:44:55:66",
  "login_entry": "http://192.0.2.1:8082/EDS/jsp/AuthenticationURL",
  "egp_uri": "/EPG/jsp/liveplay_30/en/getTvodData.jsp",

  "x-tvg-url": "http://192.168.1.100/epg/iptv-epg.xml",

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

> 示例中的服务器地址（`192.0.2.x` 等）均为文档占位，实际的认证入口、EPG 接口以抓包或运营商提供为准。

### 认证字段

程序会在 `IPTVSetTopBox` 内部生成认证表单数据。配置中的：

- `userid` 会映射到认证字段 `UserID`
- `stbid` 会映射到认证字段 `STBID`
- `mac` 会映射到认证字段 `mac`

其他认证表单字段未配置时会自动使用空字符串。

### 输出路径

- `m3u`：M3U 输出配置，可以是字符串或数组
- `x-tvg-url`：全局 EPG 地址，每个 M3U 输出可在对应选项中覆盖
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
- `catchup-proxy`：回看地址是否走 `proxy` 代理，默认 `false`；为 `true` 且配置了 `proxy` 时，`catchup-source` 按回看类型改写代理中间段：`rtp` → `/rtp/`、`rtsp` → `/rtsp/`、`http` → `/http/`；`https` 及其他类型不支持，回退原始地址
- `proxy`：代理地址
- `fcc`：FCC 参数开关/类型；`true` ≡ `"telecom"`（仅附加 `fcc=`），`"huawei"` 会附加 `fcc-type=huawei`
- `fcc-type`：FCC 协议类型（telecom / huawei 两种标准实现，跨运营商适用）；也可 `"fcc": true, "fcc-type": "huawei"` 拆开写
- `x-tvg-url`：EPG 地址，写入 `#EXTM3U x-tvg-url="..."`；未配置时回退使用顶层同名字段

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
- EPG 匹配：本机频道的 `tvg-id` / `tvg-name` 保持频道真实名，不会因显示名替换而改变；合并 M3U 频道则采用精选名，使 `tvg-id` / `tvg-name` / 显示名一致
- 同一真实频道只会输出一次

### 频道清洗与分组规则（rules）

`rules` 用于自定义频道过滤、名称清洗与分组归类。均可省略：`filter` / `strip` /
`groups` 未提供时使用内置默认；`rename` 无内置默认，按需配置。

```json
{
  "rules": {
    "filter": ["画中画", "购物"],
    "strip": ["专区$", "高清$", "超清$", "频道$", "-$", "_$", "－$", "—$", " $", "^IPTV"],
    "rename": {"CCTV-少儿": "CCTV-14"},
    "groups": [
      {"title": "4K超高清", "match": ["4K"]},
      {"title": "CCTV央视", "match": ["CCTV", "CGTN", "音乐现场", "CETV"]},
      {"title": "数字频道"}
    ]
  }
}
```

处理顺序：过滤 -> 尾部/头部剥离 -> 精确改名 -> 分组归类（仅作用于本机频道）。

- `filter`：频道名包含任一关键词（字面匹配）即剔除
- `strip`：`词$` 或无标记剥尾部，`^词` 剥头部；词条按字面匹配，无需转义正则；
  同方向词条合并生效，可一次剥净连续组合（如 `CCTV-1高清频道` -> `CCTV-1`）；
  应用顺序为先尾部后头部
- `rename`：清洗后的名称精确匹配时改名
- `groups`：`match` 关键词任一命中即归入该组，按列表顺序首个命中生效；
  省略 `match` 的条目为兜底组；全部未命中且无兜底条目时回退“数字频道”

分组归属：本机频道按清洗后的名称归类；外部合并频道沿用其自带的 `group-title`，
缺失时按内置规则归类。

`strip` / `rename` 与 `groups` 同样作用于外部合并频道：名称清洗后，`tvg-id` /
`tvg-name` 统一重建为清洗后的频道名（外部源 id 常缺失或重复，不可信），分组按
`groups` 重新归类；`filter` 不过滤合并频道。外部 EPG 需按清洗后的频道名提供节目。

### 代理与 FCC

配置了 `proxy` 后，直播 URL 会转换为代理格式。例如：

```json
{
  "proxy": "http://192.168.1.50:4022?r2h-token=12345678",
  "fcc": "telecom"
}
```

输出示例：

```text
http://192.168.1.50:4022/rtp/239.94.0.31:5140?r2h-token=12345678&fcc=192.0.2.2:8027
```

`fcc` 支持：

- `telecom` 与 `huawei` 是 FCC 协议的两种实现，电信 / 移动 IPTV 均可能采用
  其中一种，与运营商无关
- `true` ≡ `"telecom"`：输出 `fcc=FCC服务器IP:端口`，不附加 `fcc-type` 参数
- `"huawei"`：输出 `fcc=FCC服务器IP:端口&fcc-type=huawei`
- 也可拆开写：`"fcc": true, "fcc-type": "huawei"`
- 只有频道携带 FCC 服务器参数时才会附加（移动取自 chlist 频道 URL 内嵌参数、
  电信取自频道表字段），缺失时静默省略

也可以写成：

```json
{
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

外部 M3U 会先解析成频道，按本机 `rules` 统一处理：`strip` / `rename` 清洗名称，
`tvg-id` / `tvg-name` 重建为清洗后的频道名（外部源 id 不可信），分组按 `groups`
归类，之后与本机频道一起参与精选匹配、排序和去重；如果目标 M3U 配置了 `proxy`，外部 M3U 中的 `rtp://` 播放地址也会转换为代理地址，`http://` / `https://` 地址保持原样。频道自带的 `catchup` / `catchup-days` / `catchup-source` 等回看属性会被保留；目标 M3U 开启 `catchup-proxy` 时，外部频道的回看地址也按同样规则套用代理。合并频道命中精选时，`tvg-id` / `tvg-name` / 显示名会统一替换为精选名，因此外部 EPG 需使用对应的频道名才能匹配。

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
