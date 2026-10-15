#!/usr/bin/env sh
# Use an existing runtime. No package manager, download, or environment-file load.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON_BIN="${RAG_PYTHON:-python3}"
"${PYTHON_BIN}" -B -m zhiwiki doctor
exec "${PYTHON_BIN}" -B -m zhiwiki --config "${RAG_CONFIG:-config.example.json}" serve --port "${RAG_PORT:-8765}"
