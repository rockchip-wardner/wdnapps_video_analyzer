"""监控反查（video-retrieval）路由：检索 + 向导预览。

由卡片 ``__init__.py`` 挂载到 ``/api/video-retrieval/*``（扁平，无 /search 子前缀）。
业务逻辑在 ``app.core.video.search``（遵守卡片零跨卡片 import 规则）。
字段名（``chunk_seconds`` / ``start_time`` 等）与前端契约保持不变；切片
口径由 gscore 判据 + ``app/core/video/slicing.py`` 的 ``SLICE_*`` 常量
决定，匹配阈值 / 检索上限固化在 ``app/core/video/search.py``。
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import queue
import shutil
import threading
from pathlib import Path
from typing import Optional

from fastapi import (APIRouter, File, HTTPException, UploadFile, WebSocket,
                    WebSocketDisconnect)
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.config import default_resource_root
from app.core.video.frame_judge import (
    DEFAULT_LONG_SIDE,
    MODE_MANUAL,
    cost_ms,
    default_threshold,
    metrics,
)
from app.core.video.session import get_session
from app.core.video.search import (
    DEFAULT_JUDGE_EVERY_N,
    FIXED_SIM_THRESHOLD,
    FIXED_TOP_K,
    get_service,
)


LOG = logging.getLogger(__name__)
router = APIRouter(tags=["video-retrieval"])


preview_router = APIRouter(tags=["video-retrieval-preview"])

# 向导模式拖放上传的视频文件落地目录 — 三个视频 app 共用同一目录
# ``<resource_dir>/uploads``(设备端 ``/userdata/assets/uploads``)。
# 与 vd / vtd / vtl 完全一致;同名文件直接覆盖(``open("wb")`` 截断写)。
UPLOAD_DIR = default_resource_root() / "uploads"
try:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


# --------------------------------------------------------------------- #
# 向导模式预览 — 复用 vd 单例 VideoSession（vd / vtd / vtl / vr 共享）
# --------------------------------------------------------------------- #
@preview_router.post("/upload")
async def upload_video(file: UploadFile = File(...)) -> dict:
    """向导模式拖放上传 → 写到 ``UPLOAD_DIR``(与 vd / vtd / vtl 共用)。

    与 vd 的 ``/api/video-detection/upload`` 行为完全一致(由
    ``app.core.uploads`` 兜底),保证用户从任一视频应用拖放上传,
    在另一应用里直接用同一绝对路径不会再产生第二份副本。
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="empty filename")
    safe_name = Path(file.filename).name
    if not safe_name:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not Path(safe_name).suffix:
        safe_name = safe_name + ".bin"
    dest = UPLOAD_DIR / safe_name
    try:
        # 同名覆盖: 先 unlink 再 open("wb"),避免目标文件由其他用户/不可写
        # 拥有时 ``open("wb")`` 因需 inode 上的写权限而失败
        # (PermissionError: [Errno 13]);unlink 走的是目录级写权限,与
        # 新建文件一致。missing_ok=True 容忍首次上传时目标不存在。
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
        with dest.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"write failed: {e}")
    finally:
        try:
            file.file.close()
        except Exception:
            pass
    return {
        "path": str(dest),
        "name": file.filename,
        "size": dest.stat().st_size,
    }


@preview_router.post("/source/open")
async def open_source(payload: dict) -> dict:
    """向导模式预览:打开视频源(复用 vd 单例 VideoSession)。

    与 vd 的 ``/open`` 行为一致 —— ``get_session()`` 拿到的是 vd
    / vtd / vtl / vr 四页共享的同一个 ``VideoSession`` 单例;vr
    仅消费 WS 帧流、不注册 frame sink,不会与 vd 的 detector 抢帧。
    视频源路径可以是本地文件 / RTSP / RTMP / HLS / 摄像头,统一
    走 :func:`VideoSession.open` 路由。
    """
    payload = payload or {}
    source = (payload.get("source") or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="source required")
    loop = bool(payload.get("loop", False))
    try:
        return get_session().open(source, loop=loop)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"open failed: {e}")


