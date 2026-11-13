"""播放地址选择。不访问网络，也不导入 MoviePilot。"""

from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "plugins.v2" / "anime115"
_pkg = types.ModuleType("anime115")
_pkg.__path__ = [str(_ROOT)]
_pkg.__package__ = "anime115"
sys.modules["anime115"] = _pkg

from anime115.anime_config import PLAY_BASE, PLAY_BASE_LAN  # noqa: E402
from anime115.anime_playurl import (  # noqa: E402
    PlayUrlError,
    classify_request,
    decide_location,
    nginx_snippet,
    play_url_for_strm,
    strm_template,
)


PICK = "lolihousecode0001"


class ClassifyTest(unittest.TestCase):
    def test_direct_lan_client_is_intranet(self):
        intranet, reason = classify_request(peer_ip="192.168.1.20", host="192.168.1.9:3000")
        self.assertTrue(intranet)
        self.assertEqual(reason, "client-ip")

    def test_cloudflare_is_public_even_with_lan_header(self):
        intranet, reason = classify_request(
            peer_ip="172.17.0.2",
            host="mp.yuanhot.top",
            headers={"CF-Connecting-IP": "1.2.3.4", "X-Anime115-Net": "lan", "X-Forwarded-For": "192.168.1.20"},
        )
        self.assertFalse(intranet)
        self.assertEqual(reason, "cloudflare")

    def test_public_peer_cannot_spoof_forwarded_lan(self):
        intranet, reason = classify_request(
            peer_ip="8.8.8.8",
            host="mp.yuanhot.top",
            headers={"X-Forwarded-For": "192.168.1.20"},
        )
        self.assertFalse(intranet)
        self.assertEqual(reason, "public")

    def test_private_proxy_uses_real_ip(self):
        intranet, reason = classify_request(
            peer_ip="127.0.0.1",
            host="mp.yuanhot.top",
            headers={"X-Real-IP": "192.168.1.33"},
        )
        self.assertTrue(intranet)
        self.assertEqual(reason, "client-ip")

    def test_private_proxy_public_client_stays_public(self):
        intranet, reason = classify_request(
            peer_ip="172.18.0.2",
            host="192.168.1.9:3000",
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        self.assertFalse(intranet)
        self.assertEqual(reason, "public")

    def test_header_marks_wan_without_cloudflare(self):
        intranet, reason = classify_request(
            peer_ip="192.168.1.9",
            host="192.168.1.9:3000",
            headers={"X-Anime115-Net": "wan"},
        )
        self.assertFalse(intranet)
        self.assertEqual(reason, "header")

    def test_split_dns_public_name_with_lan_ip_is_intranet(self):
        intranet, reason = classify_request(peer_ip="192.168.1.20", host="mp.yuanhot.top")
        self.assertTrue(intranet)
        self.assertEqual(reason, "client-ip")


class StrmTemplateTest(unittest.TestCase):
    def test_empty_mode_keeps_existing_public_template(self):
        url = play_url_for_strm(PLAY_BASE, PICK, "a.mkv")
        self.assertTrue(url.startswith("https://mp.yuanhot.top:8443/api/v1/plugin/P115StrmHelper/redirect_url"))
        self.assertIn(f"pickcode={PICK}", url)

    def test_lan_mode_does_not_use_public_domain(self):
        url = play_url_for_strm(PLAY_BASE, PICK, "第01话.mkv", PLAY_BASE_LAN, "lan")
        self.assertTrue(url.startswith("http://192.168.1.9:3000/"))
        self.assertNotIn("mp.yuanhot.top", url)
        self.assertIn(f"pickcode={PICK}", url)

    def test_redirect_mode_stays_on_lan_host(self):
        url = play_url_for_strm(PLAY_BASE, PICK, "a b.mkv", PLAY_BASE_LAN, "redirect")
        self.assertTrue(url.startswith("http://192.168.1.9:3000/api/v1/plugin/Anime115/redirect_url?"))
        self.assertNotIn("mp.yuanhot.top", url)

    def test_documented_8787_template_still_renders(self):
        template = strm_template(
            PLAY_BASE,
            "http://192.168.1.10:8787/redirect?pickcode={pickcode}",
            "lan",
        )
        self.assertEqual(template, "http://192.168.1.10:8787/redirect?pickcode={pickcode}")

    def test_lan_template_cannot_be_public_domain(self):
        with self.assertRaises(PlayUrlError):
            strm_template(PLAY_BASE, PLAY_BASE, "lan")

    def test_package_version_is_1_0_42(self):
        package = json.loads((Path(__file__).resolve().parents[1] / "package.v2.json").read_text(encoding="utf-8"))
        self.assertEqual(package["Anime115"]["version"], "1.0.76")
        self.assertIn("按长度排序", package["Anime115"]["history"]["v1.0.42"])
        self.assertIn("蓝光大包", package["Anime115"]["history"]["v1.0.43"])
        self.assertIn("zh-CN", package["Anime115"]["history"]["v1.0.44"])
        self.assertIn("Cookie 通道", package["Anime115"]["history"]["v1.0.45"])
        self.assertIn("风控", package["Anime115"]["history"]["v1.0.46"])
        self.assertIn("整包拆分", package["Anime115"]["history"]["v1.0.47"])
        self.assertIn("多版本", package["Anime115"]["history"]["v1.0.48"])
        self.assertIn("整包", package["Anime115"]["history"]["v1.0.49"])
        self.assertIn("列目录", package["Anime115"]["history"]["v1.0.50"])
        self.assertIn("轮流", package["Anime115"]["history"]["v1.0.51"])
        self.assertIn("蓝光合集", package["Anime115"]["history"]["v1.0.52"])
        self.assertIn("access_token", package["Anime115"]["history"]["v1.0.53"])
        self.assertIn("外挂", package["Anime115"]["history"]["v1.0.54"])
        self.assertIn("整段", package["Anime115"]["history"]["v1.0.55"])
        self.assertIn("冷却", package["Anime115"]["history"]["v1.0.56"])
        self.assertIn("跳过搜索", package["Anime115"]["history"]["v1.0.57"])
        self.assertIn("预热", package["Anime115"]["history"]["v1.0.58"])
        self.assertIn("episodes", package["Anime115"]["history"]["v1.0.59"])
        self.assertIn("短形式", package["Anime115"]["history"]["v1.0.41"])
        self.assertIn("别名候选", package["Anime115"]["history"]["v1.0.40"])
        self.assertIn("别名搜索", package["Anime115"]["history"]["v1.0.39"])
        self.assertIn("拖垮整轮", package["Anime115"]["history"]["v1.0.38"])
        self.assertIn("别名", package["Anime115"]["history"]["v1.0.37"])
        self.assertIn("选片阶段", package["Anime115"]["history"]["v1.0.36"])
        self.assertIn("同名冲突", package["Anime115"]["history"]["v1.0.35"])
        self.assertIn("整包拆分", package["Anime115"]["history"]["v1.0.34"])
        self.assertIn("季判定", package["Anime115"]["history"]["v1.0.33"])
        self.assertIn("整包入库", package["Anime115"]["history"]["v1.0.32"])
        self.assertIn("拆包任务", package["Anime115"]["history"]["v1.0.31"])
        self.assertIn("整包入库", package["Anime115"]["history"]["v1.0.30"])
        self.assertIn("标题比对", package["Anime115"]["history"]["v1.0.29"])
        self.assertIn("单条整理任务", package["Anime115"]["history"]["v1.0.28"])
        self.assertIn("时间预算", package["Anime115"]["history"]["v1.0.27"])
        self.assertIn("限时", package["Anime115"]["history"]["v1.0.26"])
        self.assertIn("并集", package["Anime115"]["history"]["v1.0.25"])
        self.assertIn("库里已有", package["Anime115"]["history"]["v1.0.24"])
        self.assertIn("Emby", package["Anime115"]["history"]["v1.0.23"])
        self.assertIn("没有视频", package["Anime115"]["history"]["v1.0.22"])
        self.assertIn("共享预留", package["Anime115"]["history"]["v1.0.21"])
        self.assertIn("fs_mkdir", package["Anime115"]["history"]["v1.0.20"])
        self.assertIn("每 30 分钟", package["Anime115"]["history"]["v1.0.19"])
        text = (_ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('plugin_version = "1.0.76"', text)
        self.assertIn("内网播放地址", text)
        self.assertIn("外网播放地址", text)


class RedirectDecisionTest(unittest.TestCase):
    def test_lan_request_redirects_to_lan_helper(self):
        decision = decide_location(
            peer_ip="192.168.1.20",
            host="192.168.1.9:3000",
            headers={},
            public_template=PLAY_BASE,
            lan_template=PLAY_BASE_LAN,
            pickcode=PICK,
            filename="a.mkv",
            request_path="/api/v1/plugin/Anime115/redirect_url",
        )
        self.assertTrue(decision.intranet)
        self.assertTrue(decision.url.startswith("http://192.168.1.9:3000/api/v1/plugin/P115StrmHelper/"))
        self.assertNotIn("mp.yuanhot.top", decision.url)

    def test_remote_request_redirects_to_public_helper(self):
        decision = decide_location(
            peer_ip="203.0.113.8",
            host="mp.yuanhot.top",
            headers={"cf-connecting-ip": "203.0.113.8"},
            public_template=PLAY_BASE,
            lan_template=PLAY_BASE_LAN,
            pickcode=PICK,
            filename="a.mkv",
            request_path="/api/v1/plugin/Anime115/redirect_url",
        )
        self.assertFalse(decision.intranet)
        self.assertTrue(decision.url.startswith("https://mp.yuanhot.top:8443/api/v1/plugin/P115StrmHelper/"))

    def test_self_redirect_is_rejected(self):
        with self.assertRaises(PlayUrlError):
            decide_location(
                peer_ip="192.168.1.20",
                host="192.168.1.9:3000",
                headers={},
                public_template=PLAY_BASE,
                lan_template="http://192.168.1.9:3000/api/v1/plugin/Anime115/redirect_url?pickcode={pickcode}&file_name={filename}",
                pickcode=PICK,
                filename="a.mkv",
                request_path="/api/v1/plugin/Anime115/redirect_url",
            )

    def test_remote_is_not_sent_to_private_address(self):
        with self.assertRaises(PlayUrlError):
            decide_location(
                peer_ip="203.0.113.8",
                host="mp.yuanhot.top",
                headers={},
                public_template="http://192.168.1.9:3000/redirect?pickcode={pickcode}",
                lan_template=PLAY_BASE_LAN,
                pickcode=PICK,
                filename="a.mkv",
            )

    def test_nginx_snippet_is_public_vhost_only(self):
        text = nginx_snippet()
        self.assertIn("只放在 Emby 的公网反代", text)
        self.assertIn("内网直连 Emby 时不要加", text)
        self.assertIn('sub_filter "http://192.168.1.9:3000" "https://mp.yuanhot.top:8443"', text)


if __name__ == "__main__":
    unittest.main()
