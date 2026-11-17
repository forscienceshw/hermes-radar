# 微信 4.2 本地数据读取模块 —— 交接包

> 给另一台电脑上的 AI（WorkBuddy）用。目标：把「读微信 4.2.0 本地数据库、拿到公众号推文与
> 自己转发的文章链接」这套能力，合并进你手上那个更新版的项目里。
>
> 本包在 Windows 11 + 微信 **4.2.0.53** 上真机跑通并留下实测数据（见 `05-实测记录.md`）。

---

## 一、一句话结论

微信 4.2 的数据库**可以只读解密**，密钥必须从运行中的 `Weixin.exe` 内存里取；取法不是老版本的
"扫 `x'<hex>'` 字符串"，而是定位 `com.Tencent.WCDB.Config.Cipher` 配置对象、按固定掩码异或还原。
解密是 SQLCipher 4，必须把 `-wal` 一起合并才能读到"此刻"的数据。
**最容易翻车的地方不是密码学，是文章链接的 query 参数（见 `03-踩坑清单.md` 第 1 条）。**

---

## 二、交付物

```
README.md                   ← 本文件，先读这个
01-技术原理.md               加密结构、密钥怎么来、WAL 怎么合
02-集成指南.md               逐文件、逐调用点的合并步骤（含代码签名）
03-踩坑清单.md               7 个真踩过的坑，每条都有"症状 / 原因 / 正确做法"
04-验证方法.md               在新机器上如何逐层验证（4 个层次）
05-实测记录.md               本机实测数字与失败原因分类

src/                       当前完整实现，可直接对照（不是让你整文件覆盖，见第三节铁律）
  app/services/wechat4_live.py        ★ 新增，整个 4.x 读取器（约 1100 行）
  app/services/wechat4.py             改造：probe/quick_status 委托给 wechat4_live
  app/services/wechat_self_archive.py 改造：3.x / 4.x 分流 + 启用名单指纹回填
  app/services/archive.py             改造：新增 WechatNeedsVerification 异常
  app/api.py                          改造：sync-local、retry-archives、直连自检、stats 取整
  app/worker.py                       改造：验证页 → 分级重试而不是永久失败
  app/net.py + app/settings.py        改造：可选代理（http_proxy）
  web/app.js / index.html / setup.js  改造：公众号库崩溃修复、同步按钮、被挡提示、代理输入
  tests/test_core.py                  改造：用例自带前提（不影响业务代码）
  requirements.txt                    +zstandard +Pillow
  （installer/setup_gui.py、tools/build_exe.py、workbuddy_llm.py、llm_gateway.py、
    setup_status.py 属于同批做的安装器与大模型修复，与本模块无强耦合，可选合并）

patches/                   相对"出厂 payload"的 unified diff，一个文件一个 .diff
tools/probe.py             零依赖探针，放项目根目录直接跑，先确认能读再谈集成
tools/repro_url_param.py   第 1 号坑的决定性对照实验（必读）
```

---

## 三、五条铁律（违反任何一条都会"看起来能跑但拿不到正文"）

1. **抓取用原始完整链接，去重才用规范化链接。**
   公众号卡片的 `<url>` 里有 `chksm` / `mpshare` / `scene` / `srcid`，**必须原样保留**去请求。
   把参数裁成 `__biz&mid&idx&sn` 再抓，微信会返回腾讯云滑块验证页（约 17 KB），正文一个字都没有。
   这条是本项目上一轮"归档全部失败"的唯一原因。

2. **XML 里的 `&amp;` 必须先还原成 `&`。**
   消息正文是 XML，`<url>` 里的 `&` 被转义成了 `&amp;`。直接用会 404 或被判非法请求。

3. **合并 WAL 时，帧的 salt 必须等于 WAL 头的 salt。**
   跨 checkpoint 的残留帧 HMAC 也能验过（mac_key 只跟库的 salt 有关），混进来会把库拼坏。
   症状是 `PRAGMA integrity_check` 报 `invalid page`。

4. **每页解密后要验 HMAC，不能只解不验。**
   密钥对不上时 AES 不报错，只会输出垃圾；HMAC 是唯一可靠的判定。

5. **不要整文件覆盖你手上的新版本。**
   本包的 `src/` 是给你**对照**的。以 `patches/` 和 `02-集成指南.md` 里的调用点为准，
   逐个函数移植。你的版本可能已经改过这些文件。

---

## 四、集成顺序（建议照这个顺序做，每步都能单独验证）

| 步骤 | 做什么 | 验证方式 |
|---|---|---|
| 1 | 把 `src/app/services/wechat4_live.py` 原样放进你的 `app/services/` | `python -c "from app.services import wechat4_live"` 不报错 |
| 2 | 装依赖：`zstandard`（正文解压必须）、`pycryptodome`（AES）、`psutil` | `tools/probe.py status` 能列出进程和库目录 |
| 3 | 跑 `tools/probe.py refresh`，看能否拿到密钥并解开 20 个库 | 输出 `更新 N 个库，失败 0`，缓存目录出现 `.db` |
| 4 | 跑 `tools/probe.py articles`，看能否列出公众号文章（标题+链接） | 能看到真实标题 |
| 5 | **跑 `tools/repro_url_param.py`，确认完整链接能拿到正文** | 完整链接 3.6 MB / 裁参后 17 KB 验证页 |
| 6 | 按 `02-集成指南.md` 接进采集链路（分流、候选构造、游标） | 来源跑一次，条目进库、状态 `discovered` |
| 7 | 接进归档链路（异常 + 重试），看条目变 `archived` / `ready` | 资料库出现 `reader.html` + `content.txt` |

---

## 五、前置条件

- Windows（读进程内存、DPAPI、WMI 都依赖 Windows；Linux/macOS 思路不同，见 `01` 末尾）
- 微信 **4.x 已登录并保持运行**——密钥只在进程内存里。关掉微信只能读上一次解密的快照，
  读不到新消息。
- 需要 `pycryptodome`（AES）、`zstandard`（正文压缩）、`psutil`（找进程）。
  读内存用的是 `ctypes`，不需要 `pymem`、不需要管理员权限（实测同用户即可）。
- 微信 3.x 与 4.x **并存**时不要互相覆盖数据目录；本模块只读 4.x 的 `xwechat_files`。

---

## 六、如果我只需要"能跑的最小集"

只合这 3 个文件就能拿到文章列表（不含界面）：

1. `app/services/wechat4_live.py`（新增，整个读取器）
2. `requirements.txt`（加 `zstandard`）
3. 你现有的采集器里，把"读公众号文章"这一步换成调用
   `wechat4_live.official_articles(since_ts=...)` / `wechat4_live.self_links(since_ts=...)`

归档能不能成功，取决于你有没有守住铁律 1 和 2。**先用 `tools/repro_url_param.py` 验一遍。**
