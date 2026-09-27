"""RemoteAccessGuard — 远程访问安全与网络信任守卫深模块 (Deep Module).

统一处理：
1. 客户端网络区域分类 (Localhost, Tailscale 私有网, 局域网 LAN, 公网 WAN)
2. 跨网络访问的 Bearer Token / API Key 安全鉴权
3. Tailscale 身份透传与安全审计
4. Starlette / ASGI 守卫中间件
5. 浏览器侧攻击防护：状态变更请求的 Origin/Referer 校验 (CSRF) 与
   Host 头白名单 (DNS rebinding)。IP 白名单挡不住"用户浏览器里的恶意网页"，
   因为那类请求的源 IP 正是受信任的本机/局域网。
"""
import os
import socket
import struct
import ipaddress
import logging
from typing import Optional, Dict, Any, List, Iterable, Tuple
from urllib.parse import urlsplit
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

logger = logging.getLogger("literature_analyzer.guard")

# 受信任的私有子网网段定义
TRUSTED_SUBNETS = [
    ipaddress.ip_network("127.0.0.0/8"),          # IPv4 Loopback
    ipaddress.ip_network("::1/128"),               # IPv6 Loopback
    ipaddress.ip_network("100.64.0.0/10"),         # Tailscale / Carrier-grade NAT
    ipaddress.ip_network("10.0.0.0/8"),            # Class A Private LAN
    ipaddress.ip_network("172.16.0.0/12"),         # Class B Private LAN / Docker
    ipaddress.ip_network("192.168.0.0/16"),        # Class C Private LAN
    ipaddress.ip_network("fd00::/8"),              # IPv6 ULA (Tailscale IPv6)
]


class NetworkClassification:
    LOCALHOST = "localhost"
    TAILSCALE = "tailscale"
    LAN = "lan"
    WAN = "wan"


