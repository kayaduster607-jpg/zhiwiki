"""在现有 Python 3.9 环境执行核心测试和真实字节码编译，不安装依赖。"""

import json
import py_compile
import sys
import unittest
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    # 应用层依赖不在 3.9 环境中，因此单独列出标准库核心的运行验收。
    patterns = ["test_store.py", "test_ingest.py", "test_lifecycle_integration.py",
                "test_query_consistency.py", "test_cli_core.py", "test_chunking.py", "test_seed.py",
                "test_retrieval_cache.py", "test_wiki_revision.py", "test_upload_versions.py",
                "test_benchmarks.py", "test_diagnostics.py"]
    for pattern in patterns:
        suite.addTests(loader.discover(str(root / "tests"), pattern=pattern))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    files = []
    for folder in ("zhiwiki", "tests", "scripts"):
        files.extend((root / folder).rglob("*.py"))
    for path in files:
        py_compile.compile(str(path), doraise=True)
    print(json.dumps({"python": sys.version.split()[0], "tests_run": result.testsRun,
                      "success": result.wasSuccessful(), "python_files_compiled": len(files),
                      "scope": "standard-library core; HTTP server requires verified 3.13 environment"}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
