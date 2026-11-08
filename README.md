# 动漫 115

MoviePilot V2 插件。从动漫花园给追更名单补磁力，提交到 MoviePilot 里已经登录的 115 离线，下完后整理进季目录。整包摊平先做分类，没有安全移动就不改文件。

外壳跟 [mp-115](https://github.com/wyh137815840/mp-115) 的「115 AV刮削」一样：`plugins.v2/`、`package.v2.json`、Vuetify 表单、`只预览` 默认开着。115 只用存储 ID `u115` 这一个已登录会话，不另填 cookie。

## 安装

在 MoviePilot 的插件仓库里加入本仓库，或把 `PLUGIN_LOCAL_REPO_PATHS` 指到这个目录。同步插件源后安装「动漫 115」。

也可以把 `plugins.v2/anime115` 复制到 MoviePilot 的 `app/plugins/anime115`。

## 使用

1. MoviePilot 里先登录 115，存储 ID 保持 `u115`。115 Cookie 和「115 AV刮削」同一格式（`UID=...; CID=...; SEID=...; KID=...`）。留空就用这个已登录存储，不另建客户端。`strm 目录` 填 Emby 正在扫描的库路径，`播放地址模板` 用外网域名上的 115网盘STRM助手跳转，不能写 `127.0.0.1`。
2. 追更每行一部：`剧名 | 别名 | group=字幕组 | tmdb_id=编号 | 连载中`。`[05]`、`[01v2]` 这样的集数可以入选。标题比对会忽略星号和空格，并把常见繁体折成简体，所以 `魔法★探险家` 能对上 `魔法★探險家`；仍建议把 `Magical Explorer` 写成别名。写了「连载中」才按 TMDB 已播集数封顶；`tmdb_id=` 对不上名字时用来指定剧。插件配置里的 TMDB API Key 填 v3 密钥，留空则用 MoviePilot 系统设置里的 `TMDB_API_KEY`。
3. 离线目录填 115 上的真实路径，例如 `/离线下载/动漫`。日期子目录由插件加。`/115open` 只是旧挂载前缀，填在「挂载前缀」里会被剥掉。
4. 库根填整理目标，例如 `/动漫`。搬过去的形状是 `剧名/Season 01/原来的文件名`。
5. 先保持「只预览」。每日任务只会列出将要提交的磁力。
6. 确认后再关掉「只预览」。摊平还要另外打开「摊平允许落盘」。全是碰撞或不确定时，摊平一次都不移动。

定时留空就只手动运行。打开「保存后立即跑一次每日任务」，或调用 `POST /api/v1/plugin/Anime115/run`。

动漫花园走 MoviePilot 系统设置里的代理（`PROXY_HOST`）。没填就直连。容器里直连若出现 `SSL: UNEXPECTED_EOF_WHILE_READING`，把代理填进 MoviePilot，不要改插件代码。日志里会写「经系统代理拉取」或「直连拉取」。

## 测试

```powershell
python -m unittest tests.test_anime115 -v
```
