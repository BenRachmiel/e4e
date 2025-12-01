from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import asyncio
import hashlib
import os
import tempfile
import tarfile
import io
import base64
from pathlib import Path
from typing import Optional
from datetime import datetime

from builder import BuildQueue, BuildJob, BuildStatus

app = FastAPI(title="e4e-builder", version="0.1.0")

build_queue = BuildQueue()

CONFIG_CACHE = Path("/var/cache/e4e/configs")
CONFIG_CACHE.mkdir(parents=True, exist_ok=True)


class BuildRequest(BaseModel):
    packages: list[str]
    config_hash: str
    config: Optional[str] = None  # base64 encoded tarball, only if needed


class BuildResponse(BaseModel):
    build_id: str
    status: str
    need_config: bool = False


class BuildStatusResponse(BaseModel):
    build_id: str
    status: str
    packages: list[str]
    packages_built: list[str] = []
    log_tail: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None


@app.on_event("startup")
async def startup():
    """Start the build worker on startup."""
    asyncio.create_task(build_queue.worker())


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "queue_size": build_queue.queue.qsize()}


@app.post("/build", response_model=BuildResponse)
async def submit_build(request: BuildRequest):
    """Submit a build job."""
    config_path = CONFIG_CACHE / request.config_hash

    if not config_path.exists():
        if request.config is None:
            return BuildResponse(
                build_id="",
                status="need_config",
                need_config=True
            )

        # Decode and save config
        try:
            config_bytes = base64.b64decode(request.config)
            config_path.mkdir(parents=True, exist_ok=True)

            with tarfile.open(fileobj=io.BytesIO(config_bytes), mode='r:*') as tar:
                tar.extractall(config_path)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid config tarball: {e}")

    job = BuildJob(
        packages=request.packages,
        config_hash=request.config_hash,
        config_path=config_path
    )

    await build_queue.submit(job)

    return BuildResponse(
        build_id=job.build_id,
        status=job.status.value,
        need_config=False
    )


@app.get("/build/{build_id}")
async def get_build_status(build_id: str):
    """Get the status of a build job."""
    job = build_queue.get_job(build_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Build not found")

    return {
        "build_id": job.build_id,
        "status": job.status.value,
        "packages": job.packages,
        "packages_built": job.packages_built,
        "log_tail": job.get_log_tail(50),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error": job.error
    }


@app.get("/build/{build_id}/logs")
async def get_build_logs(build_id: str, lines: int = 100):
    """Get build logs."""
    job = build_queue.get_job(build_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Build not found")

    return {"log": job.get_log_tail(lines)}


@app.get("/build/{build_id}/artifact")
async def get_build_artifact(build_id: str):
    """Download the build artifact (tarball of binpkgs)."""
    job = build_queue.get_job(build_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Build not found")

    if job.status != BuildStatus.COMPLETE:
        raise HTTPException(
            status_code=400,
            detail=f"Build not complete, current status: {job.status.value}"
        )

    if not job.artifact_path or not job.artifact_path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")

    return FileResponse(
        job.artifact_path,
        media_type="application/x-tar",
        filename=f"binpkgs-{build_id}.tar"
    )


@app.get("/queue")
async def get_queue_status():
    """Get the current queue status."""
    return {
        "queue_size": build_queue.queue.qsize(),
        "current_job": build_queue.current_job.build_id if build_queue.current_job else None,
        "jobs": [
            {
                "build_id": job.build_id,
                "status": job.status.value,
                "packages": job.packages
            }
            for job in build_queue.jobs.values()
        ]
    }
