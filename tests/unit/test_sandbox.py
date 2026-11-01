import socket
import struct
from unittest.mock import MagicMock

import pytest
from docker.errors import APIError, NotFound

from rd_platform.runtime.sandbox import (
    DockerSandbox, SandboxError, SandboxOutputLimit, SandboxTimeout,
)


class FakeSocket:
    def __init__(self, data=b"", stall=False):
        self.data = data
        self.stall = stall
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def recv(self, size):
        if self.stall:
            raise socket.timeout()
        # Fragment headers and payloads to exercise real streaming boundaries.
        chunk, self.data = self.data[:min(size, 3)], self.data[min(size, 3):]
        return chunk

    def close(self):
        self.closed = True


def frame(channel, data):
    return struct.pack(">BxxxI", channel, len(data)) + data


@pytest.fixture
def setup(tmp_path):
    paths = [tmp_path / name for name in ("workspace", "input", "output")]
    for path in paths:
        path.mkdir()
    client = MagicMock()
    client.info.return_value = {"OSType": "linux"}
    image = client.images.get.return_value
    image.id = "sha256:trusted"
    image.attrs = {"Os": "linux", "Config": {}}
    container = client.containers.create.return_value
    container.id = "container-id"
    client.api.exec_create.return_value = {"Id": "exec-id"}
    client.api.exec_start.return_value = FakeSocket()
    client.api.exec_inspect.return_value = {"Running": False, "ExitCode": 0}
    return paths, client, container


def make(setup, **kwargs):
    paths, client, _ = setup
    return DockerSandbox("trusted:local", *paths, client=client, **kwargs)


def test_hardening_and_lifecycle(setup):
    _, client, container = setup
    box = make(setup)
    with box:
        assert box.start() is box
        assert box.container_id == "container-id"
        config = client.containers.create.call_args.kwargs
        assert config["image"] == "sha256:trusted"
        assert config["network_mode"] == "none"
        assert config["network_disabled"] is True
        assert config["nano_cpus"] == 2_000_000_000
        assert config["mem_limit"] == config["memswap_limit"] == "6g"
        assert config["pids_limit"] == 256
        assert config["read_only"] is True
        assert config["privileged"] is False
        assert config["cap_drop"] == ["ALL"]
        assert config["security_opt"] == ["no-new-privileges:true"]
        assert config["user"] == "65532:65532"
        assert config["auto_remove"] and config["init"]
        assert config["use_config_proxy"] is False
        assert config["entrypoint"] == ["/bin/sleep"]
        assert config["healthcheck"] == {"test": ["NONE"]}
        assert config["log_config"]["Config"]["max-file"] == "2"
        assert [(m["Target"], m["ReadOnly"]) for m in config["mounts"]] == [
            ("/workspace", True), ("/input", True), ("/output", False),
        ]
    client.containers.create.assert_called_once()
    container.start.assert_called_once()
    container.remove.assert_called_once_with(force=True, v=True)
    client.close.assert_not_called()  # Injected clients belong to their caller.
    assert box.container_id is None
    box.cleanup()
    with pytest.raises(SandboxError):
        box.start()


def test_exec_demultiplexes_and_preserves_exit_code(setup):
    _, client, _ = setup
    stream = FakeSocket(frame(1, b"hello \xe4\xb8\xad\n") + frame(2, b"bad\xff") + frame(1, b"again"))
    client.api.exec_start.return_value = stream
    client.api.exec_inspect.return_value = {"Running": False, "ExitCode": 7}
    with make(setup) as box:
        assert box.exec_run(["python", "-c", "untrusted code"]) == (7, "hello 中\nagain", "bad�")
    assert stream.closed
    assert client.api.exec_create.call_args.args == ("container-id", ["python", "-c", "untrusted code"])
    assert client.api.exec_create.call_args.kwargs["privileged"] is False


def test_no_implicit_shell(setup):
    _, client, _ = setup
    with make(setup) as box:
        assert box.exec_run('echo "a b" ; whoami') == (0, "", "")
    assert client.api.exec_create.call_args.args[1] == ["echo", "a b", ";", "whoami"]


@pytest.mark.parametrize("cmd", [[], " ", [""], ["echo", "\0"], ["echo", 1]])
def test_invalid_commands(setup, cmd):
    with make(setup) as box, pytest.raises(ValueError):
        box.exec_run(cmd)
    setup[1].api.exec_create.assert_not_called()


def test_exec_requires_start(setup):
    with pytest.raises(SandboxError):
        make(setup).exec_run(["echo"])


@pytest.mark.parametrize("options", [{"timeout_seconds": 0}, {"timeout_seconds": float("inf")},
                                          {"max_output_bytes": 0}, {"max_output_bytes": 1.5}])
def test_invalid_limits(setup, options):
    with pytest.raises(ValueError):
        make(setup, **options)


def test_paths_must_exist_and_not_overlap(setup):
    paths, client, _ = setup
    with pytest.raises(FileNotFoundError):
        DockerSandbox("image", paths[0] / "missing", *paths[1:], client=client)
    alias = paths[1] / "alias"
    alias.symlink_to(paths[0], target_is_directory=True)
    with pytest.raises(ValueError):
        DockerSandbox("image", paths[0], alias, paths[2], client=client)
    with pytest.raises(ValueError):
        DockerSandbox("image", paths[0].parent, paths[1], paths[2], client=client)


