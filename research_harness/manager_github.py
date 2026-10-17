"""Authenticated, read-only Issue intake in the protected manager only.

Only the installed gh executable and existing /control/gh authentication are
used. This module cannot log in, create credentials, inherit token environment
variables, post comments, execute Issue commands or choose a repository/host.
PublicGitHub's snapshot validation, pagination and incomplete-scan failures are
preserved. No worker receives this client, its config, or its private pipes.

HTTP failures expose only their numeric status and selected numeric rate-limit
headers. Remote response bodies, other headers and CLI stderr never escape in
exceptions or logs. Rate-limit observations suppress every subsequent GET until
the known reset/retry delay; a successful last-budget response also closes the
gate for later reads. A process restart does not preserve this in-memory gate.
"""
from __future__ import annotations

from email.message import Message
import math
import os
import re
import selectors
import signal
import subprocess
import time
from urllib.error import HTTPError

from .issue_intake import MAX_PAGES, MAX_RESPONSE, PublicGitHub
from .result_publisher import GhResultTransport
from .owner_approval import approval_read_timeout
from .schema import ContractError, REPOSITORY, loads

MANAGER_UID = 10001
MAX_SECONDS = 20
MAX_HEADERS = 16384
MAX_RETRY_SECONDS = 86400
MIN_RATE_DELAY = 300
_PREFIX = f'repos/{REPOSITORY}'
_LIST = '/issues?state=open&sort=created&direction=asc&per_page=100&page='


class ManagerGitHubError(ContractError):
    """Sanitized setup, process, protocol or response failure."""


class ManagerGitHubTimeout(ManagerGitHubError, TimeoutError):
    """Fixed timeout classification without remote text."""


class ManagerGitHubHTTPError(HTTPError):
    """HTTPError-compatible status/rate metadata, with no URL/body/CLI text."""
    def __init__(self, status, *, remaining=None, reset=None, retry_at=None):
        headers = Message()
        if remaining is not None:
            headers['X-RateLimit-Remaining'] = str(remaining)
        if reset is not None:
            headers['X-RateLimit-Reset'] = str(reset)
        super().__init__('', status, 'GitHub manager read unavailable', headers, None)
        self.retry_at = retry_at


class ManagerGitHubRateLimited(ManagerGitHubHTTPError):
    """No request is issued while the manager's observed rate gate is closed."""


def _number(value):
    if type(value) is bytes and re.fullmatch(rb'[0-9]{1,12}', value):
        return int(value)
    return None


def _response(raw):
    if type(raw) is not bytes or not raw or len(raw) > MAX_RESPONSE:
        raise ManagerGitHubError('invalid GitHub manager response size')
    separator = b'\r\n\r\n' if b'\r\n\r\n' in raw else b'\n\n'
    try:
        header, body = raw.split(separator, 1)
        if len(header) > MAX_HEADERS:
            raise ValueError()
        lines = header.splitlines()
        status = re.fullmatch(rb'HTTP/(?:1\.[01]|2(?:\.0)?|3(?:\.0)?) ([1-5][0-9]{2})(?: [^\r\n]*)?', lines[0])
        if status is None or len(lines) > 128:
            raise ValueError()
        values = {}
        for line in lines[1:]:
            name, value = line.split(b':', 1)
            if not re.fullmatch(rb'[A-Za-z0-9!#$%&\x27*+.^_`|~-]+', name):
                raise ValueError()
            key = name.lower()
            if key in (b'x-ratelimit-remaining', b'x-ratelimit-reset', b'retry-after'):
                if key in values:
                    raise ValueError()
                values[key] = _number(value.strip())
        return int(status.group(1)), values, body
    except (ValueError, IndexError):
        raise ManagerGitHubError('invalid GitHub manager response headers') from None


