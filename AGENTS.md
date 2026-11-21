# 协作规则（给 AI 助手看，也给人看）

这是一个长期演进的项目，改动必须可回退。任何一次会话结束前，代码要处在"能存下来"的状态。

## 每次改完代码，必须存版本

```bash
python radar.py check                      # 语法检查，几十秒
python radar.py check --full               # 连单元测试一起跑，约 1 分钟
python radar.py save "一句话说清改了什么"     # 存
```

`save` 的说明写"做了什么"，不写"修改了文件"。好例子：`修好图注被截在90字`、`社会新闻不再入库`。
坏例子：`update`、`修复bug`、`改了一些东西`。

一次会话里如果有多个独立改动，分成多次 save，不要攒成一大坨 —— 将来出问题时才能只退一半。

**跑测试不用再手动关垫片**（2026-11-18 起）：`app/__init__.py` 导入时会调
`app/native_delete.restore_native_deletes()`，把垫片换掉的 `os.remove` / `shutil.rmtree`
/ `Path.unlink` 装回原始版本，测试和服务进程从此都是真删除。带上
`CODEBUDDY_SAFE_DELETE_ENABLED=0` 仍然更省事，但忘了也不会再往回收站堆东西。

## 动手前的规矩

- 先 `python radar.py status`。工作区有别人的未保存改动时，先问清楚再动。
- 要动超过 3 个文件、或要改数据库结构、或要删数据，先 `python radar.py save "改之前"` 留个点。
- **绝不要**手工 `git reset --hard` / `git checkout --` / `git clean`。回滚统一走 `radar.py undo` 或 `radar.py back`，它们会先把当前状态留底，手敲 git 不会。

## data/ 目录是用户的资料，不是缓存

`data/` 里是 3.6G 的离线文档、图片、数据库和日志，**不在版本库里**，回滚动不到它，也意味着改坏了没得退。
所以：

- 不要为了"清理"批量删 `data/` 下的东西。要清理先输出清单让人确认。
- 不要往 `data/` 里塞临时文件。临时文件放 `.tmp/`（已被忽略）。
- 数据库结构改动要同步把 `app/db.py` 的 `SCHEMA_VERSION` 加一。

## 资料库有四道闸门，别搞混

| 闸门 | 代码 | 判什么 | 误伤代价 |
|---|---|---|---|
| 入库前准入 | `relevance.assess_candidate` | 这是不是科研内容 | 高：连索引都不留 |
| 归档后引流识别 | `relevance.detect_marketing` | 是不是广告软文 | 高：整篇不进库 |
| 用户口味 | `services/filters.py` 的 `filter_rules` | 你想不想看这个栏目 | 中：可从「含已删除」找回 |
| 手动删除 | `items.excluded_at` | 你自己点的 | 低：随时恢复 |

前两道是"全站统一"的判断，后两道是"只对你"的判断。**别把个人口味写进 `relevance.py`**，
也别把"是不是科研"塞进 `filter_rules`——两者的测试要求完全不同。

### 自动过滤的铁律：先给出拦截样本，再谈默认隐藏

用户的原话是"前提是要确保低相关分辨得足够精准，否则可能会错过真正有用的东西"。
所以任何新的自动过滤上线前，必须按这个顺序做：

1. 拿正式库跑一遍只读体检，把**所有**会被拦的条目连标题、公众号、理由打出来；
2. 逐条人工核对，只要有一条真内容被拦，改判据而不是改阈值；
3. 把样本写成 `tests/` 里的正例 + 反例，反例（不该拦的）数量必须 ≥ 正例。

`status='filtered'` 是精读打分低于阈值的**中间档**，实测这一档里约有一半是真文章
（核钟、MPC、混合键合都掉在这一档）。所以它**默认照旧显示**，只在用户勾
「隐藏低相关」时让位（`/api/items?hide_low=true`）。别把它改回默认隐藏。

## 公众号面板：读的是本机微信，不是网络

`services/wechat_public.py` 直接读 `Msg/PublicMsg.db`（只读）。两个坑踩过一次就别再踩：

1. **按 `StrTalker` 查是全表扫描。** 表里 36 万行、每页都要 SQLCipher 解密，实测一次 20 秒以上。
   按 `TalkerId` 查走 `MsgTalkerIdTypeSeqIndex`，1~3 毫秒；`TalkerId` 就是 `PublicNameToID`
   的 rowid，所以先映射再查。别图省事用号名直接过滤。
2. **封面图有防盗链。** `mmbiz.qpic.cn` 被浏览器直连（带 `Referer: 127.0.0.1`）会换成
   「此图片来自微信公众平台，未经允许不可引用」的占位图，看着像图全挂了。必须走服务端
   `/api/wechat/cover` 代理，顺带缓存进 `blobs/covers/`。

面板的「排除」不是删：它写 `wechat_article_marks`（这张卡变灰）**并且**按原因在该号名下
建一条 `filter_rules`。所以入库闸门只有 `filter_rules` 这一处，别再给面板单独加一套过滤逻辑。

## 删除必须直删，别走回收站

这台机器给 `python.exe` / `bash.exe` 挂了安全删除垫片（`sitecustomize` + `safe-bin/rm`），
`shutil.rmtree` 和 `rm -rf` 都会被转成"移入回收站"：

| 方式 | 80 个小文件 | 回收站 |
| --- | --- | --- |
| `shutil.rmtree` 默认 | 1.11 秒 | +1 条 |
| 直删（`purge` / `trash.py`） | 0.01 秒 | 0 条 |

一次全量测试能在回收站堆出上千条 `test.db-shm`，之后每次删除都更慢，
用户看到的就是"删个东西卡半天"。所以定两条规矩：

**一、我们造出来的东西，删就得真删。**

- 应用代码里删临时文件、缓存、旧安装包、滚动备份，一律用 `app/services/purge.py`
  （`purge.remove()` / `purge.unlink()`）。它直接调 `SHFileOperationW` 且不带
  `FOF_ALLOWUNDO`，不经过 Python 那层垫片。
- **进程启动时自动还原，不靠人记**（2026-11-18 起）：`app/__init__.py` 调
  `app/native_delete.restore_native_deletes()`，把垫片 `patch` 之前存下的 `_orig_*`
  装回去。这不是洁癖 —— 服务每关一次数据库连接，SQLite 删 `radar.db-wal` 就走垫片，
  几分钟能堆 51 条；用户第三次说"回收站又几千个项目"就是它。
