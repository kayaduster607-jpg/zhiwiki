"""`python -m zhiwiki` 的入口保持轻量，核心命令不依赖 Web 框架。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
