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

browser = CdpBrowser()
session = DoubaoSession(browser, chat_timeout=CHAT_TIMEOUT)


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
        hint = "（已自动重试仍失败：豆包风控生效中，请等待 3-5 分钟后再调用）"
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
        "问题请具体、可直接回答；需要多个附件时可分多次调用后自行综合。"
    ),
    lifespan=_lifespan,
)


# ── 工具实现 ─────────────────────────────────────────────────

async def _ask(kind: str, source: str, question: str,
               max_bytes: int, default_name: str,
               default_question: str) -> str:
    """通用流程：解析输入 → 上传 → 带附件对话 → 返回回答。

    内置风控自愈：
    - 710022002（瞬时限流）：退避 20 秒重试一次
    - 710022004（设备指纹被拉黑）：重置 profile（换新设备指纹）重试一次
    """
    if not source or not source.strip():
        return _err("缺少附件参数")
    question = (question or "").strip() or default_question

    try:
        if source.strip().startswith("tos-cn-i-"):
            ref = _tos_ref(source.strip(), kind)
        else:
            data, name = await _load_source(source, max_bytes, default_name)
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if kind == "image" and ext and ext not in IMAGE_EXTS:
                log.warning("look 收到非图片扩展名: %s（仍按图片发送）", ext)
            if kind == "video" and ext and ext not in VIDEO_EXTS:
                log.warning("watch 收到非视频扩展名: %s（仍按文件发送）", ext)
            uploaded = await session.upload(data, name, is_image=(kind == "image"))
            if kind == "image":
                w, h = _probe_image_size(data)
                ref = {"uri": uploaded["uri"], "name": name, "width": w,
                       "height": h, "format": ext or "png"}
            else:
                ref = {"uri": uploaded["uri"], "name": name,
                       "size": uploaded.get("size", len(data))}

        try:
            return await _chat_with_retry(kind, question, ref)
        except _RateLimited as exc:
            return _err(str(exc))
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        log.exception("工具调用失败")
        return _err(str(exc))


class _RateLimited(RuntimeError):
    """风控持续触发、重试后仍失败。"""


async def _chat_with_retry(kind: str, question: str, ref: dict) -> str:
    """执行对话，针对风控错误自动自愈重试。

    实测规律：匿名通道的附件调用有约 60-90 秒的间隔要求，因此 710022002
    的退避要足够长（75 秒）才能扛过限流窗口。
    """
    backoff = _env_float("DOUBAO_EYE_BACKOFF", 75.0)
    for attempt in range(2):
        try:
            if kind == "image":
                result = await session.chat(question, images=[ref])
            else:
                result = await session.chat(question, files=[ref])
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
async def look(image: str, question: str = "") -> str:
    """看一张图片并回答问题（给无视觉能力的模型当眼睛）。

    Args:
        image: 图片来源，支持三种格式：
            1. 本地文件绝对/相对路径（png/jpg/jpeg/webp/gif/bmp）
            2. http(s) URL
            3. 之前上传返回的 tos-cn-i-* URI
        question: 关于图片的问题；留空则默认"请详细描述这张图片的内容"。

    Returns:
        豆包对图片的文字回答（含 OCR、图表理解、推理等）。
    """
    return await _ask("image", image, question, MAX_IMAGE_BYTES,
                      "image.png", "请详细描述这张图片的内容。")


@server.tool()
async def read(file: str, question: str = "") -> str:
    """读取一个文件并回答问题（PDF/Word/Excel/PPT/代码/文本等 60+ 格式）。

    Args:
        file: 文件来源，支持三种格式：
            1. 本地文件路径（最大 50MB）
            2. http(s) URL
            3. 之前上传返回的 tos-cn-i-* URI
        question: 关于文件的问题；留空则默认"请总结这个文件的主要内容"。

    Returns:
        豆包基于文件内容的文字回答（摘要、问答、翻译、数据提取等）。
    """
    return await _ask("file", file, question, MAX_FILE_BYTES,
                      "file.txt", "请总结这个文件的主要内容。")


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
            "逐帧查看；音频部分可先转写为文本。"
        )
    # tos URI 或未知扩展：仍尝试发送（万一豆包后端支持）
    return await _ask("file", video, question, MAX_FILE_BYTES,
                      "video.mp4", "请描述这个视频的内容。")


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