- 命令行里批量删构建产物、临时目录、测试残留，走 `python tools/trash.py wipe <路径>`。
- **`wipe` 删的是你给的那个路径本身，不是"回收站里该路径的条目"。** 清条目用
  `purge --under <前缀>`。2026-11-18 把 `wipe E:/HermesResearchRadar/app` 当清条目用，
  删掉的是生产目录的 `app/`，api 与 worker 每 4 秒崩一次。现在 `wipe` 会拒绝任何受
  git 跟踪的路径（守卫在 `tools/trash.py::_git_tracked`，测试 `tests/test_trash_guard.py`），
  真要从仓库里删文件请用 `git rm`；`--force` 是明确的硬删。
- 跑测试带 `-t .`：`python -m unittest discover -s tests -t .`。少了它模块会以顶层名
  导入，`tests/__init__.py` 的 `purge` 加速层不生效（不会脏回收站，但删临时库要慢十倍）。
- 收尾跑一次 `python radar.py tidy`：清 `.tmp`，再把回收站里属于本项目的条目直删。
- 用户自己删的东西（桌面文件、录屏、网盘资料）不在此列，**不要**替他清。

**二、不是我们造的东西，删之前先留底。**

- `data/documents`、`data/blobs`、`data/radar.db` 是几个月的积累，删任何一条都要先确认。
- 数据库每天自动快照到 `data/backups/radar-YYYYMMDD.db`，保留 7 份（`services/backup.py`）。
  手动补一份：`POST /api/backup/run`，或设置页的"立即备份"。
- 要动 `data/` 下的目录，先 `python radar.py pack` 打一份包，或者把目标目录复制到
  `data/backups/` 再动。
- 备份只滚自己的快照，永远不删用户文件；`purge.py` 也只给"我们自己造的"东西用。

## 改完之后服务要重启

`app/` 下任何文件改动后，Worker 进程不会自己重载：

```bash
python radar.py stop && python radar.py start
```

或者只杀 API/Worker 子进程让 `supervisor.pyw` 拉起来。改了 `web/` 下的静态文件不用重启 ——
`api.py` 会按文件 mtime 给 `?v=` 版本号，浏览器不会用旧缓存。

## 不要在代码里写死绝对路径

`E:\HermesResearchRadar` 这类路径出现在任何 `.py` / `.vbs` / `.json` 模板里都是 bug，
换台电脑就崩。一律用 `Path(__file__).resolve().parent` 推。

## 打包前

```bash
python radar.py pack
```

它会检查工作区是否干净；有未保存改动时会警告 —— 那种包里的代码和任何历史版本都对不上。

## 发版：用户说"导出为新版本"就跑这一条

```bash
python radar.py release "这一版改了什么"
```

它把测试、版本号、tag、安装包、GitHub Release、`version.json` 串成一条链，不用手工操作。
发之前必须已经在 `main` 上、工作区干净 —— 这两条它自己会拦。

几条不能碰的红线：

- **`version.json` 是客户端唯一认的版本清单**，只能由 `release.py` 写。手改它会让各机器
  的更新提示和实际安装包对不上（sha256 校验会直接拒装）。
- **tag 推上去就别删**。已经有人装了就会有人回滚到那个版本。真要作废，发一个新版本，
  用 `min_version` 强制升级，而不是删历史。
- **`VERSION` 是版本号的唯一来源**。`app/__init__.py` 已经从它读 `__version__` 了，
  不要再往代码里写死版本号。
- 发完必须验证一次：`curl -s https://raw.githubusercontent.com/forscienceshw/hermes-radar/main/version.json`
  能拿到新版本号，且 `assets[0].size` 和本地安装包一致。

安装包覆盖安装的三个必须：

1. 同目录顶替，绝不起第二个文件夹；
2. `data/`、`config/settings.json`、`config/secrets*.json`、`.venv` 一个都不许动；
3. 上一版有、这一版没有的文件要删干净（靠 `install.json` 旁边的 `.payload-manifest.json`），
   否则用户的目录里会越积越多没人用的死文件。

## 两个入口，两种打包

程序有两层入口，别搞混：

| 东西 | 源码 | 作用 | 什么时候要重打 |
|---|---|---|---|
| `Radar.exe` | `installer/launcher.py` | 拉起 supervisor、开浏览器、停服务、卸载 | 只改了启动逻辑 |
| `RadarSetup.exe` | `installer/setup_gui.py` | 安装向导：选目录、建 venv、装依赖、建快捷方式 | 改了安装流程或 `payload.zip` |

```bash
python tools/build_exe.py --launcher   # 只重打 Radar.exe
python tools/build_exe.py              # payload.zip + Radar.exe + RadarSetup.exe
```

依赖住在安装目录的 `.venv` 里，**改业务代码不需要重打 exe**，重启服务即可。
打 exe 需要 PyInstaller，用一个单独的构建环境，不要往运行环境里装：

```bash
python -m venv E:\HermesRadarBuild\.venv
E:\HermesRadarBuild\.venv\Scripts\python -m pip install pyinstaller
```

改完安装流程要真的装一遍验证（装到 `E:\RadarInstallTest` 这类临时目录，别往现有实例上装）：

```bash
RadarSetup.exe /S --dir=E:\RadarInstallTest --port=8899 --no-autostart --no-desktop --no-launch
```

## 单实例锁按安装目录区分

`supervisor.pyw` 的互斥量和 `app/worker.py` 的 `single_instance()` 都把安装/数据目录
算进名字里了。这样机器上同时存在两个实例（升级期间、开发实例 + 正式实例）时不会互相
挤掉。**新加单实例锁时照这个写**，别再退回全局名字。

## 4.x 订阅号通道：读内存，不读库（2026-11-18 起）

微信 4.x 把订阅号折叠进「订阅号消息」盒子，**正文不落 `biz_message_*.db`**：
本机 1630 个公众号会话里，只有 25 个服务号/工具号有正文表，追踪名单内 0 个，
最近 3 天 294 个科研号推过东西、落盘 0 篇。等库这条路是死的，别再往里加逻辑。

真正的推送流在 `Weixin.exe` 的 private 堆里：

- 盒子卡片 JSON：`{"AppMsgId":mid,"DateTime":ts,"uniqueId":"gh_x@mid","Title":"…"}`
- 一池带 `sn` 的文章 URL，按 `mid` 对齐（实测 2057 个 mid，卡片命中率 16/16）

`app/services/wechat4_memory.py` 只读 `OpenProcess + ReadProcessMemory`，
0.8 秒扫 404 MB，产出 80+ 条卡片 / 40+ 条在名单内 / 70% 带完整链接。

几条不能踩的规矩：

- 首次运行（`seen` 为空）会把盒子里的条目一次性补齐，这是有意的——11-17 之后的
  缺口就是靠它补的。之后按 `gh@mid` 增量。
