from __future__ import annotations

import base64
import shlex
import threading
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass
class FakeResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


class FakeFiles:
    def __init__(self) -> None:
        self.values = {}
        self.directories = {"/workspace", "/tmp"}

    def write(self, path, content):
        self.values[path] = content
        self.directories.add(str(PurePosixPath(path).parent))

    def read(self, path):
        return self.values[path]

    def exists(self, path):
        return path in self.values or path in self.directories

    def list(self, path):
        prefix = path.rstrip("/") + "/"
        names = set()
        for candidate in [*self.values, *self.directories]:
            if candidate.startswith(prefix):
                remainder = candidate[len(prefix) :]
                if remainder:
                    names.add(remainder.split("/", 1)[0])
        return [self.stat(prefix + name) for name in sorted(names)]

    def stat(self, path):
        if path not in self.values and path not in self.directories:
            raise FileNotFoundError(path)
        value = self.values.get(path)
        size = len(value if isinstance(value, bytes) else (value or "").encode("utf-8"))
        return {
            "name": PurePosixPath(path).name,
            "path": path,
            "type": "dir" if path in self.directories else "file",
            "size": size,
            "isDir": path in self.directories,
        }

    def make_dir(self, path):
        self.directories.add(path)
        return self.stat(path)

    def remove(self, path):
        self.values.pop(path, None)
        self.directories.discard(path)

    def rename(self, path, destination):
        if path in self.values:
            self.values[destination] = self.values.pop(path)
        elif path in self.directories:
            self.directories.remove(path)
            self.directories.add(destination)
        else:
            raise FileNotFoundError(path)
        return self.stat(destination)


class FakeCommands:
    def __init__(self, sandbox) -> None:
        self.sandbox = sandbox
        self.last_command = ""

    def run(self, command, **_kwargs):
        self.last_command = command
        if "echo $!" in command:
            return FakeResult(stdout="4321\n")
        if command.startswith("base64 -w0 -- "):
            path = shlex.split(command)[-1]
            content = self.sandbox.files.values[path]
            if isinstance(content, str):
                content = content.encode("utf-8")
            return FakeResult(stdout=base64.b64encode(content).decode("ascii"))
        if command.startswith("kill -0 "):
            return FakeResult(exit_code=0 if self.sandbox.pty.running else 1)
        return FakeResult(stdout="remote-ok\n")


class FakePtyHandle:
    def __init__(self, pid=2468, chunks=()) -> None:
        self.pid = pid
        self.exit_code = None
        self._chunks = list(chunks)
        self.disconnected = False

    def __iter__(self):
        for chunk in self._chunks:
            yield chunk
        self.exit_code = 0

    def disconnect(self):
        self.disconnected = True


class FakePty:
    def __init__(self) -> None:
        self.running = True
        self.input = []
        self.size = None

    def create(self, size, **_kwargs):
        self.size = size
        return FakePtyHandle()

    def connect(self, pid, **_kwargs):
        return FakePtyHandle(pid, [b"hello ", b"pty\n"])

    def send_stdin(self, pid, data, **_kwargs):
        self.input.append((pid, data))

    def resize(self, _pid, size, **_kwargs):
        self.size = size

    def kill(self, _pid, **_kwargs):
        was_running = self.running
        self.running = False
        return was_running


@dataclass
class FakeSnapshot:
    snapshot_id: str


class FakeSandbox:
    created = []
    by_id = {}
    _counter = 0
    _lock = threading.Lock()

    def __init__(self, **create_args):
        with self._lock:
            type(self)._counter += 1
            number = type(self)._counter
        self.sandbox_id = f"{number:08d}-full-private-id"
        self.traffic_access_token = f"traffic-{number}"
        self.create_args = create_args
        self.files = FakeFiles()
        self.commands = FakeCommands(self)
        self.pty = FakePty()
        self.paused = False
        self.killed = False
        self.closed = False
        self.rolled_back_to = None
        self.__class__.created.append(self)
        self.__class__.by_id[self.sandbox_id] = self

    @classmethod
    def reset(cls):
        cls.created.clear()
        cls.by_id.clear()
        cls._counter = 0

    @classmethod
    def create(cls, **kwargs):
        return cls(**kwargs)

    @classmethod
    def connect(cls, sandbox_id):
        return cls.by_id[sandbox_id]

    def pause(self, wait=True):
        self.paused = wait

    def kill(self):
        self.killed = True

    def close(self):
        self.closed = True

    def get_info(self):
        return {"state": "paused" if self.paused else "running"}

    def create_snapshot(self, name=None):
        return FakeSnapshot(f"snapshot-{name or len(self.created)}")

    def rollback(self, snapshot_id):
        self.rolled_back_to = snapshot_id


class FakeVolume:
    created = []

    def __init__(self, name, driver=None):
        self.name = name
        self.driver = driver
        self.volume_id = "volume-" + name
        self.__class__.created.append(self)

    @classmethod
    def create(cls, name, driver=None):
        return cls(name, driver)


def fake_template(_template):
    return {"status": "ready"}
