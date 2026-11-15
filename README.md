# Hermes 科研雷达

本地的科研信息收集器：抓公众号与 B 站、归档原文、用模型精读、按相关度推给你。
跑在自己电脑上，数据也在自己电脑上。

## 启动与停止

装好之后从开始菜单或桌面双击 **Hermes 科研雷达**（就是安装目录里的 `Radar.exe`），
浏览器会自动打开 http://127.0.0.1:8766 。关掉浏览器不会停服务，要停请用托盘/命令行：

```bash
Radar.exe start      # 启动，不开浏览器
Radar.exe stop       # 停止
Radar.exe status     # 现在什么状态、地址是多少
Radar.exe restart    # 重启（改了代码或依赖后用）
Radar.exe uninstall  # 卸载：快捷方式、开机自启、注册表登记一起清掉
```

第一次装完，打开页面会被带到 **设置 → 配置向导**，从上往下五步：
连接微信 → 接入大模型 → 汇报发到哪里 → 挑公众号 → 开机自启。
每一步都会当场告诉你"现在什么状态、还差什么、为什么是这样"，配完就是能用的状态。

源码方式（没装 exe 的时候）：

```bash
python radar.py start
python radar.py stop
python radar.py where      # 各个目录在哪、数据库是哪一版
```

## 改坏了怎么办

代码有版本控制，不用怕改崩。

```bash
python radar.py status                  # 现在有哪些改动还没存
python radar.py save "改了什么"          # 存一个版本
python radar.py list                    # 看历史，带编号
python radar.py undo                    # 退回上一个版本
python radar.py back 3                  # 退回列表里第 3 个版本
python radar.py config-restore          # 只把设置恢复成上次保存的样子
```

**每一次回滚都会先把当前状态留底**，所以退错了还能再退回来。`undo` 的语义按情况定：
有未保存改动时是"丢掉改动回到上次保存"，工作区干净时是"退回再上一个已保存版本"。

## 换台电脑 / 给别人用

### 用安装包（推荐）

双击 **`RadarSetup.exe`**：可以选安装目录、要不要桌面快捷方式、要不要开机自启；
它自己找 Python、建虚拟环境、装依赖、写配置、建数据库，最后可选直接打开页面。
装完可以在 设置 → 配置向导 里把微信和大模型配好，不需要碰命令行。

静默安装（批量装机、自动化测试用）：

```bash
RadarSetup.exe /S --dir=D:\Radar --port=8899 --no-autostart --no-desktop --no-launch
```

卸载走 Windows「应用和功能」，或者 `Radar.exe uninstall`（会问两次：要不要卸、要不要连
`data\` 里的资料一起删）。

### 从源码装（开发、或想看得见每一步）

把整个文件夹拷过去，跑：

```bash
python install.py
```

安装向导会建虚拟环境、装依赖、问几个配置问题（端口、用哪个模型、要不要推送）、
把数据库建起来。已经装过再运行是安全的，不会覆盖现有设置。

`python install.py --reconfigure` 重新配一遍，
`--reinstall` 重装依赖，
`--dry-run` 只检查不安装（想确认这个包完整时用）。
`Install.vbs` 是同一件事的双击版，留着给不方便开命令行的时候用。

### 自己打安装包

```bash
python tools/build_exe.py            # payload.zip + Radar.exe + RadarSetup.exe
python tools/build_exe.py --launcher # 只重打 Radar.exe
```

产物在 `dist/exe/`。`Radar.exe` 只做"拉起 supervisor、开浏览器、停服务"，
依赖住在安装目录的 `.venv` 里，所以改代码不用重打 exe；`RadarSetup.exe` 才是要发给别人的那个。

### 只搬程序，不搬资料

```bash
python radar.py pack
```

生成 `dist/hermes-radar-<版本>-<日期>.zip`，里面只有代码、模板和安装脚本，
几百 KB。解压出来跑一次 `install.py` 就是一套干净的实例。

### 连资料一起搬

除了上面那个包，再拷 `data/` 整个目录（这个仓库不跟踪它）。
`data/` 里有：

| 目录 | 内容 | 能不能不拷 |
|---|---|---|
| `data/radar.db` | 条目、评分、笔记、对话 | 不拷就是空的资料库 |
| `data/documents/` | 每篇文章的离线页面与正文 | 不拷则阅读页打不开原文 |
| `data/blobs/` | 图片原图（按内容寻址） | 不拷则图看不全 |
| `data/papers/` | 下载的论文 PDF | 不拷则要重新下 |
| `data/logs/`、`data/backups/` | 日志和历史备份 | 可以不拷 |

拷完在目标机器上跑一次 `install.py`，它不会覆盖已有数据库，只补环境。

**密钥不会跟着走。** `config/secrets.dpapi.json` 用 Windows DPAPI 加密，只对
"这台机器 + 这个 Windows 账户"有效。到了新机器，旧的解不开，程序会当成没配，
在设置页重新填一次即可（`secret_status()["broken"]` 会列出哪些需要重填）。
这也意味着：**别把 secrets 文件发给别人**，发了也没用，但没必要。

## 目录结构

```
app/          后端：采集、归档、分析、推送、API
web/          前端：单页界面，无构建步骤
installer/    安装程序（RadarSetup.exe）与主程序入口（Radar.exe）的源码
tools/        工程脚本：版本控制、打包、打 exe（与业务无关，可单独拷走）
scripts/      一次性数据修复脚本
tests/        单元测试
config/       settings.json（入库）、secrets（不入库）、settings.example.json
data/         运行时数据，不入库
VERSION       版本号
radar.py      所有日常操作的入口（版本控制 / 启停 / 打包）
install.py    源码模式安装向导
Install.vbs   双击安装（源码模式）
Radar.exe     装好后的主入口（由 installer/launcher.py 打包而来）
Start Radar.vbs / Stop Radar.vbs   源码模式的双击启停
AGENTS.md     改动前后要遵守的规矩
```

## 依赖

Python 3.11+，Windows（微信公众号监测与 DPAPI 密钥存储依赖 Windows API）。
依赖清单在 `requirements.txt`，安装向导会处理。

## 已知的取舍

- 微信公众号自动监测需要本机装微信 3.x 并解密其数据库，只在验证过的版本上可用。
  微信 4.x 的读取代码（`app/services/wechat4.py`）已经写好了，但**没有在真机上验证过**，
  设置页里那个「尝试读取 4.x」按钮就是拿来验证的；方案与退路见 `docs/微信4.x验证方案.md`。
- 微信和本程序之间没有"扫码授权读数据库"这种接口可用——扫码只能登录，授权不了磁盘文件。
  所以走的是"在本机只读地取一次密钥"这条路，全程不联网、不改微信的文件。
- 图片分诊走视觉模型，按张计费，有每日调用上限（`llm.daily_call_limit`）当保险丝。
- 离线页面是抓取当时的样子，原文被删也能看；但发布方后来改过的地方不会同步。