- **名单外的卡片不写 `seen`**，这样以后勾进名单还能补回来；写进去就永远补不到了。
- 卡片没链接的留在 `pending_url`，下一轮重试。**不要用 `__biz + mid` 拼 URL**，
  没有 `sn` 微信只回「参数错误」，实测过。
- 内存里的卡片是「每个号一条最新」，不是全部历史。同批多条靠 URL 池兜底，
  别指望卡片给全。
- 体检里 `memory.available` 为真时微信项直接给绿灯，不要再教用户去点订阅号。

## 额度用尽 ≠ 这篇读坏了（2026-11-18 起）

`_check_daily_limit` 以前抛普通 `RuntimeError`，worker 按普通失败处理：重试四次后
`fail_item` 把条目写成 `error`。当天额度一满，剩下的真文章全被判死，第二天额度
恢复了也没人再读——一天之内 230 条真文章这样没了。

现在的分工是死的：

- 额度检查抛 `DailyLimitReached`，带 `retry_at`（本地次日零点 + 0~15 分钟错峰）。
- worker 单独 catch：`reschedule_job(..., reset_attempts=True)`，**不扣重试次数、
  不改条目状态、不记 item_error**。顺延是排队，不是失败。
- `ensure_daily_budget()` 放在 analyze 任务最前面，没额度就不抓正文、不分诊图片，
  整单直接顺延。别把它挪到 LLM 调用前——那就又白抓一遍正文了。
- 启动时 `requeue_limit_blocked()` 只捞 `error` 文本里提到保险丝/额度的条目，
  别的错因不动。捞回后状态回到 `discovered`，第二天额度归零时自然排队。
- 别用 `RuntimeError` 表达"今天排不上"。凡是"等一等就好"的情况，都要有自己的
  异常类型 + 顺延，不能走失败重试这条路。

## 精读并发：只放精读，不堵轮询（2026-11-18 起）

- 设置项 `analysis.concurrency`（面板「同时精读几篇」），默认 3，范围 1–8，
  改完 5 秒内生效，不需要重启。精读全是网络等待，串行就是自己把自己拖慢。
- **只限制 `analyze_item`**：`Worker._skip_kinds()` 在精读排满时返回
  `("analyze_item",)`，`claim_job(exclude_kinds=...)` 照常领轮询/归档。
  否则把并发设成 1，30 秒扫号就会被一篇长文堵到十几分钟。
- 总协程数 = `max(worker_concurrency, 精读上限 + 1)`，上限 12。`settings_watch`
  按需补协程（**只增不减**，不取消正在跑的任务），所以调高上限马上多出槽位。
- `budget_watch` 每 30 秒看一次额度：大于 0 就把因额度顺延、`run_at` 在未来的任务
  分批（40 个）放回 READY。否则用户把上限从 1200 调到 5000，队列还要干等到零点。
- 计数用 `self._analyze_running`。asyncio 单线程，增减之间没有 await，不用加锁。
- 并发不是越高越好：云模型端会限流，且每篇都要下载正文和图片，8 以上收益接近 0。

## 批量归档必须能停（2026-11-18 起）

- 停止走「标记 + 排空」，不 kill 协程：`runtime["archive_batch_paused"]` 置位 →
  `archive_history_batch` 在**每个条目之间**检查一次 → 做完手上这条就退出，不续排。
  直接杀会把条目留在半截状态（正文抓到一半、图片缺几张）。
- 排队中的批次由 `UPDATE jobs SET state='CANCELLED'` 撤销；`LEASED` 的动不了，
  靠标记收尾。所以按停止之后还会看到一条"批量归档"在跑几秒，是正常的。
- 手动点「归档全部仅索引条目」= 继续：`start_archive_batch()` 会先清掉标记。
- `start_archive_batch` / `stop_archive_batch` 是纯数据库函数，放在 `pipeline.py`，
  API 只是薄壳。测试不该为了调它们去 import 整个 FastAPI 应用。

## 内存盒子有两种 JSON，缺一种少一半（2026-11-18 起）

- A 形态：`{"AppMsgId","DateTime","weight","uniqueId":"gh_x@mid","localId","Title"}`
  → 有 gh 和标题，**没有链接**。
- B 形态：`{"AppMsgId","DateTime","isPreview","detailInfo":{"ContentUrl",…},"showName","sourceUsername"}`
  → 有链接和号名，**没有 Title**。
- 必须按 `mid` 合并：gh 取 A 的 `uniqueId`（精确配对，不看字段顺序），标题取 A 的
  `Title`（窗口截止到下一个 `"AppMsgId"`，否则会把邻居的标题认成自己的），
  URL 取 URL 池里 `?mid=` 对得上的那条。实测 84 → 225 张卡片，带链接 31 → 190。
- 别用 `json.loads` 整体解析：堆里的 JSON 会被截断、会被复制几十份、还会夹二进制。
  逐字段正则比解析器稳。
- `_decode_json_string` 里 **不要**对 `json.loads(bytes)` 的返回值再 `.decode()`：
  它已经是 str，多调一次会抛异常并被吞掉，`\uXXXX` 转义就永远解不开。

## 公众号名对不上 gh，就是收不到推送（2026-11-18 起）

- 公众号库里的名字有一批是 OCR 出来的：`Al-Protein Design` 其实是 `AI-Protein Design`，
  `ScienceAl` 是 `ScienceAI`，`A14Protein` 是 `AI4Protein`。启用名单里 644 个名字
  有 35 个因此映射不到 gh，这些号一条推送都收不到，而且**没有任何报错**。
- 匹配顺序固定：精确 → 忽略大小写 → 包含（唯一命中）→ `name_match_key` 折叠
  （唯一命中）。折叠会把 `l`/`1`/`|` 折成 `i`、`0` 折成 `o`，所以撞车时必须放弃，
  宁可报"对不上"也不能把 A 的文章算到 B 头上。
- `wechat_accounts.account_id` 有值就直接用，不要再拿名字去猜。
- **名单本身也是 OCR 来的**：1460 条全部由 2026-11-13 的手机长截图识别导入
  （`data/account_ocr_raw_*.json` 里还留着原图结果，单张图高 39642px），此后
  从未从数据库重建过——到 11-18 只有 5 条带 `account_id`。「能读微信数据库」
  指的是运行时把名字对成 gh，不是名单来自数据库，别把这两件事混为一谈。
- 根治靠 `tools/calibrate_accounts.py`：以 `contact.db` 里 `username LIKE 'gh_%'`
  的 1717 条真实名单为权威源，分档回填 `account_id` 和真名（exact → folded →
  fuzzy → loose）。
