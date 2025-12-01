import asyncio
import os
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class BuildStatus(Enum):
    QUEUED = "queued"
    BUILDING = "building"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class BuildJob:
    packages: list[str]
    config_hash: str
    config_path: Path
    build_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: BuildStatus = BuildStatus.QUEUED
    packages_built: list[str] = field(default_factory=list)
    log: str = ""
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    artifact_path: Optional[Path] = None

    def get_log_tail(self, lines: int = 50) -> str:
        """Get the last N lines of the log."""
        log_lines = self.log.splitlines()
        return "\n".join(log_lines[-lines:])

    def append_log(self, text: str):
        """Append text to the build log."""
        self.log += text


class BuildQueue:
    def __init__(self):
        self.queue: asyncio.Queue[BuildJob] = asyncio.Queue()
        self.jobs: dict[str, BuildJob] = {}
        self.current_job: Optional[BuildJob] = None

    async def submit(self, job: BuildJob):
        """Submit a job to the queue."""
        self.jobs[job.build_id] = job
        await self.queue.put(job)

    def get_job(self, build_id: str) -> Optional[BuildJob]:
        """Get a job by ID."""
        return self.jobs.get(build_id)

    async def worker(self):
        """Background worker that processes build jobs."""
        while True:
            job = await self.queue.get()
            self.current_job = job

            try:
                await self._run_build(job)
            except Exception as e:
                job.status = BuildStatus.FAILED
                job.error = str(e)
                job.append_log(f"\n\nFATAL ERROR: {e}\n")
            finally:
                job.completed_at = datetime.now()
                self.current_job = None
                self.queue.task_done()

    async def _run_build(self, job: BuildJob):
        """Execute a build job."""
        job.status = BuildStatus.BUILDING
        job.started_at = datetime.now()
        job.append_log(f"Starting build at {job.started_at.isoformat()}\n")
        job.append_log(f"Packages: {', '.join(job.packages)}\n")
        job.append_log(f"Config hash: {job.config_hash}\n\n")

        await self._apply_config(job)

        timestamp_file = Path("/var/db/repos/gentoo/metadata/timestamp.chk")
        skip_sync = False
        if timestamp_file.exists():
            tree_age = datetime.now().timestamp() - timestamp_file.stat().st_mtime
            if tree_age < 604800:  # 7 days
                skip_sync = True
                job.append_log(f"=== Skipping sync (tree is {tree_age/3600:.1f}h old) ===\n")

        if not skip_sync:
            job.append_log("=== Syncing portage tree ===\n")
            await self._run_command(job, ["emerge", "--sync"])

        job.append_log("\n=== Building packages ===\n")

        binpkg_dir = Path("/var/cache/binpkgs")
        before_build = set(binpkg_dir.rglob("*.gpkg.tar")) if binpkg_dir.exists() else set()

        emerge_cmd = [
            "emerge",
            "--buildpkg",
            "--verbose",
            "--with-bdeps=y",
            "--jobs=4",
            "--load-average=8",
            "--ask=n",  # Override any --ask in EMERGE_DEFAULT_OPTS
        ] + job.packages

        result = await self._run_command(job, emerge_cmd)

        if result != 0:
            job.status = BuildStatus.FAILED
            job.error = f"emerge failed with exit code {result}"
            return

        after_build = set(binpkg_dir.rglob("*.gpkg.tar")) if binpkg_dir.exists() else set()
        new_packages = after_build - before_build

        job.packages_built = [str(p.relative_to(binpkg_dir)) for p in new_packages]
        job.append_log(f"\n=== Built {len(new_packages)} packages ===\n")
        for pkg in job.packages_built:
            job.append_log(f"  - {pkg}\n")

        if new_packages:
            await self._create_artifact(job, new_packages)

        job.status = BuildStatus.COMPLETE
        job.append_log(f"\nBuild completed at {datetime.now().isoformat()}\n")

    async def _apply_config(self, job: BuildJob):
        """Apply the portage config from the cached tarball."""
        job.append_log("=== Applying portage config ===\n")

        portage_dir = Path("/etc/portage")

        backup_dir = Path(f"/tmp/portage-backup-{job.build_id}")
        if portage_dir.exists():
            shutil.copytree(portage_dir, backup_dir)

        config_portage = job.config_path / "etc" / "portage"

        if config_portage.exists():
            if portage_dir.exists():
                shutil.rmtree(portage_dir)
            shutil.copytree(config_portage, portage_dir, symlinks=True)
            job.append_log(f"Applied config from {config_portage}\n")
        else:
            # Maybe it's directly the portage contents
            for item in job.config_path.iterdir():
                if item.name not in [".", ".."]:
                    dest = portage_dir / item.name
                    if dest.exists():
                        if dest.is_dir():
                            shutil.rmtree(dest)
                        else:
                            dest.unlink()
                    if item.is_dir():
                        shutil.copytree(item, dest, symlinks=True)
                    elif item.is_symlink():
                        os.symlink(os.readlink(item), dest)
                    else:
                        shutil.copy2(item, dest)
            job.append_log(f"Applied config from {job.config_path}\n")

    async def _run_command(self, job: BuildJob, cmd: list[str]) -> int:
        """Run a command and stream output to the job log."""
        job.append_log(f"$ {' '.join(cmd)}\n")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "TERM": "dumb", "NOCOLOR": "1"}
        )

        while True:
            line = await process.stdout.readline()
            if not line:
                break
            job.append_log(line.decode("utf-8", errors="replace"))

        await process.wait()
        return process.returncode

    async def _create_artifact(self, job: BuildJob, packages: set[Path]):
        """Create a tarball of the built packages."""
        artifact_dir = Path("/var/cache/e4e/artifacts")
        artifact_dir.mkdir(parents=True, exist_ok=True)

        artifact_path = artifact_dir / f"{job.build_id}.tar"

        job.append_log(f"\n=== Creating artifact tarball ===\n")

        with tarfile.open(artifact_path, "w") as tar:
            for pkg_path in packages:
                arcname = pkg_path.relative_to("/var/cache/binpkgs")
                tar.add(pkg_path, arcname=arcname)
                job.append_log(f"  Added: {arcname}\n")

        job.artifact_path = artifact_path
        job.append_log(f"Artifact created: {artifact_path} ({artifact_path.stat().st_size} bytes)\n")
