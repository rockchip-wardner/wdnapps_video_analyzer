"""``wdnapps_video_analyzer`` —— wdnwebui 的视频分析类应用插件包。

提供 5 张卡片（均通过 ``wdnwebui.cards`` entry point 暴露给 wdnwebui 的
manifest 加载器）：

* ``video_detection``          —— 音视频描述
* ``video_target_detection``   —— 视频目标监控
* ``video_target_localization``—— 视频任意目标定位
* ``video_retrieval``          —— 监控反查
* ``audio_analysis``           —— 音视频分析（hidden，被音视频描述的音轨
  分析面板调用）

与宿主 wdnwebui 的关系
----------------------

本包是 **wdnwebui 的插件**，不是独立应用：卡片后端 ``from app.core.*``
import 宿主的服务（``ComputeRegistry`` / ``MultiChannelSession`` /
detector 基类等），这是 wdnwebui 的设计原则 4/5 要求的调用路径，
插件不得绕过它们直接调 wdnai / wdnav。因此本 wheel 必须与 wdnwebui
在同一个解释器里、且 wdnwebui 的仓库根在 ``sys.path`` 上——这正是
``pip install -e`` 到 ``.venv/src/``（与 wdnai/wdnav/wdndds 平级）
这种装法的用途。

卡片目录布局是刻意的：每个卡仍是 ``<card_dir>/{manifest.json, api.py,
service.py, static/}``，与它在 wdnwebui 仓库里时逐字一致。宿主按
``/static/apps/<id>`` → ``<card_dir>/static`` 挂载，所以**所有前端
URL、后端路由、wizard 字段 id 一个都没改**，只有 manifest.json 里的
``backend.module`` 换成了本包的模块路径。
"""
from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]