- 分档阈值：fuzzy 要 `SequenceMatcher ≥ 0.80` **且** 领先第二名 ≥ 0.15，
  形近字/漏字（`漆草`→`潦草`、`数字李生`→`数字孪生`、`老干`→`老千`）能自动改；
  像 `生物材料研究` 这种第一二名只差 0.10 的必须人工看。loose（包含关系）只动启用的。
- 写库前先备份 `data/backups/wechat_accounts_before_calib.json`，改动明细落在
  报告同名的 `.applied.json`。实测一次 1403 条对上，启用 644 条里 638 条拿到 gh。

## 别在服务运行时猛写库（2026-11-18 起）

- 现象：网页上点「收藏到收藏夹」报 **Internal Server Error**，`web.log` 里是
  `sqlite3.OperationalError: attempt to write a readonly database`，报在
  `db.py` 的 `BEGIN IMMEDIATE`。读一切正常，只有写失败，重启 API 就好。
- 成因：`Database.init()` 每次都执行 `PRAGMA journal_mode=WAL`。这句即使已经是
  WAL，在别的连接正读写时仍要抢一把排他锁。服务在跑的时候另开进程构造
  `Database()`（一次性脚本、批量修数据），就会把服务进程踢进 readonly，
  而且**它自己不会恢复**。11-18 那次是校准脚本 1400 条各开一个事务，堵了三分钟。
- 现在 `init()` 先读 `PRAGMA journal_mode`，只有不是 wal 才切；`transaction()`
  在 readonly 重试到一半时会 `_repair_journal_mode()` 重置一次。
- 批量改数据必须**一个事务写完**（`executemany`），不要逐条 `db.execute`。
- 真复现了：重启 app.api 即可（`kill` 掉后 supervisor 自动拉起）。

## 解密缓存要原子替换（2026-11-18 起）

`decrypt_database` 以前 `open(out,"wb")` 就地覆盖，别的进程正在读时会读到半截文件，
报 `database disk image is malformed`——看起来像库坏了，其实只是写了一半。现在写
`.tmp` 再 `os.replace`；Windows 上目标被占用时重试 4 次，仍不行才退回就地覆盖。

## 一次群发多篇要按 (mid, idx) 出卡片（2026-11-19 起）

公众号一次推两篇时，微信的 `AppMsgId`(mid) 是**同一个**，靠 `idx` 区分（idx=1
是靠上那篇）。内存通道以前按 mid 归并，`setdefault` 把 idx=2 起全吞掉了 ——
用户看到的就是"同时推两篇，只读到了上面那篇"。

现在 `_assemble_cards` 对每个 (mid, idx) 出一张卡：key 用 `gh@mid`（idx=1，
兼容历史 seen）或 `gh@mid#idx`，`external_key` 同理。标题只有在条数够时才按序号
配，配不上就留空（入库用 URL 兜底，归档后以页面 `og:title` 为准）。
2026-11-19 实测进程堆里有 101 个 mid 带多个 idx，最多一次推 4 篇。

## 重点账号的放行要排在泛词拦截之前（2026-11-19 起）

`relevance.assess_candidate` 里 `catalog_matched` 的分支原来写在"只命中泛词"
之后，**永远走不到**。用户明确勾进追踪名单的号，标题里只要只有"实验""研究"
这类泛词，就被静默丢弃 —— 连 filtered 记录都没有，搜不到也查不出。
2026-11-19「民间审稿办主任」那条"培养皿可能是你实验的最大因素"就是这样丢的。

顺序现在固定为：广告/引流 → 社会新闻 → **启用名单放行** → 具体名词 → 泛词拦截。
名单外、只有泛词的照旧挡掉：个人口味不能借这条混过去。

## PDF 兜底：直接问出版社要（2026-11-19 起）

OpenAlex / Unpaywall 对新刊、国内刊收录滞后。Vita（10.15302/vita.2026.10.0084）
两个库都查不到，被判成 `closed`，可官网页面一直挂着 `citation_pdf_url`。

`papers.py` 现在在"两个库都没给出 PDF"时抓一次落地页的
`<meta name="citation_pdf_url">`（Highwire 规范，几乎所有 OA 平台都遵守）。
属性顺序都兼容，失败不影响主流程。

## 改完代码必须重启 api / worker（2026-11-19 补）

`radar.py save` 只提交代码，**运行中的进程不会重新加载**。2026-11-19 排查
"文章为什么没入库"时，跑着的 api/worker 还是前一天 22:31 启动的，当天所有修复
都没生效，白等了两轮采集。

改完代码要 terminate 掉 `app.api` / `app.worker` 进程，supervisor 5 秒内自动
拉起新进程；或在面板上点一次「停止 → 启动」。验收前先看进程启动时间。

## 选链接按参数完整度，不按长度（2026-11-19 补）

同一篇在微信进程堆里有多个 URL 变体。**微信只对带 `chksm` 的原始链接返回正文**：
只有 `sn` 的短版本返回十几 KB 空壳页，`#js_content` 根本不存在，归档只会报
"微信返回的页面里没有正文，也不是验证页"，重试到死也没用。

`wechat4_memory._url_score` 的排序固定为：无 U+FFFD → https → **有 `chksm`** →
`sn` 更长 → 更短（长度只作平手裁决）。实测同一条推文：带 chksm 的 158 字符链接
给 3.9 MB 正文，不带 chksm 的 94 字符链接只给 17 KB 空壳。

历史遗留：`items` 里 7457 条公众号链接中 344 条缺 chksm，多数是 3.x 通道抓的
（那条路不校验），不用回头改；`archive_item` 里报"没有正文"的 17 条才是真受影响的。

## 一次群发多篇：3.x 也是"一条消息挂几篇"（2026-11-19 补）

4.x 内存盒子按 `AppMsgId`(mid) 归并，3.x 的 PublicMsg.db 更直接：**一行**里
并排几个 `<appmsg>`（也有把几篇塞进 `<mmreader><category><item>` 列表的写法）。
旧代码 `root.find("appmsg")` 只取第一个，靠下的那几篇不光不入库，连面板列表里
都不出现，连"补录"入口都点不到。11-05 黄硕课题组那批（环肽 + JACS 位阻标签）
就是这么漏的，11-19 的 Nature 红外 + Nature Chemistry 适配体又漏了一次。

现在三个地方都按篇展开：`wechat3_public.cards()`（历史通道与面板 4.x 合并）、
`wechat_public.parse_cards()`（面板 3.x 直读）、`wechat4_memory._assemble_cards()`。
凡是入库键都要带 `#idx`（`public3:` / `wechat-panel:` / `public4mem:` 三套都改了），
否则第 2 篇与第 1 篇撞唯一键，直接被吞。

