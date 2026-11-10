"""音频理解 REST + WS 端点。

错误码：400 (prompt/source 非法) / 404 (source 不存在) /
502 (capability 未启用) / 500 (其他)。

WS 协议：连接即推 ``hello``；状态变化推 ``state``；
推理完成推 ``event``；段落推进推 ``progress``。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from wdnapps_video_analyzer.cards.audio_analysis.service import get_service


LOG = logging.getLogger("wdnwebui.api.audio_analysis")
router = APIRouter(tags=["audio-analysis"])

# WebSocket 发送超时（秒）。对端半开（CLOSE-WAIT 且写缓冲排不空）时
# send 会永远挂着 ⇒ 必须有上限。详见 image_editor 卡的同款说明。
_WS_SEND_TIMEOUT = 5.0


class StartPayload(BaseModel):
    source: str | None = Field(
        default=None,
        description=("单路：视频文件绝对路径（向后兼容字段）。"
                     "与 sources 二选一"),
    )
    sources: list[str] | None = Field(
        default=None,
        description=("多路：1~32 条音频源，每条一路。"
                     "文件走顺序解码，rtsp:// 走环形缓冲实时取"),
    )
    prompt: str = Field(..., min_length=1, description="音频理解提示词")
    model_id: str | None = Field(
        default=None,
        description=("可选,从下拉框选的 swcap_audio_chat 实例 id;"
                     " 不传/空 = auto-pick (本机优先,旧行为)"),
    )

    def resolved_sources(self) -> list[str]:
        """``sources`` 优先；否则退回单值 ``source``。"""
        if self.sources:
            return [s.strip() for s in self.sources if s and s.strip()]
        if self.source and self.source.strip():
            return [self.source.strip()]
        return []


@router.get("/")
async def get_audio_analysis() -> dict:
    return get_service().snapshot()


@router.get("/events")
async def list_audio_analysis_events() -> dict:
    svc = get_service()
    return {
        "snapshot": svc.snapshot(),
        "events":   svc.list_events(),
    }


@router.post("/start")
async def start_audio_analysis(payload: StartPayload) -> dict:
    srcs = payload.resolved_sources()
    if not srcs:
        raise HTTPException(status_code=400, detail="source / sources 不能为空")
    try:
        return get_service().start(
            srcs, payload.prompt,
            model_id=payload.model_id,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:                                   # noqa: BLE001
        LOG.exception("start_audio_analysis failed")
        raise HTTPException(
            status_code=500,
            detail=f"{type(e).__name__}: {e}",
        ) from e


@router.post("/stop")
async def stop_audio_analysis() -> dict:
    return get_service().stop()


@router.post("/events/clear")
async def clear_audio_analysis_events() -> dict:
    get_service().clear_events()
    return {"ok": True}


@router.websocket("/ws")
async def audio_analysis_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    svc = get_service()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=128)

    def _push(msg: dict) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, msg)
        except Exception:                                     # noqa: BLE001
            pass

    svc.register_broadcaster(_push)

    async def _push_loop() -> None:
        """把队列里的消息推给前端。

        ⚠️ **send 必须加超时**（同仓 image_editor 2026-11-04 实测踩到）：
        对端「消失但不干净关闭」时（浏览器标签被杀 / 网断 / 进程被 kill），
        socket 会停在 CLOSE-WAIT 且**还有数据没发出去** ⇒ uvicorn 的
        ``await send`` 一直等写缓冲排空 ⇒ 这个协程**永远不返回** ⇒
        webui 关停时 ``server_state.tasks`` 一直非空 ⇒ 关停卡死。
        ⇒ 超时即视为「连接已死」⇒ 主动收尾。
        """
        while True:
            msg: Any = await queue.get()
            try:
                await asyncio.wait_for(
                    websocket.send_text(json.dumps(msg, ensure_ascii=False)),
                    timeout=_WS_SEND_TIMEOUT,
                )
            except (asyncio.TimeoutError, WebSocketDisconnect):
                return
            except Exception:                                 # noqa: BLE001
                return

    async def _watch_disconnect() -> None:
        """只读协程：客户端断开时抛 ``WebSocketDisconnect``。

        ⚠️ **必须同时「读」一次**（同仓 image_editor 2026-11-04 实测）：
        只写不读的协程**看不到 disconnect** —— 它不写就不报错 ⇒ 这个
        ASGI 任务永远不结束 ⇒ 每断一次线就漏一个 handler ⇒ 关停时
        ``server_state.tasks`` 越积越多 ⇒ 关停越来越慢，最后被 systemd
        强杀（连坐干掉 compute_worker）。
        """
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            return
        except Exception:                                     # noqa: BLE001
            return

    push = asyncio.create_task(_push_loop())
    watch = asyncio.create_task(_watch_disconnect())
    try:
        # 连接即推快照，让前端不必再额外 GET 一次
        await asyncio.wait_for(
            websocket.send_text(json.dumps(
                {"type": "hello", **svc.snapshot()},
                ensure_ascii=False,
            )),
            timeout=_WS_SEND_TIMEOUT,
        )
        # 推送协程与「只读」watcher 赛跑：谁先结束都收尾
        await asyncio.wait({push, watch},
                           return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        return
    except Exception:                                         # noqa: BLE001
        LOG.debug("audio_analysis ws loop error", exc_info=True)
        return
    finally:
        svc.unregister_broadcaster(_push)
        LOG.info("audio_analysis ws closed")
        for t in (push, watch):
            t.cancel()
        await asyncio.gather(push, watch, return_exceptions=True)