@preview_router.post("/source/close")
async def close_source() -> dict:
    """向导模式预览:关闭视频源。

    与 vd 的 ``/close`` 等价;只是清理共享 session,不会杀掉分析侧
    / 检索侧的解码 / VLM 任务。
    """
    return get_session().close()


@preview_router.post("/source/control")
async def control_source(payload: dict) -> dict:
    """向导模式预览:播放 / 暂停 / 停止(复用 vd 单例 VideoSession)。

    与 vd 的 ``/control`` 完全同款 —— ``play`` → ``VideoSession.play()``,
    ``pause`` → ``pause()``,``stop`` → ``close()``(vd 的 stop 即为关源,
    这里保持一致,故 停止 与 关闭 等价)。
    """
    action = (payload or {}).get("action")
    if action == "play":
        return get_session().play()
    if action == "pause":
        return get_session().pause()
    if action == "stop":
        return get_session().close()
    raise HTTPException(status_code=400, detail=f"unknown action: {action!r}")


@preview_router.get("/source")
async def get_source_snapshot() -> dict:
    """向导模式预览:取当前预览 session 快照(状态 / 尺寸 / fps / loop)。"""
    return get_session().snapshot()


@preview_router.websocket("/source/ws")
async def ws_source(ws: WebSocket) -> None:
    """向导模式预览:JPEG 帧流(由 ``VideoSession`` 广播给每个订阅者)。

    与 ``/api/video-detection/ws`` 走同一个 ``VideoSession.subscribe()``,
    因此 vr 向导预览的实时性和 vd / vtd / vtl 严格一致(共享一份解码 +
    编码线程;每路 WS 各自一个订阅队列,broadcast 模式)。
    """
    await ws.accept()
    session = get_session()
    loop = asyncio.get_event_loop()
    sub_q = session.subscribe()
    last_status = 0.0
    eof_sent = False
    try:
        while True:
            try:
                item = await loop.run_in_executor(
                    None, lambda: sub_q.get(timeout=0.05)
                )
            except queue.Empty:
                item = None
            if item is None:
                now = loop.time()
                if now - last_status >= 1.0:
                    snap = session.snapshot()
                    snap["type"] = "status"
                    await ws.send_json(snap)
                    last_status = now
                continue
            if item is session._EOF:
                if not eof_sent:
                    snap = session.snapshot()
                    snap["type"] = "eof"
                    await ws.send_json(snap)
                    eof_sent = True
                await asyncio.sleep(0.2)
                continue
            eof_sent = False
            await ws.send_bytes(item)
            now = loop.time()
            if now - last_status >= 1.0:
                snap = session.snapshot()
                snap["type"] = "status"
                await ws.send_json(snap)
                last_status = now
    except WebSocketDisconnect:
        pass
    except Exception:
        LOG.warning("vr source/ws: abnormal disconnect", exc_info=True)
    finally:
        try:
            session.unsubscribe(sub_q)
        except Exception:
            pass





@router.get("/")
async def get_snapshot() -> dict:
    """当前快照（状态 / 进度 / 检索结果）。"""
    return get_service().snapshot()


@router.get("/cache/stats")
async def get_cache_stats() -> dict:
    """清空缓存按钮的提示条数来源：{enabled, max_entries, videos, rows}。"""
    from app.core.video.cache import get_video_cache
    c = get_video_cache()
    if c is None:
        return {"enabled": False, "max_entries": 0, "videos": 0, "rows": 0}
    return c.stats()


