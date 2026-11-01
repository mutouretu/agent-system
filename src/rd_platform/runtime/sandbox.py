"""Bounded Docker execution; this module never launches a host subprocess.

Only trusted, pre-provisioned Linux images and staged directories may be supplied
by the control plane. Commands are untrusted data, sent exclusively to Docker.
"""

from __future__ import annotations

import math
import shlex
import socket
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Sequence

import docker
from docker.errors import DockerException, NotFound
from docker.types import LogConfig, Mount, Ulimit


class SandboxError(RuntimeError):
    """Lifecycle or Docker transport failure (distinct from a command exit)."""


class SandboxTimeout(SandboxError):
    """Command exceeded its wall-clock deadline; the sandbox was destroyed."""


class SandboxOutputLimit(SandboxError):
    """Command exceeded the combined stdout/stderr byte budget."""


class DockerSandbox:
    """Single-use, context-managed sandbox with persistent state between execs.

    Paths must exist, be disjoint, and refer to the Docker daemon's host.
    /workspace and /input are read-only; /output is the sole writable bind.
    Its directory must already be writable by UID/GID 65532. The class never
    changes host permissions, pulls images, or falls back to host execution.

    ``cmd`` is an argv sequence or a shlex-split string, never an implicit shell.
    To use shell syntax, explicitly pass ["/bin/sh", "-c", script].
    Output is UTF-8 text with invalid bytes replaced. Execs are serialized;
    cleanup may be called from another thread to cancel the entire sandbox.
    """

    def __init__(
        self,
        image: str,
        workspace_dir: str | Path,
        input_dir: str | Path,
        output_dir: str | Path,
        *,
        client: docker.DockerClient | None = None,
        timeout_seconds: float = 300,
        max_output_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if not isinstance(image, str) or not image.strip():
            raise ValueError("image must be a non-empty, pre-provisioned Linux image")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if not isinstance(max_output_bytes, int) or max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be a positive integer")
        paths = [Path(p).resolve(strict=True) for p in (workspace_dir, input_dir, output_dir)]
        for path in paths:
            if not path.is_dir() or path == Path(path.anchor):
                raise ValueError("mount sources must be existing, non-root directories")
        for index, path in enumerate(paths):
            for other in paths[index + 1 :]:
                if path == other or path in other.parents or other in path.parents:
                    raise ValueError("workspace, input and output directories must not overlap")
        self.image = image
        self.workspace_dir, self.input_dir, self.output_dir = paths
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self._client = client
        self._owns_client = client is None
        self._container = None
        self._creation_attempted = False
        self._closed = False
        self._state_lock = threading.RLock()
        self._exec_lock = threading.Lock()
        self.name = f"rd-sandbox-{uuid.uuid4().hex}"

    @property
    def container_id(self) -> str | None:
        with self._state_lock:
            return self._container.id if self._container is not None else None

    def start(self) -> DockerSandbox:
        with self._state_lock:
            if self._closed:
                raise SandboxError("a cleaned sandbox cannot be restarted")
            if self._container is not None:
                return self
            try:
                if self._client is None:
                    self._client = docker.from_env(timeout=10)
                if self._client.info().get("OSType") != "linux":
                    raise SandboxError("a Linux Docker daemon is required")
                image = self._client.images.get(self.image)
                if image.attrs.get("Os") != "linux":
                    raise SandboxError("a Linux image is required")
                if image.attrs.get("Config", {}).get("Volumes"):
                    raise SandboxError("images with implicit VOLUME mounts are not allowed")
                self._creation_attempted = True
                self._container = self._client.containers.create(
                    image=image.id,
                    name=self.name,
                    # Override image startup code, healthchecks and stop behavior.
                    entrypoint=["/bin/sleep"],
                    command=["3600"],
                    healthcheck={"test": ["NONE"]},
                    stop_signal="SIGKILL",
                    working_dir="/workspace",
                    user="65532:65532",
                    network_mode="none",
                    network_disabled=True,
                    nano_cpus=2_000_000_000,
                    mem_limit="6g",
                    memswap_limit="6g",
                    pids_limit=256,
                    read_only=True,
                    privileged=False,
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges:true"],
                    ipc_mode="private",
                    cgroupns="private",
                    init=True,
                    auto_remove=True,
                    restart_policy={"Name": "no"},
                    mounts=[
                        Mount("/workspace", str(self.workspace_dir), type="bind", read_only=True),
                        Mount("/input", str(self.input_dir), type="bind", read_only=True),
                        Mount("/output", str(self.output_dir), type="bind", read_only=False),
                    ],
                    tmpfs={"/tmp": "rw,nosuid,nodev,size=512m,mode=1777"},
                    shm_size="64m",
                    ulimits=[Ulimit(name="nofile", soft=1024, hard=1024), Ulimit(name="core", soft=0, hard=0)],
                    environment={"HOME": "/tmp", "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"},
                    use_config_proxy=False,
                    labels={"rd-platform.sandbox": "true"},
                    log_config=LogConfig(type="local", config={"max-size": "1m", "max-file": "2"}),
                    stdin_open=False,
                    tty=False,
                    detach=True,
                )
                self._container.start()
                return self
            except BaseException as error:
                self._cleanup_after(error)
                raise

    def exec_run(self, cmd: str | Sequence[str]) -> tuple[int, str, str]:
        argv = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
        if not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv) or not argv[0]:
            raise ValueError("cmd must contain a non-empty executable and NUL-free string arguments")
        with self._exec_lock:
            with self._state_lock:
                if self._closed or self._container is None:
                    raise SandboxError("start the sandbox before executing a command")
                container = self._container
                client = self._client
            deadline = time.monotonic() + self.timeout_seconds
            stream = None
            try:
                execution = client.api.exec_create(
                    container.id, argv, stdout=True, stderr=True, stdin=False,
                    tty=False, privileged=False, user="65532:65532", workdir="/workspace",
                )["Id"]
                stream = client.api.exec_start(execution, socket=True, tty=False)
                # Docker SDK returns SocketIO for unix sockets, or a socket for TCP.
                raw_socket = getattr(stream, "_sock", stream)
                stdout, stderr = self._read_output(raw_socket, deadline)
                while True:
                    self._remaining(deadline)
                    state = client.api.exec_inspect(execution)
                    self._remaining(deadline)
                    if not state["Running"]:
                        if not isinstance(state.get("ExitCode"), int):
                            raise SandboxError("Docker returned no command exit code")
                        return state["ExitCode"], stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
                    time.sleep(min(0.05, self._remaining(deadline)))
            except BaseException as error:
                self._cleanup_after(error)
                raise
            finally:
                if stream is not None:
                    stream.close()

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxTimeout("command deadline exceeded; sandbox destroyed")
        return remaining

    def _read_output(self, sock, deadline: float) -> tuple[bytes, bytes]:
        outputs = {1: bytearray(), 2: bytearray()}
        total = 0

        def read_exact(size: int, *, allow_eof: bool = False) -> bytes:
            data = bytearray()
            while len(data) < size:
                sock.settimeout(min(1.0, self._remaining(deadline)))
                try:
                    chunk = sock.recv(min(size - len(data), 65536))
                except socket.timeout:
                    continue
                if not chunk:
                    if allow_eof and not data:
                        return b""
                    raise SandboxError("truncated Docker output frame")
                data.extend(chunk)
            return bytes(data)

        while True:
            header = read_exact(8, allow_eof=True)
            if not header:
                break
            channel, length = struct.unpack(">BxxxI", header)
            if channel not in outputs or header[1:4] != b"\0\0\0":
                raise SandboxError("invalid Docker output frame")
            total += length
            if total > self.max_output_bytes:
                raise SandboxOutputLimit("stdout/stderr exceeded byte budget; sandbox destroyed")
            outputs[channel].extend(read_exact(length))
        return bytes(outputs[1]), bytes(outputs[2])

    def _cleanup_after(self, error: BaseException) -> None:
        try:
            self.cleanup()
        except Exception as cleanup_error:
            raise SandboxError(
                f"sandbox {self.name} cleanup failed after {type(error).__name__}; retry cleanup()"
            ) from cleanup_error

    def cleanup(self) -> None:
        """Force-remove the container and its anonymous volumes; retryable on failure.

        NotFound means successful cleanup (including daemon auto-removal). Other
        failures are surfaced and the container reference is retained for retry.
        Docker rm(force=True) kills and removes the entire container process tree.
        """
        with self._state_lock:
            # Recover ambiguous create failures by this instance's unique name.
            if self._container is None and self._creation_attempted:
                try:
                    self._container = self._client.containers.get(self.name)
                except NotFound:
                    self._creation_attempted = False
            if self._container is not None:
                for attempt in range(3):
                    try:
                        self._container.remove(force=True, v=True)
                        break
                    except NotFound:
                        break
                    except DockerException as error:
                        if attempt == 2:
                            raise SandboxError(f"cannot remove sandbox {self.name}; retry cleanup()") from error
                self._container = None
                self._creation_attempted = False
            self._closed = True
            if self._owns_client and self._client is not None:
                self._client.close()
                self._client = None

    def __enter__(self) -> DockerSandbox:
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.cleanup()