class RemoteAccessGuard:
    """远程访问安全策略核心引擎。"""

    def __init__(
        self,
        auth_token: Optional[str] = None,
        allow_public_wan: bool = False,
        trusted_cidrs: Optional[List[str]] = None,
        trusted_proxy_cidrs: Optional[List[str]] = None,
    ):
        self._auth_token = auth_token or os.getenv("REMOTE_AUTH_TOKEN") or os.getenv("MCP_AUTH_TOKEN")
        self._allow_public_wan = allow_public_wan or (os.getenv("ALLOW_PUBLIC_WAN", "0").lower() in ("1", "true"))
        self._custom_subnets = []
        proxy_cidrs = trusted_proxy_cidrs if trusted_proxy_cidrs is not None else (
            os.getenv("REMOTE_TRUSTED_PROXY_CIDRS", "").split(","))
        self._proxy_subnets = [ipaddress.ip_network(c.strip()) for c in proxy_cidrs if c.strip()]
        if trusted_cidrs:
            for c in trusted_cidrs:
                try:
                    self._custom_subnets.append(ipaddress.ip_network(c.strip()))
                except Exception as e:
                    logger.warning(f"无法解析受信任 CIDR: {c}: {e}")

    def classify_ip(self, ip_str: str) -> str:
        """对客户端 IP 地址进行网络区域分类。"""
        if not ip_str:
            return NetworkClassification.WAN
        
        clean_ip = ip_str.strip()
        if clean_ip.startswith("[") and "]" in clean_ip:
            clean_ip = clean_ip[1:clean_ip.index("]")]
        elif ":" in clean_ip and clean_ip.count(":") == 1:
            clean_ip = clean_ip.split(":")[0]

        if clean_ip in ("localhost", "127.0.0.1", "::1", "testclient", "testserver"):
            return NetworkClassification.LOCALHOST

        try:
            ip_obj = ipaddress.ip_address(clean_ip)
        except ValueError:
            return NetworkClassification.WAN

        if ip_obj.is_loopback or ip_obj in ipaddress.ip_network("127.0.0.0/8") or ip_obj in ipaddress.ip_network("::1/128"):
            return NetworkClassification.LOCALHOST

        # 检查 Tailscale CGNAT 范围 (100.64.0.0/10) 或 Tailscale IPv6 ULA
        tailscale_v4 = ipaddress.ip_network("100.64.0.0/10")
        tailscale_v6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
        if (ip_obj.version == 4 and ip_obj in tailscale_v4) or (ip_obj.version == 6 and ip_obj in tailscale_v6):
            return NetworkClassification.TAILSCALE

        # 检查标准私有 LAN (RFC 1918)
        rfc1918_subnets = [
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16")
        ]
        for subnet in rfc1918_subnets:
            if ip_obj.version == subnet.version and ip_obj in subnet:
                return NetworkClassification.LAN

        # 检查自定义子网
        for subnet in self._custom_subnets:
            if ip_obj.version == subnet.version and ip_obj in subnet:
                return NetworkClassification.LAN

        return NetworkClassification.WAN

    def _is_proxy(self, address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip.version == net.version and ip in net for net in self._proxy_subnets)

    def client_ip(self, peer: str, headers: Dict[str, str]) -> str:
        """Only configured proxies may supply a forwarding chain.

        Walk from the socket towards the client, stopping at the first untrusted
        hop; a forged prefix can never override that hop. A configured proxy
        without valid forwarding data fails closed instead of inheriting LAN trust.
        """
        if not self._is_proxy(peer):
            return peer
        forwarded = headers.get("x-forwarded-for") or headers.get("x-real-ip", "")
        hops = [hop.strip() for hop in forwarded.split(",")]
        try:
            for hop in hops:
                ipaddress.ip_address(hop)
        except ValueError:
            return "unknown-proxy-client"
        for hop in reversed(hops):
            if not self._is_proxy(hop):
                return hop
        return "unknown-proxy-client"

    def is_trusted_network(self, ip_str: str) -> bool:
        """判断是否属于免鉴权的受信任网络区域 (Local / Tailscale / Private LAN)。"""
        zone = self.classify_ip(ip_str)
        return zone in (NetworkClassification.LOCALHOST, NetworkClassification.TAILSCALE, NetworkClassification.LAN)

    def verify_request_access(self, client_ip: str, headers: Dict[str, str]) -> tuple[bool, str]:
        """验证请求是否允许访问。返回 (is_allowed, reason)。"""
        zone = self.classify_ip(client_ip)
        tailscale_user = headers.get("tailscale-user-login") or headers.get("tailscale-user-name")

        # 1. 如果来自受信任网络（本地回环、Tailscale 专网、局域网），天然放行
        if zone in (NetworkClassification.LOCALHOST, NetworkClassification.TAILSCALE, NetworkClassification.LAN):
            user_info = f" (Tailscale用户: {tailscale_user})" if tailscale_user else ""
            return True, f"Trusted network zone: {zone}{user_info}"

        # 2. 如果来自公网 (WAN)，检查是否配置并提供了合法的 Auth Token
        if self._auth_token:
            auth_hdr = headers.get("authorization", "")
            api_key = headers.get("x-api-key", "")
            token = ""
            if auth_hdr.lower().startswith("bearer "):
                token = auth_hdr[7:].strip()
            elif api_key:
                token = api_key.strip()

            if token and token == self._auth_token:
                return True, "Authenticated via Token from WAN"
            return False, "Unauthorized: Public WAN request requires valid Bearer token"

        # 3. 未配置 Auth Token 且来自公网
        if not self._allow_public_wan:
            return False, "Forbidden: Untrusted external IP access rejected (ALLOW_PUBLIC_WAN=0)"

        return True, "Public WAN access allowed (allow_public_wan=True)"


# ── 浏览器侧防护：CSRF 与 DNS rebinding ────────────────────────────────────

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def cors_origins() -> List[str]:
    """WEB_CORS_ORIGINS 逗号分隔的跨域白名单；默认空 = 不开放任何跨域。

    前端 web_static 同源访问，不需要 CORS。通配 "*" 会让用户浏览的任意网站
    借其 IP 身份调用本 API，因此显式忽略。Web 与 MCP HTTP 共用这一白名单
    （既用于 CORSMiddleware，也用于状态变更请求的 Origin 校验）。
    """
    raw = os.getenv("WEB_CORS_ORIGINS", "")
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip() and o.strip() != "*"]