@router.get("/cache/list")
async def list_cache_entries() -> dict:
    """UI"已缓存视频"窗口数据源（按 analyzed_at 倒序）。

    每条 = (md5 × 切片参数) —— 同视频不同切片参数是不同行。
    ``source_path`` 为空（微信管线）或文件已不在本机的条目，
    ``frame_enabled=false`` —— 前端据此置灰取帧回看。
    """
    from app.core.video.cache import get_video_cache
    c = get_video_cache()
    if c is None:
        return {"enabled": False, "videos": []}
    # 窗口只显示 web 端（VCS）条目：渠道判别复用 source_path 双语义
    # （微信管线固定存 "" 占位 —— 见 wechat_video_cache.list_videos）。
    entries = c.list_videos(channel="web")
    for e in entries:
        e["frame_enabled"] = bool(
            e.get("source_path") and os.path.isfile(e["source_path"]))
    return {"enabled": True, "videos": entries}


@router.delete("/cache/entry")
async def delete_cache_entry(payload: dict) -> dict:
    """按行（md5 × 切片参数 × 判据口径）删除单个缓存条目。

    Body: ``{md5, chunk_seconds, chunk_overlap_seconds, judge_fp?,
    slice_version?}``。同视频其它切片参数 / 判据档的索引保留；旧前端
    不传判据字段时按 ``None``（不过滤）处理，保持向后兼容。
    """
    payload = payload or {}
    md5 = (payload.get("md5") or "").strip()
    chunk_seconds = int(payload.get("chunk_seconds") or 0)
    chunk_overlap_seconds = int(payload.get("chunk_overlap_seconds") or 0)
    judge_fp = payload.get("judge_fp")
    slice_version = payload.get("slice_version")
    if not md5:
        raise HTTPException(status_code=400, detail="md5 不能为空")
    from app.core.video.cache import get_video_cache
    c = get_video_cache()
    deleted = c.delete_video(
        md5, chunk_seconds, chunk_overlap_seconds,
        judge_fp=judge_fp, slice_version=slice_version) \
        if c is not None else 0
    # 与 /cache/clear 保持一致：如果删的是当前会话对应的缓存条目，必须
    # 作废内存索引 / 状态 —— 否则持久缓存已删、单例仍是 ready + 旧 _index，
    # 前端「开始 / 向导完成」会走 ready+同源短路，只查旧索引：进度条直接
    # 100%、检索有结果，但不会重新提取、也就不生成新的缓存条目（用户实测）。
    service = get_service()
    session_reset = False
    if getattr(service, "_md5", "") == md5:
        service.reset_session()
        session_reset = True
    LOG.info("vr API: DELETE /cache/entry md5=%s chunk=%s → %d rows "
             "(session_reset=%s)",
             md5[:12], chunk_seconds, deleted, session_reset)
    return {"ok": True, "rows_deleted": deleted,
            "session_reset": session_reset}


def _clean_cache_entries(entries: list) -> list:
    """Validate/dedupe the cache-entry keys used by the batch endpoint."""
    clean: list = []
    seen = set()
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        md5 = (e.get("md5") or "").strip()
        try:
            cs = int(e.get("chunk_seconds") or 0)
            ov = int(e.get("chunk_overlap_seconds") or 0)
        except (TypeError, ValueError):
            continue
        jfp = e.get("judge_fp")
        sver = e.get("slice_version")
        key = (md5, cs, ov, jfp, sver)
        if md5 and key not in seen:
            seen.add(key)
            clean.append({"md5": md5, "chunk_seconds": cs,
                          "chunk_overlap_seconds": ov,
                          "judge_fp": jfp, "slice_version": sver})
    return clean