同一篇在 XML 里常出现两份，参数完整度不同，按 (mid,idx) 去重后留带 `chksm` 的。

## 公众号面板的「最近推文」必须并上资料库（2026-11-20 补）

微信 4.x 不把公众号正文落盘（`wechat4_live.recent_for` 长期为空），3.x 的
`PublicMsg.db` 在升级那天就冻结，内存盒子只留最近几百条（实测 331 张卡 / 209 个号）。
只问微信的话，面板列表会永远停在换通道那天——而右侧小字来自 `items`（本站最近
收录），于是出现「小字写着 11-19，点开只有到 11-16」的割裂。

现在 `wechat_account_articles` 在 4.x 分支并三路：现役会话库 → 3.x 历史 →
`wechat_panel.library_articles()`（资料库里已收录的）。去重按 canonical_url，
排序按 create_time，分页必须在**合并之后**切片——`rows[offset:]` 会把 300 条
一次全吐出来，`rows[offset:offset+limit]` 才对。

## showModal 会压住精读页（2026-11-20 补）

公众号面板 `#waArticles` 用 `showModal()`，处在浏览器 top layer；精读页
`#detailDialog` 用 `show()` + 自绘 backdrop，z-index 再高也压不过 top layer。
点「在资料库」看起来"没反应"，其实是精读页被面板盖住了。`openDetail()` 现在先
关掉除自己以外的所有 `dialog[open]`。

## 402 不是网络抖动，重试到天亮也不会好（2026-11-21 补）

DataX 余额不足时每个账号都回 `402 Payment Required`，`poll_source` 原来照单全收：
`error_count+1`、写一条 `source_error`、隔 5 分钟再来。41 个号就是 41 行红字，
一晚上几百条，把真正要看的问题全埋了，用户只会觉得软件坏了。

现在 `pipeline.source_fatal_error()` 把 401/402/403 认出来（`response.status_code`
优先，httpx 的 `Client error '402 Payment Required'` 文案兜底；URL 里恰好出现的
数字不算，避免误伤），命中就：

- `sources.enabled=0`，不再调度；
- `last_error` 写成一句人话加下一步（充值 / 换 Key / 查权限）；
- 只记一条 `source_disabled` 事件，然后 `return`，不重试。

500、超时、连接失败仍然算普通错误，继续退避重试——对方挂了有可能自己好。

## 失败任务不该当日志用（2026-11-20 补）

`poll_source` 的 dedupe_key 带时间窗，失败一次就留一条 DEAD：一天 288 条，
本机三天堆到 813 条「未能识别当前微信账号」。两条规矩：

- 环境性错误（微信没登录、适配器不认 4.x、数据源没余额）→ `CANCELLED`，原因
  留在 `sources.last_error`，不占失败列表（`job_error_is_environmental`）。
- 同一个来源的历史死信只留最新一条（`prune_stale_jobs`），其余删掉。

`/api/jobs/bulk` 提供批量 retry/delete；`/api/jobs/summary` 多了 `failed_groups`，
前端失败页据此显示「失败原因 Top」——800 条往往是同一件事，不该让用户逐条翻。

## 同批第二篇没有标题，不能挡（2026-11-20 补）

4.x 内存盒子只把第一篇的标题关联到 mid 上，idx≥2 的卡片 `title` 为空，候选里
用 URL 兜底。`assess_candidate` 原来要求 `catalog_matched and title 非空` 才放行，
于是第二篇走到「只有裸链接，无法确认科研相关性」被拒——这是「同一时间推两篇、
只读到上面那篇」的另一半原因；只把读取器按 (mid,idx) 拆开是不够的。

现在启用账号（`catalog_matched`）下标题为空/URL 也放行，reason 写明「标题本地
没带出，先收下」；归档时 `archive_web` 会用正文标题覆盖 `items.title`。广告、
社会新闻仍在前面单独挡过一轮，名单外的裸链接照旧拒绝（都有测试锁着）。

注意副作用：改完不会自动补历史——`memory.seen` 已经把这些 key 记成"看过"了。
要补只能重扫 3.x 库（清 `cursor_json.wechat3`）或让用户转发链接。

## 关注列表和「收谁的号」是两件事（2026-11-20 补）

`wechat_accounts` 的 1460 条是 11-13 一次性 OCR 快照。之后用户再关注一个号，
库里**不会**自动出现——他的体感就是「我明明关注了，公众号库里却没有」。
11-20 的 `Vita 生命`(gh_4620cb736abb) 就是这样：OCR 把名字截成两字的「生命」，
`account_id` 为空、`enabled=0`，校准脚本因为名字太短（loose 要求 ≥4 字）也没对上。

现在三处共用 `wechat_panel.sync_account_rows()`：

- `POST /api/wechat/accounts/sync-local`（面板上的「同步本机关注」按钮）
- worker 的 `account_sync_loop`：每 30 分钟一次（`runtime.wechat_accounts_synced_at`
  限流 `ACCOUNTS_SYNC_INTERVAL`），只新增、不追踪
- 新装环境首次灌库

间隔定 30 分钟而不是一天：解一次密读一张联系人表只要一秒，而按天同步的代价是
用户上午关注的号要到第二天才出现——「抗体XDC」没进库就是这么来的，看着就是同步坏了。

匹配顺序必须是 **gh → 名字 → 折叠键**（`name_match_key`）。只按名字的话，
同一份关注列表每同步一次就会给 OCR 错名的号再造一条空壳。已存在的记录**不改名**
（微信备注名不一定比用户认得的写法好），只补 gh 和空 description。

`wechat_accounts.name` 是 `UNIQUE COLLATE NOCASE`：插入撞键时退回按名字补 gh，
不能让一次同步整批回滚。新号一律 `enabled=0`——自动按分类启用会把资料库变成垃圾场。

## 弹层要叠，不要互相关（2026-11-20 补）

公众号面板 `#waArticles`、排除弹窗、规则页都是 `showModal()`，待在浏览器 top layer。
精读页 `#detailDialog` 以前用 `show()`（非模态）——z-index 写多高都压不过 top layer，
所以「在资料库」点了没反应。当时的修法是 `openDetail()` 先把其它 dialog 全关掉，
代价是推文列表跟着消失，看下一篇要重新点开号名、重新加载。

现在精读页也走 `showModal()`：后开的模态叠在最上面，关掉自动回到下面那层，
列表原样还在。两个连带约束：

- **Esc 不要在 keydown 里手动 `close()`**。手关掉最上面那层以后，浏览器的默认
  行为会接着关下一次层——精读页和推文面板一起没。模态层自带「先关最上面」的顺序。
