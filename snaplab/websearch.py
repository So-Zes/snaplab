"""Web search and page reading for bots.

Search providers
- "duckduckgo" (no key): the HTML results page of DuckDuckGo.
- "brave": Brave Search API, used when BRAVE_API_KEY is set.
- "tavily": Tavily API, used when TAVILY_API_KEY is set.
- "tavily-keyless": Tavily's own keyless access (header X-Tavily-Access-Mode: keyless), free and rate-limited
  per connection.
- "rustxng": a RustXNG (or SearXNG) on this PC, see rustxng.py: open indexes only, no key, no limit.
With provider "auto" SnapLab tries them in this order and uses the first that answers: Tavily (key), Brave (key),
DuckDuckGo, Tavily keyless, RustXNG (when it is installed and running). Free sources are often refused on a connection that shares its public address with
many people (mobile and 5G home routers); a key of the user's own does not depend on that.

A source that answers with a human check (CAPTCHA / "are you a robot") instead of results is skipped for this
search and the next one is tried; it is asked again next time. A source that says its limit is used up is paused
for its Retry-After (otherwise cooldown_minutes), because asking before then only fails again.

read_page(): fetches one http(s) page, text only, with size limits. Private, local and tailnet addresses
are refused, and so are the PC's own addresses (looked up live, public IPv6 included) with their IPv6 /64, and
IPv4 addresses hidden inside IPv6 ones (mapped, compatible, NAT64, 6to4, Teredo). Every redirect is checked
again, and the address actually connected to is checked after connecting. Everything read from the web is untrusted material: the caller wraps it so that the model
treats it as data, never as instructions.
"""
from __future__ import annotations

import html
import http.client
import ipaddress
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

UA = "SnapLab/0.1 (self-hosted personal assistant)"
CAPTCHA_MARKERS = ("captcha", "anomaly-modal", "challenge-form", "cf-chl", "are you a robot", "unusual traffic",
                   "bots use duckduckgo too", "verify you are human", "just a moment...", "attention required")