def _run_cache_query_batch(service, cache, clean: list, queries: list,
                           check_quality: bool, min_results: int) -> list:
    """Execute 1..5 queries against selected cache entries.

    Cache lookup happens once per entry; each query is scored independently.
    Lenient fallback is per query, matching the legacy one-query semantics.
    """
    import numpy as np
    from app.core.video.search import _score_hits

    session = service._ensure_session()
    model_id = (getattr(session, "model_id", None)
                or getattr(session, "_model_id", "") or "")
    cached_entries = []
    for e in clean:
        try:
            cached = cache.lookup(
                e["md5"], model_id, e["chunk_seconds"],
                e["chunk_overlap_seconds"], judge_fp=e["judge_fp"],
                slice_version=e["slice_version"])
        except Exception as exc:  # noqa: BLE001
            LOG.warning("vr query_batch: lookup %s failed: %s",
                        e["md5"][:12], exc)
            cached = None
        cached_entries.append((e, cached))

    out = []
    for qi, q in enumerate(queries):
        text = (q.get("text") or "").strip()
        image_path = (q.get("image_path") or "").strip()
        image_name = os.path.basename(image_path) if image_path else ""
        item = {
            "index": qi,
            "text": text,
            "image_path": image_path,
            "image_name": image_name,
            "error": "",
            "groups": [],
        }
        if not text and not image_path:
            item["error"] = "请至少填写检索问题或选择一张图片"
            out.append(item)
            continue
        if text and len(text) > 50:
            item["error"] = str(ValueError("检索问题不能超过 50 个字"))
            out.append(item)
            continue
        try:
            qvec = service.encode_query(text, image_path, check_quality)
            qvec = np.asarray(qvec, dtype=np.float32).reshape(-1)
            qvec = qvec / (float(np.linalg.norm(qvec)) + 1e-9)
            groups = []
            index_by_gi = []
            total_above = 0
            for e, cached in cached_entries:
                if cached is None:
                    groups.append({
                        "md5": e["md5"], "miss": True, "filename": "",
                        "hits": [], "frame_enabled": False,
                    })
                    continue
                hits_, meta = _score_hits(
                    qvec, cached["index"], FIXED_SIM_THRESHOLD,
                    FIXED_TOP_K, 0)
                total_above += int(meta["above_threshold_count"])
                source_path = cached.get("source_path") or ""
                g = {
                    "md5": e["md5"],
                    "miss": False,
                    "filename": cached.get("filename") or "",
                    "source_path": source_path,
                    "frame_enabled": bool(source_path) and os.path.isfile(source_path),
                    "chunk_seconds": e["chunk_seconds"],
                    "chunk_overlap_seconds": e["chunk_overlap_seconds"],
                    "judge_fp": cached.get("judge_fp")
                                or (e["judge_fp"] if e["judge_fp"] is not None else ""),
                    "slice_version": cached.get("slice_version")
                                or (e["slice_version"] if e["slice_version"] is not None else ""),
                    "duration_s": cached.get("duration_s") or 0.0,
                    "slices": len(cached["index"]),
                    "hits": hits_,
                    "top_score": meta["top_score"],
                    "above_threshold_count": meta["above_threshold_count"],
                    "below_threshold_count": meta["below_threshold_count"],
                    "fallback_used": False,
                }
                groups.append(g)
                index_by_gi.append((g, cached["index"]))
            if min_results > 0 and total_above == 0 and index_by_gi:
                cands = []
                for gi, (g, index) in enumerate(index_by_gi):
                    if g["hits"]:
                        continue
                    fb, _ = _score_hits(
                        qvec, index, FIXED_SIM_THRESHOLD, FIXED_TOP_K,
                        min_results)
                    for h in fb:
                        if h.get("similarity", 0.0) >= FIXED_SIM_THRESHOLD:
                            continue
                        cands.append((float(h["similarity"]), gi, h))
                cands.sort(key=lambda t: -t[0])
                for _, gi, h in cands[:min_results]:
                    g = index_by_gi[gi][0]
                    h["above_threshold"] = False
                    g["hits"].append(h)
                    g["fallback_used"] = True
            item["groups"] = groups
        except ValueError as exc:
            item["error"] = str(exc)
        out.append(item)
    return out