- `body.modal-open` 由所有 dialog 的 `close` 事件统一判断：底下还有 `dialog[open]`
  就不解锁滚动，否则关掉精读页会把还开着的面板一起解锁。
- **锚定的小弹层也必须是 `dialog`**。「收藏到」原来是个普通 `div` + `z-index:96`，
  从精读页点它，弹层整块长在对话框背后——看起来就是收藏夹坏了。改成 `showModal()`
  后进 top layer 就压得住；配套要写 `inset:auto;margin:0`（否则弹层自己居中，
  JS 算出来的 left/top 全落空），`::backdrop` 透明，点外面关掉自己判坐标。
- **要一边读一边用的浮层用 `popover="manual"`，不要用 `showModal()`**。对话抽屉、
  划词工具条、「存到笔记库」都属这一类：它们要压在精读页上，但不能像模态那样把
  底下的文章锁死（锁了就没法一边滚文章一边问）。`showPopover()` 同样进 top layer、
  后开的在上面，却不会让底下不可点。两个坑：`popover` 的 UA 样式带 `inset:0` 与
  `margin:auto`，必须清掉才能用 JS 定位；`toast` 也得是 popover，否则精读页开着时
  所有提示都长在遮罩底下，用户只看到「点了没反应」。
- **精读页开着时，页面上的浮动「问 AI」是按不到的**（被模态挡住）。所以对话框顶栏
  有一条 `#detailAsk`；`body.modal-open` 时把浮动按钮收起来，别让它看起来可点。

## 重复记录要合，不要靠名字猜（2026-11-20 补）

11-13 的 OCR 快照留下 25 条「名字被截断、`account_id` 为空」的残记录，11-20 按
关注列表补进真记录以后就成对出现。名字相近**不等于**同一个号（「湖南师范大学」
和「湖南师范大学图书馆」是两个号），所以没做自动模糊合并，而是
`wechat_panel.merge_accounts()` + `tools/merge_dupes.py` 里一份**人工核对过**的清单：

- 前半段靠 `items.metadata_json.wechat_account_id` 对账——入库时从链接取的真 gh，
  作者署名挂在哪个 gh 下，OCR 那条就是它；
- 后半段靠 3.x `PublicMsg.db` 卡片反查——OCR 记录里的 `description` 是截屏上的标题，
  拿去历史消息里搜，命中的号就是它。

合并规则：一律并到**有 gh** 的那条上（认不出号的就是空壳），`enabled` 取或，
`fit` 取更靠前的一档，`category_code` 优先非 OTHER，`reason`/`description` 只填空。
`wechat_article_marks`（面板排除记录）必须跟着搬。条目本身不动——`items.author`
是文章页上的真实署名，拿号名去改只会把溯源改坏。

## 同步循环别在事件循环里做磁盘活（2026-11-20 补）

`wechat4_live.refresh()` 是 SQLCipher 解密 + 全表读，`account_rows()` 要读整张
`contact.db`。直接在 `account_sync_loop` 里同步调用会把采集、监听、任务队列一起
卡住几十秒（单次预算就有 90 秒）。两处都必须 `asyncio.to_thread`。

微信没登录时 4.x 根本没有密钥，这一趟读不到任何号——**不能写**
`wechat_accounts_synced_at`，否则一天只有一次机会，刚好撞上关机就白等 24 小时。
现在的处理是：`quick_status()` 说没运行就一小时后重试；读到 0 个也一小时后重试。

对账口径：`contact.db` 里 `username LIKE 'gh_%'` 的号，加上库里已有 gh 的号，
两边必须一一对上，跑一次同步 `inserted` 必须是 0。**非 gh 的老式号**
（果壳、央视新闻、丁香园这类 `wxid_*` 用户名）这条链路读不到，它们的记录永远
没有 `account_id`，也就永远收不到推文——这是通道限制，不是同步 bug。

## 精读走客户端自己的登录态（2026-11-21 补）

**症状**：`analyze_item` 全部失败，`RuntimeError: login_required：该能力需要登录后使用，请先登录。`
164 条精读死信，最后一次成功是 11-20 14:56。

**根因**：官方云 SDK（`cloud/dist/llm-worker.mjs`）在 Node 里没有 `localStorage`，
`CloudAuthStorage` 退回内存 → 每次都是匿名请求。2026-11-20 17:40 起网关对匿名
调用一律 401，所以整条链路在服务端侧关掉了，跟本地配置无关。

**修法**：`app/services/workbuddy_direct.py` 借用客户端已经登录好的身份：

- WorkBuddy.exe 的 private 堆里扫 JWT（`OpenProcess` + `ReadProcessMemory`），
  解 payload 取 `exp` / `aud`，只要 `aud` 含 `account`、有效期还剩 60 秒以上的；
- 直连 `https://www.workbuddy.cn/v2/chat/completions`（**必须 stream=true**，
  网关明确拒绝非流式：`11101 Non-stream chat request is currently not supported`）；
- `call_workbuddy_full` 先走这条路，`DirectUnavailable`（客户端没开）才退回 SDK，
  两次错误合并报出来，不掩盖。

**令牌生命周期**：客户端 token 有效期 28 天。内存里拿到后同时用 DPAPI 加密存
`data/token-cache.bin`（`CryptProtectData`，绑定当前 Windows 账户），客户端关掉
时用这份顶。内存里没找到 + 副本解不开，才报"取不到登录令牌"。

**不要做的事**：不要去 `signUp`/`signInWithPassword` 绕过——这个应用的终端用户
注册要求邮箱验证码（`signUp requires a verificationToken`），拿不到码就会走死路；
也不要在请求上硬加 `Origin` / `User-Agent` 试（实测无效，401 与来源头无关）。

**模型目录**：`/v2/models` 这类端点不存在（404）。设置页「查看可用模型」改成先问
SDK，失败读客户端缓存的 `acc-product-config-v3-*.json`（59 个模型，字段
`id`/`name`/`credits`），再失败才退回当前模型。设置页「测试连接」不再依赖目录，
直接用直连发一个最小请求验证。

**依赖边界**：这条路要求 WorkBuddy 客户端至少在本机登录过（有加密副本）。
客户端彻底换机器/重装 → 副本解不开，需要客户端登录一次再跑。

<!-- aoci:begin -->
## AOCI 仓库认知

AOCI 为本仓库维护一个稳定、可版本化、可增量更新的仓库级认知层，供模型跨任务复用对系统的理解。

`aoci.txt` 是面向模型的结构化认知索引。它以每个受管理文件、数据库表或其他受管理对象一条独立 Entry 的方式，用符号标签与 F/R/A/S 语义表达对象的核心职责、重要关系、对外契约，以及理解或修改系统时必须知道的非显然约束和设计决策。

