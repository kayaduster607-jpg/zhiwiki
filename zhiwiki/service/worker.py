"""Supervised import process: checkpoints survive restarts; leases fence writers."""

import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import hashlib

from ..ingest import parse_upload
from .auth import Authorization
from .common import ServiceError
from .database import ServiceDatabase
from .jobs import Jobs


def verify_blob(path, expected, limit):
    if path.stat().st_size > limit:
        raise ServiceError(413, 'PAYLOAD_TOO_LARGE')
    checksum = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            checksum.update(block)
    if checksum.hexdigest() != expected:
        raise ServiceError(409, 'UPLOAD_HASH_MISMATCH')


def process_job(settings, job_id, fence):
    database = ServiceDatabase(settings)
    jobs = Jobs(database, Authorization(database))
    try:
        with database.tx() as connection:
            job = jobs.fenced(connection, job_id, fence)
            checkpoint = job["pages_done"]
            if not re.fullmatch(r"[0-9a-f]{64}", job["file_hash"]):
                raise ServiceError(422, "INVALID_ARTIFACT_HASH")
            path = database.artifact_path("uploads/" + job["file_hash"])
            filename = job["filename"]
        verify_blob(path, job['file_hash'], settings.max_upload_bytes)
        if filename.lower().endswith(".pdf"):
            import pypdf

            with pypdf.PdfReader(str(path)) as reader:
                if reader.is_encrypted and not reader.decrypt(""):
                    raise ServiceError(422, "PDF_ENCRYPTED")
                total = len(reader.pages)
                if total > settings.max_pdf_pages:
                    raise ServiceError(422, "PDF_TOO_MANY_PAGES")
                for index in range(checkpoint, total):
                    raw = reader.pages[index].extract_text() or ""
                    text = unicodedata.normalize("NFKC", raw)
                    quality = {}
                    if "\x00" in text:
                        quality["missing_glyphs"] = text.count("\x00")
                        text = text.replace("\x00", "\ufffd")
                    if not text.strip():
                        quality["empty"] = True
                    elif len(text.strip()) < 40:
                        quality["low_text"] = True
                    if len(text) > 500000:
                        raise ServiceError(422, "PAGE_TEXT_TOO_LARGE")
                    jobs.page(job_id, fence, index + 1, raw, text, quality, total)
        else:
            parsed = parse_upload(filename, path.read_bytes())
            jobs.page(job_id, fence, 1, parsed["text"], parsed["text"], {}, 1)
        jobs.finish(job_id, fence)
    except ServiceError as error:
        if error.code != "WORKER_LEASE_LOST":
            jobs.finish(job_id, fence, error.code)
    except Exception as error:
        # Public failure classification never includes raw PDF/vendor content.
        jobs.finish(job_id, fence, "PARSER_" + type(error).__name__.upper())
        raise


def supervise(settings, once=False):
    database = ServiceDatabase(settings)
    jobs = Jobs(database, Authorization(database))
    heartbeat = 0
    while True:
        if time.monotonic() - heartbeat >= 5:
            with database.tx(write=True) as connection:
                connection.execute("INSERT OR REPLACE INTO team_runtime VALUES('worker_heartbeat',?)", (str(time.time()),))
            heartbeat = time.monotonic()
        claimed = jobs.claim()
        if not claimed:
            if once:
                return
            time.sleep(0.5)
            continue
        # Only public non-secret settings enter this private runtime file.
        config = database.root / "worker-settings.json"
        from dataclasses import asdict
        config.write_text(json.dumps(asdict(settings)), encoding="utf-8")
        command = [settings.worker_python, "-B", "-m", "zhiwiki.service", "--config", str(config),
                   "process-job", claimed["id"], str(claimed["fence"])]
        options = {"cwd": str(__import__("pathlib").Path(__file__).resolve().parents[2]),
                   "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        child = subprocess.Popen(command, **options)
        started = time.monotonic()
        row = claimed
        while child.poll() is None:
            with database.tx() as connection:
                row = connection.execute("SELECT updated,cancel FROM team_jobs WHERE id=?", (claimed["id"],)).fetchone()
            if time.monotonic() - heartbeat >= 5:
                with database.tx(write=True) as connection:
                    connection.execute("INSERT OR REPLACE INTO team_runtime VALUES('worker_heartbeat',?)", (str(time.time()),))
                heartbeat = time.monotonic()
            if row["cancel"] or time.time() - row["updated"] > settings.page_timeout_seconds or time.monotonic() - started > settings.max_job_seconds:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                jobs.finish(claimed["id"], claimed["fence"], "IMPORT_DEADLINE_EXCEEDED")
                break
            time.sleep(0.2)
        if child.returncode and not row["cancel"]:
            jobs.finish(claimed["id"], claimed["fence"], "IMPORT_PROCESS_FAILED")
        if once:
            return