@router.post("/query_batch")
async def query_batch(payload: dict) -> dict:
    """Unified single/multi query endpoint.

    Body: ``{queries: [{text?, image_path?}], entries?: [...],
    check_image_quality?, min_results?}``.  ``entries`` selects the cached
    multi-video path; otherwise the current ``VideoSession`` index is used.
    """
    payload = payload or {}
    raw = payload.get("queries")
    if raw is None:
        # Keep one-query body compatibility for callers that predate the UI.
        raw = [{"text": payload.get("text") or "",
                "image_path": payload.get("image_path") or ""}]
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="queries 必须是列表")
    if not 1 <= len(raw) <= 5:
        raise HTTPException(status_code=400, detail="queries 必须包含 1~5 组")
    queries = []
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="每个 query 必须是对象")
        queries.append({
            "text": (item.get("text") or "").strip(),
            "image_path": (item.get("image_path") or item.get("imagePath") or "").strip(),
        })
    entries = payload.get("entries") or []
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="entries 必须是列表")
    check_quality = bool(payload.get("check_image_quality", True))
    try:
        min_results = max(0, int(payload.get("min_results", 0) or 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="min_results 必须为数值")

    service = get_service()
    loop = asyncio.get_event_loop()
    timeout = min(180.0, max(35.0, 35.0 * len(queries)))
    if entries:
        from app.core.video.cache import get_video_cache
        cache = get_video_cache()
        if cache is None:
            raise HTTPException(status_code=409, detail="缓存未启用（cache_enabled=false 或 lancedb 不可用）")
        clean = _clean_cache_entries(entries)
        if not clean:
            raise HTTPException(status_code=400, detail="entries 里的 md5 均为空")
        call = functools.partial(
            _run_cache_query_batch, service, cache, clean, queries,
            check_quality, min_results)
        mode = "cache"
    else:
        call = functools.partial(
            service.query_batch, queries,
            check_image_quality=check_quality, min_results=min_results)
        mode = "source"

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, call), timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="检索超时（可能 NPU 繁忙）")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if mode == "cache":
        return {"ok": True, "mode": "cache", "queries": result}
    return {"ok": True, **result}


@router.post("/cache/clear")
async def clear_cache() -> dict:
    """一键删除 LanceDB 里全部已缓存的视频分析结果。

    幂等：缓存不可用 / 空表都返回 200（videos_cleared=0）。同时作废
    当前会话的内存索引（``get_service().reset_session()``）—— 持久缓存
    已删，会话索引若继续存在，前端 ``start_extract`` 会走 ``state=ready``
    短路跳过重新提取（表现为"进度条直接满格 + 历史无新条目"）。
    """
    from app.core.video.cache import get_video_cache
    c = get_video_cache()
    cleared = c.clear() if c is not None else 0
    get_service().reset_session()
    LOG.info("vr API: POST /cache/clear → %d entries (session reset)", cleared)
    return {"ok": True, "videos_cleared": cleared}


@router.post("/open")
async def open_source(payload: dict) -> dict:
    """选择视频源。Body: ``{source: str}``（本期只允许单个视频文件）。"""
    payload = payload or {}
    source = (payload.get("source") or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="source 不能为空")
    return get_service().open(source)


class JudgePayload(BaseModel):
    """``POST /judge`` 的请求体。

    **必须定义在模块级**：``from __future__ import annotations`` 让端点签名
    变成字符串，FastAPI 解析时只查模块 globals。字段缺失 = 沿用当前值
    （与三卡判据端点同契约）。
    """
    mode: Optional[str] = None
    every_n: Optional[int] = Field(default=None, ge=1, le=3600)
    threshold: Optional[float] = Field(default=None, gt=0, le=1000)


@router.get("/judge/metrics")
async def get_judge_metrics() -> dict:
    """下拉可选项 + 实测单帧成本 + 各算法默认阈值。

    字段与 ``/api/video/judge/metrics`` 逐字一致 —— 共享前端组件
    ``frame-judge.js`` 读的就是这套结构。额外给 ``default_every_n``
    （VR 的兜底间隔是秒级采样口径，与三卡的「解码帧」不同）。
    """
    ms = metrics()
    return {
        "mode": MODE_MANUAL,
        "metrics": ms,
        "cost_ms": {m: round(cost_ms(m) or 0.0, 2) for m in ms},
        "default_threshold": {m: default_threshold(m) for m in ms},
        "long_side": DEFAULT_LONG_SIDE,
        "default_every_n": DEFAULT_JUDGE_EVERY_N,
    }


