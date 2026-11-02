"""video-depth card.

Manifest-driven backend entry point. The plugin loader (``app.core.manifest``)
imports this module and calls :func:`register_api` to mount the router
and :func:`register_shutdown` to register the cleanup hook.

Backend layout (per card)::

    cards/video_depth/
    ├── manifest.json      # plugin declaration
    ├── api.py             # FastAPI router (mounted at /api/video-depth)
    ├── static/            # template.html + app.js + app.css + wizard.json
    └── __init__.py        # this file — register_api / register_shutdown

The card may only import from ``app.core.*``. No cross-card imports.
"""
from __future__ import annotations

from fastapi import FastAPI

from wdnapps_video_analyzer.cards.video_depth.api import router

LOG = __import__("logging").getLogger("apps.video_depth")


def register_api(app: FastAPI) -> None:
    """Mount the card's FastAPI router at ``/api/video-depth``.

    **不注册 frame sink**。多路化之后本卡的帧由 ``MultiChannelSession``
    自带：工厂在 ``POST /prompt`` 里 ``attach_detector`` + 轮转调度器供给，
    并带着正确的 ``channel``。挂单例 ``VideoSession`` 的 sink 只会把别的卡
    （``video_retrieval`` 的单路预览）的帧以 channel=0 灌进本卡的检测器。
    """
    app.include_router(router, prefix="/api/video-depth")
    LOG.info("video_depth: API mounted at /api/video-depth")


def register_shutdown() -> None:
    """Release the card's service singletons (join worker threads).

    Best-effort: each cleanup call is wrapped in try/except so a single
    card's failure does not break the rest of the shutdown sequence.
    """
    import traceback
    try:
        from app.core.depth import stop_depth_detector
        stop_depth_detector()
    except Exception:  # noqa: BLE001
        LOG.warning("video_depth: stop_depth_detector failed")
        LOG.debug("%s", traceback.format_exc())
