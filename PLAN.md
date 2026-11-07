# 动漫 115 插件 — 实现计划（待批准）

状态：**只计划，未写插件代码。** 批准前不要加 `plugins.v2/`、`package.v2.json` 或任何会改变 MoviePilot 行为的文件。

本文是实现前的合同。批准时可以直接写「按 PLAN.md 做」，或在文末的决定项上改默认值。改过的默认值以你的回复为准。

## 这份仓库里实际有什么

`wyh137815840/mp-animate` 现在只有一行 `README.md`，没有插件、没有测试、没有 `package.v2.json`。

插件的外壳以 [jinyuhao-886/MoviePilot-Plugins](https://github.com/jinyuhao-886/MoviePilot-Plugins) 为准，不另起一套结构。该仓库是 MoviePilot V2 插件市场源：根上 `package.v2.json`（`package.json` 是空对象 `{}`），插件在 `plugins.v2/<类名小写>/`。市场里实际登记了两个插件：

| 插件 | 目录 | 和本计划的关系 |
| --- | --- | --- |
| `P115StrgmSub`「115网盘订阅追更魔改版」1.7.4 | `plugins.v2/p115strgmsub/` | 外壳照它：类字段、`ui/config.py`、`handlers/`、`get_service`、`VCronField`、`onlyonce`、`save_data('history')`。115 调用不照它 |
| `Doc911Subscribe`「911文档订阅添加」 | `plugins.v2/doc911subscribe/__init__.py` | 单文件每日任务。它的解析在「本月更新【动漫】」处停，不追番 |

`libraryscraper_enhanced/` 在目录里，但没有写进 `package.v2.json`，市场不会出现。Anime115 必须进索引。

这个仓库里**没有** dmhy、磁力离线、整包摊平。`P115StrgmSub` 的追更是：读 MoviePilot 订阅，用 PanSou / HDHive / TG 找 **115 分享链接**，再用自己的 cookie 建 `P115Client` 去转存。表单里有「115 Cookie」。另有 `wheels/` 里的 `p115client` 和 405 补丁。这些都不进 Anime115。

同账号的 [mp-115](https://github.com/wyh137815840/mp-115)「115 AV刮削」只作 Emby/strm 的旁证，不再当外壳。

Grok Bot 的连载补磁力和整包摊平**源码不在本仓库，也不在 bot 记忆里**。能对上的只有：

- 日记一行（2026-10-21）：连载番每日补磁力曾走 p115，例：「冷淡的佐藤同学只对我撒娇」E03 离线并整理；另外两部当天没有新资源。插件不再走那条客户端。
- 记忆里还有主号、CD2 前缀 `/115open/`、以及 keepalive 同步的 `115-cookies.txt`。这些只说明旧 bot 怎么登录。插件不读 cookie、不核对用户号，只用 MoviePilot 里已经登录的 115。Emby 动漫库名「动漫」、条目 3、路径 `F:\动漫`、类型 tvshows。AV 插件的播放地址结论仍然有效：Unraid 上的 Emby 不能用 `127.0.0.1`，应是 `http://192.168.1.10:8787/redirect?pickcode={pickcode}`，刷新走 MoviePilot 里已存的 Emby。

所以下面的搜索词表、字幕组偏好、摊平分类器细节是**按结果反推的默认规则**，不是从旧脚本逐行搬来的。实现时不要假装恢复了一份不存在的 bot 源码。

## 要替换的三件事

1. **每天连载补磁力。** 从动漫花园拿连载磁力，用 MoviePilot 已登录的 115 会话提交离线，落到 `/离线下载/动漫/YYYYMMDD`（口头上的 `/115open/...` 只是旧 CD2 前缀）。不另建客户端，不走 CloudDrive2。
2. **离线完成后的 Emby 友好布局。** 完成的任务从日期收件箱整理进动漫库路径。认不准的留在收件箱。
3. **大约每 2 小时的「动漫整包摊平」。** 先干跑。只有存在 `ok_eligible_non_collision` 的移动才落盘。碰撞和认不准的重命名一律不动。干跑结果全是碰撞/不确定时，整次什么都不做。

## 照抄什么，改掉什么

照抄 `P115StrgmSub` 的外壳：

- 仓库能被 MoviePilot「插件源」直接添加。索引键是类名，带 `release: true`、`labels`、`level`、`history`。图标用官方仓库上的完整 URL，不用本地 png 名。不写 `system_version`（对方索引也没写）。README 写明只给 V2，因为调用的是 V2 的 `U115Pan`。
- 类字段与对方一致：`plugin_name`、`plugin_desc`、`plugin_icon`、`plugin_version`（与索引相同）、`plugin_author`、`author_url`、`plugin_config_prefix`、`plugin_order = 20`、`auth_level = 1`。
- 表单放在 `ui/config.py` 的 `UIConfig.get_form` / `get_page`。组件用 `VForm`、`VRow`、`VCol`、`VSwitch`、`VCronField`、`VAlert`、`VExpansionPanels`、`VTextarea`。顶栏是「启用 / 发送通知 / 立即运行 / 执行周期」。
- 周期任务只通过 `get_service` 注册。`CronTrigger.from_crontab`。表达式先用 `settings.TZ` 校验，不合法就打日志并回退该任务的默认式，不让整个插件起不来。
- 「立即运行」照对方：`onlyonce` 时用 `BackgroundScheduler(timezone=settings.TZ)` 加一个 3 秒后的 `date` 任务，然后把开关写回 false。周期任务不用私有调度器（`Doc911Subscribe` 那种自建 `BackgroundScheduler` 不抄）。
- 详情页读 `get_data('history')`。每次运行往列表头插一条摘要，最多留 50 条。通知用 `post_message(mtype=NotificationType.Plugin)`，并且只在「发送通知」打开时发。
- 长循环里看 `global_vars.is_system_stopped`，系统停止就跳出。
- 业务放 `handlers/`，115 访问放 `clients/` 里的一个类。解析和分类函数不 import `app`，单测只打这些函数。

改掉，不从那个仓库带过来：

- 不建 `P115Client`，不放 `wheels/`、`requirements.txt`、`patches/`，表单里没有「115 Cookie」。
- 不转存 115 分享链接。每日任务提交的是 dmhy 磁力。
- 不接 PanSou、HDHive、Nullbr、TG，不改 MoviePilot 的订阅站点、规则组和洗版评分。
- 追更名单第一版用表单里的多行文本，不扫 `SubscribeOper.list()`。对方的追更对象是 MP 订阅；我们的搜索词、别名和字幕组它的订阅行里没有。是否改成读订阅，见决定项 14。
- 对方只有一个 cron，空了也会用 `0 18-23 * * *` 顶上。我们三个任务各一个 `VCronField`。某一栏留空就**不注册**那一个服务，避免摊平或整理被默认式打开。
- `dry_run` 和 `flatten_apply` 是我们多出来的开关，放在顶栏。对方没有干跑。

## 插件身份与仓库布局（批准后才建）

```text
package.v2.json                 # 只登记 Anime115
package.json                    # {}
README.md
plugins.v2/anime115/
    __init__.py                 # 类 Anime115：配置、服务、立即运行、历史
    ui/config.py                # UIConfig.get_form / get_page
    clients/u115.py             # U115Session，只包已登录的 U115Pan / StorageChain
    handlers/dmhy.py            # 搜索、打分、集数
    handlers/offline.py         # 提交与轮询
    handlers/organize.py        # 完成后的库内移动
    handlers/flatten.py         # 整包摊平与安全门
    utils/names.py              # 标题归一、季/集、Windows 非法字符
tests/
    test_dmhy_score.py
    test_flatten_gate.py
```

索引条目：

| 字段 | 值 |
| --- | --- |
| 键 / 类名 | `Anime115` |
| 目录 | `plugins.v2/anime115` |
| `name` | 动漫 115 |
| `description` | 从动漫花园补连载磁力，用 MoviePilot 已登录的 115 离线，并做整包摊平 |
| `labels` | `动漫,115网盘` |
| `version` | `1.0.0`，与 `plugin_version` 相同 |
| `release` | `true` |
| `icon` | `https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/cloud.png` |
| `author` | `wyh137815840` |
| `level` | `1` |
| `history` | `v1.0.0` 一行说明 |

类上再写 `author_url = "https://github.com/wyh137815840"`、`plugin_config_prefix = "anime115_"`。不建 V3 目录。

## 模块边界

`__init__.py` 只做配置、锁、`get_service`、立即运行、通知和 `history`。handlers 接收一个 `U115Session`。测试用假会话，方法为：`check_login`、`ensure_dir`、`list_dir`、`move`、`delete`、`add_offline`、`list_offline`、`quota`。

| 模块 | 输入 | 输出 | 不做的事 |
| --- | --- | --- | --- |
| `handlers/dmhy.py` | 追更列表、RSS/HTML | 候选磁力：标题、发布时间、体积、infohash、集数、分数、丢弃原因 | 不访问 115 |
| `handlers/offline.py` | 选中的磁力、当天目录 | 提交记录、任务状态 | 不改库目录 |
| `handlers/organize.py` | 已完成任务、库根 | 移动计划：`move` / `leave`，带原因 | 不提交磁力，不摊平别的目录 |
| `handlers/flatten.py` | 扫描根、目录树 | 每条提案一个分类 | 不提交磁力，不按 TMDB 改名 |
| `utils/names.py` | 原始标题、文件名 | 归一标题、季、集、安全文件名 | 不发网络请求 |
| `clients/u115.py` | 上面的方法调用 | 转成 `StorageChain` / `U115Pan` | 不创建 `P115Client`，不读 cookie |

## 对应到 MoviePilot V2 的哪些钩子

基类是 `app.plugins._PluginBase`，注册方式和 `P115StrgmSub.get_service` 相同。

| 钩子 | 用法 |
| --- | --- |
| `init_plugin` | 读配置，校验三个 cron。`onlyonce` 为真时用 `BackgroundScheduler` 约 3 秒后跑所选任务，然后写回 false。 |
| `get_state` | 返回 `_enabled`。某个 cron 为空只是不注册那一个服务，不把整个插件算成关闭。 |
| `get_form` | 交给 `UIConfig.get_form`。顶栏：启用、发送通知、只预览、摊平允许落盘、立即运行，外加三个 `VCronField`。没有 cookie 输入框。 |
| `get_page` | 最近三次任务的摘要：提交了几条、留下几条、摊平提案各类多少、实际移动多少。 |
| `get_service` | `_enabled` 为假时返回 `[]`。否则按三个 cron 各追加一条 `{id, name, trigger, func, kwargs}`。某一栏为空或校验失败则跳过该条，不套用对方那个「空了就用 18–23 点」的回退。 |
| `get_api` | 与对方一样不写 `auth`（宿主默认 apikey）：`GET /run?job=daily\|organize\|flatten`，`GET /status`。`job` 缺省时跑每日补磁力。 |
| `get_command` | 第一版不做。需要时再加 `/anime115_daily` 之类，走 `EventType.PluginAction`。 |
| `stop_service` | 置取消事件。正在跑的循环在下一轮停。 |
| `post_message` | 有提交、有整理、115 未登录、离线接口失败、配额用尽时通知。干跑零结果不通知，避免每两小时刷屏。 |
| `save_data` | `history`（详情页，最多 50 条）、`seen`（infohash 与「剧+季+集」）、`tasks`（离线任务）。 |
| 锁 | 一把 `threading.Lock`。补磁力、整理、摊平互斥。第二次进来直接记「已有任务」。 |

详情页和接口都只返回截断后的结果（AV 插件是 200 条）。完整干跑文本写到插件数据目录的 `flatten-last.txt`，不进配置库。

Emby 刷新若开启，照 AV 插件：`ModuleManager` 里找 `EmbyModule`，按媒体库名字刷新。名字对不上就**不要**刷新全部媒体库（AV 插件会回退到全库刷新；动漫这边默认更保守，避免误刷 AV 库）。

## 配置项

默认值是建议。空着的项在你拍板前保持空白，插件拒绝在该功能上写 115。

| 键 | 默认建议 | 作用 |
| --- | --- | --- |
| `enabled` | 关 | 总开关 |
| `onlyonce` | 关 | 保存后跑一次，随即写回关 |
| `dry_run` | **开** | 总干跑。开着时三个任务都不提交磁力、不移动文件 |
| `storage` | `u115` | MoviePilot 里已经登录的 115 存储 ID。`U115Pan.check()` 失败就整次停止 |
| `daily_cron` | `15 10 * * *` | 每天补磁力。cron 用 MoviePilot 宿主机时区 |
| `organize_cron` | `*/30 * * * *` | 看离线是否下完并整理 |
| `flatten_cron` | `15 */2 * * *` | 整包摊平，错开整点 |
| `offline_root` | `/离线下载/动漫` | 115 真实路径。日期子目录由插件建：`/离线下载/动漫/YYYYMMDD` |
| `cd2_prefix` | `/115open` | 只用于把你口头上的 CD2 路径换算成 115 路径，不拿去调用 CD2 |
| `library_root` | 空，待你填 | 整理目标，例如 `/动漫` |
| `watchlist` | 空 | 多行追更，格式见下 |
| `fansub_prefer` | 空 | 全局字幕组加分，空=不偏向 |
| `resolution` | `1080` | `1080` 或 `720`。4K 默认不收 |
| `require_sub` | 开 | 丢掉生肉 / RAW |
| `allow_batch` | 关 | 合集、全集、Fin、BDrip 整季默认不收 |
| `lookback_hours` | `36` | 只考虑这段时间内新发布的资源 |
| `min_episode_mb` | `80` | 更小的当预告或垃圾 |
| `max_episode_gb` | `8` | 单集更大的当整包误标，不自动提交 |
| `offline_gap_seconds` | `20` | 两次离线提交的间隔 |
| `max_submit_per_run` | `10` | 单次最多提交几条磁力 |
| `rename_episodes` | **关** | 关：只搬目录，保留发布文件名。开：仅对集数确定的文件改成 `剧名 - SxxExx.ext` |
| `flatten_roots` | 空，待你填 | 摊平扫描根，每行一个。空则本任务拒绝运行 |
| `flatten_apply` | 关 | 即便总干跑关掉，摊平仍要这个开关才真正移动 |
| `flatten_max_moves` | `30` | 单次最多执行的安全移动 |
| `move_gap_seconds` | `2` | 两次 115 移动间隔 |
| `write_strm` | **关** | 第一版默认不写 strm |
| `strm_root` | 空 | Emby 正在扫的本地或挂载路径 |
| `play_base` | `http://192.168.1.10:8787/redirect?pickcode={pickcode}` | 与 AV 插件同一跳转服务。不要写 `127.0.0.1` |
| `refresh_emby` | 关 | 仅在有实际写入且非干跑时刷新 |
| `emby_library` | `动漫` | 只刷新这个名字 |

追更列表一行一部：

```text
冷淡的佐藤同学只对我撒娇 | 佐藤同学 | group=喵萌奶茶屋
另一部剧名
```

竖线前是搜索词，后面是别名，`group=` 是这部剧的字幕组优先（可多个，逗号分隔）。`#` 开头是注释。没有这份列表，每日任务直接返回「未配置追更」，不搜全站。

## 时间表

三个 `get_service` 条目，`id` 固定为 `Anime115`、`Anime115.Organize`、`Anime115.Flatten`（主任务的 id 用类名，与 `P115StrgmSub` 的 `"id": "P115StrgmSub"` 相同）。触发器是 `CronTrigger.from_crontab`。校验时带 `pytz.timezone(settings.TZ)`。

| 任务 | 建议 cron | 做什么 |
| --- | --- | --- |
| 每日补磁力 | `15 10 * * *` | 搜 dmhy、打分、去重、提交离线到当天目录 |
| 整理 | `*/30 * * * *` | 只处理本插件提交过的任务；下完了就尝试入库 |
| 摊平 | `15 */2 * * *` | 干跑分类；满足安全门才移动 |

`15` 分是为了避开整点上的其它定时任务。时区用 MoviePilot 的 `settings.TZ`，不在代码里写死 `Asia/Shanghai`（`Doc911Subscribe` 写死了上海，那一处不抄）。插件不参与 cookie keepalive。

手动口与定时走同一函数。`dry_run` 或接口 `apply=false` 时只记结果。

## 115 会话

会话只有 MoviePilot 已经登录的 **`U115Pan`**（存储 ID 默认 `u115`，类是 `WeakSingleton`，token 在 MoviePilot 自己的存储配置里刷新）。插件不创建 `P115Client`，不读 `115-cookies.txt`，不做 Open / cookie 模式开关，不核对 `101481377`。任务开始时 `U115Pan.check()` 必须为真；否则通知「115 未登录」并停止，不扫码。

**不使用 CloudDrive2，也不使用本机下载器。** 不调用 CD2。`DownloaderHelper` 以及 qBittorrent / Transmission / rTorrent 是本机做种客户端，不是 115 离线。`StorageChain.download_file` 是把云盘文件拉到本地磁盘，不用它提交磁力。

口头路径 `/115open/离线下载/动漫/YYYYMMDD` 里，`/115open` 只是旧挂载前缀。`U115Pan` 使用的路径是 `/离线下载/动漫/YYYYMMDD`。配置里若带上这个前缀，插件只做字符串剥离。网盘里的真实目录若仍含 `115open` 这段，改 `offline_root`，不改调用方式。

对照的是 MoviePilot `v2` 分支当前源码。`StorageChain` 没有离线方法，也没有 `move`。`U115Pan` 有列目录、建目录和 `move`。离线任务没有公开包装方法，所以提交和查进度走这个对象已有的 `access_token` 与 `_request_api`（限流和 token 刷新都在里面）。插件不复制一套登录。

| 步骤 | 调用 |
| --- | --- |
| 是否已登录 | `U115Pan.check()` |
| 按路径取文件或目录 | `StorageChain.get_file_item(storage, Path)` |
| 列一层 | `StorageChain.list_files(fileitem, recursion=False)` |
| 确保当天目录、季目录存在 | `StorageChain.get_folder(storage, Path)`，没有则递归创建。返回的 `FileItem.fileid` 就是离线保存目录 id |
| 只改名、不搬家 | `StorageChain.rename_file(fileitem, name)`。仅 `rename_episodes` 打开且集数确定时使用 |
| 搬家（整理和摊平） | `U115Pan.move(fileitem, 目标目录 Path, new_name)`。`StorageChain` 没有对应方法。不改名时 `new_name` 传当前文件名。宿主的 `move` 在移动成功后还会 `rename`；同名 rename 视为成功 |
| 删除空包装目录 | `StorageChain.delete_file`。先 `list_files` 确认里面没有视频和字幕。不用 `delete_media_file`，它会按媒体库规则继续删上级 |
| 提交一条磁力 | `U115Pan._request_api("POST", "/open/offline/add_task_urls", data={"urls": magnet, "wp_path_id": 当天目录 fileid})`。一次一条 |
| 查离线进度 | `U115Pan._request_api("GET", "/open/offline/get_task_list", params={"page": n})`，用提交时记下的 infohash 对上那一条 |
| 提交前看配额 | `U115Pan._request_api("GET", "/open/offline/get_quota_info")`。剩余次数不够，或添加接口返回配额错误：本轮停止并通知 |

完成态以 `get_task_list` 里该 infohash 的完成标志为准，不把「目录里已经出现文件」当成下完。字段名按 115 Open 这条接口的返回读取，不另猜一套状态机。添加接口若把文件放进「云下载」而不是 `wp_path_id`，把这次记为失败并通知，不换 cookie 客户端、不换 CD2。

插件不再带 `requirements.txt`。用宿主里已有的 `U115Pan`，不声明 `p115client`。

限速：`_request_api` 自带约 3 QPS 和 429 冷却。插件仍按 `offline_gap_seconds`（默认 20 秒）隔开两次添加，按 `move_gap_seconds`（默认 2 秒）隔开两次移动。429 或风控：本轮停止，下一轮 cron 再继续，不在同一轮里猛重试。整理不列举整个动漫库根，只看本插件记下的任务目录和库根下要进入的那一层。

## 动漫花园：搜索和过滤

优先 RSS，解析失败再退回列表页 HTML。不抓详情页。

- 分类固定动画：`sort_id=2`。
- RSS：`https://share.dmhy.org/topics/rss/sort_id/2/rss.xml?keyword=`
- 列表：`https://share.dmhy.org/topics/list?keyword=&sort_id=2&order=date-desc`
- 每个追更词单独查。别名不再各查一遍，只用于标题命中。
- 请求间隔至少 2 秒。非 200、验证页、空文档：这次每日任务失败并通知，不用旧缓存冒充新结果。

从条目里取：标题、发布时间、体积、磁力、infohash（40 位十六进制，小写）。没有 infohash 的丢掉。

集数按这个顺序认，认出两个不同集数就当不确定，不提交：

1. `S(\d+)E(\d+)`
2. `第(\d+)[话話集]`
3. 标题后的 `- 03` / `- 3`（v2 这种版本后缀不当成另一集）
4. 季：有 `Sxx` 用它；标题含「第二季 / 2nd season」等才标第 2 季；否则第 1 季。OVA、SP、特典、剧场版标成不确定，不自动提交、不自动整理。

打分（同一「剧 + 季 + 集」只留最高分）：

| 条件 | 分数 |
| --- | --- |
| 命中该剧 `group=` 或全局 `fansub_prefer` | +100 |
| 分辨率符合 `resolution` | +40 |
| 简体 / CHS / GB / 简中 | +30 |
| 简繁 | +10 |
| 繁体且没有简体候选 | +5 |
| 生肉 / RAW，且 `require_sub` | 丢弃 |
| 4K / 2160p，而 `resolution` 不是 4K | 丢弃 |
| 合集 / 全集 / batch / Fin / BDrip 整季，且 `allow_batch` 关 | 丢弃 |
| 体积 &lt; `min_episode_mb` 或 &gt; `max_episode_gb` | 丢弃 |
| 发布时间早于 `lookback_hours` | 丢弃 |
| 标题对不上搜索词和任一别名 | 丢弃 |

平分时取得发布时间更新的，再比体积更大的。

去重键存在 `seen`：

- infohash 已提交过：跳过。
- 同一季集已有未失败的任务：跳过，不换字幕组重复下。
- 任务失败或超时（默认 3 天仍未完成）：允许下一轮换下一条。

每日任务的干跑报告要写出：每部剧选中了哪条、丢了哪些以及原因。这样可以和 bot 以前的「今天没有新资源」对上。

## 离线完成后的整理

整理任务只认 `tasks` 里状态变为完成的记录。完成判定来自上一节的 `get_task_list`，不靠「目录里出现了文件」猜测，避免下到一半就搬走。目标目录先 `StorageChain.get_folder`，再 `U115Pan.move`。

目标形状（`rename_episodes` 关闭时）：

```text
{library_root}/{剧名}/Season {季:02d}/{原始文件名}
```

同目录的字幕、字体以外的 nfo 不主动生成。字幕 sidecar（`.ass` `.srt` `.ssa` `.sup`，文件名主干与视频相同）跟着视频走。

| 情况 | 动作 |
| --- | --- |
| 剧名、季、集都确定，目标没有同名文件 | `move` |
| 目标已有同名文件 | `leave`，原因 `collision` |
| 剧名对不上追更列表，或集数不确定，或一个任务里有多集以上 | `leave`，原因 `uncertain` |
| 任务目录就是 `offline_root` 或 `library_root` | 拒绝，当配置错误 |

`rename_episodes` 打开时，只重命名那些集数确定的视频，目标名 `剧名 - S01E03.ext`。不确定的仍然留在收件箱，不编一个集数。

整理**不会**删除非空目录，也**不会**动追更列表之外的剧。

strm（默认关）：若以后打开，沿用 AV 插件的写法，但多一条记忆里的规则：目标季目录里已经有同集 mkv 就不要再叠一层 strm。播放地址用 `192.168.1.10:8787`，strm 目录必须是 Emby 实际在扫的路径。MoviePilot 进程写不到 Unraid 路径时，这个开关保持关，只做 115 上的目录整理。

## 整包摊平的安全门

摊平解决的是离线整包多出来的包装目录，例如：

```text
剧名/[字幕组] 剧名/剧名/Season 1/E01.mkv
        → 剧名/Season 01/E01.mkv
```

它不是刮削重命名。文件基名默认保持不变。只有「整层目录只有一个子目录、名字归一后相同」才允许整目录上提；否则按文件上提到已经存在的 `Season xx`，前提是目标名空闲。

扫描只从 `flatten_roots` 往下，深度上限 6。根目录本身、`offline_root`、`library_root` 若被误填成 `/` 或空，任务直接失败。建议根就是库根和收件箱根，由你确认后再填。

每条提案四选一：

| 分类 | 含义 | 能否执行 |
| --- | --- | --- |
| `ok_eligible_non_collision` | 视频或 sidecar；目标路径不存在；不跨剧；不改基名，或只是同名包装目录上提；源不在保护根上 | 可以 |
| `collision` | 目标已存在（含大小写、全角 `？` 洗成合法名之后撞车） | 否 |
| `uncertain` | 多集混在一个无法对应的文件名里、要改成另一个剧名、季数冲突、一个目录里有两部以上剧、或任何需要「猜集数才能改名」的提案 | 否 |
| `skip` | 样本、`sample`、小于 `min_episode_mb`、非视频且不是 sidecar | 否 |

执行规则：

1. 每次都先干跑，并把四类数量写进 `last_flatten`。
2. **安全移动条数为 0：什么都不移动。** 这就是现在干跑经常全是碰撞/不确定时的行为。
3. 安全移动条数 &gt; 0，且总干跑关闭、`flatten_apply` 打开：只执行 `ok_eligible_non_collision`，最多 `flatten_max_moves` 条。碰撞和不确定留在原处。
4. 不因为同一文件夹里有一条不确定，就连累同文件夹里已经判定安全的上提。若你希望改成「一个目录里有任何不确定则该目录整包不动」，见决定项 7。
5. 源目录要等里面没有视频和字幕了才删，并且只删空目录。失败的移动不删。
6. 不碰 AV 路径。根不在配置里就不会扫到。

## 从 bot 迁过来

插件第一周保持 `dry_run` 开、`flatten_apply` 关。每日报告能对上「这部今天有 E0x / 那部没有」之后，再关干跑。

| 现在 | 插件上线后 |
| --- | --- |
| Bot 搜 dmhy，再用另一套客户端离线到 `/115open/离线下载/动漫/日期` | 同一收件箱（去掉挂载前缀），由插件经 MoviePilot 的 `U115Pan` 提交。确认第一天干跑正确之前，bot 任务先停，避免双倍离线 |
| Bot 每两小时摊平，全碰撞就不动 | 同一安全门，开关默认关 |
| cookie keepalive 写 `115-cookies.txt` | 与本插件无关。插件不读这个文件 |
| CD2 做 115 文件操作 | 插件不调用 CD2 |
| Emby 库 `F:\动漫`、strm 与本地 mkv 混放 | 第一版不写 strm，避免叠在已有 mkv 上。库内整理只发生在 115 |
| 追更名单在 bot 里 | 本仓库没有这份名单。需要你贴进 `watchlist` |

Bot 例行任务的关闭不在本仓库里做。计划批准并插件干跑通过之后，再在 bot 侧停掉对应定时。

## 批准之后的实现顺序

每一步都能单独停，且默认干跑。

1. 按对方仓库的索引和目录把壳搭起来：`package.v2.json`、`ui/config.py`、三个 `get_service` 空任务、`history` 详情页。不接网。
2. dmhy 解析和打分，加单测。每日任务只打印将要提交的磁力。
3. 用已登录的 `U115Pan` 建当天目录、提交离线、轮询。不引入第二套登录。干跑仍跳过提交。
4. 整理：只移动确定项。
5. 摊平分类器和安全门单测，然后接上只读扫描。`flatten_apply` 仍默认关。
6. 你明确要求后再做 strm 和 Emby 刷新。

单测用 `python -m unittest`，只覆盖纯函数：打分、集数、去重、摊平分类、路径里的 `/115open` 前缀剥离、`check()` 失败时拒绝提交。不在 CI 里打 dmhy 或 115。

中文 docstring 按 MoviePilot 插件审查习惯加在公有类和函数上。日志用 MoviePilot 的 `logger`。

## 风险

- **离线没有公开包装方法。** `v2` 的 `StorageChain` 不能提交磁力。插件只通过已登录的 `U115Pan._request_api` 打 `/open/offline/add_task_urls` 和 `get_task_list`。若安装的宿主把 `_request_api` 改了签名，实现时对着那台宿主改调用，仍然不另建客户端。
- **添加接口可能忽略 `wp_path_id`，文件进「云下载」。** 这种情况记失败并通知，不改走 cookie 或 CD2。
- **`U115Pan.move` 移动后会再 rename。** 不改名的整理和摊平传入当前文件名，同名 rename 当成功。rename 失败则这次移动记失败，不删源目录。
- **dmhy 页面改版或拦机房 IP。** RSS 优先；失败就停，不提交空结果。MoviePilot 所在的 Windows 要能访问 `share.dmhy.org`。
- **简体标题对不上繁体发布名。** 靠别名行。别名没填就会漏，这是可接受的漏，不是错下。
- **整包被当成单集。** 体积上限和合集关键词负责挡住。挡不住的留在收件箱（集数不确定）。
- **摊平误伤已经排好的季目录。** 保护根、不改基名、目标已存在即碰撞。全碰撞则零移动。
- **115 风控。** 间隔、单次上限。`U115Pan` 自己的 429 冷却触发后，本轮停止。
- **和 bot 双开。** 迁移动作里写明先停 bot。
- **strm 盖住已有 mkv，或 strm 写了 `127.0.0.1`。** 第一版不做 strm；以后做也跳过已有 mkv，并且用 `192.168.1.10`。
- **换到 V3 宿主时这个插件对不上 `U115Pan`。** 索引照对方，不写 `system_version`。README 写明只在 V2 上用。不在这次做 V3。
- **配置进了 git。** 插件配置里没有 cookie，也没有 token。登录只留在 MoviePilot 自己的存储配置里。

## 这次明确不做

- 不接 CloudDrive2，也不把 CD2 405 当成回退条件。
- 不做 JavBus、番号、AV 库。
- 不自动发现新番（没有追更列表就不搜）。
- 不按 TMDB/TVDB 刮 nfo。那是 Emby 或以后的开关。
- 不引入 `P115Client`、cookie 文件、`wheels/`、405 补丁或 Open 模式开关，也不在插件里扫码或刷新 token。登录完全沿用 MoviePilot 已有的 `U115Pan`。
- 不转存 115 分享链接，不接管 MoviePilot 订阅站点和规则组。
- 不改 bot 进程。本计划只规定何时可以停它。

## 需要你拍板的项

下面每一条都有建议。回复时按编号改即可；没改的视为接受建议。

1. **追更名单**谁提供？建议你贴第一批剧名（含别名、字幕组）。仓库里没有 bot 的名单，插件不会自带。
2. **字幕组策略**用「有 `group=` 就大幅加分，没有就收最高分的 1080 简体」，还是必须命中字幕组才许提交？建议加分，不强制。
3. **115 收件箱真实路径**是不是 `/离线下载/动漫/YYYYMMDD`（`/115open` 只是 CD2 前缀）？建议是。
4. **库根路径**用哪条？建议你给 115 上的动漫库根（记忆里的 cid `3542094558873519866` 只是 2026-10-19 的快照，不写进代码）。整理形态建议 `{库根}/{剧名}/Season 01/原始文件名`。
5. **第一版要不要写 strm？** 建议不要。Emby 路径 `F:\动漫` 和 Unraid 上的库路径还没对上，且库里已经有 mkv。只做 115 目录整理。
6. **摊平扫描根**有哪些？建议先只扫收件箱 `/离线下载/动漫`，不要扫整个库，直到干跑稳定。确认「摊平 = 去掉重复包装目录、不改文件名」。
7. **安全门粒度**：建议全局只要有至少一条 `ok_eligible_non_collision` 就只执行那些安全条；碰撞/不确定跳过。备选是「同一目录里出现任何不确定，则该目录一条都不动」。
8. **已按更正关掉。** 不读 cookie 文件，不强制用户号 `101481377`。还要确认的只是：MoviePilot 里这个 115 的存储 ID 是不是 `u115`。建议是。对方插件用的是表单里的 cookie，那一栏不抄。
9. **每日时刻和时区。** 建议宿主机本地 `15 10 * * *`。若 Windows 不是中国时区，请给出要换成的 cron。
10. **整理之后刷不刷 Emby「动漫」库？** 建议第一版不刷。strm 没写之前，115 上的移动 Emby 不一定看得见。
11. **bot 何时停？** 建议插件每日干跑对上一天之后立刻停 bot 的补磁力和摊平，再把插件 `dry_run` 关掉。不要并行写 115。
12. **分辨率和合集。** 建议只收 1080、要字幕、不收合集、回溯 36 小时、单集 80MB–8GB。
13. **已按更正关掉。** 插件使用 MoviePilot 当前的 115 登录，不准备第二套 Open 或 cookie 登录。
14. **追更名单从哪来？** 建议仍用表单多行文本。对方是扫 MoviePilot 订阅再转存分享链接；订阅行里没有 dmhy 搜索词和字幕组。若要改成「只处理类型为电视剧、分类含动漫的 MP 订阅」，请明确说。
15. **作者和图标。** 建议 `author` 用 `wyh137815840`，图标用对方同款官方 `cloud.png` URL。要换图标再说。
16. **空 cron 怎么办？** 建议该任务不注册。对方会在 cron 为空时改跑 `0 18-23 * * *`，那个回退不抄。

批准之后的实现 PR 只做第 1–5 步里你点名的范围，并继续让 `dry_run` 和 `flatten_apply` 默认关闭。目录按上面的 `ui/`、`handlers/`、`clients/` 来，不按之前的平铺文件名。
