"""视频深度估计 endpoints —— 本页特有的那一小块。

共用的 REST / WS 端点由 :func:`app.core.video.multi_api.build_multi_router`
生成；这里只剩「深度卡片专属」的两件事：

* 会话单例（key 隔离，见 :func:`app.core.video.multi_session.get_multi_session`）；
* ``parse_prompt`` —— 深度估计**没有文本输入**，所以这里不解析 prompt，
  只解析轮转节奏与勾选的实例。

送检尺寸为什么不覆盖
--------------------
``MultiChannelSession`` 的送检尺寸默认按源尺寸算
（:func:`~app.core.video.multi_session.derive_inference_size`，720p → 640
长边）。本页**不**强制覆盖成 DA3 的 280×280，理由：

* DA3 的输入是**固定 280×280 的拉伸**（``preprocess_image_rgb`` 是 plain
  stretch，不是 letterbox），没有「vision 变体」可选 —— 送检尺寸不决定
  模型精度档位（那是 VLM 的概念）；
* 两次轴对齐缩放可复合，所以「640×360 → 280×280」与「原图 → 280×280」
  在几何上等价，只是多一次重采样。而中间尺寸**越大**，重采样质量越高
  （640×360 → 280×280 是缩小；288×157 → 280×280 是先缩到 157 行再放大
  回 280 行，纵向细节先丢了一次）；
* 送检帧是**每次推理才产出一张**（不是每帧），640 长边的 RGA 开销在
  8.6 FPS 的推理节奏下可以忽略。

深度图回主进程后按**送检帧的宽高比**回拉（见
:class:`app.core.depth.depth_detector.DepthDetector`），左右两栏因此
覆盖同一个视场。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.core.depth import get_depth_detector
from app.core.video.multi_api import (
    MultiCardSpec,
    PromptPlan,
    build_multi_router,
    parse_models_field,
)
from app.core.video.multi_session import MultiChannelSession, get_multi_session

# 会话单例按卡隔离（见 get_multi_session 的 docstring）。
# **不能用别的卡的 key** —— 同一个进程里两张卡共用一个 MultiChannelSession
# 的话，vdp 的 open() 会把对方的解码器全 close 掉，表现为「切回上一页
# 画面全黑」。
SESSION_KEY = "vdp"


def _new_session() -> MultiChannelSession:
    """新建会话，并把它**默认判据**设成 manual（每帧都送检）。

    为什么深度页不要默认 gscore
    ----------------------------
    共享的 :class:`MultiChannelSession` 默认判据是 ``gscore``（画面结构残差
    超阈值才送检）。那是为 VLM 设计的 —— 一次 grounding 推理 1.66 s、一次
    VLM 对话 25 s，跳帧能省下**几十秒**，值得。

    DA3 单 view 只要 ~116 ms（比 grounding 快 14 倍），跳帧省不下什么，
    却会让右栏的深度画面在「画面变化不够明显」时**停住不动** —— 用户
    看不出是模型没跑还是场景真没变。

    所以深度页默认 manual：**每一轮轮转都送检**，节奏完全由模型速度决定
    （轮转本身已经是天然节流）。判据 UI 仍然保留 —— 用户想省算力时
    可以切回 gscore。

    注意这里改的是 ``_judge_proto``（**通道创建时的模板**），不是已有通道：
    ``configure_judge`` 只在**已有通道**上生效，而通道是在 ``open()`` 里
    按 proto 建的。在 factory 里设 proto，才能让首次 open 的通道就是
    manual。同时它只跑一次（factory 只在 key 首次创建时调用），用户之后
    用 UI 改判据不会被这里冲掉。
    """
    sess = MultiChannelSession()
    sess.configure_judge(mode="manual", every_n=1)
    return sess


def _session() -> MultiChannelSession:
    return get_multi_session(SESSION_KEY, _new_session)


def _parse_prompt(payload: dict) -> PromptPlan:
    """解析 ``POST /prompt`` 的 body —— 深度估计**没有 prompt**。

    只认两件事：

    * ``rr_interval`` —— 轮转间隔（秒/路），本路推理完后额外等多久再抢
      下一路。这是多路下真正的节奏旋钮。
    * ``models`` —— 勾选的实例列表（多卡并行，每条一个推理 slot）。

    老字段 ``every_n`` 作为 ``rr_interval`` 的兜底（与 vtl 同一套解读：
    「秒/路」）。**默认值取 0**（不停留，轮转跑满）—— DA3 单 view 推理约
    116 ms，多路轮转本身就够慢，再叠加固定间隔会让刷新明显发涩。
    """
    payload = payload or {}

    try:
        legacy_every_n = int(payload.get("every_n", 0) or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="every_n 必须为整数")

    raw_interval = payload.get("rr_interval", None)
    if raw_interval is None:
        rr_interval = float(legacy_every_n)
    else:
        try:
            rr_interval = float(raw_interval)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="rr_interval 必须是数字")

    return PromptPlan(
        # prompt 恒为空串：DepthDetector 显式忽略它（深度估计没有文本入口），
        # 但工厂的 start_kwargs 契约要求这个键存在。
        start_kwargs={"prompt": "", "enabled_tools": [],
                      "models": parse_models_field(payload)},
        rr_interval=rr_interval,
    )


router = build_multi_router(
    MultiCardSpec(
        session_getter=_session,
        detector_getter=get_depth_detector,
        parse_prompt=_parse_prompt,
    ),
    tag="video-depth",
)