@router.get("/judge")
async def get_judge() -> dict:
    return get_service().judge_snapshot()


def _sync_preview_judge(data: dict) -> None:
    """Apply the same judge settings to the shared preview session.

    VR's wizard preview runs on the shared ``VideoSession`` while slicing
    uses ``VideoSearchService._judge``. The curve reads the preview judge
    first, so a POST that only updated the search service would make the
    source-step curve disagree with the controls.
    """
    try:
        get_session().configure_judge(
            mode=data.get("mode"),
            every_n=data.get("every_n"),
            threshold=data.get("threshold"),
        )
    except Exception as exc:  # noqa: BLE001 — preview may be closed
        LOG.debug("video-retrieval: preview judge sync failed: %s", exc)


@router.post("/judge")
async def set_judge(payload: JudgePayload) -> dict:
    """配置判据（真节流）：改完立刻对后续采样生效。

    ``model_dump(exclude_unset=True)`` 保证「字段缺失 = 沿用当前值」；
    显式 ``mode=""`` 也视为沿用（与三卡端点同语义）。
    """
    data = payload.model_dump(exclude_unset=True)
    if isinstance(data.get("mode"), str) and not data["mode"].strip():
        data.pop("mode", None)
    result = get_service().set_judge(data)
    _sync_preview_judge(data)
    return result


@router.post("/judge/reset")
async def reset_judge() -> dict:
    """清空切片与预览判据的参照帧（不动配置）。"""
    result = get_service().reset_judge()
    try:
        get_session().reset_judge()
    except Exception as exc:  # noqa: BLE001
        LOG.debug("video-retrieval: preview judge reset failed: %s", exc)
    return result


@router.post("/extract")
async def start_extract(payload: dict) -> dict:
    """开始切片 embedding 提取。

    Body 不接受切片参数：``chunk_seconds`` / ``chunk_overlap_seconds``
    即使传入也会被忽略（改参数请改 ``app/core/video/search.py`` 的
    ``FIXED_CHUNK_SECONDS`` / ``FIXED_CHUNK_OVERLAP_SECONDS``）。
    """
    LOG.info("vr API: POST /extract payload=%s", payload)
    try:
        return get_service().start_extract()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        # wdnai 缺失 / 功能未启用（qwen3-vl-embedding-2b 实例未启动）
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/extract/stop")
async def stop_extract() -> dict:
    """停止切片提取。"""
    return get_service().stop_extract()


