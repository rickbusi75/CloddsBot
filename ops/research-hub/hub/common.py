import datetime as dt
import hashlib
import ipaddress
import json
import math
import socket
import urllib.error
import urllib.parse
import urllib.request

UTC = dt.timezone.utc


def now():
    return dt.datetime.now(UTC)


def iso(value=None):
    return (value or now()).isoformat()


def stamp(value):
    if not value:
        return None
    try:
        result = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def number(value, default=None):
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class UpstreamError(RuntimeError):
    def __init__(self, status=0):
        self.status = status
        super().__init__('upstream_http_' + str(status) if status else 'upstream_unavailable')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def public_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('public_https_required')
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise ValueError('private_source_rejected')
    return url


def request(url, *, headers=None, body=None, method=None, raw=False, timeout=20, max_bytes=8_000_000, untrusted=False):
    if untrusted:
        public_url(url)
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        'User-Agent': 'CloddsBot-Research-Hub/1.0',
        **({'Content-Type': 'application/json'} if data else {}), **(headers or {})})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
            value = response.read(max_bytes + 1)
            if len(value) > max_bytes:
                raise ValueError('upstream_response_too_large')
            text = value.decode('utf-8', errors='replace')
            return (text if raw else json.loads(text)), dict(response.headers)
    except urllib.error.HTTPError as error:
        # Never echo response bodies or URLs: query strings can contain API keys.
        raise UpstreamError(error.code) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        raise UpstreamError() from None


def query(url, params):
    return url + ('&' if '?' in url else '?') + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
