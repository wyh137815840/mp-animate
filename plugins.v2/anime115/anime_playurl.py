"""按访问来源选择 strm 播放地址。不访问 115。"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from .anime_config import PLAY_BASE, PLAY_BASE_LAN
from .anime_strm import build_play_url


REDIRECT_PATH = "/api/v1/plugin/Anime115/redirect_url"
DEFAULT_LAN_CIDRS = (
    "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
    "169.254.0.0/16,::1/128,fc00::/7,fe80::/10"
)
DEFAULT_PUBLIC_HOSTS = "mp.yuanhot.top"
STRM_MODES = ("lan", "public", "redirect")
_LIST_SPLIT = re.compile(r"[\s,;，；]+")
_WAN_HEADER = {"wan", "public", "remote"}


class PlayUrlError(ValueError):
    """当前网络下不能给出安全的播放地址。"""


@dataclass(frozen=True)
class PlayDecision:
    """一次播放请求该去哪个地址。"""

    intranet: bool
    url: str
    reason: str


def parse_list(value: str) -> tuple[str, ...]:
    """把逗号、分号或换行分隔的配置拆成小写条目。"""
    items = []
    for part in _LIST_SPLIT.split((value or "").strip()):
        text = part.strip().lower().rstrip(".")
        if text:
            items.append(text)
    return tuple(items)


def parse_cidrs(value: str) -> tuple[ipaddress._BaseNetwork, ...]:
    """解析内网网段。写错就报错，避免把局域网静默当成外网。"""
    networks = []
    for part in parse_list(value):
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError as exc:
            raise PlayUrlError(f"内网网段无效：{part}") from exc
    return tuple(networks)


def _parse_ip(value: str) -> ipaddress._BaseAddress | None:
    raw = (value or "").strip().strip("[]")
    if not raw:
        return None
    if "%" in raw:
        raw = raw.split("%", 1)[0]
    try:
        parsed = ipaddress.ip_address(raw)
    except ValueError:
        return None
    mapped = getattr(parsed, "ipv4_mapped", None)
    return mapped or parsed


def _is_private(ip: ipaddress._BaseAddress) -> bool:
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local)


def _header(headers: Mapping[str, str], name: str) -> str:
    lowered = {str(key).lower(): str(val) for key, val in headers.items()}
    return (lowered.get(name.lower()) or "").strip()


def _via_cloudflare(headers: Mapping[str, str]) -> bool:
    return bool(_header(headers, "cf-connecting-ip") or _header(headers, "cf-ray"))


def _forwarded_ip(headers: Mapping[str, str]) -> str:
    real = _header(headers, "x-real-ip")
    if real:
        return real.split(",")[0].strip()
    forwarded = _header(headers, "x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return ""


def client_ip(peer_ip: str, headers: Mapping[str, str]) -> str:
    """取出实际客户端地址。只有内网对端才相信转发头。"""
    peer = _parse_ip(peer_ip)
    forwarded = _forwarded_ip(headers)
    if peer is not None and _is_private(peer) and forwarded:
        return forwarded
    return (peer_ip or "").strip()


def host_name(value: str) -> str:
    """从 Host 头里取出主机名，去掉端口。"""
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        return raw[1:end] if end > 1 else raw.strip("[]")
    if raw.count(":") == 1:
        return raw.split(":", 1)[0].rstrip(".")
    return raw.rstrip(".")


def _ip_in(ip_text: str, networks: tuple[ipaddress._BaseNetwork, ...]) -> bool:
    parsed = _parse_ip(ip_text)
    if parsed is None:
        return False
    return any(parsed in network for network in networks)


def classify_request(
    *,
    peer_ip: str,
    host: str,
    headers: Mapping[str, str] | None = None,
    lan_cidrs: str = DEFAULT_LAN_CIDRS,
    public_hosts: str = DEFAULT_PUBLIC_HOSTS,
    lan_hosts: str = "",
) -> tuple[bool, str]:
    """判断这次请求是不是内网。

    Cloudflare 进来的请求一律算外网。头发绕回源之后看不到局域网地址。
    ``X-Anime115-Net`` 只在没有 Cloudflare 头时生效。
    """
    safe_headers = headers or {}
    if _via_cloudflare(safe_headers):
        return False, "cloudflare"
    mark = _header(safe_headers, "x-anime115-net").lower()
    if mark == "lan":
        return True, "header"
    if mark in _WAN_HEADER:
        return False, "header"
    networks = parse_cidrs(lan_cidrs or DEFAULT_LAN_CIDRS)
    ip_text = client_ip(peer_ip, safe_headers)
    if ip_text:
        if not _ip_in(ip_text, networks):
            return False, "public"
        return True, "client-ip"
    name = host_name(host)
    if name and (name in parse_list(lan_hosts) or _ip_in(name, networks)):
        return True, "host"
    if name and name in parse_list(public_hosts):
        return False, "public-host"
    return False, "public"


def origin_of(template: str) -> str:
    """取出模板的 scheme 和主机，不含路径。"""
    parsed = urlsplit((template or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PlayUrlError("播放地址需要以 http:// 或 https:// 开头")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _explicit_port(parsed) -> int:
    if parsed.port:
        return parsed.port
    if parsed.scheme.lower() == "https":
        return 443
    if parsed.scheme.lower() == "http":
        return 80
    return 0


def origins_equal(left: str, right: str) -> bool:
    """比较两个源站。默认端口和写出来的端口视为同一个。"""
    first = urlsplit(left)
    second = urlsplit(right)
    return (
        first.scheme.lower() == second.scheme.lower()
        and (first.hostname or "").lower().rstrip(".") == (second.hostname or "").lower().rstrip(".")
        and _explicit_port(first) == _explicit_port(second)
    )


def public_host_set(public_template: str, extra: str = "") -> frozenset[str]:
    """外网域名：配置项加上外网模板自己的主机名。"""
    hosts = set(parse_list(extra))
    host = (urlsplit(public_template or "").hostname or "").lower().rstrip(".")
    if host:
        hosts.add(host)
    return frozenset(hosts)


def _private_host(hostname: str) -> bool:
    name = (hostname or "").lower().rstrip(".")
    if name in {"localhost", "localhost.localdomain"}:
        return True
    parsed = _parse_ip(name)
    return parsed is not None and _is_private(parsed)


def _same_redirect(url: str, request_host: str, request_path: str) -> bool:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/") or "/"
    if path != REDIRECT_PATH.rstrip("/"):
        return False
    current = (request_path or "").rstrip("/") or "/"
    if current != path and not current.endswith(path):
        return False
    target = (parsed.hostname or request_host or "").lower().rstrip(".")
    return not parsed.hostname or target == host_name(request_host)


def strm_template(
    play_base: str,
    play_base_lan: str = "",
    strm_for: str = "",
    public_hosts: str = "",
) -> str:
    """选出写入 strm 的模板。

    空的 ``strm_for`` 保持旧行为，只用外网 ``play_base``。
    ``lan`` 用内网地址，结果里不能再出现外网域名。
    ``redirect`` 写内网源上的本插件跳转。
    """
    mode = (strm_for or "").strip().lower()
    public = (play_base or "").strip() or PLAY_BASE
    lan = (play_base_lan or "").strip()
    if mode in ("", "public"):
        return public
    if mode not in STRM_MODES:
        raise PlayUrlError("strm 地址只能是 lan、public 或 redirect")
    if not lan:
        lan = PLAY_BASE_LAN if mode == "lan" else ""
    if not lan:
        raise PlayUrlError("没有填写内网播放地址")
    if origins_equal(origin_of(public), origin_of(lan)):
        raise PlayUrlError("内网播放地址和外网播放地址不能相同")
    if mode == "redirect":
        base = origin_of(lan)
        chosen = f"{base}{REDIRECT_PATH}?pickcode={{pickcode}}&file_name={{filename}}"
    else:
        chosen = lan
    host = (urlsplit(chosen).hostname or "").lower().rstrip(".")
    if host in public_host_set(public, public_hosts):
        raise PlayUrlError(f"内网 strm 不能使用外网域名 {host}")
    return chosen


def play_url_for_strm(
    play_base: str,
    pickcode: str,
    filename: str,
    play_base_lan: str = "",
    strm_for: str = "",
    public_hosts: str = "",
) -> str:
    """按 strm 模式生成一行播放地址。"""
    template = strm_template(play_base, play_base_lan, strm_for, public_hosts)
    return build_play_url(template, pickcode, filename)


def location_for(
    *,
    intranet: bool,
    public_template: str,
    lan_template: str,
    pickcode: str,
    filename: str,
    public_hosts: str = "",
    request_host: str = "",
    request_path: str = "",
) -> str:
    """选出 302 目标。内网结果里不能再出现外网域名。"""
    template = (lan_template if intranet else public_template) or ""
    template = template.strip()
    if not template:
        raise PlayUrlError("没有填写对应的播放地址")
    url = build_play_url(template, pickcode, filename)
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    blocked = public_host_set(public_template or PLAY_BASE, public_hosts)
    if intranet and host in blocked:
        raise PlayUrlError(f"内网播放不能使用外网域名 {host}")
    if not intranet and _private_host(host):
        raise PlayUrlError(f"外网播放不能使用内网地址 {host}")
    if _same_redirect(url, request_host, request_path):
        raise PlayUrlError("播放地址指向了 Anime115 自己的跳转，请改成 115网盘STRM助手或内网直链")
    return url


def decide_location(
    *,
    peer_ip: str,
    host: str,
    headers: Mapping[str, str] | None,
    public_template: str,
    lan_template: str,
    pickcode: str,
    filename: str,
    lan_cidrs: str = DEFAULT_LAN_CIDRS,
    public_hosts: str = DEFAULT_PUBLIC_HOSTS,
    request_path: str = "",
) -> PlayDecision:
    """按这次请求选择播放地址。"""
    intranet, reason = classify_request(
        peer_ip=peer_ip,
        host=host,
        headers=headers,
        lan_cidrs=lan_cidrs,
        public_hosts=public_hosts,
    )
    url = location_for(
        intranet=intranet,
        public_template=public_template,
        lan_template=lan_template,
        pickcode=pickcode,
        filename=filename,
        public_hosts=public_hosts,
        request_host=host,
        request_path=request_path,
    )
    return PlayDecision(intranet=intranet, url=url, reason=reason)


def nginx_snippet(public_template: str = PLAY_BASE, lan_template: str = PLAY_BASE_LAN) -> str:
    """生成只放在 Emby 公网反代里的替换配置。"""
    public_origin = origin_of(public_template)
    lan_origin = origin_of(lan_template)
    if origins_equal(public_origin, lan_origin):
        raise PlayUrlError("内网播放地址和外网播放地址不能相同")
    return (
        "# 只放在 Emby 的公网反代里（Cloudflare 回源的那个 server）。\n"
        "# 内网直连 Emby 时不要加这段，否则局域网播放又会走外网域名。\n"
        f"# strm 里是内网地址 {lan_origin} ，公网播放响应改写成 {public_origin} 。\n"
        'proxy_set_header Accept-Encoding "";\n'
        f'sub_filter "{lan_origin}" "{public_origin}";\n'
        "sub_filter_once off;\n"
        "sub_filter_types application/json text/plain text/html application/xml application/x-mpegurl;\n"
    )
