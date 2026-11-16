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

## 删除必须直删，别走回收站

这台机器给 `python.exe` / `bash.exe` 挂了安全删除垫片（`sitecustomize` + `safe-bin/rm`），
`shutil.rmtree` 和 `rm -rf` 都会被转成"移入回收站"：

| 方式 | 80 个小文件 | 回收站 |
| --- | --- | --- |
| `shutil.rmtree` 默认 | 1.11 秒 | +1 条 |
| `shutil.rmtree` 关垫片 | 0.01 秒 | 0 条 |

一次全量测试能在回收站堆出上千条 `test.db-shm`，之后每次删除都更慢，
用户看到的就是"删个东西卡半天"。

所以：

- 批量删构建产物、临时目录、测试残留，一律走 `python tools/trash.py wipe <路径>`。
- 它内部用 `os.execv` 带 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 重启自己，删除是真删。
- 写完脚本要临时删东西，就在命令前加 `CODEBUDDY_SAFE_DELETE_ENABLED=0`；`cmd.exe` 已被策略封禁，别试。
- 收尾时 `python tools/trash.py list` 看一眼，有本项目的记录就
  `purge --under E:\HermesResearchRadar`。
- 用户自己删的东西（桌面文件、录屏、网盘资料）不在此列，**不要**替他清。

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
