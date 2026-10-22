"""doubao_eye 跨机器诊断脚本 —— 在出问题的电脑上运行，输出定位报告。

用法（在 doubao_eye 项目目录下）：
    python -X utf8 diagnose.py

分层定位（每层独立报告，失败不中断后续检查）：
  1. 环境版本：代码版本 / Windows build / Edge 版本（旧 Edge 不支持
     --headless=new 是强嫌疑）
  2. 指纹快照：WebGL 渲染器（软件渲染回退=强泄漏）/ UA-CH 完整版本号
     （读不到会退回 .0.0.0 假值）/ 屏幕几何 / plugins / AudioContext
  3. 上传测试（prepare_upload 四步）—— 限流时通常仍可用
  4. 纯文本对话（不带附件）—— 区分"设备/IP 级拉黑"与"附件通道限流"
  5. 带图对话 —— 完整链路，报出具体 error_code

把全部输出粘贴回来即可定位。
"""

import asyncio
import json
import subprocess
import sys
import time

RED_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKUlEQVR4nO3NsQ0AAAzCMP5/"
    "mj5RNkuZ4zSZtr0DAAAAAAAAAACA/gEHMsP8LofXz44AAAAASUVORK5CYII="
)


def section(title: str) -> None:
    print(f"\n{'=' * 62}\n== {title}\n{'=' * 62}", flush=True)


