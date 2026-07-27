"""Small fake Euler host for managed-process extension protocol tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import time
from typing import Any, Mapping, Optional, Sequence


_UNSET = object()


class ManagedProcessPeer:
    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        max_message_bytes: int = 1024 * 1024,
    ) -> None:
        self.process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.max_message_bytes = max_message_bytes
        self._stdout_buffer = b""

    def initialize(self) -> None:
        self.write(
            {
                "jsonrpc": "2.0",
                "id": "initialize",
                "method": "initialize",
                "params": {
                    "protocol_versions": ["euler-managed-process/1"],
                    "limits": {"max_message_bytes": self.max_message_bytes},
                },
            }
        )
        assert self.read() == {
            "jsonrpc": "2.0",
            "id": "initialize",
            "result": {"protocol_version": "euler-managed-process/1"},
        }
        self.write({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    def invoke(
        self,
        command: str,
        input_value: Any,
        *,
        request_id: str = "command",
    ) -> None:
        self.write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "euler/command",
                "params": {"command": command, "input": input_value},
            }
        )

    def respond(
        self,
        request: Mapping[str, Any],
        *,
        result: Any = _UNSET,
        error: Optional[str] = None,
    ) -> None:
        if error is None:
            message = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {} if result is _UNSET else result,
            }
        else:
            message = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": -32000, "message": error},
            }
        self.write(message)

    def finish(self) -> None:
        self.write(
            {
                "jsonrpc": "2.0",
                "id": "shutdown",
                "method": "shutdown",
                "params": {},
            }
        )
        assert self.read() == {
            "jsonrpc": "2.0",
            "id": "shutdown",
            "result": {},
        }
        self.write({"jsonrpc": "2.0", "method": "exit"})
        if self.process.stdin is not None:
            self.process.stdin.close()
        return_code = self.process.wait(timeout=5)
        stderr = self.stderr()
        assert return_code == 0, stderr

    def write(self, message: Mapping[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(
            (
                json.dumps(message, separators=(",", ":"), ensure_ascii=False)
                + "\n"
            ).encode("utf-8")
        )
        self.process.stdin.flush()

    def write_raw(self, content: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(content.encode("utf-8"))
        self.process.stdin.flush()

    def read(self, *, timeout: float = 5) -> dict[str, Any]:
        assert self.process.stdout is not None
        deadline = time.monotonic() + timeout
        while b"\n" not in self._stdout_buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    "extension timed out waiting for a protocol message"
                )
            readable, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not readable:
                raise AssertionError(
                    "extension timed out waiting for a protocol message"
                )
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise AssertionError(
                    f"extension exited {self.process.poll()}; stderr: {self.stderr()}"
                )
            self._stdout_buffer += chunk
        line, self._stdout_buffer = self._stdout_buffer.split(b"\n", 1)
        return json.loads(line)

    def wait(self, *, expected: Optional[int] = None) -> int:
        return_code = self.process.wait(timeout=5)
        if expected is not None and return_code != expected:
            raise AssertionError(
                f"expected exit {expected}, got {return_code}: {self.stderr()}"
            )
        return return_code

    def stderr(self) -> str:
        if self.process.stderr is None:
            return ""
        return self.process.stderr.read().decode("utf-8", errors="replace")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)
        for stream in (
            self.process.stdin,
            self.process.stdout,
            self.process.stderr,
        ):
            if stream is not None and not stream.closed:
                stream.close()

    def __enter__(self) -> "ManagedProcessPeer":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
