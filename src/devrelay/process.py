"""Bounded foreground commands with retained logs and crash markers."""

from contextlib import contextmanager
import json
import locale
import os
from pathlib import Path
import signal
import subprocess
import time

from .errors import RelayError
from .state import group_alive, now


_TERMINATE_GRACE_SECONDS = 3
_KILL_GRACE_SECONDS = 3
_GROUP_POLL_SECONDS = 0.05


@contextmanager
def interrupt_on_sigterm():
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _read_output(reader, pending, on_output_line, *, final=False):
    pending += reader.read()
    # JSONL uses LF, not the Unicode separators recognized by str.splitlines().
    lines = pending.split("\n")
    pending = lines.pop()
    for line in lines:
        on_output_line(line.removesuffix("\r"))
    if final and pending:
        on_output_line(pending)
        pending = ""
    return pending


def _close_stdin(process):
    stream = process.stdin
    process.stdin = None
    if stream is not None:
        try:
            stream.close()
        except OSError:
            pass


def _stream_output(process, log, timeout, started, input, on_output_line):
    payload = input.encode(locale.getpreferredencoding(False)) if input is not None else b""
    offset = 0
    deadline = started + timeout
    pending = ""
    if process.stdin is not None:
        os.set_blocking(process.stdin.fileno(), False)
    with log.open(errors="replace", newline="") as reader:
        while process.poll() is None:
            pending = _read_output(reader, pending, on_output_line)
            writes = 0
            while process.stdin is not None and offset < len(payload) and writes < 16:
                try:
                    offset += os.write(process.stdin.fileno(), payload[offset:offset + 65536])
                    writes += 1
                except BlockingIOError:
                    break
                except OSError:
                    _close_stdin(process)
            if process.stdin is not None and offset == len(payload):
                _close_stdin(process)
            if time.monotonic() >= deadline:
                return True
            time.sleep(0.05)
        if process.stdin is not None:
            _close_stdin(process)
        _read_output(reader, pending, on_output_line, final=True)
    return False


def execute(state, argv, cwd, log, timeout, *, input=None, env=None, on_output_line=None):
    # Retries can execute under the same worker lock after termination fails.
    # Do not replace the previous group's recovery marker until it is gone.
    active = state.metadata("active_process")
    if active:
        if group_alive(active["group"]):
            raise RelayError(f"Previous command still runs in process group {active['group']}; wait for it to finish")
        state.set_metadata("active_process", None)
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    process = None
    timed_out = False
    stderr_log = log.with_suffix(log.suffix + ".stderr.log") if on_output_line is not None else None
    termination_failed = False
    with log.open("x") as stream:
        diagnostics = stderr_log.open("x") if stderr_log is not None else None
        try:
            try:
                process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                           stdout=stream, stderr=diagnostics or subprocess.STDOUT,
                                           text=on_output_line is None, env=env,
                                           start_new_session=True)
            except OSError as error:
                raise RelayError(f"Cannot start {argv[0]}: {error}") from error
            state.set_metadata("active_process", {"group": process.pid, "argv": argv, "started_at": now()})
            try:
                if on_output_line is None:
                    process.communicate(input, timeout=timeout)
                else:
                    timed_out = _stream_output(process, log, timeout, started, input, on_output_line)
                    if timed_out:
                        raise subprocess.TimeoutExpired(argv, timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    terminate(process)
                except BaseException:
                    termination_failed = True
                    raise
            except BaseException:
                try:
                    terminate(process)
                except BaseException:
                    termination_failed = True
                    raise
                raise
        finally:
            if diagnostics is not None:
                diagnostics.close()
            if process is not None and process.stdin is not None:
                _close_stdin(process)
            if not termination_failed and process is not None and process.poll() is not None:
                if group_alive(process.pid):
                    # Keep evidence of detached foreground descendants; don't claim a clean finish.
                    raise RelayError(f"Command left live descendants in process group {process.pid}")
                state.set_metadata("active_process", None)
    receipt = {"argv": argv, "cwd": str(cwd), "log": str(log), "returncode": process.returncode,
               "timed_out": timed_out, "wall_seconds": round(time.monotonic() - started, 3), "finished_at": now()}
    if stderr_log is not None:
        receipt["stderr_log"] = str(stderr_log)
    log.with_suffix(log.suffix + ".receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if timed_out or process.returncode:
        raise RelayError(f"{argv[0]} {'timed out' if timed_out else 'failed (' + str(process.returncode) + ')'}; log: {log}")
    return receipt


def terminate(process):
    inspection_error = None
    for sig, wait in ((signal.SIGTERM, _TERMINATE_GRACE_SECONDS),
                      (signal.SIGKILL, _KILL_GRACE_SECONDS)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise RelayError(f"Cannot signal process group {process.pid} with {sig.name}: {error}; "
                             "recovery will remain blocked") from error
        try:
            if _wait_for_group(process, wait):
                return
            inspection_error = None
        except RelayError as error:
            # An unknown state is treated as alive so SIGTERM inspection
            # failure still escalates to SIGKILL.
            inspection_error = error
    if inspection_error is not None:
        raise RelayError(f"Cannot confirm process group {process.pid} stopped: {inspection_error}; "
                         "recovery will remain blocked") from inspection_error
    raise RelayError(f"Cannot stop process group {process.pid}; recovery will remain blocked")


def _wait_for_group(process, timeout):
    deadline = time.monotonic() + timeout
    while True:
        # Reap the direct child when possible, but completion is determined by
        # every non-zombie member of the controlled process group.
        process.poll()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if not group_alive(process.pid, timeout=remaining):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_GROUP_POLL_SECONDS, remaining))
