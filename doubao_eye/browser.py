"""轻量 CDP 浏览器驱动。

不依赖 Playwright：直接以 ``--remote-debugging-port`` 启动系统自带的
Edge / Chromium headless 实例，通过 WebSocket 说 Chrome DevTools Protocol。

设计要点（对应"按需唤起、平时零占用"的目标）：
- 懒启动：``ensure_ready()`` 第一次被调用时才拉起浏览器进程
- 空闲自动关闭：由上层 ``server.py`` 的 idle monitor 调用 ``stop()``
- 独立 user-data-dir：保持豆包匿名设备指纹 / cookie 稳定，避免每次
  重新风控
- 不传 ``--enable-automation``，``navigator.webdriver`` 为 false；
  另注入一小段 new-document 脚本做指纹加固
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

try:  # websockets >= 13
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover - 老版本回退
    from websockets import connect as ws_connect  # type: ignore

log = logging.getLogger(__name__)

DOUBAO_CHAT_URL = "https://www.doubao.com/chat/"

# 注入到每个新文档的指纹加固脚本（在页面脚本之前执行）。
# 原则：headless=new 模式原生已具备大部分真实特征（plugins、UA-CH、
# Notification 等），只修补真实残留的差异点，绝不引入真实浏览器没有的
# 东西 —— 多余的补丁本身就是泄漏（曾因伪造 plugins 破坏迭代器协议）。
# 实测（与正常浏览器逐项 diff）需要修补的仅有：
# - navigator.webdriver：headless 下为 true/undefined，真实为 false
# - screen 尺寸：headless 报告为窗口大小，真实为显示器分辨率
# - window.outerWidth/outerHeight：headless 与 inner 相同，真实含浏览器框
# - navigator.connection：headless 默认 3g/rtt150，真实桌面为 4g/低 rtt
# 屏幕分辨率与浏览器框高度由 Python 侧按运行机器探测后格式化进本模板
# （{{SCREEN_W}}/{{SCREEN_H}}/{{AVAIL_H}}/{{CHROME_H}}），不硬编码任何
# 具体机器的值。
_STEALTH_TEMPLATE = """
try {
  Object.defineProperty(navigator, 'webdriver', {get: () => false});
} catch (e) {}
try {
  Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
} catch (e) {}
try {
  Object.defineProperty(window, 'outerWidth', {get: () => window.innerWidth});
  Object.defineProperty(window, 'outerHeight', {get: () => window.innerHeight + {{CHROME_H}}});
} catch (e) {}
try {
  // headless 的 screen 来自虚拟显示器（默认 800x600），实例属性与 CDP
  // setDeviceMetricsOverride 都改不动 —— 覆盖原型 getter 才是彻底做法
  const sp = Object.getPrototypeOf(screen);
  const over = (obj, prop, val) => {
    try {
      Object.defineProperty(obj, prop, {get: () => val, configurable: true});
    } catch (e) {}
  };
  over(sp, 'width', {{SCREEN_W}});
  over(sp, 'height', {{SCREEN_H}});
  over(sp, 'availWidth', {{SCREEN_W}});
  over(sp, 'availHeight', {{AVAIL_H}});
  over(sp, 'colorDepth', 24);
  over(sp, 'pixelDepth', 24);
} catch (e) {}
try {
  if (navigator.connection) {
    Object.defineProperty(navigator.connection, 'effectiveType', {get: () => '4g', configurable: true});
    Object.defineProperty(navigator.connection, 'rtt', {get: () => 50, configurable: true});
    Object.defineProperty(navigator.connection, 'downlink', {get: () => 10, configurable: true});
    Object.defineProperty(navigator.connection, 'saveData', {get: () => false, configurable: true});
  }
} catch (e) {}
// AudioContext：真实浏览器无用户手势时 state 为 suspended，headless 默认
// running（autoplay 旗标对 AudioContext 不生效），需在构造时立即挂起
try {
  const OrigAC = window.AudioContext;
  const PatchedAC = function (...args) {
    const ctx = new OrigAC(...args);
    try { if (ctx.state === 'running') ctx.suspend().catch(() => {}); } catch (e) {}
    return ctx;
  };
  PatchedAC.prototype = OrigAC.prototype;
  Object.defineProperty(window, 'AudioContext', {value: PatchedAC, configurable: true});
} catch (e) {}
"""

# 页面就绪探测：豆包自己的 fetch hook（注入 a_bogus）已激活 + web_id 已生成
_READY_PROBE = """
(() => {
  const out = {hooked: false, web_id: false, device_id: ''};
  try { out.hooked = !window.fetch.toString().includes('native code'); } catch (e) {}
  try {
    out.device_id = JSON.parse(localStorage.getItem('samantha_web_web_id') || '{}').web_id || '';
  } catch (e) {}
  try {
    out.web_id = !!JSON.parse(localStorage.getItem('__tea_cache_tokens_497858') || '{}').web_id;
  } catch (e) {}
  return out;
})()
"""

# web_id 兜底：页面等待超时仍未生成时，写入合成的设备标识
_SYNTH_WEB_ID = """
(() => {
  const rid = () => {
    let s = '76';
    for (let i = 0; i < 17; i++) s += Math.floor(Math.random() * 10);
    return s;
  };
  try {
    if (!JSON.parse(localStorage.getItem('samantha_web_web_id') || '{}').web_id) {
      localStorage.setItem('samantha_web_web_id', JSON.stringify({web_id: rid()}));
    }
  } catch (e) {}
  try {
    const t = localStorage.getItem('__tea_cache_tokens_497858');
    if (!t || !JSON.parse(t).web_id) {
      localStorage.setItem('__tea_cache_tokens_497858', JSON.stringify({web_id: rid()}));
    }
  } catch (e) {}
  return true;
})()
"""


class BrowserError(RuntimeError):
    """浏览器生命周期相关错误。"""


class CdpError(RuntimeError):
    """CDP 调用返回 error。"""


class CdpClosed(ConnectionError):
    """CDP WebSocket 已断开。"""


def find_browser() -> Optional[str]:
    """定位系统 Edge / Chrome / Chromium 可执行文件。

    优先级：环境变量 ``DOUBAO_EYE_BROWSER`` > 常见安装路径 > PATH。
    """
    env_path = os.environ.get("DOUBAO_EYE_BROWSER", "").strip()
    if env_path and Path(env_path).exists():
        return env_path

    candidates: list[str] = []
    if sys.platform == "win32":
        for pfx in (r"C:\Program Files (x86)", r"C:\Program Files",
                    r"C:\Program Files (x86)\Microsoft\Edge\Beta",
                    os.path.expanduser(r"~\AppData\Local")):
            candidates += [
                rf"{pfx}\Microsoft\Edge\Application\msedge.exe",
                rf"{pfx}\Google\Chrome\Application\chrome.exe",
                rf"{pfx}\Chromium\Application\chrome.exe",
            ]
        candidates.append(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        candidates += [
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]

    for c in candidates:
        p = Path(os.path.expanduser(c))
        if p.exists():
            return str(p)

    for name in ("msedge", "chrome", "chromium", "google-chrome", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _screen_geometry() -> tuple[int, int, int, int]:
    """探测运行机器的真实屏幕几何 (width, height, availHeight, chrome_h)。

    - Windows：用 ctypes 查物理显示器分辨率；任务栏高度从工作区推算
    - macOS/Linux：读主显示器分辨率（ctypes/环境不可用时退回保守默认）
    - chrome_h：浏览器窗口装饰高度（标签栏+地址栏+边框），Edge/Chrome
      常见 ~87px —— 各浏览器差异不大，取该默认值

    所有值都是"这台机器的合理值"，不硬编码特定机型。
    """
    w, h, avail_h = 1920, 1080, 1040   # 保守默认（最常见桌面分辨率）
    if sys.platform == "win32":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()
            w = user32.GetSystemMetrics(0)
            h = user32.GetSystemMetrics(1)
            avail_h = user32.GetSystemMetrics(61)  # SM_CYWORKAREA
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            import re
            m = re.search(r"Resolution:\s*(\d+)\s*x\s*(\d+)", out)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                avail_h = h - 25   # macOS 菜单栏约 25px
        except Exception:
            pass
    else:
        # Linux：xdpyinfo 或环境变量兜底
        try:
            out = subprocess.run(
                ["xdpyinfo"], capture_output=True, text=True, timeout=10,
            ).stdout
            import re
            m = re.search(r"dimensions:\s*(\d+)x(\d+)", out)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                avail_h = h - 40   # 常见任务栏高度
        except Exception:
            pass
    if w <= 0 or h <= 0 or avail_h <= 0 or avail_h > h:
        w, h, avail_h = 1920, 1080, 1040
    return w, h, avail_h, 87


# ── 设备身份变动（reset_profile 时重新生成）─────────────────
#
# 背景：删 profile 只换 web_id/cookie，GPU/canvas/字体/屏幕等指纹完全
# 不变 —— 风控若按指纹聚类，"新身份"实为"同一设备清 cookie 重试"，
# 反而加重嫌疑。reset 时对指纹簇做合理范围内的变动（每一项仍是
# 真实存在的设备配置，且与运行机器的平台自洽）。

# 常见桌面显示器配置（宽，高，任务栏/菜单栏高度损失）
_DISPLAY_CONFIGS = [
    (1920, 1080, 40),   # 全高清 + Windows 任务栏
    (1920, 1080, 56),   # 全高清 + 任务栏放大
    (1536, 864, 40),    # 125% 缩放的 1920x1080
    (1600, 900, 40),    # 笔记本常见
    (1366, 768, 40),    # 入门笔记本
    (2560, 1440, 48),   # 2K
    (1680, 1050, 25),   # 16:10 / macOS
    (1440, 900, 25),    # MacBook Air 类
]

# 常见窗口装饰高度（标签栏+地址栏+边框，浏览器间差异小）
_CHROME_HEIGHTS = [80, 84, 87, 90, 94]


def _identity_path(profile_dir: str) -> Path:
    return Path(profile_dir).parent / "identity.json"


def _load_identity(path: Path) -> dict:
    """加载持久化的设备身份变体；不存在则生成并保存。

    变体在进程重启间保持稳定（同一"设备"不该每次启动换指纹），
    只有 reset_profile() 才重新生成。
    """
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "display" in data and "chrome_h" in data:
                return data
    except Exception:
        pass
    # 首个身份以本机真实屏幕为基础（探测失败才退回常见配置表）
    import random
    real_w, real_h, real_avail, _real_chrome = _screen_geometry()
    if (real_w, real_h) == (1920, 1080) and real_avail == 1040:
        # 无法区分"真实探测成功"与"默认回退值"，视为成功（默认值即
        # 最常见配置，两种情况都合理）
        taskbar = 40
    else:
        taskbar = real_h - real_avail
    identity = {
        "display": [real_w, real_h],
        "taskbar_loss": max(taskbar, 1),
        "chrome_h": random.choice(_CHROME_HEIGHTS),
        "seed": random.randint(1000, 9999),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(identity, indent=2), encoding="utf-8")
    except Exception:
        pass
    return identity


def _regenerate_identity(path: Path) -> dict:
    """删除持久化身份文件并生成新的（reset_profile 用）。"""
    with contextlib.suppress(Exception):
        path.unlink()
    return _load_identity(path)


def _build_stealth_js(identity: dict) -> str:
    """按设备身份（含屏幕几何变体）填充 stealth 模板。

    首个身份以运行机器的真实屏幕为基础（探测失败才用常见配置），
    reset 后的新身份从真实存在的显示器配置中随机取 —— 变动是
    "换了一台合理配置的机器"，而不是无法解释的随机值。
    """
    display = tuple(identity.get("display") or _screen_geometry()[:2])
    w, h = display
    taskbar = identity.get("taskbar_loss")
    if not taskbar:
        real = _screen_geometry()
        taskbar = real[1] - real[2]
    chrome_h = identity.get("chrome_h") or 87
    avail_h = max(h - taskbar, 1)
    return (_STEALTH_TEMPLATE
            .replace("{{SCREEN_W}}", str(w))
            .replace("{{SCREEN_H}}", str(h))
            .replace("{{AVAIL_H}}", str(avail_h))
            .replace("{{CHROME_H}}", str(chrome_h)))


def _needs_no_sandbox() -> bool:
    """判断是否需要 ``--no-sandbox``（仅 Linux/macOS 生效）。

    Chromium 家族在以下环境会拒绝以默认 sandbox 启动（进程秒退）：
    - root 用户（容器内常以 root 跑）
    - 无 ``DISPLAY`` 且无 ``WAYLAND_DISPLAY`` 的无头服务器
    - Docker 容器（无 user namespace 时 seccomp 配置不足）

    可用环境变量 ``DOUBAO_EYE_NO_SANDBOX=true/false`` 强制覆盖。
    桌面 Linux（有 DISPLAY）不加，避免不必要的 sandbox 降级。
    """
    if sys.platform == "win32":
        return False
    override = os.environ.get("DOUBAO_EYE_NO_SANDBOX", "").strip().lower()
    if override in ("1", "true", "yes"):
        return True
    if override in ("0", "false", "no"):
        return False
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    has_display = bool(os.environ.get("DISPLAY") or
                       os.environ.get("WAYLAND_DISPLAY"))
    in_container = Path("/.dockerenv").exists() or \
        Path("/run/.containerenv").exists()
    return not has_display or in_container


# Chromium GREASE 品牌轮换表（与真实浏览器一致的占位品牌）
_GREASE_BRANDS = ["Not_A Brand", "Not A(Brand", "Not?A_Brand",
                  "Not/A)Brand", "Not-A.Brand"]


def _windows_platform_version() -> str:
    """按真实 Windows build 号计算 UA-CH platformVersion。

    硬编码假值（如 15.0.0）会与真实系统（build 26100 → 19.0.0）不一致，
    成为跨信号矛盾点。
    """
    try:
        build = sys.getwindowsversion().build  # type: ignore[attr-defined]
        if build >= 26100:
            return "19.0.0"
        if build >= 22631:
            return "15.0.0"
        if build >= 22621:
            return "14.0.0"
        if build >= 22000:
            return "13.0.0"
        return "10.0.0"   # Win10 报告为 10.0.0 封顶
    except Exception:
        return "15.0.0"


def _browser_full_version(exe: str) -> Optional[str]:
    """读取浏览器真实完整版本号（如 155.0.2997.18）。

    UA 里只有 major（如 155.0.0.0），而 highEntropy fullVersionList 里
    真实浏览器报完整 build 号 —— 造 .0.0.0 假值是可识别的矛盾。
    Windows Edge 不响应 ``--version``，从 PE 文件版本信息读取。
    """
    import re

    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Item '{exe}').VersionInfo.FileVersion"],
                capture_output=True, text=True, timeout=20,
            ).stdout.strip()
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out)
            if m:
                return m.group(1)
        except Exception:
            pass
        return None
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=20,
        ).stdout
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def _client_hints_metadata(ua: str,
                           full_version: Optional[str] = None) -> dict:
    """从 UA 解析出 CDP ``Emulation.setUserAgentOverride`` 的
    ``userAgentMetadata``，使浏览器恢复发送真实的 Sec-CH-UA 客户端提示头。

    只覆盖 UA 而不给 metadata 时，Chromium 会丢弃全部 client hints 头 ——
    真实浏览器每个请求都带它们，缺失是显眼的自动化标记。
    """
    import re

    m_full = re.search(r"(?:Chrome|HeadlessChrome)/([\d.]+)", ua)
    major = (m_full.group(1) if m_full else "155.0.0.0").split(".")[0]
    full = full_version or (m_full.group(1) if m_full else "155.0.0.0")

    if "Windows NT" in ua:
        platform, platform_version = "Windows", _windows_platform_version()
    elif "Mac OS X" in ua:
        platform, platform_version = "macOS", "14.7.0"
    else:
        platform, platform_version = "Linux", "6.8.0"

    vendor = "Microsoft Edge" if "Edg/" in ua else "Google Chrome"
    # GREASE 品牌按主版本号轮换（与 Chromium 行为一致）
    grease = _GREASE_BRANDS[int(major) % len(_GREASE_BRANDS)] if major.isdigit() \
        else _GREASE_BRANDS[0]

    brands = [
        {"brand": "Chromium", "version": major},
        {"brand": grease, "version": "8"},
        {"brand": vendor, "version": major},
    ]
    full_brands = [
        {"brand": "Chromium", "version": full},
        {"brand": grease, "version": "8"},
        {"brand": vendor, "version": full},
    ]
    return {
        "brands": brands,
        "fullVersionList": full_brands,
        "fullVersion": full,
        "platform": platform,
        "platformVersion": platform_version,
        "architecture": "x86",
        "model": "",
        "mobile": False,
        "bitness": "64",
        "wow64": False,
    }


class _CdpConnection:
    """单页面的 CDP WebSocket 连接：请求-响应 + 事件等待。"""

    def __init__(self, ws):
        self._ws = ws
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._event_waiters: list[tuple[str, asyncio.Future]] = []
        self._closed = False
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut is not None and not fut.done():
                        if "error" in msg:
                            fut.set_exception(CdpError(str(msg["error"])[:300]))
                        else:
                            fut.set_result(msg.get("result", {}))
                elif "method" in msg:
                    for item in self._event_waiters[:]:
                        mname, fut = item
                        if mname == msg["method"] and not fut.done():
                            self._event_waiters.remove(item)
                            fut.set_result(msg.get("params", {}))
        except Exception as exc:  # 连接断开
            if not self._closed:
                log.warning("CDP websocket closed: %s", exc)
        finally:
            self._closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CdpClosed("CDP connection closed"))
            for _mname, fut in self._event_waiters:
                if not fut.done():
                    fut.set_exception(CdpClosed("CDP connection closed"))
            self._pending.clear()
            self._event_waiters.clear()

    @property
    def closed(self) -> bool:
        return self._closed

    async def call(self, method: str, params: Optional[dict] = None,
                   timeout: float = 30.0) -> dict:
        if self._closed:
            raise CdpClosed("CDP connection closed")
        mid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await self._ws.send(json.dumps({"id": mid, "method": method,
                                            "params": params or {}}))
        except Exception as exc:
            self._pending.pop(mid, None)
            raise CdpClosed(f"CDP send failed: {exc}") from exc
        return await asyncio.wait_for(fut, timeout)

    async def wait_event(self, method: str, timeout: float = 60.0) -> dict:
        if self._closed:
            raise CdpClosed("CDP connection closed")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = (method, fut)
        self._event_waiters.append(entry)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            if entry in self._event_waiters:
                self._event_waiters.remove(entry)

    async def evaluate(self, expression: str, timeout: float = 180.0) -> Any:
        """在页面里执行 JS（awaitPromise），返回值按值传回。"""
        res = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
        details = res.get("exceptionDetails")
        if details:
            desc = details.get("exception", {}).get("description") \
                or details.get("text", "unknown JS error")
            raise CdpError(f"页面 JS 执行失败: {desc[:300]}")
        return res.get("result", {}).get("value")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._ws.close()
        except Exception:
            pass
        self._reader.cancel()


class CdpBrowser:
    """按需启动 / 关闭的 headless Edge/Chromium 实例，持有一个豆包页面。

    所有对豆包的请求都通过 ``evaluate()`` 在页面上下文里用
    ``window.fetch`` 发出 —— 豆包前端 hook 会自动注入 ``a_bogus``
    签名，这正是绕过其反代防护的关键。
    """

    def __init__(
        self,
        exe: Optional[str] = None,
        profile_dir: Optional[str] = None,
        headless: Optional[bool] = None,
    ):
        self.exe = exe
        self.profile_dir = profile_dir or os.path.join(
            os.path.expanduser("~"), ".doubao_eye", "profile"
        )
        if headless is None:
            headless = os.environ.get("DOUBAO_EYE_HEADLESS", "true").strip().lower() \
                not in ("false", "0", "no")
        self.headless = headless

        self._proc: Optional[subprocess.Popen] = None
        self._conn: Optional[_CdpConnection] = None
        self._port: Optional[int] = None
        self._ready = False
        self._lifecycle_lock = asyncio.Lock()
        # 设备身份变体（屏幕几何/装饰高度等指纹簇参数）：
        # 持久化于 profile 旁的 identity.json，reset_profile 时重新生成
        self._identity: dict = _load_identity(_identity_path(self.profile_dir))

        # 状态（供 status 工具与日志使用）
        self.last_error: str = ""
        self.calls: int = 0
        self.started_at: float = 0.0
        self.last_active: float = time.monotonic()

    # ── 状态属性 ──────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def is_ready(self) -> bool:
        return self._ready and self.is_running and self._conn is not None \
            and not self._conn.closed

    def _touch(self) -> None:
        self.last_active = time.monotonic()

    # ── 生命周期 ──────────────────────────────────────────────

    async def ensure_ready(self) -> None:
        """确保浏览器已启动且豆包页面就绪（幂等，可反复调用）。"""
        self._touch()
        if self.is_ready:
            return
        async with self._lifecycle_lock:
            if self.is_ready:
                return
            await self._launch_locked()

    async def restart(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_locked()
            await self._launch_locked()

    async def reset_profile(self) -> None:
        """关闭浏览器、删除 profile 并重新生成设备身份（风控自愈手段）。

        三层设计（对照实验验证）：
        1. 只删 profile 换 web_id/cookie 不够 —— GPU/canvas/字体/屏幕等
           指纹不变，风控按指纹聚类时"新身份"实为"同一设备清 cookie
           重试"。同时重新生成指纹簇变体（屏幕几何从真实存在的显示器
           配置中随机取，窗口装饰高度小幅变动），相当于换了一台同平台
           的另一台机器。
        2. "同指纹簇换 ID"本身是风控信号 —— 实测新身份首发易吃 710022002，
           同一新身份的第二发立即成功。因此 reset 后强制冷却（默认 90
           秒，DOUBAO_EYE_RESET_COOLDOWN 可调）。
        3. 预热：冷却结束、真实请求发出前，先发一条内部探测消息把"换 ID
           后第一发"的软拒绝吃掉，让 agent 的第一发就是真请求（预热失败
           不阻塞 —— 最坏情况回退为旧行为：首发可能撞一次限流）。
        """
        async with self._lifecycle_lock:
            await self._stop_locked()
            import shutil
            with contextlib.suppress(Exception):
                shutil.rmtree(self.profile_dir, ignore_errors=True)
            self._identity = _regenerate_identity(
                _identity_path(self.profile_dir))
            cooldown = _env_float("DOUBAO_EYE_RESET_COOLDOWN", 90.0)
            log.info("设备身份已重置，冷却 %.0f 秒后重新启动", cooldown)
            await asyncio.sleep(cooldown)
            await self._launch_locked()
            log.warning("已重置浏览器 profile（%s），以新设备指纹重启",
                        self.profile_dir)
        # 预热在锁外执行（拿不到 ready 状态时锁内死等会卡其他调用方）
        await self._warmup()

    async def _warmup(self) -> None:
        """换身份后的预热：主动吃掉"新身份第一发"的软拒绝。

        实测模式：reset 后首发大概率 710022002（换 ID 观察窗），第二发
        即恢复。预热把这次必然的失败转移到内部消息上，agent 的第一发
        直接是可用状态。预热最多尝试 2 次、共约 8 秒，失败不影响服务
        （只是回退为"首条可能撞限流"的旧行为）。
        """
        try:
            await self.ensure_ready()
        except Exception:
            return
        from .doubao import DoubaoSession
        session = DoubaoSession(self, chat_timeout=20.0)
        for i in range(2):
            try:
                await session.chat("你好")
                log.info("新身份预热成功（第 %d 次）", i + 1)
                return
            except Exception as exc:
                log.info("新身份预热第 %d 次被拒（预期内）: %s",
                         i + 1, str(exc)[:80])
                await asyncio.sleep(4)

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        self._ready = False
        conn, self._conn = self._conn, None
        if conn is not None:
            await conn.close()
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            await asyncio.get_running_loop().run_in_executor(
                None, self._kill_proc, proc
            )
        self._port = None
        log.info("浏览器已关闭")

    @staticmethod
    def _kill_proc(proc: subprocess.Popen) -> None:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=15,
                )
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception as exc:
            log.warning("结束浏览器进程失败: %s", exc)

    # ── 启动 ──────────────────────────────────────────────────

    def _pidfile(self) -> Path:
        return Path(self.profile_dir).parent / "browser.pid"

    def _proc_matches_browser(self, pid: int) -> bool:
        """判断 pid 是否仍是浏览器进程（防止 pid 被复用误杀）。"""
        if pid <= 0:
            return False
        if sys.platform == "win32":
            try:
                # tasklist 在中文 Windows 输出 GBK，绝不用 text=True（默认 UTF-8 解码会崩）
                raw = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, timeout=10,
                ).stdout
                out = raw.decode("gbk", errors="replace").strip()
                if not out or out.upper().startswith("INFO"):
                    return False
                name = out.split('","')[0].strip().strip('"').lower()
                return any(k in name for k in ("msedge", "chrome", "chromium"))
            except Exception:
                return False
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip().lower()
            return any(k in comm for k in ("edge", "chrome", "chromium"))
        except Exception:
            try:
                os.kill(pid, 0)
                return True
            except Exception:
                return False

    def _kill_stale_by_pidfile(self) -> None:
        """清理上次异常退出残留的浏览器实例（同 profile 的浏览器是单例模式，
        残留实例会导致新进程启动后立即退出）。"""
        try:
            pid = int(self._pidfile().read_text(encoding="utf-8").strip())
        except Exception:
            return
        if self._proc_matches_browser(pid):
            log.info("清理上次残留的浏览器进程 pid=%d", pid)
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True, timeout=15,
                    )
                else:
                    os.kill(pid, signal.SIGTERM)
            except Exception as exc:
                log.warning("清理残留进程失败: %s", exc)
        try:
            self._pidfile().unlink()
        except Exception:
            pass

    def _find_orphan_pids(self) -> list[int]:
        """深扫占用本 profile 的孤儿浏览器进程（真实根因的自愈入口）。

        场景：宿主（MCP 客户端/DSH）重启 → 本服务新进程丢失对旧 Edge 的
        CDP 连接与 pidfile 归属（pidfile 只记录我们 spawn 的那个 PID），
        但旧 Edge 仍持有 profile 锁活着。下次启动时新 Edge 因同 profile
        单例机制让位退出（"立即退出"），pidfile 指向的 PID 已死 →
        _kill_stale_by_pidfile 找不到真凶 → 死锁。

        解法：按命令行 ``--user-data-dir=<本 profile>`` 全系统扫描，逐一
        校验命令行确实指向本 profile（绝不误杀用户其他浏览器）。
        """
        profile = str(Path(self.profile_dir).resolve()).lower()
        orphans: list[int] = []
        if sys.platform == "win32":
            try:
                raw = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-CimInstance Win32_Process -Filter "
                     "\"Name='msedge.exe' OR Name='chrome.exe' OR "
                     "Name='chromium.exe'\" | "
                     "Select-Object ProcessId, CommandLine | "
                     "ConvertTo-Json -Compress"],
                    capture_output=True, timeout=30,
                ).stdout
                out = raw.decode("utf-8", errors="replace").strip()
                if not out:
                    return []
                import json as _json
                data = _json.loads(out)
                if isinstance(data, dict):
                    data = [data]
                for item in data if isinstance(data, list) else []:
                    try:
                        pid = int(item.get("ProcessId", 0))
                        cmd = str(item.get("CommandLine") or "").lower()
                    except Exception:
                        continue
                    if pid <= 0 or pid == (self._proc.pid if self._proc else -1):
                        continue
                    if f"--user-data-dir={profile}" in cmd.replace('"', "") \
                            or f"--user-data-dir {profile}" in cmd.replace('"', ""):
                        orphans.append(pid)
            except Exception as exc:
                log.warning("孤儿进程扫描失败: %s", exc)
        else:
            # POSIX：遍历 /proc/*/cmdline
            try:
                for entry in Path("/proc").iterdir():
                    if not entry.name.isdigit():
                        continue
                    try:
                        cmdline = (entry / "cmdline").read_bytes() \
                            .decode("utf-8", errors="replace").lower()
                    except Exception:
                        continue
                    if f"--user-data-dir={profile}" in cmdline:
                        orphans.append(int(entry.name))
            except Exception as exc:
                log.warning("孤儿进程扫描失败: %s", exc)
        return orphans

    def _kill_orphans(self) -> int:
        """树杀所有占用本 profile 的孤儿进程，返回清理数。"""
        pids = self._find_orphan_pids()
        killed = 0
        for pid in pids:
            log.info("清理孤儿浏览器进程 pid=%d（持有本 profile 锁）", pid)
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True, timeout=15,
                    )
                    killed += 1
                else:
                    os.kill(pid, signal.SIGKILL)
                    killed += 1
            except Exception as exc:
                log.warning("清理孤儿 pid=%d 失败: %s", pid, exc)
        return killed

    async def _launch_locked(self) -> None:
        self._ready = False
        exe = self.exe or find_browser()
        if not exe:
            raise BrowserError(
                "找不到系统 Edge/Chrome。请安装 Microsoft Edge，或设置环境变量 "
                "DOUBAO_EYE_BROWSER 指向浏览器可执行文件。"
            )
        self.exe = exe

        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)

        last_exc: Optional[Exception] = None
        for attempt in range(3):
            log.info("浏览器启动尝试 %d/3", attempt + 1)
            self._kill_stale_by_pidfile()
            try:
                await self._spawn_and_attach(exe)
                log.info("浏览器启动成功（尝试 %d/3）", attempt + 1)
                return
            except BrowserError as exc:
                last_exc = exc
                if "立即退出" in str(exc):
                    # 新 Edge 让位退出 = profile 被孤儿实例持有。pidfile
                    # 只认我们 spawn 的 PID，此时指向已死 → 深扫命令行
                    # 匹配本 profile 的真凶并树杀，再重试
                    killed = self._kill_orphans()
                    log.warning("浏览器让位退出，深扫清理了 %d 个孤儿进程，"
                                "准备重试", killed)
                    await asyncio.sleep(2)
                    continue
                raise
        raise last_exc

    async def _spawn_and_attach(self, exe: str) -> None:
        port = _free_port()
        args = [
            exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--remote-allow-origins=*",
            # 不加 --disable-gpu：软件渲染会让 WebGL 报告
            # "Microsoft Basic Render Driver"，是自动化环境的强信号
            # 不加 --lang：该旗标会把 monospace 泛型字体解析成 CJK 字体，
            # 真实浏览器不走这条路径（语言由 CDP acceptLanguage 覆盖即可）
            "--autoplay-policy=user-gesture-required",
            "--window-size=1280,720",
            "about:blank",
        ]
        if self.headless:
            args.insert(1, "--headless=new")
        # Linux 无头环境（无 DISPLAY 的服务器 / root / Docker 容器）下
        # Chromium 家族拒绝以默认 sandbox 启动，按需放宽：
        # 仅在检测到这些环境时添加，桌面 Linux 不加（避免不必要的降级）
        if _needs_no_sandbox():
            args.insert(1, "--no-sandbox")
            args.insert(2, "--disable-dev-shm-usage")

        log.info("启动浏览器: %s (headless=%s, port=%d)", exe, self.headless, port)
        self._proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._port = port
        try:
            self._pidfile().write_text(str(self._proc.pid), encoding="utf-8")
        except Exception:
            pass

        try:
            ws_url, ua = await self._wait_devtools(port)
            ws = await ws_connect(ws_url, max_size=None, open_timeout=30)
            conn = _CdpConnection(ws)
            await conn.call("Page.enable")
            await conn.call("Runtime.enable")
            # headless 的 UA 带 "HeadlessChrome" 标记；且 CDP 覆盖 UA 时若
            # 不给 userAgentMetadata，浏览器会丢弃全部 Sec-CH-UA 客户端提示
            # 头 —— 真实浏览器每个请求都带这些头，缺失即自动化标记。
            # 因此必须同时覆盖 UA 与 client hints 元数据。
            if ua:
                clean_ua = (ua.replace("HeadlessChrome", "Chrome")
                               .replace("HeadlessEdg", "Edg"))
                try:
                    await conn.call("Emulation.setUserAgentOverride", {
                        "userAgent": clean_ua,
                        "acceptLanguage": "zh-CN,zh",
                        "userAgentMetadata": _client_hints_metadata(
                            clean_ua, _browser_full_version(exe)),
                    })
                    log.info("UA + client hints 已覆盖: %s", clean_ua[:80])
                except Exception as exc:
                    log.warning("UA 覆盖失败（继续运行）: %s", exc)
            # 指纹加固脚本：在每个新文档执行（在站点脚本之前）；
            # 屏幕几何来自设备身份（首个身份=本机真实值，reset 后=合理
            # 配置变体；覆盖 Screen 原型 getter —— headless 虚拟显示器
            # 默认 800x600，实例属性与 CDP setDeviceMetricsOverride 都
            # 改不动原型 getter）
            await conn.call("Page.addScriptToEvaluateOnNewDocument",
                            {"source": _build_stealth_js(self._identity)})

            # 导航到豆包并等待页面就绪
            load_task = asyncio.create_task(
                conn.wait_event("Page.loadEventFired", timeout=60)
            )
            await conn.call("Page.navigate", {"url": DOUBAO_CHAT_URL})
            try:
                await load_task
            except (asyncio.TimeoutError, CdpClosed):
                log.warning("等待页面 load 事件超时，继续探测就绪状态")

            await self._wait_page_ready(conn)
            self._conn = conn
            self._ready = True
            self.started_at = time.time()
            self.last_error = ""
            log.info("豆包页面就绪（免登录模式）")
        except Exception as exc:
            self.last_error = str(exc)
            await self._stop_locked()
            raise

    async def _wait_devtools(self, port: int) -> tuple[str, str]:
        """轮询 DevTools HTTP 端点。

        Returns:
            (页面 target 的 webSocketDebuggerUrl, 浏览器原始 User-Agent)
        """
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        async with httpx.AsyncClient(timeout=3) as http:
            version_json = None
            while time.monotonic() < deadline:
                if self._proc is not None and self._proc.poll() is not None:
                    raise BrowserError("浏览器进程启动后立即退出")
                try:
                    r = await http.get(f"{base}/json/version")
                    if r.status_code == 200:
                        version_json = r.json()
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            if version_json is None:
                raise BrowserError("DevTools 端点 30 秒内未就绪")
            ua = str(version_json.get("User-Agent", ""))

            # 找一个 page target
            page = None
            for _ in range(10):
                try:
                    r = await http.get(f"{base}/json")
                    targets = r.json()
                    if isinstance(targets, list):
                        page = next(
                            (t for t in targets if t.get("type") == "page"), None
                        )
                except Exception:
                    page = None
                if page:
                    break
                await asyncio.sleep(0.5)
            if page is None:
                # 新建一个 tab（新版 Chromium 要求 PUT）
                for method in ("put", "get"):
                    try:
                        r = await http.request(method.upper(),
                                               f"{base}/json/new?about:blank")
                        if r.status_code == 200:
                            data = r.json()
                            page = data[0] if isinstance(data, list) else data
                            if page:
                                break
                    except Exception:
                        continue
            if page is None or not page.get("webSocketDebuggerUrl"):
                raise BrowserError("未找到可用的页面 target")
            return page["webSocketDebuggerUrl"], ua

    async def _wait_page_ready(self, conn: _CdpConnection) -> None:
        """等待豆包 fetch hook 激活与 web_id 生成（最长 60 秒）。"""
        deadline = time.monotonic() + 60
        ok = False
        while time.monotonic() < deadline:
            try:
                state = await conn.evaluate(_READY_PROBE, timeout=10)
                if state.get("hooked") and state.get("web_id"):
                    ok = True
                    break
            except CdpClosed:
                raise
            except Exception:
                pass
            await asyncio.sleep(1.5)
        if not ok:
            # 兜底：合成 web_id（罕见——页面加载极慢时）
            log.warning("页面就绪探测超时，合成 web_id 兜底")
            try:
                await conn.evaluate(_SYNTH_WEB_ID, timeout=10)
            except Exception:
                pass

    # ── 对外接口 ──────────────────────────────────────────────

    async def evaluate(self, js: str, timeout: float = 180.0) -> Any:
        """确保就绪后在豆包页面执行 JS 并返回结果。断线自动重启一次。"""
        await self.ensure_ready()
        assert self._conn is not None
        self.calls += 1
        try:
            result = await self._conn.evaluate(js, timeout=timeout)
            self._touch()
            return result
        except (CdpClosed, ConnectionError) as exc:
            log.warning("CDP 断线，重启浏览器重试: %s", exc)
            self.last_error = str(exc)
            await self.restart()
            assert self._conn is not None
            return await self._conn.evaluate(js, timeout=timeout)
