# 动漫 115

MoviePilot V2 插件。从动漫花园给追更名单补磁力，提交到 MoviePilot 里已经登录的 115 离线，下完后整理进季目录。整包摊平先做分类，没有安全移动就不改文件。

外壳跟 [mp-115](https://github.com/wyh137815840/mp-115) 的「115 AV刮削」一样：`plugins.v2/`、`package.v2.json`、Vuetify 表单、`只预览` 默认开着。115 只用存储 ID `u115` 这一个已登录会话，不另填 cookie。

## 安装

在 MoviePilot 的插件仓库里加入本仓库，或把 `PLUGIN_LOCAL_REPO_PATHS` 指到这个目录。同步插件源后安装「动漫 115」。

也可以把 `plugins.v2/anime115` 复制到 MoviePilot 的 `app/plugins/anime115`。

## 使用

1. MoviePilot 里先登录 115，存储 ID 保持 `u115`。
2. 追更每行一部：`剧名 | 别名 | group=字幕组`。
3. 离线目录填 115 上的真实路径，例如 `/离线下载/动漫`。日期子目录由插件加。`/115open` 只是旧挂载前缀，填在「挂载前缀」里会被剥掉。
4. 库根填整理目标，例如 `/动漫`。搬过去的形状是 `剧名/Season 01/原来的文件名`。
5. 先保持「只预览」。每日任务只会列出将要提交的磁力。
6. 确认后再关掉「只预览」。摊平还要另外打开「摊平允许落盘」。全是碰撞或不确定时，摊平一次都不移动。

定时留空就只手动运行。打开「保存后立即跑一次每日任务」，或调用 `POST /api/v1/plugin/Anime115/run`。

## 测试

```powershell
python -m unittest tests.test_anime115 -v
```