def remote_allowed_hosts() -> List[str]:
    """REMOTE_ALLOWED_HOSTS：除 IP 字面量/localhost/本机主机名外额外允许的 Host 名。

    逗号分隔；"name" 精确匹配（端口任意），".example.ts.net" 匹配其所有子域；
    单独一个 "*" 表示关闭 Host 校验（不推荐）。
    """
    raw = os.getenv("REMOTE_ALLOWED_HOSTS", "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def _split_host_port(hostport: str, scheme: str = "http") -> Tuple[str, Optional[int]]:
    """把 "host[:port]" / "[v6]:port" 拆成 (小写主机名, 端口)，端口缺省按 scheme 补齐。"""
    hp = (hostport or "").strip().lower()
    host, port = hp, None
    if hp.startswith("["):
        end = hp.find("]")
        host = hp[1:end] if end > 0 else hp
        rest = hp[end + 1:] if end > 0 else ""
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
    elif hp.count(":") == 1:
        h, _, p = hp.partition(":")
        host = h
        if p.isdigit():
            port = int(p)
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    return host.rstrip("."), port


def _origin_key(url: str) -> Optional[Tuple[str, Optional[int]]]:
    """Origin/Referer → (host, port)；无法解析返回 None。"""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return _split_host_port(netloc, parts.scheme)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def local_hostnames() -> List[str]:
    names = {"localhost"}
    try:
        names.add(socket.gethostname().lower())
        names.add(socket.getfqdn().lower())
    except OSError:
        pass
    return sorted(n for n in names if n)


def is_allowed_host(host_header: Optional[str], extra: Optional[Iterable[str]] = None) -> bool:
    """DNS rebinding 防护：Host 必须是 IP 字面量、localhost、单标签名、本机主机名或显式白名单。

    重绑定攻击里浏览器发出的 Host 是攻击者自己的域名；IP 字面量不可能是
    攻击者可控的名字，所以对 IP 一律放行，Tailscale/LAN 直连 IP 的客户端不受影响。
    """
    extra_list = list(remote_allowed_hosts() if extra is None else extra)
    if "*" in extra_list:
        return True
    if not host_header:
        return True  # HTTP/1.0 等无 Host 的请求不可能来自重绑定的浏览器页面
    host, _ = _split_host_port(host_header)
    if not host:
        return False
    if _is_ip_literal(host):
        return True
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if "." not in host:
        # 单标签名（dudu-sv、Starlette 的 testserver）无法经公网 DNS 解析，攻击者注册不了
        return True
    if host in local_hostnames():
        return True
    for allowed in extra_list:
        allowed = allowed.lower().rstrip(".")
        if allowed.startswith(".") and host.endswith(allowed):
            return True
        if host == _split_host_port(allowed)[0]:
            return True
    return False


def check_csrf(method: str, headers: Dict[str, str], scheme: str = "http",
               allowed_origins: Optional[Iterable[str]] = None) -> Tuple[bool, str]:
    """状态变更请求的来源校验。headers 需为小写键。返回 (允许, 原因)。

    - 安全方法 (GET/HEAD/OPTIONS) 不校验；
    - 无 Origin、无 Referer 且无跨站 Sec-Fetch-Site：非浏览器客户端 (curl/MCP)，放行；
    - Origin 为 "null"（沙箱 iframe、file://、跨站重定向）一律拒绝；
    - Origin (缺失时退回 Referer) 与 Host 同源，或在 WEB_CORS_ORIGINS 白名单内，放行；
    - 浏览器声明 Sec-Fetch-Site: same-origin 视为同源（该头网页无法伪造，
      可兼容反代改写 Host 的部署），cross-site/same-site 且不在白名单则拒绝。
    """
    if method.upper() not in UNSAFE_METHODS:
        return True, "safe method"
    allow = list(cors_origins() if allowed_origins is None else allowed_origins)
    allow_keys = {k for k in (_origin_key(o) for o in allow) if k}
    fetch_site = (headers.get("sec-fetch-site") or "").strip().lower()
    origin = headers.get("origin")
    referer = headers.get("referer")

    if origin is not None and origin.strip().lower() == "null":
        return False, "Origin: null is not allowed for state-changing requests"

    source = origin if origin else referer
    if not source:
        if fetch_site in ("cross-site", "same-site"):
            return False, f"Cross-origin request (Sec-Fetch-Site: {fetch_site}) without allowed Origin"
        return True, "no browser origin (non-browser client)"

    src_key = _origin_key(source)
    if src_key is None:
        return False, "Unparseable Origin/Referer"
    if src_key in allow_keys:
        return True, "origin in WEB_CORS_ORIGINS allowlist"
    if fetch_site == "same-origin":
        return True, "Sec-Fetch-Site: same-origin"
    if fetch_site in ("cross-site", "same-site"):
        return False, f"Cross-origin request from {source} (Sec-Fetch-Site: {fetch_site})"
    host_key = _split_host_port(headers.get("host", ""), scheme)
    if host_key[0] and src_key == host_key:
        return True, "same origin"
    return False, f"Cross-origin request from {source} is not in WEB_CORS_ORIGINS"


# ── MCP 传输层 DNS rebinding 设置 ──────────────────────────────────────────

_SIOCGIFADDR = 0x8915


def local_interface_ips() -> List[str]:
    """枚举本机网卡地址（Linux：ioctl 取 IPv4，/proc/net/if_inet6 取 IPv6）。

    纯本地系统调用、不发网络包；非 Linux 或失败时返回空列表。
    """
    ips: List[str] = []
    try:
        import fcntl  # noqa: WPS433 (POSIX only)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for _, name in socket.if_nameindex():
                try:
                    packed = fcntl.ioctl(s.fileno(), _SIOCGIFADDR,
                                         struct.pack("256s", name[:15].encode()))
                    ips.append(socket.inet_ntoa(packed[20:24]))
                except OSError:
                    continue
    except (ImportError, OSError, AttributeError):
        pass
    try:
        with open("/proc/net/if_inet6") as fh:
            for line in fh:
                raw = line.split()[0]
                if len(raw) == 32:
                    ips.append(str(ipaddress.ip_address(int(raw, 16))))
    except OSError:
        pass
    return sorted(set(ips))


def mcp_allowed_hosts_and_origins(bind_host: str = "", port: Optional[int] = None,
                                  interface_ips: Optional[Iterable[str]] = None,
                                  extra_hosts: Optional[Iterable[str]] = None,
                                  extra_origins: Optional[Iterable[str]] = None
                                  ) -> Tuple[bool, List[str], List[str]]:
    """计算 mcp TransportSecuritySettings 的 (enable, allowed_hosts, allowed_origins)。

    mcp 自带校验只支持精确匹配与 "host:*"，这里展开为本机全部网卡地址 (含 Tailscale
    100.x 与 LAN)、主机名与 REMOTE_ALLOWED_HOSTS 全名，作为显式清单；实际成员判断见
    build_mcp_transport_security 的 _PolicyList。REMOTE_ALLOWED_HOSTS="*" 时返回
    enable=False（显式退回宽松模式）。
    """
    extras = list(remote_allowed_hosts() if extra_hosts is None else extra_hosts)
    if "*" in extras:
        return False, ["*"], ["*"]
    names = ["127.0.0.1", "localhost", "::1"]
    if bind_host and bind_host not in ("0.0.0.0", "::", ""):
        names.append(bind_host)
    names += list(local_interface_ips() if interface_ips is None else interface_ips)
    names += local_hostnames()
    names += [h for h in extras if not h.startswith(".")]

    hosts: List[str] = []
    origins: List[str] = []
    for n in names:
        n = n.strip().lower()
        if not n:
            continue
        base, _ = _split_host_port(n) if not _is_ip_literal(n) else (n, None)
        h = f"[{base}]" if ":" in base else base
        for item in (h, f"{h}:*"):
            if item not in hosts:
                hosts.append(item)
        for scheme in ("http", "https"):
            for item in (f"{scheme}://{h}", f"{scheme}://{h}:*"):
                if item not in origins:
                    origins.append(item)
    for o in (cors_origins() if extra_origins is None else extra_origins):
        if o not in origins:
            origins.append(o)
    return True, hosts, origins


class _PolicyList(list):
    """内容为启动时展开的显式白名单（mcp 若改为复制/遍历列表仍然可用），
    成员判断额外委托给与 Web 守卫相同的策略函数，保证两层校验口径一致，
    且网卡换 IP 后无需重启。"""

    def __init__(self, items, predicate):
        super().__init__(items)
        self._predicate = predicate

    def __contains__(self, item):
        return list.__contains__(self, item) or (isinstance(item, str) and self._predicate(item))


def _origin_allowed(origin: str, extra_hosts: Optional[Iterable[str]],
                    extra_origins: Optional[Iterable[str]]) -> bool:
    allow = list(cors_origins() if extra_origins is None else extra_origins)
    key = _origin_key(origin)
    if key is None:
        return False
    if key in {k for k in (_origin_key(o) for o in allow) if k}:
        return True
    netloc = urlsplit(origin.strip()).netloc.rsplit("@", 1)[-1]
    return is_allowed_host(netloc, extra=extra_hosts)


def build_mcp_transport_security(bind_host: str = "", port: Optional[int] = None,
                                 interface_ips: Optional[Iterable[str]] = None,
                                 extra_hosts: Optional[Iterable[str]] = None,
                                 extra_origins: Optional[Iterable[str]] = None):
    """返回启用了 DNS rebinding 防护的 mcp TransportSecuritySettings。

    Host/Origin 判定与 RemoteAccessGuardMiddleware 的 is_allowed_host 一致：
    IP 字面量、localhost、单标签主机名、本机主机名、REMOTE_ALLOWED_HOSTS。
    """
    from mcp.server.transport_security import TransportSecuritySettings
    enable, hosts, origins = mcp_allowed_hosts_and_origins(
        bind_host, port, interface_ips=interface_ips, extra_hosts=extra_hosts, extra_origins=extra_origins)
    ts = TransportSecuritySettings(enable_dns_rebinding_protection=enable,
                                   allowed_hosts=hosts, allowed_origins=origins)
    if enable:
        extras = None if extra_hosts is None else list(extra_hosts)
        extra_o = None if extra_origins is None else list(extra_origins)
        # 赋值不触发 pydantic 校验（validate_assignment 默认关闭），保留自定义成员判断
        ts.allowed_hosts = _PolicyList(hosts, lambda h: is_allowed_host(h, extra=extras))
        ts.allowed_origins = _PolicyList(origins, lambda o: _origin_allowed(o, extras, extra_o))
    return ts


class RemoteAccessGuardMiddleware(BaseHTTPMiddleware):
    """Starlette 远程访问安全中间件。"""

    def __init__(self, app, guard: Optional[RemoteAccessGuard] = None):
        super().__init__(app)
        self.guard = guard or RemoteAccessGuard()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 静态资源与健康检查始终放行
        if request.url.path in ("/favicon.ico", "/api/health", "/api/ping"):
            return await call_next(request)

        headers_dict = {k.lower(): v for k, v in request.headers.items()}
        client_ip = self.guard.client_ip(request.client.host if request.client else "", headers_dict)
        allowed, reason = self.guard.verify_request_access(client_ip, headers_dict)

        if not allowed:
            logger.warning(f"🚫 拦截未授权远程访问: IP={client_ip} Path={request.url.path} 原因={reason}")
            return JSONResponse(
                status_code=403,
                content={
                    "error": "Forbidden: Remote access denied",
                    "reason": reason,
                    "client_ip": client_ip,
                    "tip": "请通过 Tailscale 专网 (100.83.137.43) 或局域网访问，或提供有效 Authorization Bearer Token。"
                }
            )

        if not is_allowed_host(headers_dict.get("host")):
            logger.warning(f"🚫 拦截非法 Host (疑似 DNS rebinding): Host={headers_dict.get('host')} IP={client_ip} Path={request.url.path}")
            return JSONResponse(
                status_code=421,
                content={
                    "error": "Misdirected Request: Host not allowed",
                    "host": headers_dict.get("host"),
                    "tip": "用 IP / localhost / 本机主机名访问，或把该域名加入 REMOTE_ALLOWED_HOSTS。"
                }
            )

        csrf_ok, csrf_reason = check_csrf(request.method, headers_dict, request.url.scheme)
        if not csrf_ok:
            logger.warning(f"🚫 拦截跨站状态变更请求: IP={client_ip} {request.method} {request.url.path} 原因={csrf_reason}")
            return JSONResponse(
                status_code=403,
                content={
                    "error": "Forbidden: cross-origin request rejected",
                    "reason": csrf_reason,
                    "tip": "如需从其他前端域名调用，请把完整 Origin 加入 WEB_CORS_ORIGINS。"
                }
            )

        return await call_next(request)
