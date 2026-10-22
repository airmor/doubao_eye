"""doubao_eye MCP 服务：look / read / watch / status 四个工具。

stdio 传输 —— 由 MCP 客户端（Claude Code、Claude Desktop 等）在需要时
spawn，会话结束即退出，无守护进程、无开机自启。浏览器实例同样只在
第一次实际工具调用时拉起，空闲可自动关闭。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import mimetypes
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from mcp.server.mcpserver import MCPServer

from . import __version__
from .browser import CdpBrowser, find_browser
from .doubao import DoubaoSession

# MCP stdio 协议走 stdout，所有日志必须走 stderr
logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("DOUBAO_EYE_LOG", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("doubao_eye")

MAX_IMAGE_BYTES = 20 * 1024 * 1024   # 20MB
MAX_FILE_BYTES = 50 * 1024 * 1024    # 50MB（TOS 单次上传上限）

IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "bmp"}
VIDEO_EXTS = {"mp4", "mov", "avi", "mkv", "webm", "flv", "wmv", "m4v", "3gp"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


IDLE_TIMEOUT = _env_float("DOUBAO_EYE_IDLE_TIMEOUT", 600.0)
CHAT_TIMEOUT = _env_float("DOUBAO_EYE_TIMEOUT", 150.0)
# 两次对话的最小间隔。指纹修复后实测 12 秒间隔连发 5 次全部成功
# （10 秒以上即为正常人类节奏）；默认 15 秒留少量余量。
MIN_CHAT_INTERVAL = _env_float("DOUBAO_EYE_MIN_INTERVAL", 15.0)
MAX_ATTACHMENTS = 9

browser = CdpBrowser()
session = DoubaoSession(browser, chat_timeout=CHAT_TIMEOUT)
_last_chat_at: float = 0.0
_pace_lock = asyncio.Lock()


# ── 输入解析：本地路径 / URL / data URI / tos URI ────────────

def _probe_image_size(data: bytes) -> tuple[int, int]:
    """读取 PNG/JPEG/GIF/WebP 的像素尺寸，失败返回 64x64。"""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            return (int.from_bytes(data[16:20], "big"),
                    int.from_bytes(data[20:24], "big"))
        if data[:2] == b"\xff\xd8":  # JPEG：扫描 SOF 段
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h = int.from_bytes(data[i + 5:i + 7], "big")
                    w = int.from_bytes(data[i + 7:i + 9], "big")
                    return w, h
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            return (int.from_bytes(data[6:8], "little"),
                    int.from_bytes(data[8:10], "little"))
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
            if data[12:16] == b"VP8X":
                return (1 + int.from_bytes(data[24:27], "little"),
                        1 + int.from_bytes(data[27:30], "little"))
            if data[12:16] == b"VP8 ":
                return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                        int.from_bytes(data[28:30], "little") & 0x3FFF)
            if data[12:16] == b"VP8L" and len(data) >= 25:
                b = data[21:25]
                w = 1 + ((b[0] | (b[1] << 8) | (b[2] << 16)) & 0x3FFF)
                h = 1 + (((b[2] >> 6) | (b[3] << 2)) & 0x3FFF)
                return w, h
    except Exception:
        pass
    return 64, 64


async def _load_source(source: str, max_bytes: int,
                       default_name: str) -> tuple[bytes, str]:
    """把输入源（本地路径 / http URL / data URI）读成 (bytes, filename)。"""
    source = source.strip()

    if source.startswith("data:"):
        try:
            header, encoded = source.split(",", 1)
        except ValueError as exc:
            raise ValueError("data URI 格式错误") from exc
        mime = header[5:].split(";", 1)[0] or "application/octet-stream"
        try:
            data = base64.b64decode(encoded)
        except Exception as exc:
            raise ValueError("data URI base64 解码失败") from exc
        name = default_name
        if mime and mime != "application/octet-stream":
            ext = mimetypes.guess_extension(mime)
            if ext:
                name = default_name + ext
        if len(data) > max_bytes:
            raise ValueError(f"内容超过大小限制 {max_bytes // 1024 // 1024}MB")
        return data, name

    if source.startswith(("http://", "https://")):
        from urllib.parse import urlparse
        path = urlparse(source).path
        name = path.rsplit("/", 1)[-1] if "/" in path else ""
        if not name or "." not in name:
            name = default_name
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as http:
            resp = await http.get(source)
            resp.raise_for_status()
            data = resp.content
        if len(data) > max_bytes:
            raise ValueError(f"下载内容超过大小限制 {max_bytes // 1024 // 1024}MB")
        return data, name

    # 本地路径
    path = Path(source).expanduser()
    if not path.exists():
        raise ValueError(f"文件不存在: {source}")
    if not path.is_file():
        raise ValueError(f"不是普通文件: {source}")
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"文件超过大小限制 {max_bytes // 1024 // 1024}MB "
                         f"（实际 {size // 1024 // 1024}MB）")
    data = path.read_bytes()
    if not data:
        raise ValueError("文件内容为空")
    return data, path.name


def _tos_ref(uri: str, kind: str) -> dict:
    """已上传的 tos URI 直接构造附件引用（不再重复上传）。"""
    tail = uri.rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1].lower() if "." in tail else ""
    if kind == "image":
        return {"uri": uri, "name": tail, "width": 64, "height": 64,
                "format": ext or "png"}
    return {"uri": uri, "name": tail, "size": 0}


def _err(msg: str) -> str:
    hint = ""
    if "710022004" in msg or "710022002" in msg or "风控" in msg:
        hint = "（已自动重试仍失败：豆包风控生效中，请稍等 1-2 分钟后再调用）"
    elif "登录" in msg:
        hint = "（免登录接口临时失效，请稍后重试）"
    elif "超时" in msg:
        hint = "（可设置环境变量 DOUBAO_EYE_TIMEOUT 增加等待秒数）"
    return f"[doubao_eye] 错误：{msg}{hint}"


# ── 空闲监控 ─────────────────────────────────────────────────

async def _idle_monitor() -> None:
    while True:
        await asyncio.sleep(15)
        try:
            if IDLE_TIMEOUT <= 0:
                continue
            if browser.is_running and \
                    time.monotonic() - browser.last_active > IDLE_TIMEOUT:
                log.info("浏览器空闲超过 %.0f 秒，自动关闭", IDLE_TIMEOUT)
                await browser.stop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("idle monitor 异常: %s", exc)


@asynccontextmanager
async def _lifespan(server):
    monitor = asyncio.create_task(_idle_monitor())
    try:
        yield {}
    finally:
        monitor.cancel()
        with contextlib.suppress(Exception):
            await monitor
        with contextlib.suppress(Exception):
            await browser.stop()


server = MCPServer(
    name="doubao_eye",
    instructions=(
        "doubao_eye 为不具备多模态能力的模型提供视觉："
        "看图用 look，读文档/代码/数据文件用 read，看视频用 watch（实验性）。"
        "每次调用 = 附件 + 一个问题，返回豆包的文字回答。"
        "重要：两次调用之间有约 1 分钟的服务端间隔限制 —— "
        "请尽量把要看的多个文件/图片合并进一次调用（look 传 images 列表、"
        "read 传 files 列表），一次提问全部解决，而不是逐个调用。"
        "问题请具体、可直接回答。"
    ),
    lifespan=_lifespan,
)


# ── 工具实现 ─────────────────────────────────────────────────

def _collect_sources(single: str, many: Optional[list[str]]) -> list[str]:
    """合并单数/复数参数，去空去重，保持顺序。"""
    seen: list[str] = []
    for s in [single or ""] + list(many or []):
        s = (s or "").strip()
        if s and s not in seen:
            seen.append(s)
    return seen


async def _materialize(kind: str, source: str, default_name: str) -> dict:
    """单个输入源 → 附件引用（tos URI 直引；本地/URL/data URI 则上传）。"""
    if source.startswith("tos-cn-i-"):
        return _tos_ref(source, kind)
    max_bytes = MAX_IMAGE_BYTES if kind == "image" else MAX_FILE_BYTES
    data, name = await _load_source(source, max_bytes, default_name)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if kind == "image" and ext and ext not in IMAGE_EXTS:
        log.warning("look 收到非图片扩展名: %s（仍按图片发送）", ext)
    if kind == "video" and ext and ext not in VIDEO_EXTS:
        log.warning("watch 收到非视频扩展名: %s（仍按文件发送）", ext)
    uploaded = await session.upload(data, name, is_image=(kind == "image"))
    if kind == "image":
        w, h = _probe_image_size(data)
        return {"uri": uploaded["uri"], "name": name, "width": w,
                "height": h, "format": ext or "png"}
    return {"uri": uploaded["uri"], "name": name,
            "size": uploaded.get("size", len(data))}


async def _run(question: str, items: list[tuple[str, str]],
               default_question: str) -> str:
    """通用流程：解析多个输入 → 逐个上传 → 一次对话带全部附件。

    附件上传不受匿名通道限流约束，稀缺的只有 chat/completion 一次调用，
    因此多附件合并进一次对话能把等待时间从 N 倍间隔塌缩为一次间隔。
    """
    if not items:
        return _err("缺少附件参数")
    question = (question or "").strip() or default_question
    if len(items) > MAX_ATTACHMENTS:
        return _err(f"单次调用最多 {MAX_ATTACHMENTS} 个附件（收到 {len(items)} 个），"
                    "请分批调用")

    try:
        images: list[dict] = []
        files: list[dict] = []
        for kind, source in items:
            ref = await _materialize(kind, source,
                                     "image.png" if kind == "image" else "file.txt")
            (images if kind == "image" else files).append(ref)
        try:
            return await _chat_with_retry(question, images, files)
        except _RateLimited as exc:
            return _err(str(exc))
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        log.exception("工具调用失败")
        return _err(str(exc))


class _RateLimited(RuntimeError):
    """风控持续触发、重试后仍失败。"""


async def _pace() -> None:
    """主动限速：保证两次对话的间隔不小于 MIN_CHAT_INTERVAL。

    与其撞上 710022002 后被动退避，不如发请求前就把间隔等足 —— agent
    侧只需一次可预期的等待，不会看到错误。锁保证并发调用时排队等待，
    不会同时放行。
    """
    global _last_chat_at
    async with _pace_lock:
        elapsed = time.monotonic() - _last_chat_at
        wait = MIN_CHAT_INTERVAL - elapsed
        if wait > 0:
            log.info("主动限速：距上次对话 %.0fs，等待 %.0fs 后发送",
                     elapsed, wait)
            await asyncio.sleep(wait)
        _last_chat_at = time.monotonic()


async def _chat_with_retry(question: str, images: list[dict],
                           files: list[dict]) -> str:
    """执行对话：先主动限速，再针对风控错误自动自愈重试。

    指纹修复后 10 秒以上间隔即为正常节奏；主动限速（默认 15s）挡住
    绝大多数情况，偶发 710022002 时退避重试一次。
    """
    global _last_chat_at
    backoff = _env_float("DOUBAO_EYE_BACKOFF", 45.0)
    for attempt in range(2):
        try:
            await _pace()
            result = await session.chat(question, images=images, files=files)
            return result.get("text", "")
        except RuntimeError as exc:
            msg = str(exc)
            if "710022002" in msg and attempt == 0:
                # 瞬时限流：退避后原样重试
                log.warning("豆包瞬时限流（710022002），%.0f 秒后重试", backoff)
                await asyncio.sleep(backoff)
                continue
            if "710022004" in msg and attempt == 0:
                # 设备指纹被拉黑：换新设备指纹重试
                log.warning("设备指纹被风控（710022004），重置 profile 重试")
                try:
                    await browser.reset_profile()
                except Exception as reset_exc:
                    raise _RateLimited(
                        f"设备被风控且 profile 重置失败: {reset_exc}"
                    ) from exc
                continue
            raise
    raise _RateLimited("风控持续触发，请稍等几分钟后再试")


@server.tool()
async def look(image: str = "", question: str = "",
               images: Optional[list[str]] = None) -> str:
    """看图片并回答问题（给无视觉能力的模型当眼睛）。

    支持一次看多张图：传 images 列表时所有图片合并进一次提问
    （推荐 —— 服务的调用间隔约 1 分钟，批量只需等一次）。

    Args:
        image: 单张图片来源：本地路径（png/jpg/jpeg/webp/gif/bmp）、
            http(s) URL、或之前上传返回的 tos-cn-i-* URI。
        images: 多张图片来源列表（与 image 二选一，最多 9 张）。
        question: 关于图片的问题，多图时请指明如何对比/综合；留空则默认
            "请详细描述这张图片的内容"（多图时逐张描述）。

    Returns:
        豆包对图片的文字回答（含 OCR、图表理解、推理等）。
    """
    sources = _collect_sources(image, images)
    if not sources:
        return _err("缺少附件参数：请提供 image 或 images")
    if len(sources) > 1 and not (question or "").strip():
        question = "请逐张详细描述这些图片的内容。"
    return await _run(question, [("image", s) for s in sources],
                      "请详细描述这张图片的内容。")


@server.tool()
async def read(file: str = "", question: str = "",
               files: Optional[list[str]] = None) -> str:
    """读取文件并回答问题（PDF/Word/Excel/PPT/代码/文本等 60+ 格式）。

    支持一次读多个文件：传 files 列表时所有文件合并进一次提问
    （推荐 —— 服务的调用间隔约 1 分钟，批量只需等一次）。

    Args:
        file: 单个文件来源：本地路径（最大 50MB）、http(s) URL、
            或之前上传返回的 tos-cn-i-* URI。
        files: 多个文件来源列表（与 file 二选一，最多 9 个）。
        question: 关于文件的问题，多文件时请说明要对比/汇总什么；留空则
            默认"请总结这个文件的主要内容"（多文件时逐个总结）。

    Returns:
        豆包基于文件内容的文字回答（摘要、问答、翻译、数据提取等）。
    """
    sources = _collect_sources(file, files)
    if not sources:
        return _err("缺少附件参数：请提供 file 或 files")
    if len(sources) > 1 and not (question or "").strip():
        question = "请逐个总结这些文件的主要内容。"
    return await _run(question, [("file", s) for s in sources],
                      "请总结这个文件的主要内容。")


@server.tool()
async def watch(video: str, question: str = "") -> str:
    """观看一个视频并回答问题（实验性）。

    注意：豆包网页版聊天目前不支持视频附件输入（实测其文件选择器
    仅接受文档与图片格式），因此本工具对多数视频会返回"不支持"说明。
    可以尝试对视频截图后改用 look。

    Args:
        video: 视频来源：本地路径（mp4/mov/webm 等，最大 50MB）、http(s) URL
            或 tos-cn-i-* URI。
        question: 关于视频的问题；留空则默认"请描述这个视频的内容"。

    Returns:
        豆包对视频的文字描述/回答；不支持时返回明确说明与替代建议。
    """
    src = (video or "").strip()
    # 从路径/URL 推断扩展名
    tail = src.split("?")[0].rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1].lower() if "." in tail else ""
    if ext and ext in VIDEO_EXTS and not src.startswith("tos-"):
        return (
            "[doubao_eye] 豆包网页版聊天不支持视频附件输入（其上传接口仅接受"
            "文档与图片格式），无法直接观看视频文件。\n"
            "建议：先用工具对视频截取关键帧（如 ffmpeg -i video.mp4 -vf "
            "\"select=eq(n\\,0,30,60)\" -vsync vfb frames.jpg），再用 look 工具"
            "（可传 images 列表一次看多帧）；音频部分可先转写为文本。"
        )
    # tos URI 或未知扩展：仍尝试发送（万一豆包后端支持）
    return await _run(question, [("file", video)],
                      "请描述这个视频的内容。")


@server.tool()
async def status() -> str:
    """查看 doubao_eye 服务状态（不会启动浏览器，可放心调用）。"""
    exe = find_browser()
    info = {
        "service": "doubao_eye",
        "version": __version__,
        "mode": "anonymous（豆包免登录网页版）",
        "browser": {
            "found": bool(exe),
            "path": exe or "未找到（可设置 DOUBAO_EYE_BROWSER）",
            "running": browser.is_running,
            "ready": browser.is_ready,
            "headless": browser.headless,
        },
        "profile_dir": browser.profile_dir,
        "calls_served": browser.calls,
        "last_error": browser.last_error or "",
        "config": {
            "idle_timeout_s": IDLE_TIMEOUT,
            "chat_timeout_s": CHAT_TIMEOUT,
            "min_chat_interval_s": MIN_CHAT_INTERVAL,
            "max_attachments_per_call": MAX_ATTACHMENTS,
            "max_image_mb": MAX_IMAGE_BYTES // 1024 // 1024,
            "max_file_mb": MAX_FILE_BYTES // 1024 // 1024,
        },
    }
    if browser.is_running and browser.started_at:
        info["browser"]["uptime_s"] = round(time.time() - browser.started_at)
    lines = [f"service: doubao_eye v{__version__}（免登录模式）"]
    for key, value in info.items():
        if key in ("service", "version", "mode"):
            continue
        if isinstance(value, dict):
            lines.append(f"{key}:")
            lines.extend(f"  {k}: {v}" for k, v in value.items())
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)
