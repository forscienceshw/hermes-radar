# Hermes 科研雷达

本地的科研信息收集器：抓公众号与 B 站、归档原文、用模型精读、按相关度推给你。
跑在自己电脑上，数据也在自己电脑上。

## 启动与停止

双击 `Start Radar.vbs`，浏览器打开 http://127.0.0.1:8766 。
双击 `Stop Radar.vbs` 停。

命令行也可以：

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

### 装到新机器

把整个文件夹拷过去，然后：

```bash
python install.py
```

安装向导会建虚拟环境、装依赖、问几个配置问题（端口、用哪个模型、要不要推送）、
把数据库建起来。已经装过再运行是安全的，不会覆盖现有设置。

`python install.py --reconfigure` 重新配一遍，`--reinstall` 重装依赖。

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
tools/        工程脚本：版本控制、打包（与业务无关，可单独拷走）
scripts/      一次性数据修复脚本
tests/        单元测试
config/       settings.json（入库）、secrets（不入库）、settings.example.json
data/         运行时数据，不入库
VERSION       版本号
radar.py      所有日常操作的入口（版本控制 / 启停 / 打包）
install.py    安装向导
```

## 依赖

Python 3.11+，Windows（微信公众号监测与 DPAPI 密钥存储依赖 Windows API）。
依赖清单在 `requirements.txt`，安装向导会处理。

## 已知的取舍

- 微信公众号自动监测需要本机装微信 3.x 并解密其数据库，只在验证过的版本上可用；
  微信 4.x 没验证过，不要套用。
- 图片分诊走视觉模型，按张计费，有每日调用上限（`llm.daily_call_limit`）当保险丝。
- 离线页面是抓取当时的样子，原文被删也能看；但发布方后来改过的地方不会同步。
