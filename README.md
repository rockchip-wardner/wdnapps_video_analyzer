# wdnapps_video_analyzer

wdnwebui 的**视频分析类应用插件包**。装上之后，宿主页面上会出现这 5 张卡：

| 卡 id | 标题 | 说明 |
|---|---|---|
| `video_detection` | 音视频描述 | 多路画面逐帧 VLM 描述；带帧判据、音轨分析面板、可选 `tts_alert` 语音提醒 |
| `video_target_detection` | 视频目标监控 | 1~32 路宫格 + VLM 逐路轮转，输出结构化目标事件 |
| `video_target_localization` | 视频任意目标定位 | 自然语言指定目标 → 输出 grounding bbox（事件到达时命中定格 2s） |
| `video_retrieval` | 监控反查 | 按自然语言在监控录像里反查目标片段 |
| `audio_analysis` | 音视频分析 | `hidden`，不显示在应用网格；被音视频描述的「音轨分析」面板调用（`/api/audio-analysis/*`） |

## 它是什么形态

**这是 wdnwebui 的插件，不是独立应用。** 卡片后端 `from app.core.*` import 宿主
的服务——`ComputeRegistry`（LLMSession 唯一来源）、`MultiChannelSession`、
`WdnaiDetector` 基类等。这些是 wdnwebui 设计原则 4/5 规定的调用路径，插件绕不过
去也不该绕。所以本 wheel 必须和 wdnwebui 在**同一个解释器**里，且 wdnwebui 仓库根
在 `sys.path` 上。

## 安装

宿主仓库根有一条 `update_rkwhl.sh`，它同时驱动四个 SDK 和两个卡片插件包。
**首选跑它**，别手动 pip：

```bash
cd <wdnwebui>
./update_rkwhl.sh                       # 按 git HEAD 决定哪些要重装
UPDATE_RKWHL_FORCE_REBUILD=1 ./update_rkwhl.sh   # 强制全部重装
```

脚本对本包做的事：HEAD 没变就完全不碰 `.venv`（连 pip 都不跑）；HEAD 变了
才 `purge_install_traces` + `pip install -e`。装完会跑一遍卡片注册校验
（`scripts/check_plugin_entrypoints.py`），逐张卡确认「entry point 能解析 →
manifest `"id"` 与 entry point 名逐字相同 → 卡片目录有 `static/`」。这挡的
是那个静默失败：pip 全程报成功，宿主启动却把这几张卡 FATAL 跳过。

手工装也可以（首次 clone 时脚本走的就是这条路）：

```bash
# 落在 wdnwebui 的 venv 里，与 wdnai/wdnav/wdndds 平级
cd <wdnwebui>/.venv/src
git clone git@github.com:rockchip-wardner/wdnapps_video_analyzer.git
pip install -e wdnapps_video_analyzer
```

宿主重启时扫描 `wdnwebui.cards` entry point 组，逐张卡片读 `manifest.json` 并
挂载路由 + `/static/apps/<id>` 静态目录。装完不必改宿主任何一行代码或配置。

> `pip install` 不会因为缺依赖而静默跳过——宿主侧的
> `_discover_from_entry_points()` 对每张卡单独 try/except，某张卡 import 失败
> 只 `LOG.error` 并跳过该卡，其余卡照常加载，web UI 不会因此起不来。
> 排错直接看宿主日志里的 `[<card_id>] plugin entry point ... not importable`。

## 加一张新卡

1. 建 `src/wdnapps_video_analyzer/cards/<新卡>/`，目录结构照抄任意一张现有卡：
   `__init__.py`（`register_api` / `register_shutdown`）、`api.py`、`service.py`、
   `manifest.json`、`static/{template.html,app.js,app.css,wizard.json}`。
2. 卡内 import 一律写全 `wdnapps_video_analyzer.cards.<新卡>.xxx`，**不要**写
   `app.apps.*`。
3. 在 `pyproject.toml` 注册 **两处**，缺一不可：
   - `[project.entry-points."wdnwebui.cards"]` 加 `<卡id> = "…cards.<新卡>"`
     ——左边必须与 `manifest.json` 的 `"id"` **逐字**相同，否则宿主判 FATAL 跳过。
     **这里用连字符不用下划线**：包目录是 `cards/video_detection/`（下划线），
     卡 id 是 `"video-detection"`（连字符），照着模块名写会全部注册失败
     （表现为日志里一串 `[video_detection] FATAL: manifest id 'video-detection'
     != entry-point name 'video_detection'`，且插件静默不出现）。
   - `[tool.setuptools.package-data]` 加该卡的 `["manifest.json", "static/*"]`
     ——不加的话静态资源不进 wheel，卡片能被发现但前端 404。

## 与宿主的边界

插件**只能** `from app.core.*`，不能 import 宿主的 `app.apps.*`（那是别的卡，
可能压根没装）。反向也不允许：core 不得依赖插件——这也是本包搬走时
`app/apps/video_detection/tts_alert.py` 被移进宿主 `app/core/tools/tts_alert.py`
的原因：`app/core/tools/_builtins.py` 要 import 它，而 `_builtins` 也被 detach 的
`compute_worker` 子进程 import，它一旦指向可选插件，插件没装时工具会静默消失。