async def main() -> None:
    # ── 1. 环境版本 ──────────────────────────────────────────
    section("1. 环境版本")
    try:
        from doubao_eye import __version__
        print(f"代码版本: doubao_eye v{__version__}")
    except Exception as e:
        print(f"!! doubao_eye 包导入失败: {e}")
        print("   请在项目目录里运行（pip install -e . 后任意目录也可）")
        return

    import doubao_eye.browser as bm
    print(f"Python: {sys.version.split()[0]}  平台: {sys.platform}")
    try:
        wv = sys.getwindowsversion()
        print(f"Windows: build {wv.build}（platformVersion 应为 "
              f"{bm._windows_platform_version()}）")
    except Exception:
        print("Windows: 非 Windows 或读取失败")

    exe = bm.find_browser()
    print(f"浏览器: {exe or '未找到!'}")
    if exe:
        full = bm._browser_full_version(exe)
        print(f"浏览器完整版本: {full or '读取失败（→ 会退回 .0.0.0 假值!）'}")
        if full:
            major = int(full.split(".")[0])
            if major < 109:
                print(f"!! 警告: Edge {major} 不支持 --headless=new（109+ 才有），"
                      "旧 headless 指纹泄漏严重，请升级 Edge")
            else:
                print(f"   headless=new 可用（{major} >= 109）")

    # ── 2. 指纹快照 ──────────────────────────────────────────
    section("2. 指纹快照（重点看 WebGL 与 fullVersionList）")
    browser = bm.CdpBrowser()
    try:
        await browser.ensure_ready()
    except Exception as e:
        print(f"!! 浏览器启动失败: {e}")
        return

    try:
        fp = await browser.evaluate("""
        (() => {
          const out = {};
          out.ua = navigator.userAgent;
          try {
            out.chFull = (navigator.userAgentData.getHighEntropyValues
              ? 'async-see-below' : 'no-getHighEntropyValues');
          } catch (e) { out.chFull = String(e).slice(0, 60); }
          try {
            const gl = document.createElement('canvas').getContext('webgl');
            const ext = gl.getExtension('WEBGL_debug_renderer_info');
            out.webgl = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : 'no-ext';
          } catch (e) { out.webgl = String(e).slice(0, 80); }
          out.plugins = navigator.plugins.length;
          out.screen = screen.width + 'x' + screen.height;
          out.screenProto = Object.getOwnPropertyDescriptor(
            Object.getPrototypeOf(screen), 'width') ? 'proto-getter' : 'data';
          try { out.audio = (new AudioContext()).state; } catch (e) { out.audio = 'err'; }
          out.webdriver = navigator.webdriver;
          out.conn = navigator.connection
            ? navigator.connection.effectiveType : null;
          return out;
        })()
        """)
        for k, v in fp.items():
            print(f"  {k}: {v}")
        # 高熵 UA-CH（异步）
        he = await browser.evaluate("""
        (async () => {
          try {
            return await navigator.userAgentData.getHighEntropyValues(
              ['fullVersionList', 'platformVersion']);
          } catch (e) { return {err: String(e).slice(0, 80)}; }
        })()
        """)
        print(f"  fullVersionList: "
              f"{[b['brand'] + '=' + b['version'] for b in he.get('fullVersionList', [])]}")
        print(f"  platformVersion: {he.get('platformVersion')}")

        webgl = str(fp.get("webgl", ""))
        if "SwiftShader" in webgl or "Basic Render Driver" in webgl or "llvmpipe" in webgl:
            print("!! 泄漏嫌疑: WebGL 报告软件渲染 —— 自动化强信号")
            print("   （本机 GPU 驱动缺失/虚拟机/远程会话都会导致，"
                  "尝试更新显卡驱动或换真机）")
        fvl = str(he.get("fullVersionList", ""))
        if ".0.0.0" in fvl and "155.0.0.0" not in fvl:
            print("!! 泄漏嫌疑: fullVersionList 是 .0.0.0 假值（版本读取失败的回退）")
    except Exception as e:
        print(f"!! 指纹读取失败: {e}")

    # ── 3. 上传测试 ──────────────────────────────────────────
    section("3. 上传测试（限流状态下通常仍可用）")
    from doubao_eye.doubao import DoubaoSession
    session = DoubaoSession(browser)
    upload_ok = False
    try:
        import base64
        t0 = time.monotonic()
        up = await session.upload(base64.b64decode(RED_PNG), "diag_red.png",
                                  is_image=True)
        print(f"   上传成功（{time.monotonic() - t0:.1f}s）: {up['uri'][:52]}…")
        upload_ok = True
        diag_uri = up["uri"]
    except Exception as e:
        print(f"   上传失败: {str(e)[:160]}")
        diag_uri = None

    # ── 4. 纯文本对话（无附件）──────────────────────────────
    section("4. 纯文本对话（区分设备拉黑 vs 附件通道限流）")
    plain_ok = False
    try:
        t0 = time.monotonic()
        r = await session.chat("回复两个字：正常")
        print(f"   纯文本对话成功（{time.monotonic() - t0:.1f}s）: "
              f"{r.get('text', '')[:40]}")
        plain_ok = True
    except Exception as e:
        print(f"   纯文本对话失败: {str(e)[:200]}")

    # ── 5. 带图对话（完整链路）──────────────────────────────
    section("5. 带图对话（完整链路）")
    if upload_ok and diag_uri:
        try:
            t0 = time.monotonic()
            await asyncio.sleep(12)  # 正常人类节奏
            r = await session.chat(
                "这是什么颜色？只答颜色名",
                images=[{"uri": diag_uri, "name": "diag_red.png",
                         "width": 32, "height": 32, "format": "png"}])
            print(f"   带图对话成功（{time.monotonic() - t0:.1f}s）: "
                  f"{r.get('text', '')[:40]}")
            print("\n>>> 诊断结论: 全链路正常（若仍偶发限流，加大"
                  " DOUBAO_EYE_MIN_INTERVAL）")
        except Exception as e:
            msg = str(e)
            print(f"   带图对话失败: {msg[:200]}")
            if "710022004" in msg:
                print("\n>>> 诊断结论: 710022004 = 设备指纹被拉黑。")
                print("    纯文本" + ("也失败 → 指纹/IP 级问题，重点看第 2 节的泄漏嫌疑"
                      if not plain_ok else "成功 → 附件通道对指纹更敏感"))
            elif "710022002" in msg:
                print("\n>>> 诊断结论: 710022002 = 瞬时限流（服务端节奏窗口），")
                print("    不是指纹问题；等待 1-2 分钟后重试或调大 "
                      "DOUBAO_EYE_MIN_INTERVAL")
    else:
        print("   （跳过：上传未成功）")

    await browser.stop()

    section("诊断完成")
    print("请把以上全部输出粘贴回开发机会话。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
