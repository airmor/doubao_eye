"""入口：``python -m doubao_eye`` 或安装后的 ``doubao-eye`` 命令。

以 stdio 传输运行 MCP 服务 —— 由客户端按需 spawn，随会话退出。
"""

import sys

from .server import server


def main() -> None:
    try:
        server.run("stdio")
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
