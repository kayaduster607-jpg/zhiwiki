"""Read-only runtime diagnostics; no installation or environment-file loading."""
import importlib.util
import platform
import sys
from importlib import metadata


PACKAGES = {"fastapi": "fastapi", "uvicorn": "uvicorn", "pydantic": "pydantic",
            "httpx": "httpx", "python-multipart": "multipart"}


def environment_report():
    packages = {}
    for distribution, module in PACKAGES.items():
        available = importlib.util.find_spec(module) is not None
        try:
            version = metadata.version(distribution) if available else None
        except metadata.PackageNotFoundError:
            version = None
        packages[distribution] = {"available": available, "version": version}
    missing = [name for name, value in packages.items() if not value["available"]]
    pydantic_version = packages["pydantic"]["version"] or ""
    return {"python": platform.python_version(), "executable": sys.executable,
            "platform": platform.system(), "core_ready": sys.version_info >= (3, 9),
            "server_dependencies_ready": not missing and pydantic_version.startswith("2."),
            "packages": packages, "missing": missing,
            "note": "Checks package presence and Pydantic major version; run tests to verify full compatibility. No packages installed."}