@router.websocket("/ws")
async def vr_ws(ws: WebSocket) -> None:
    """单向上行（server → client）。

    协议：
      - ``{"type":"hello", ...snapshot...}``  连上瞬间的初始快照
      - ``{"type":"progress", embedded, total}``  切片嵌入进度
      - ``{"type":"state", ...snapshot...}``  状态变更（完成 / 停止 / 出错）
    """
    await ws.accept()
    svc = get_service()
    loop = asyncio.get_event_loop()
    main_q: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=128)

    def _push(payload: dict) -> None:
        try:
            loop.call_soon_threadsafe(main_q.put_nowait, payload)
        except Exception:
            pass

    svc.register_broadcaster(_push)
    try:
        await ws.send_json({"type": "hello", **svc.snapshot()})
        while True:
            try:
                payload = await asyncio.wait_for(main_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            await ws.send_json(payload)
    except WebSocketDisconnect:
        return
    except Exception as e:  # pragma: no cover - defensive
        try:
            await ws.send_json({"type": "error", "detail": str(e)})
        except Exception:
            pass
    finally:
        svc.unregister_broadcaster(_push)


@router.websocket("/play")
async def vr_play_ws(ws: WebSocket) -> None:
    """播放命中片段：服务端按源 fps 顺序推 JPEG 帧。

    协议（双向）：
      客户端 → 服务端 首条 JSON：``{"source", "start", "end"}``
      服务端 → 客户端 binary：一帧 JPEG
      客户端 → 服务端：``{"action":"pause"}`` / ``{"action":"resume"}``
      服务端 → 客户端 JSON：``{"type":"eof"}`` / ``{"type":"error", detail}``

    后台 daemon 线程跑 ``iter_segment_jpeg``（PyAV 顺序解码 + JPEG 编码），
    经 ``loop.call_soon_threadsafe`` 投递到 asyncio 队列；WS 循环 drain。
    断开连接即停线程。暂停用 threading.Event 阻塞取帧循环。
    """
    from app.core.video.search import iter_segment_jpeg

    await ws.accept()
    loop = asyncio.get_event_loop()
    main_q: "asyncio.Queue" = asyncio.Queue(maxsize=16)
    pause_event = threading.Event()
    pause_event.set()   # 初始 = 未暂停；pause 时 clear()，resume 时 set()
    stop_event = threading.Event()
    thread: Optional[threading.Thread] = None
    get_task: Optional[asyncio.Future] = None
    recv_task: Optional[asyncio.Future] = None

    def _push_bytes(data: bytes) -> None:
        try:
            loop.call_soon_threadsafe(main_q.put_nowait, data)
        except Exception:
            pass

    def _push_json(payload: dict) -> None:
        try:
            loop.call_soon_threadsafe(main_q.put_nowait, ("json", payload))
        except Exception:
            pass

    def _reader(source: str, start: float, end: float) -> None:
        try:
            for jpeg in iter_segment_jpeg(source, start, end, paced=True,
                                          pause_event=pause_event):
                if stop_event.is_set():
                    return
                _push_bytes(jpeg)
            _push_json({"type": "eof"})
        except BaseException as e:
            LOG.warning("vr-play reader error: %s", e)
            _push_json({"type": "error", "detail": f"播放取帧失败: {e}"})

    try:
        # 等首条参数
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
        except Exception:
            await ws.close(code=1008)
            return
        source = (first.get("source") or "").strip()
        try:
            start = float(first.get("start", 0))
            end = float(first.get("end", start + 1))
        except (TypeError, ValueError):
            await ws.close(code=1008)
            return
        if not source or not os.path.isfile(source) or end <= start:
            await ws.send_json({"type": "error", "detail": "参数无效或文件不可访问"})
            await ws.close()
            return

        thread = threading.Thread(
            target=_reader, args=(source, start, end), name="vr-play", daemon=True
        )
        thread.start()

        # 帧队列 + 客户端控制消息**并发**等待：有帧立即发（不等控制，
        # 保持满帧率）；无帧时（暂停中）也能立刻响应 resume。
        # 旧实现"发帧后串行等控制"会在每帧叠加超时（帧率被拖慢），
        # 且超时 continue 会跳过收控制（暂停后收不到 resume）。
        recv_task = asyncio.ensure_future(ws.receive_json())
        while True:
            get_task = asyncio.ensure_future(main_q.get())
            done, _pending = await asyncio.wait(
                {get_task, recv_task},
                timeout=1.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if get_task in done:
                item = get_task.result()
                if isinstance(item, tuple):
                    await ws.send_json(item[1])
                    if item[1].get("type") in ("eof", "error"):
                        break
                else:
                    await ws.send_bytes(item)
            elif not get_task.done():
                get_task.cancel()

            if recv_task in done:
                try:
                    ctl = recv_task.result()
                except Exception:
                    break                    # 客户端断开
                action = (ctl or {}).get("action")
                if action == "pause":
                    pause_event.clear()      # 阻塞 reader（生成器内 wait）
                elif action == "resume":
                    pause_event.set()        # 放行 reader + 重置时钟基准
                recv_task = asyncio.ensure_future(ws.receive_json())
    except WebSocketDisconnect:
        return
    finally:
        for _t in (get_task, recv_task):
            if _t is not None and not _t.done():
                _t.cancel()
        # 先放行可能阻塞在 pause_event.wait() 的 reader（clear 会让它永久
        # 等待），再置 stop_event 让它尽快退出循环。
        pause_event.set()
        stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)


@router.get("/image/thumb")
async def get_image_thumb(path: str = "") -> Response:
    """查询图缩略图（只读；功能页/向导回显选中的图片）。

    与检索图走同一套路径校验（绝对路径、格式、大小上限），只返回
    长边 240 的 JPEG，不触发 NPU。
    """
    from app.core.video.search import read_image_thumb
    try:
        data = read_image_thumb(path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return Response(content=data, media_type="image/jpeg")


@router.get("/frame")
async def get_video_frame(source: str = "", t: float = 0.0) -> Response:
    """从源视频取指定时间点的帧（JPEG），用于命中卡片缩略图。

    Query: ``source``（视频文件路径）+ ``t``（绝对秒）。PyAV seek 到
    最近的 keyframe 后 decode 到目标时间点，RGA 硬转 BGR + JPEG 编码。
    """
    from fastapi.responses import Response

    source = (source or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="source 不能为空")
    if t < 0:
        raise HTTPException(status_code=400, detail="t 必须 >= 0")
    import os as _os
    if not _os.path.isfile(source):
        raise HTTPException(status_code=404, detail=f"视频文件不可访问: {source}")

    jpeg = _seek_frame_jpeg(source, t)
    if jpeg is None:
        raise HTTPException(status_code=404, detail="取帧失败（可能超出视频时长）")
    return Response(content=jpeg, media_type="image/jpeg")


def _seek_frame_jpeg(source: str, t: float) -> Optional[bytes]:
    """PyAV seek 到 ``t`` 秒附近的帧，JPEG 编码返回。失败返回 None。"""
    import av

    try:
        container = av.open(source, timeout=(2.0, 5.0))
    except Exception:
        return None
    try:
        stream = container.streams.video[0]
        time_base = float(stream.time_base)
        fps = float(stream.average_rate) if stream.average_rate else 0.0
        if time_base <= 0 or fps <= 0:
            return None
        target_frame = int(round(t * fps))
        offset = int((target_frame / fps) / time_base)
        try:
            container.seek(offset, backward=True, any_frame=False, stream=stream)
        except Exception:
            pass
        for frame in container.decode(video=0):
            if frame.pts is None:
                continue
            abs_time = float(frame.pts) * time_base
            frame_number = int(round(abs_time * fps))
            if frame_number >= target_frame:
                bgr = frame.to_ndarray(format="bgr24")
                import cv2
                ok, buf = cv2.imencode(
                    ".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
                )
                return bytes(buf) if ok else None
    except Exception as e:  # pragma: no cover - defensive
        LOG.debug("video-retrieval: 取帧 %s t=%s 失败: %s", source, t, e)
    finally:
        container.close()
    return None


@router.get("/history")
async def list_unified_history() -> dict:
    """统一历史记录（检索缓存 + 分析历史，按视频 md5 聚合）。

    前端「监控反查」页的历史列表数据源 —— 一个视频一行，行内
    ``retrieval`` 是该视频的检索索引档位列表、``analysis`` 是其分析
    记录列表（含 run_id，供加载 / 反查 / 删除）。按 ``updated_at``
    倒序；``frame_enabled`` 按源文件是否仍在本机判定（决定能否取帧 /
    播放）。
    """
    from app.core.video.cache import get_video_cache
    c = get_video_cache()
    if c is None:
        return {"enabled": False, "items": []}
    c.checkout_latest()
    items = c.list_history_grouped(channel="web")
    for it in items:
        sp = it.get("source_path") or ""
        it["frame_enabled"] = bool(sp and os.path.isfile(sp))
    return {"enabled": True, "items": items}


@router.post("/query/clear")
async def clear_query() -> dict:
    """清空最近一次检索结果（query / hits），前端"清空"按钮调用。"""
    return get_service().clear_query()