class ManagerGitHub(PublicGitHub):
    """Same Issue snapshot/list API, authenticated with manager-only gh config.

    ``retry_at`` is the earliest UTC retry time known to this client (or None).
    HTTP errors also provide it; standard .code/.headers remain compatible with
    the service's sanitized HTTP error reports. Clock injection is test-only
    trusted construction, never Issue configuration.
    """
    def __init__(self, *, clock=time.time, monotonic=time.monotonic):
        if not callable(clock) or not callable(monotonic):
            raise ManagerGitHubError('trusted manager clocks required')
        # Deliberately do not construct PublicGitHub's unauthenticated opener.
        self.clock = clock
        self.monotonic = monotonic
        self.retry_at = None
        self._retry_monotonic = None
        self._rate_status = 429

    @staticmethod
    def _environment():
        if os.getresuid() != (MANAGER_UID,) * 3:
            raise ManagerGitHubError('protected manager identity required')
        # No HOME/config/host/proxy/token/path/debug/pager values from os.environ.
        return dict(HOME='/control/home', GH_CONFIG_DIR='/control/gh',
                    PATH='/usr/local/bin:/usr/bin:/bin', GH_HOST='github.com',
                    GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1',
                    GH_PAGER='cat', LC_ALL='C.UTF-8')

    @staticmethod
    def _executable():
        # Existing publisher helper only verifies installed executable ownership;
        # it performs no credential read, process launch, network call or write.
        return GhResultTransport._executable()

    @staticmethod
    def _run(command):
        """Bound both output streams while they arrive; no unbounded communicate."""
        timeout = approval_read_timeout(MAX_SECONDS)
        environment = ManagerGitHub._environment()
        process = None
        completed = False
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                cwd='/', env=environment, close_fds=True, start_new_session=True)
            output, total = bytearray(), 0
            deadline = time.monotonic() + min(timeout, approval_read_timeout(MAX_SECONDS))
            with selectors.DefaultSelector() as selector:
                for stream in (process.stdout, process.stderr):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ManagerGitHubTimeout('GitHub manager read deadline exceeded')
                    for key, _ in selector.select(min(remaining, .1)):
                        try:
                            chunk = os.read(key.fd, min(65536, MAX_RESPONSE - total + 1))
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > MAX_RESPONSE:
                            raise ManagerGitHubError('GitHub manager response exceeded bound')
                        if key.fileobj is process.stdout:
                            output.extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ManagerGitHubTimeout('GitHub manager read deadline exceeded')
            code = process.wait(timeout=remaining)
            completed = True
            return code, bytes(output)
        except (TimeoutError, subprocess.TimeoutExpired):
            raise ManagerGitHubTimeout('GitHub manager read deadline exceeded') from None
        except (OSError, subprocess.SubprocessError):
            raise ManagerGitHubError('GitHub manager read unavailable') from None
        finally:
            if process is not None:
                try:
                    if not completed:
                        try:
                            # This new session belongs to this Popen invocation,
                            # never an Issue/worker-selected PID or process group.
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    raise ManagerGitHubError('GitHub manager read cleanup unavailable') from None
                finally:
                    for stream in (process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()

    @staticmethod
    def _route(suffix):
        if type(suffix) is not str:
            raise ManagerGitHubError('Issue read route is not allowlisted')
        if re.fullmatch(r'/issues/[1-9][0-9]{0,9}', suffix):
            if int(suffix.rsplit('/', 1)[1]) <= 2 ** 31 - 1:
                return suffix
        if suffix.startswith(_LIST):
            page = suffix[len(_LIST):]
            if re.fullmatch(r'[1-9][0-9]?', page) and int(page) <= MAX_PAGES:
                return suffix
        raise ManagerGitHubError('Issue read route is not allowlisted')

    def _now(self):
        wall, monotonic = self.clock(), self.monotonic()
        if (type(wall) not in (int, float) or type(monotonic) not in (int, float)
                or not math.isfinite(wall) or not math.isfinite(monotonic)
                or wall < 0 or monotonic < 0):
            raise ManagerGitHubError('invalid manager clock')
        return wall, monotonic

    def _gate(self, now, monotonic):
        if self.retry_at is None:
            return
        if now < self.retry_at or monotonic < self._retry_monotonic:
            retry = max(self.retry_at, now + max(0, self._retry_monotonic - monotonic))
            raise ManagerGitHubRateLimited(self._rate_status, remaining=0,
                reset=math.ceil(retry), retry_at=retry)
        self.retry_at = self._retry_monotonic = None

    def _get(self, suffix):
        suffix = self._route(suffix)
        # Reject incorrect identity even when _run is replaced with a test fake.
        self._environment()
        now, monotonic = self._now()
        self._gate(now, monotonic)
        command = [self._executable(), 'api', '--hostname', 'github.com',
                   '--method', 'GET', '--include',
                   '-H', 'Accept: application/vnd.github+json',
                   '-H', 'X-GitHub-Api-Version: 2022-11-28', _PREFIX + suffix]
        code, raw = self._run(command)
        status, headers, body = _response(raw)
        remaining = headers.get(b'x-ratelimit-remaining')
        reset = headers.get(b'x-ratelimit-reset')
        retry_after = headers.get(b'retry-after')
        now, monotonic = self._now()
        valid_reset = reset is not None and now < reset <= now + MAX_RETRY_SECONDS
        valid_retry = retry_after is not None and 0 < retry_after <= MAX_RETRY_SECONDS
        limited = status in (403, 429)
        if limited or remaining == 0:
            delay = max(MIN_RATE_DELAY,
                        reset - now + 1 if valid_reset else 0,
                        retry_after if valid_retry else 0)
            self.retry_at = now + delay
            self._retry_monotonic = monotonic + delay
            self._rate_status = status if limited else 429
        if not 200 <= status < 300:
            error_type = ManagerGitHubRateLimited if limited else ManagerGitHubHTTPError
            raise error_type(status, remaining=remaining, reset=reset, retry_at=self.retry_at)
        if type(code) is not int or code != 0 or status != 200:
            raise ManagerGitHubError('GitHub manager read did not complete successfully')
        try:
            return loads(body.decode('utf-8'))
        except (ValueError, UnicodeError, RecursionError, TypeError):
            raise ManagerGitHubError('invalid GitHub manager response JSON') from None