@pytest.mark.parametrize("case", ["daemon", "image", "volumes"])
def test_unsafe_runtime_rejected(setup, case):
    _, client, _ = setup
    if case == "daemon":
        client.info.return_value = {"OSType": "windows"}
    elif case == "image":
        client.images.get.return_value.attrs["Os"] = "windows"
    else:
        client.images.get.return_value.attrs["Config"] = {"Volumes": {"/unsafe": {}}}
    with pytest.raises(SandboxError):
        make(setup).start()
    client.containers.create.assert_not_called()


def test_start_failure_cleans_created_container(setup):
    _, _, container = setup
    container.start.side_effect = APIError("start failed")
    with pytest.raises(APIError):
        make(setup).start()
    container.remove.assert_called_once_with(force=True, v=True)


def test_lost_create_response_recovers_by_unique_name(setup):
    _, client, container = setup
    client.containers.create.side_effect = APIError("lost response")
    client.containers.get.return_value = container
    box = make(setup)
    with pytest.raises(APIError):
        box.start()
    client.containers.get.assert_called_once_with(box.name)
    container.remove.assert_called_once_with(force=True, v=True)


def test_cleanup_failures_remain_retryable(setup):
    _, _, container = setup
    box = make(setup).start()
    container.remove.side_effect = APIError("daemon unavailable")
    with pytest.raises(SandboxError, match="retry cleanup"):
        box.cleanup()
    assert box.container_id == "container-id"
    assert container.remove.call_count == 3
    container.remove.side_effect = None
    box.cleanup()
    assert box.container_id is None


def test_cleanup_not_found_is_success(setup):
    box = make(setup).start()
    setup[2].remove.side_effect = NotFound("already removed")
    box.cleanup()
    box.cleanup()
    assert box.container_id is None


def test_context_exception_cleans_up(setup):
    with pytest.raises(ValueError, match="caller failed"):
        with make(setup):
            raise ValueError("caller failed")
    setup[2].remove.assert_called_once_with(force=True, v=True)


def test_silent_hang_times_out_and_destroys_container(setup):
    setup[1].api.exec_start.return_value = FakeSocket(stall=True)
    box = make(setup, timeout_seconds=0.01).start()
    with pytest.raises(SandboxTimeout):
        box.exec_run(["sleep", "infinity"])
    assert box.container_id is None
    setup[2].remove.assert_called_once_with(force=True, v=True)


@pytest.mark.parametrize("data,error", [
    (frame(1, b"too much"), SandboxOutputLimit),
    (b"\x01\x00", SandboxError),
    (frame(1, b"abc")[:-1], SandboxError),
    (frame(3, b""), SandboxError),
])
def test_output_failure_destroys_container(setup, data, error):
    setup[1].api.exec_start.return_value = FakeSocket(data)
    box = make(setup, max_output_bytes=4).start()
    with pytest.raises(error):
        box.exec_run(["untrusted"])
    assert box.container_id is None


def test_transport_failure_destroys_container(setup):
    setup[1].api.exec_start.side_effect = APIError("transport failed")
    box = make(setup).start()
    with pytest.raises(APIError):
        box.exec_run(["untrusted"])
    assert box.container_id is None


def test_owned_client_closed(setup, monkeypatch):
    paths, client, _ = setup
    monkeypatch.setattr("rd_platform.runtime.sandbox.docker.from_env", lambda **kwargs: client)
    with DockerSandbox("trusted", *paths):
        pass
    client.close.assert_called_once()


def test_lost_create_and_recovery_failure_can_be_cleaned_later(setup):
    _, client, container = setup
    client.containers.create.side_effect = APIError("create response lost")
    client.containers.get.side_effect = APIError("daemon unavailable")
    box = make(setup)
    with pytest.raises(SandboxError, match="cleanup failed"):
        box.start()
    client.containers.get.side_effect = None
    client.containers.get.return_value = container
    box.cleanup()
    container.remove.assert_called_once_with(force=True, v=True)


def test_exec_cleanup_failure_is_reported_and_retryable(setup):
    _, client, container = setup
    client.api.exec_start.side_effect = APIError("transport failed")
    container.remove.side_effect = APIError("daemon unavailable")
    box = make(setup).start()
    with pytest.raises(SandboxError, match="cleanup failed after APIError"):
        box.exec_run(["untrusted"])
    assert box.container_id == "container-id"
    container.remove.side_effect = None
    box.cleanup()


def test_still_running_after_eof_is_bounded(setup):
    setup[1].api.exec_inspect.return_value = {"Running": True, "ExitCode": None}
    box = make(setup, timeout_seconds=0.01).start()
    with pytest.raises(SandboxTimeout):
        box.exec_run(["untrusted"])
    assert box.container_id is None


def test_missing_exit_code_is_not_reported_as_success(setup):
    setup[1].api.exec_inspect.return_value = {"Running": False, "ExitCode": None}
    box = make(setup).start()
    with pytest.raises(SandboxError, match="no command exit code"):
        box.exec_run(["untrusted"])
    assert box.container_id is None