class SearchError(Exception):
    def __init__(self, kind: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind          # captcha | limit | http | network | config | blocked
        self.message = message
        self.retry_after = retry_after   # seconds the site asked us to wait (limit only)


def _retry_after(headers: dict) -> float | None:
    v = {k.lower(): v for k, v in (headers or {}).items()}.get("retry-after")
    try:
        return min(max(float(v), 60.0), 86400.0)
    except (TypeError, ValueError):
        return None


def looks_like_captcha(status: int, body: str) -> bool:
    """True when a response is a human check (CAPTCHA / bot wall) instead of the content we asked for."""
    low = (body or "")[:300000].lower()
    if any(m in low for m in ("anomaly-modal", "cf-chl-", "challenge-form", "bots use duckduckgo too")):
        return True
    m = re.search(r"<title[^>]*>(.*?)</title>", low, re.S)
    title = m.group(1) if m else ""
    if any(w in title for w in ("captcha", "robot", "verify you", "just a moment", "attention required", "access denied")):
        return True
    if status in (202, 403, 429, 503) and any(w in low for w in CAPTCHA_MARKERS):
        return True
    return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


PRIVATE_NETS = [ipaddress.ip_network(n) for n in ("100.64.0.0/10", "fd7a:115c:a1e0::/48", "fec0::/10")]
NAT64_NETS = [ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48")]
V4_COMPAT = ipaddress.ip_network("::/96")
LOCAL_TTL_S = 30
_local: dict = {"at": 0.0, "addrs": frozenset(), "nets": (), "override": None}


def _gather_local_addresses() -> set:
    """Every address of this PC, looked up now: IPv4 and IPv6, public ones included (QA round 2, H-2: a public IPv6
    address of the PC itself reached its own services)."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(info[4][0])
    except OSError:
        pass
    for fam, target in ((socket.AF_INET, ("8.8.8.8", 53)), (socket.AF_INET6, ("2001:4860:4860::8888", 53))):
        try:                                     # a UDP "connect" only picks the source address; nothing is sent
            sk = socket.socket(fam, socket.SOCK_DGRAM)
            sk.connect(target)
            found.add(sk.getsockname()[0])
            sk.close()
        except OSError:
            pass
    import subprocess
    cmds = [["ipconfig"]] if os.name == "nt" else [["ip", "-o", "addr"], ["ifconfig"]]
    for cmd in cmds:
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=6,
                                 creationflags=0x08000000 if os.name == "nt" else 0).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            continue
        for tok in re.findall(r"[0-9A-Fa-f.:%]{3,}", out):
            tok = tok.split("%")[0].split("/")[0].strip(".:")
            try:
                ipaddress.ip_address(tok)
            except ValueError:
                continue
            found.add(tok)
        break
    return {a.split("%")[0] for a in found}


def local_addresses() -> tuple[frozenset, tuple]:
    """(addresses of this PC, networks to refuse): every IPv6 global address brings its /64 (the home network), a
    public IPv4 address only itself. Looked up again every LOCAL_TTL_S seconds."""
    if _local["override"] is not None:
        addrs = _local["override"]
    else:
        now = time.monotonic()
        if now - _local["at"] < LOCAL_TTL_S and _local["addrs"]:
            return _local["addrs"], _local["nets"]
        addrs = _gather_local_addresses()
    ips, nets = set(), []
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        ips.add(ip)
        if isinstance(ip, ipaddress.IPv6Address) and ip.is_global:
            nets.append(ipaddress.ip_network(f"{ip}/64", strict=False))
    result = (frozenset(ips), tuple(nets))
    if _local["override"] is None:
        _local.update(at=time.monotonic(), addrs=result[0], nets=result[1])
    return result


def set_local_addresses(addrs) -> None:
    """Tests: pretend these are the PC's addresses (None = look them up again)."""
    _local.update(override=None if addrs is None else set(addrs), at=0.0, addrs=frozenset(), nets=())


def embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 address carries, if any: mapped (::ffff:a.b.c.d), compatible (::a.b.c.d), NAT64
    (64:ff9b::a.b.c.d), 6to4 (2002:AABB:CCDD::) and Teredo."""
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip in V4_COMPAT and int(ip) > 1:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if any(ip in n for n in NAT64_NETS):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    return None


def ip_is_public(addr: str) -> bool:
    """True only for a public internet address that is not this PC nor its home network (IPv6 /64)."""
    try:
        ip = ipaddress.ip_address(str(addr).split("%")[0])
    except ValueError:
        return False
    checks = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        inner = embedded_ipv4(ip)
        if inner is not None:
            checks = [inner]                     # judge the IPv4 address inside, not the wrapper
    mine, nets = local_addresses()
    for c in checks:
        if not c.is_global or any(c in n for n in PRIVATE_NETS):
            return False
        if c in mine or any(c in n for n in nets):
            return False
    return True


def _check_peer(sock) -> None:
    """After connecting, make sure we really talk to a public address (defeats DNS rebinding tricks)."""
    peer = sock.getpeername()[0]
    if not ip_is_public(peer):
        sock.close()
        raise SearchError("blocked", "the address turned out to be local, private or on the tailnet; refused")


class _SafeHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        _check_peer(self.sock)


class _SafeHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        _check_peer(self.sock)


class _SafeHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_SafeHTTPConnection, req)


class _SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_SafeHTTPSConnection, req, context=self._context)


# no system proxies (the peer check must see the real server), no automatic redirects (each hop is checked)
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _SafeHTTPHandler, _SafeHTTPSHandler, _NoRedirect)


def _http(url: str, *, data: bytes | None = None, headers: dict | None = None, timeout: float = 20,
          max_bytes: int = 3_000_000) -> tuple[int, dict, bytes, str]:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})},
                                 method="POST" if data is not None else "GET")
    try:
        resp = _opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read(max_bytes) if e.fp else b""
        return e.code, dict(e.headers or {}), body, url
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SearchError("network", f"could not connect: {getattr(e, 'reason', e)}") from None
    with resp:
        body = resp.read(max_bytes + 1)
        return resp.status, dict(resp.headers), body, resp.geturl()


# ------------------------------------------------------------------ search providers

class _DDGParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._mode = None
        self._cur = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and "result__a" in cls.split():
            self._cur = {"title": "", "url": a.get("href") or "", "snippet": ""}
            self.results.append(self._cur)
            self._mode = "title"
        elif tag in ("a", "div", "td") and "result__snippet" in cls.split() and self._cur is not None:
            self._mode = "snippet"

    def handle_endtag(self, tag):
        if tag in ("a", "div", "td") and self._mode:
            self._mode = None

    def handle_data(self, data):
        if self._cur is not None and self._mode:
            self._cur[self._mode] += data


def _clean_ddg_url(u: str) -> str:
    if u.startswith("//"):
        u = "https:" + u
    p = urllib.parse.urlparse(u)
    if p.netloc.endswith("duckduckgo.com") and p.path.startswith("/l/"):
        q = urllib.parse.parse_qs(p.query).get("uddg")
        if q:
            return q[0]
    return u


def parse_duckduckgo(body: str, count: int) -> list[dict]:
    p = _DDGParser()
    p.feed(body)
    out = []
    for r in p.results:
        url = _clean_ddg_url(r["url"].strip())
        if not url.startswith(("http://", "https://")) or "duckduckgo.com/y.js" in url:
            continue          # ads
        out.append({"title": " ".join(r["title"].split())[:200], "url": url[:500],
                    "snippet": " ".join(r["snippet"].split())[:400]})
        if len(out) >= count:
            break
    return out


def search_duckduckgo(query: str, count: int) -> list[dict]:
    data = urllib.parse.urlencode({"q": query, "kl": "wt-wt"}).encode()
    status, _, body, _ = _http("https://html.duckduckgo.com/html/", data=data,
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
    text = body.decode("utf-8", "replace")
    if looks_like_captcha(status, text):
        raise SearchError("captcha", "DuckDuckGo answered with a human check (CAPTCHA) instead of results")
    if status != 200:
        raise SearchError("http", f"DuckDuckGo answered HTTP {status}")
    return parse_duckduckgo(text, count)


def search_brave(query: str, count: int, key: str) -> list[dict]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": count})
    status, hdrs, body, _ = _http(url, headers={"Accept": "application/json", "X-Subscription-Token": key})
    if status == 429:
        raise SearchError("limit", "Brave Search says the key's limit is used up for now", _retry_after(hdrs))
    if status in (401, 403):
        raise SearchError("http", f"Brave Search refused the API key (HTTP {status})")
    if status != 200:
        raise SearchError("http", f"Brave Search answered HTTP {status}")
    data = json.loads(body.decode("utf-8", "replace"))
    return [{"title": (r.get("title") or "")[:200], "url": (r.get("url") or "")[:500],
             "snippet": re.sub(r"<[^>]+>", "", r.get("description") or "")[:400]}
            for r in ((data.get("web") or {}).get("results") or [])[:count]]


def search_tavily(query: str, count: int, key: str | None) -> list[dict]:
    """With key=None this is Tavily's keyless access: free, limited per connection (docs.tavily.com, "keyless")."""
    payload = json.dumps({"query": query, "max_results": count}).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    else:
        headers["X-Tavily-Access-Mode"] = "keyless"
    status, hdrs, body, _ = _http("https://api.tavily.com/search", data=payload, headers=headers)
    if status in (429, 432, 433):
        what = "the key's limit is used up for now" if key else "its free keyless limit is used up for this connection"
        try:
            said = str((json.loads(body.decode("utf-8", "replace")).get("error") or {}).get("message") or "")
        except (ValueError, AttributeError):
            said = ""
        raise SearchError("limit", f"Tavily says {what}" + (f' ("{" ".join(said.split())[:300]}")' if said else ""),
                          _retry_after(hdrs))
    if status in (401, 403) and key:
        raise SearchError("http", f"Tavily refused the API key (HTTP {status})")
    if status != 200:
        raise SearchError("http", f"Tavily answered HTTP {status}")
    data = json.loads(body.decode("utf-8", "replace"))
    return [{"title": (r.get("title") or "")[:200], "url": (r.get("url") or "")[:500],
             "snippet": (r.get("content") or "")[:400]} for r in (data.get("results") or [])[:count]]


LABELS = {"tavily": "Tavily", "brave": "Brave Search", "duckduckgo": "DuckDuckGo", "tavily-keyless": "Tavily (no key)",
          "rustxng": "RustXNG"}
NOTES = {"rustxng": "RustXNG searches open indexes (Mwmbl, Wikipedia and others); it finds few current or Japanese pages"}
KEY_ENVS = {"tavily": "TAVILY_API_KEY", "brave": "BRAVE_API_KEY"}


_local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def search_rustxng(query: str, count: int, url: str) -> list[dict]:
    """The JSON API of RustXNG / SearXNG on a URL the user configured (normally 127.0.0.1)."""
    u = url.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json", "max_results": count})
    req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with _local_opener.open(req, timeout=30) as resp:
            status, body = resp.status, resp.read(3_000_000)
    except urllib.error.HTTPError as e:
        status, body = e.code, b""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SearchError("network", f"RustXNG did not answer: {getattr(e, 'reason', e)}") from None
    if status == 429:
        raise SearchError("limit", "RustXNG says it is busy", 60)
    if status != 200:
        raise SearchError("http", f"RustXNG answered HTTP {status}")
    data = json.loads(body.decode("utf-8", "replace"))
    out = []
    for r in data.get("results") or []:
        link = str(r.get("url") or "")
        if not link.startswith(("http://", "https://")):
            continue
        out.append({"title": " ".join(str(r.get("title") or "").split())[:200], "url": link[:500],
                    "snippet": " ".join(str(r.get("content") or "").split())[:400]})
        if len(out) >= count:
            break
    return out


class Searcher:
    def __init__(self, cfg: dict, redactor=None):
        self.provider = (cfg.get("provider") or "auto").lower()
        self.cooldown_s = float(cfg.get("cooldown_minutes", 30)) * 60
        self.redactor = redactor
        self.paused_until: dict[str, float] = {}
        self.pause_kind: dict[str, str] = {}
        self.last: dict = {}
        self.rustxng = None            # a rustxng.RustXNG, set by the app

    def chain(self) -> list[tuple[str, str | None]]:
        """The providers to try, in order, with their keys."""
        from .llm import get_api_key
        tav, brave = get_api_key("TAVILY_API_KEY"), get_api_key("BRAVE_API_KEY")
        if self.provider == "tavily":
            return [("tavily", tav)]
        if self.provider == "brave":
            return [("brave", brave)]
        rx = self.rustxng
        if self.provider in ("duckduckgo", "tavily-keyless"):
            return [(self.provider, None)]
        if self.provider == "rustxng":
            return [("rustxng", rx.url if rx else None)]
        out = []
        if tav:
            out.append(("tavily", tav))
        if brave:
            out.append(("brave", brave))
        out += [("duckduckgo", None), ("tavily-keyless", None)]
        if rx is not None and rx.usable():
            out.append(("rustxng", rx.url))
        return out

    def pick(self) -> tuple[str, str | None]:
        return self.chain()[0]

    def paused_minutes(self, chain=None) -> int:
        """0 when some provider in the chain can be tried now, otherwise minutes until the first one can."""
        now = time.time()
        waits = [self.paused_until.get(name, 0) for name, _ in (chain or self.chain())]
        return 0 if any(w <= now for w in waits) else int((min(waits) - now) / 60) + 1

    def view(self) -> dict:
        chain = self.chain()
        now = time.time()

        def mins(n):
            until = self.paused_until.get(n, 0)
            return int((until - now) / 60) + 1 if until > now else 0
        return {"mode": self.provider, "keys": {p: any(n == p and k for n, k in chain) for p in KEY_ENVS},
                "order": [{"name": n, "label": LABELS[n], "paused_minutes": mins(n)} for n, _ in chain],
                "paused_minutes": self.paused_minutes(chain), "last": self.last,
                "rustxng": self.rustxng.view() if self.rustxng is not None else None}

    def run(self, name: str, query: str, count: int, key: str | None) -> list[dict]:
        if name == "tavily":
            return search_tavily(query, count, key)
        if name == "tavily-keyless":
            return search_tavily(query, count, None)
        if name == "brave":
            return search_brave(query, count, key)
        if name == "rustxng":
            if not key:
                raise SearchError("config", "RustXNG is selected but not installed (Settings > Web search)")
            return search_rustxng(query, count, key)
        return search_duckduckgo(query, count)

    def search(self, query: str, count: int = 5) -> dict:
        query = " ".join((query or "").split())[:300]
        if not query:
            raise SearchError("config", "empty query")
        if self.redactor:
            query = self.redactor.clean(query)
        count = max(1, min(int(count or 5), 10))
        chain = self.chain()
        if len(chain) == 1 and chain[0][0] in KEY_ENVS and not chain[0][1]:
            raise SearchError("config", f"{chain[0][0]} is selected but its API key is not set (Settings > Web search)")
        notes, stopped, captcha, other = [], False, False, None
        for name, key in chain:
            label = LABELS[name]
            until = self.paused_until.get(name, 0)
            if until > time.time():
                mins = int((until - time.time()) / 60) + 1
                notes.append(f"{label} said its limit was used up and is paused for about {mins} more minutes")
                stopped = True
                continue
            try:
                results = self.run(name, query, count, key)
            except SearchError as e:
                if e.kind == "limit":
                    self.paused_until[name] = time.time() + (e.retry_after or self.cooldown_s)
                    self.pause_kind[name] = e.kind
                    stopped = True
                elif e.kind == "captcha":
                    captcha = True
                else:
                    other = e
                notes.append(e.message if e.message.startswith(label.split()[0]) else f"{label}: {e.message}")
                continue
            self.last = {"provider": name, "at": time.time()}
            out = {"provider": name, "query": query, "results": results}
            if notes:
                out["skipped"] = notes
            if name in NOTES:
                out["note"] = NOTES[name]
            return out
        msg = "; ".join(notes) + "."
        if stopped and self.paused_minutes(chain):
            msg += f" The paused sources can be asked again in about {self.paused_minutes(chain)} minutes."
        if not any(k for n, k in chain if n in KEY_ENVS):
            msg += (" These free sources refuse searches more often on a connection that shares its public address"
                    " with many people. With a Tavily or Brave Search API key (Settings > Web search) search does not"
                    " depend on them.")
        raise SearchError("captcha" if captcha else "limit" if stopped else (other.kind if other else "network"), msg)


# ------------------------------------------------------------------ reading pages

def host_status(host: str) -> str:
    """"public" when every address the host resolves to is a public internet address, "unknown" when the name
    does not resolve at all (no such site, or DNS failed), otherwise "local"."""
    if not host or host.lower() in ("localhost",) or host.lower().endswith((".local", ".localhost", ".internal", ".ts.net", ".lan", ".home.arpa")):
        return "local"
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return "unknown"
    if not infos:
        return "unknown"
    for info in infos:
        if not ip_is_public(info[4][0]):
            return "local"
    return "public"


def host_is_public(host: str) -> bool:
    """True only if every address the host resolves to is a public internet address."""
    return host_status(host) == "public"


class _TextParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "canvas", "head", "nav", "footer", "form"}
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "section",
             "article", "header", "pre", "blockquote", "dd", "dt", "hr", "main"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li" and not self._skip:
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        t = "".join(self.parts)
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t)
        return "\n".join(ln.strip() for ln in t.splitlines()).strip()


