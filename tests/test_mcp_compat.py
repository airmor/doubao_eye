"""mcp SDK 1.x/2.x 兼容层测试。

模拟 1.x 环境（block 掉 mcp.server.mcpserver、注入仿 FastMCP stub），
验证 doubao_eye.server 的 fallback import 与工具注册全链路。

运行：python -X utf8 tests/test_mcp_compat.py
"""

import asyncio
import sys
import types


def _install_1x_stub() -> None:
    """把当前进程伪装成 mcp 1.x：屏蔽 mcpserver、提供 fastmcp.FastMCP。"""

    class _FastMCP:
        """最小 1.x FastMCP 替身：构造参数与 @tool()/run() 与真实版一致。"""

        def __init__(self, name=None, instructions=None, lifespan=None, **kw):
            self.name = name
            self.instructions = instructions
            self.lifespan = lifespan
            self._tools = {}

        def tool(self, fn=None, **kw):
            def deco(f):
                self._tools[f.__name__] = f
                return f
            return deco(fn) if fn else deco

        async def list_tools(self):
            return list(self._tools)

        def run(self, transport="stdio", **kw):
            self._ran = transport

    # 屏蔽 2.x 路径：sys.modules 值为 None 时 import 会抛 ImportError
    sys.modules["mcp.server.mcpserver"] = None
    # 提供伪 1.x 模块
    fake = types.ModuleType("mcp.server.fastmcp")
    fake.FastMCP = _FastMCP
    sys.modules["mcp.server.fastmcp"] = fake


async def main() -> None:
    _install_1x_stub()

    # 强制重新加载，走 fallback import 路径
    for mod in [m for m in sys.modules if m.startswith("doubao_eye")]:
        del sys.modules[mod]

    import doubao_eye.server as srv

    # 1. 服务对象来自 fallback（FastMCP 替身）
    assert type(srv.server).__name__ == "_FastMCP", \
        f"server class = {type(srv.server).__name__}（应走 fastmcp fallback）"
    print("fallback import OK: server 是 FastMCP（1.x 形态）")

    # 2. 四个工具注册成功
    tools = await srv.server.list_tools()
    names = sorted(t if isinstance(t, str) else t.name for t in tools)
    assert names == ["look", "read", "status", "watch"], names
    print(f"tools registered OK: {names}")

    # 3. status 工具可调用（不启动浏览器）
    out = await srv.status()
    assert "doubao_eye" in out
    print("status() OK（1.x 形态下可正常调用）")
    print("\nALL 1.x COMPAT TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
