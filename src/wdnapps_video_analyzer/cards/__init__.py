"""卡片集合。

每个子包都是一张独立的 wdnwebui 卡片，通过 ``pyproject.toml`` 里的
``[project.entry-points."wdnwebui.cards"]`` 逐个注册。**不要在这里
import 具体卡片**——宿主的 manifest 加载器是按 entry point 逐个
``import_module`` 的，在这里统一 import 会把全部卡片的重依赖
（视频解码、VLM session）在启动时一次性拉起来，既拖慢启动，也让某
张卡缺依赖时把整个包拖垮。
"""
from __future__ import annotations