def html_to_text(body: str) -> tuple[str, str]:
    p = _TextParser()
    try:
        p.feed(body)
        p.close()
    except Exception:  # noqa: BLE001 - broken HTML still gives us what was parsed
        pass
    return " ".join(p.title.split())[:200], p.text()


TEXT_TYPES = ("text/html", "text/plain", "application/xhtml+xml", "application/json", "text/markdown", "text/xml",
              "application/xml", "text/csv")


def read_page(url: str, *, max_bytes: int = 1_500_000, max_chars: int = 12000, redactor=None,
              resolver=host_is_public) -> dict:
    url = (url or "").strip()
    if redactor:
        url = redactor.clean(url)
    auth_header = {}
    for _ in range(31):           # follow up to 30 redirects (a loop guard), checking every hop
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https"):
            raise SearchError("blocked", "only http and https pages can be read")
        auth_header = {}
        if p.username is not None or p.password is not None:
            # user:password@host is sent as HTTP Basic authentication to that host only (not along redirects).
            import base64
            pair = f"{urllib.parse.unquote(p.username or '')}:{urllib.parse.unquote(p.password or '')}"
            auth_header = {"Authorization": "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")}
            url = urllib.parse.urlunparse(p._replace(netloc=p.netloc.rsplit("@", 1)[-1]))
        if not resolver(p.hostname or ""):
            if resolver is host_is_public and host_status(p.hostname or "") == "unknown":
                raise SearchError("network", f"{p.hostname} was not found: no such site, or the name could not be looked up")
            raise SearchError("blocked", "this address is local, private or on the tailnet; SnapLab only reads public pages")
        status, headers, body, _ = _http(url, headers={"Accept": "text/html,text/plain;q=0.9,*/*;q=0.1", **auth_header},
                                         max_bytes=max_bytes)
        if status in (301, 302, 303, 307, 308):
            loc = {k.lower(): v for k, v in headers.items()}.get("location")
            if not loc:
                raise SearchError("http", f"redirect without a target (HTTP {status})")
            url = urllib.parse.urljoin(url, loc)
            continue
        break
    else:
        raise SearchError("http", "too many redirects")
    hdr = {k.lower(): v for k, v in headers.items()}
    ctype = (hdr.get("content-type") or "").split(";")[0].strip().lower()
    if ctype and not ctype.startswith(TEXT_TYPES):
        raise SearchError("blocked", f"not a text page ({ctype}); only text is read")
    truncated_bytes = len(body) > max_bytes
    body = body[:max_bytes]
    m = re.search(r"charset=([\w\-]+)", hdr.get("content-type") or "", re.I)
    enc = m.group(1) if m else None
    if not enc:
        m2 = re.search(rb"<meta[^>]+charset=[\"']?([\w\-]+)", body[:4000], re.I)
        enc = m2.group(1).decode("ascii", "ignore") if m2 else "utf-8"
    try:
        text = body.decode(enc, "replace")
    except LookupError:
        text = body.decode("utf-8", "replace")
    if looks_like_captcha(status, text):
        raise SearchError("captcha", "the page answered with a human check (CAPTCHA) instead of its content")
    if status >= 400:
        raise SearchError("http", f"the page answered HTTP {status}")
    if ctype in ("text/html", "application/xhtml+xml") or (not ctype and "<html" in text[:2000].lower()):
        title, plain = html_to_text(text)
    else:
        title, plain = "", text
    plain = html.unescape(plain) if "&#" in plain[:2000] else plain
    truncated = truncated_bytes or len(plain) > max_chars
    full = plain
    if len(plain) > max_chars:
        plain = plain[:max_chars]
    return {"url": url, "title": title, "text": plain, "truncated": truncated, "content_type": ctype or "unknown",
            "full_text": full}