Header、目录段和全部 Entry 共同组成完整仓库索引，可以覆盖前端、后端、配置、数据库结构及其他受管理内容。受管理内容发生变化时，通常只需维护受影响的认知条目，不需要重新生成整个索引。

AOCI 提供系统架构、对象职责、重要关系、对外契约和关键约束的高密度视图。

### 工作原理

AOCI 采用“模型生成、模型读取”的认知闭环。

Header、Entry 和 Curation 语义的创作只按当前机器签发的 Plan 与实时 Guide 执行；由 Host 模型基于当前绑定证据独立完成。

Entry 的语义必须来自模型对真实证据的理解。不得仅依据路径、文件名、扩展名、AST、符号列表、依赖扫描、正则、固定模板或规则引擎推导、预填、拼接或改写索引语义。

对 Fresh Bootstrap，只按当前机器签发的 Plan 和实时 Guide 执行。当它们要求创作时，Host 模型创作 Root、Meta、Tag 和 F/R/A/S，提供 authoring-run 声明，并把它绑定到 Plan、Evidence 与完整 Candidate。不得要求 AOCI 填写 `origin=host_model`、制造 Receipt 或把程序生成的 Framework 当作语义。本文件不自行重建 Onboarding 流程。内部批次不是用户决策；只有遇到既有批准边界或真实的安全、漂移、CAS、Recovery 条件才停止。

### 最小使用入口

- `aoci_rules`：取得当前AOCI版本的会话运行合同。
- `aoci_overview`：建立或恢复本仓库的完整认知。
- `aoci_maintain`：受管理对象达到最终稳定状态后检查认知是否需要维护。
- `aoci_update_entry`：提交与当前证据和源码摘要绑定的完整语义更新批次。
- `aoci_report`：仅当当前布局和工具状态支持时，在证据不足、无法可靠生成语义时登记待办，不猜写。

其他MCP工具、CLI命令、参数和专项流程，以当前工具说明、Guide和 `--help` 返回内容为准，不在本文件中重复完整手册。

本区块只规定仓库接入、认知使用和收尾原则。`aoci_rules` 承载当前会话合同，Guide实时输出承载当前Plan的执行顺序与停点，工具Schema、Spec和Validator承载机器结构与判据；Prompt、Description、README和静态文档不能覆盖这些机器事实。

### 建立、生成和恢复认知

1. 每个新的 Agent Run 开始时，应先判断：

   - 本仓库是否已经存在可用的完整AOCI索引；
   - 当前上下文中是否已有与本仓库根、当前索引版本和当前AOCI服务相匹配，并且模型仍可可靠使用的完整仓库认知。

2. 仓库已经存在可用的完整索引，但当前Run没有可靠完整认知时，先调用 `aoci_rules`，再调用 `aoci_overview`。

   完整认知仍可靠时直接复用。局部不确定本身不要求机械重读系统全貌。可靠认知建立后，默认以它作为仓库级系统上下文，只读取当前任务直接需要的源码、测试、配置与契约，只有某个缺失的具体事实可能改变回答或验证结论时才扩大证据范围；AOCI不能替代修改对象与验证行为所需的直接源码证据。

   本Run从已知Host上下文压缩恢复时（包括宿主注入的压缩摘要），必须把此前模型认知视为不可靠。压缩handoff不得保留或摘要正式Whole-Index，也不得保留或摘要任何Overview Header、Entry、Chunk、Challenge或Attestation正文；只能保留安全续接所需的receipt身份、未完成write或Recovery状态，以及立即重载指令。复制进handoff的Whole-Index语义或receipt不能证明恢复后模型的当前认知可靠。若当前上下文已无法可靠保留运行合同，先调用 `aoci_rules`。继续业务任务前，使用 `refresh_reasons=["context_compaction"]` 和新的 `refresh_event_id` 调用普通完整Whole-Index `aoci_overview`（不设置 `check_only` 或设为false）；不得使用 `check_only` 或认知probe。原样跟随每个 `next_cursor` 直到 `completed=true`，确认交付，并且只基于新交付正文提交一次Attestation。完成这次新的完整传输后，即使Attestation为partial或fail也消费该generation，并按既有合同继续source-bound任务，不再自动调用第二次Overview。若该请求返回 `delivery_mode=capacity_stop`，说明Whole-Index装不进本Host的上下文：不得重试或调小 `chunk_tokens`；该停止消费本generation，任务按source-bound继续，且不作无保留的完整系统认知声明。

   AOCI可以针对 `context_compaction`、项目 `cognition_refresh_threshold` 下的机器 `semantic_threshold` 或主要 `phase_transition` 提供checkpoint与认知状态事实。只需要这些紧凑事实时使用 `check_only=true`；这些事实只向Agent提供建议，不替模型决定是否需要系统全貌。

   Agent显式调用普通 `aoci_overview`（未设置 `check_only` 或为false）时，只要能形成一致的CognitionSet，AOCI必须完整交付请求scope。不得因为已有receipt、阈值未达到或没有待处理刷新原因而抑制正文；唯一例外是待处理压缩刷新的上述容量停止。正式认知Dirty或Stale时仍交付正文，但必须标记不可靠。存在未决恢复或无法形成一致snapshot时失败关闭，不返回混合正文。

   普通Overview返回 `continuation_required=true` 时，必须原样提交 `next_cursor` 并自动继续到 `completed=true`。不得询问用户、开始业务任务或给出阶段性系统结论。每块单独一次工具调用；不得在一个脚本里连取多块或合并其输出。缺块、重复、乱序、cursor失败、Index变化或`chunk_tokens`变化时停止本次认知链。Attestation完成前不得用Memory、源码、Spec、`aoci.txt`、历史会话、scope、search或Entry读取修补或补充Whole-Index认知。Challenge ordinal是正式Entry序列中的1-based位置；Header内容、注释、空行、Section/Overview/Chunk Marker、Receipt与Metadata均不计数，Chunk Receipt ordinal使用同一序列。Attestation必须原样回绑本次Challenge发布的当前`index_sha256`、`entry_sequence_sha256`与`entry_count`；旧Index、旧Entry序列、旧数量或旧Attestation均无效。完整链结束后只正式提交一次既有模型认知Attestation；同一响应只允许一次不改变语义答案的JSON Schema或字段格式修正。未达通过（全对的序号不足80%，或对象身份错超过一题）即记为不确定回执，不得语义重试或旁路补答。首次认知失败时还不得执行Root/Meta、Migration、全局布局或其他未重新绑定的系统级决策。上下文压缩刷新若传输完整、认知身份不变、治理对齐且没有Recovery或第三方冲突，即使Attestation为partial或fail也消耗该refresh generation，并继续原任务，不再自动重读Overview。`system_mastery_percent`只自评系统框架——架构、职责、强关系、稳定外部契约以及高熵安全和维护约束——不表示完整实现或运行实况知识；机器索引覆盖率必须分开。默认只向用户输出由本次真实覆盖率、Challenge、块数、Token和掌握度生成的规定成功或失败一句话。Host截断某一块时，用同一cursor重取一次并核对 `chunk_sha256`，服务端返回完全相同的字节。仍被截断才停止，提示用户把 `overview_delivery.chunk_tokens` 设置为更小的合法值后重新开始，不得自动修改。

   加法认知等级必须与严格证明字段分开解释。`delivery_verified`表示已加载Index且Host交付已确认，但完整认知验证仍未完成；应表达为“已加载且交付已验证”，不得描述为“没有认知”或“没有理解系统”。`cognition_verified`要求Attestation通过（Challenge至少80%的ordinal完全正确且对象身份至多失手一处），`cognition_governed`还要求治理对齐。通用完整读取失败句只用于真实交付故障。

   当Overview响应包含可选`cognition-state/v2`投影时，必须分别解释各维度。其Level止于`model_cognition_usable`；`strict_attestation_verified`、`governance_aligned`与`current_system_cognition_reliable`都是独立状态，绝不参与该Level。ordinal、对象身份、Tag或核心F不匹配可以导致严格Attestation失败，而模型认知仍然可用；不得仅凭这种不匹配就宣称模型没有理解系统。只有`current_system_cognition_reliable=true`允许无保留地声称当前完整系统认知可靠。投影缺失时继续使用上述Legacy解释。

   普通的只读审计、分析、检查、不修改代码或不提交、不push，不自动等于严格零写入，也不改变上述认知有效性判断。Codex Memory和历史Skill只能辅助恢复经验、用户偏好与调查方向，不能替代与当前仓库根、索引摘要、AOCI服务身份和认知范围匹配的当前认知收据；项目AGENTS和当前AOCI身份在AOCI状态上优先于历史Memory。

   只有用户明确禁止Ledger、元数据、`.aoci`运行资产及任何文件写入时，才按严格零写入处理。若必要的认知建立与该边界冲突，必须报告冲突并请求用户裁决或建议使用隔离副本，不得静默以Memory替代当前仓库认知。

