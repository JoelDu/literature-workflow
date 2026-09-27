"""RemoteAccessGuard — 远程访问安全与网络信任守卫深模块 (Deep Module).

统一处理：
1. 客户端网络区域分类 (Localhost, Tailscale 私有网, 局域网 LAN, 公网 WAN)
2. 跨网络访问的 Bearer Token / API Key 安全鉴权
3. Tailscale 身份透传与安全审计
4. Starlette / ASGI 守卫中间件
"""
import os
import ipaddress
import logging
from typing import Optional, Dict, Any, List
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

        return await call_next(request)
