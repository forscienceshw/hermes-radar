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