3. 仓库没有可用的完整索引，或当前只有最小骨架、Header不完整、Entries未完成、必要Curation尚未裁决时，如果需要建立正式完整AOCI索引，先取得 `aoci_rules`，然后进入当前AOCI Guide。由Guide依据仓库真实状态决定下一阶段并完成必要安全步骤。

   `aoci_maintain` 不替代索引建立流程。

   不在本文件中自行重建或硬编码完整索引生成状态机。

4. 在长程任务中，模型负责保留当前认知收据并正确使用刷新门禁：

   - Host报告上下文压缩或模型已知系统全貌丢失时，执行上述强制 `context_compaction` 重载规则；AOCI不能自行推断Host事件；
   - 进入真正的主要阶段时声明 `phase_transition`，不得把函数、测试运行或小步骤当作阶段；
   - 在有用的稳定检查点通过 `check_only=true` 取得机器语义计数；
   - 除已知压缩的强制重载外，由Agent判断当前任务是否需要再次显式获取指定scope或完整Overview；
   - 在维护和对齐完成前，保留AOCI报告的Dirty或Stale可靠性状态。

### 任务收尾与认知维护

5. 纯只读问答、分析、版本核验，或没有产生受AOCI管理对象变化的任务，不需要调用维护工具。当前AOCI版本是任意`aoci_overview` check_only或`aoci_maintain`响应里的`cognition_receipt.mcp_service_version`；二进制路径是项目`.mcp.json`里的`command`，CLI不必在PATH上。

6. 发生受AOCI管理对象变化时，待其达到本次任务的最终稳定状态后，只调用一次 `aoci_maintain`。不要在每次中间修改后逐文件维护。

7. 若维护结果返回真实语义候选，Host 模型必须基于每个候选绑定的对象和必要证据，独立创作完整标签与F/R/A/S更新。通过 `aoci_update_entry` 一次提交当前机器签发批次的完整候选集合，同时原样保留每项路径与对应domain批次身份；`source_sha256` 与 `candidate_id` 可以省略，由服务端按签发的批次取得。`max_entries`只限制单次请求和原子事务，不限制logical plan、Whole-Index或Managed Scope。`remaining`非零时，在当前批次成功Apply后重新调用Maintain并从新preimage继续；绝不能为满足transport上限缩减Index覆盖或自行截取返回批次。

   没有足够证据且当前布局支持 `aoci_report` 时，使用它而不猜测、套用模板或为消除待办而生成缺乏证据的认知。

8. 必须遵守工具返回的结构化状态和安全边界：

   - `repair_required`：只修复明确命中的候选，再重新提交当前机器签发的完整批次；
   - `stopped`：结束当前写入尝试并检查 `failed_step`、错误、正式写入证据与Recovery。auto模式下，已证明零写入则记录closure并重新Plan；完整Intent和可证明postimage则Resume；策略要求Rollback且preimage可证明则精确恢复后重新Plan。只有证据不足、第三方正式字节冲突、需要审批或外部动作，或命中其他真实安全边界时，才停止整个用户任务；
   - 冲突、审批、人工裁决、权限和安全信号不得忽略；
   - 已经对齐后不得重复维护或重复写入；`refresh_ready_for_overview` 是checkpoint事实，由Agent决定是否为下一阶段请求普通完整Overview。

   维护完成后如果又修改了任何受管理对象，之前的维护结果失效，应在新的最终稳定状态重新完成收尾。

9. 用户只限制业务文件范围，但没有明确禁止仓库托管资产时，AOCI托管资产可以在收尾阶段为保持认知一致而更新，并应在审计和提交中与业务文件区分。

   用户明确禁止修改 `aoci.txt`、`.aoci`、元数据或任何额外文件时，以用户限制为准，不得写入，并如实报告剩余不一致。

### 专项流程

初始化、完整索引生成、Header生成、Entries生成、数据库结构索引、Curation、人工评审和故障恢复，只按当前AOCI Guide或工具在对应阶段返回的指令、命令和安全停点执行。

不预加载、不猜测，也不自行重建这些专项流程。平台调用方式、请求格式、批次上限、审批规则、索引格式细节和恢复步骤由对应Guide、工具说明、模型Prompt和CLI帮助按需提供。
<!-- aoci:end -->
