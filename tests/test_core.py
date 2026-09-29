from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from pathlib import Path

from source.core.browser import (
    browser_environment,
    configure_direct_connection,
    direct_connection_enabled,
    extract_user_id,
    initial_posted_has_more,
    logged_in_from_state,
    notes_from_api_payload,
    notes_from_initial_state,
    profile_candidates_from_links,
)
from source.core.cli import format_elapsed
from source.core.exporter import CreatorExporter, stable_detail


class FakeCrawler:
    async def crawl(
        self,
        profile_url,
        *,
        max_notes=0,
        headless=True,
        progress=None,
    ):
        notes = [
            {
                "note_id": "abc123",
                "xsec_token": "token",
                "xsec_source": "pc_user",
                "type": "video",
                "title": "示例作品",
                "liked_count": "42",
                "cover_url": "https://example.invalid/cover",
            }
        ]
        return {
            "profile": {
                "user_id": "user123",
                "nickname": "示例作者",
                "red_id": "",
                "description": "",
                "avatar": "",
            },
            "notes": notes[:max_notes] if max_notes else notes,
            "cookie": "",
        }


class BrowserParserTests(unittest.TestCase):
    def test_format_elapsed(self):
        self.assertEqual(format_elapsed(0.4), "0 秒")
        self.assertEqual(format_elapsed(65.2), "1 分 5 秒")
        self.assertEqual(format_elapsed(3661), "1 小时 1 分 1 秒")

    def test_login_state_requires_strict_true(self):
        self.assertTrue(logged_in_from_state({"user": {"loggedIn": True}}))
        self.assertTrue(logged_in_from_state({"user": {"loggedIn": {"value": True}}}))
        self.assertFalse(logged_in_from_state({"user": {"loggedIn": False}}))
        self.assertFalse(logged_in_from_state({"user": {"loggedIn": {"value": False}}}))

    def test_direct_connection_environment_is_default(self):
        proxy_values = {
            "HTTP_PROXY": os.environ.get("HTTP_PROXY"),
            "NO_PROXY": os.environ.get("NO_PROXY"),
            "no_proxy": os.environ.get("no_proxy"),
            "REDNOTE_DIRECT": os.environ.get("REDNOTE_DIRECT"),
        }
        try:
            os.environ["HTTP_PROXY"] = "http://127.0.0.1:7890"
            os.environ.pop("REDNOTE_DIRECT", None)
            self.assertTrue(direct_connection_enabled())
            self.assertNotIn("HTTP_PROXY", browser_environment())
            self.assertEqual(browser_environment()["NO_PROXY"], "*")
            configure_direct_connection()
            self.assertNotIn("HTTP_PROXY", os.environ)
        finally:
            for name, value in proxy_values.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_extract_user_id(self):
        self.assertEqual(
            extract_user_id("https://www.xiaohongshu.com/user/profile/user123?x=1"),
            "user123",
        )

    def test_extract_profile_candidate_from_search_card(self):
        candidates = profile_candidates_from_links(
            [
                {
                    "href": "https://www.xiaohongshu.com/user/profile/internal-id?xsec_token=token&xsec_source=pc_search",
                    "text": "谭思慧\n13天前更新\n小红书号：819540154\n粉丝・4.1万",
                },
                {
                    "href": "https://www.xiaohongshu.com/user/profile/internal-id",
                    "text": "谭思慧\n01-20",
                },
            ]
        )
        self.assertEqual(
            candidates,
            [
                {
                    "user_id": "internal-id",
                    "nickname": "谭思慧",
                    "red_id": "819540154",
                    "profile_url": "https://www.xiaohongshu.com/user/profile/internal-id?xsec_token=token&xsec_source=pc_search",
                }
            ],
        )

    def test_parse_initial_state_and_api_payload(self):
        state = {
            "user": {
                "notes": [
                    [
                        {
                            "noteId": "note-1",
                            "displayTitle": "标题一",
                            "xsecToken": "token-1",
                            "interactInfo": {"likedCount": "7"},
                        }
                    ],
                    [],
                ]
            }
        }
        self.assertEqual(notes_from_initial_state(state)[0]["note_id"], "note-1")
        ssr_state = {
            "user": {
                "notes": [
                    [
                        {
                            "id": "note-ssr",
                            "xsecToken": "wrapper-token",
                            "noteCard": {
                                "noteId": "note-ssr",
                                "displayTitle": "首屏作品",
                            },
                        }
                    ]
                ]
            }
        }
        self.assertEqual(
            notes_from_initial_state(ssr_state)[0],
            {
                "note_id": "note-ssr",
                "xsec_token": "wrapper-token",
                "xsec_source": "pc_user",
                "type": "",
                "title": "首屏作品",
                "liked_count": "",
                "cover_url": "",
            },
        )
        notes, has_more = notes_from_api_payload(
            {
                "data": {
                    "notes": [{"note_id": "note-2", "display_title": "标题二"}],
                    "has_more": True,
                }
            }
        )
        self.assertTrue(has_more)
        self.assertEqual(notes[0]["title"], "标题二")

    def test_initial_posted_pagination_flag(self):
        self.assertFalse(
            initial_posted_has_more(
                {"user": {"noteQueries": [{"hasMore": False}, {"hasMore": True}]}}
            )
        )
        self.assertTrue(
            initial_posted_has_more(
                {"user": {"noteQueries": [{"has_more": True}]}}
            )
        )
        self.assertIsNone(initial_posted_has_more({"user": {}}))

    def test_stable_detail_drops_media_urls(self):
        result = stable_detail(
            {
                "note_id": "note-1",
                "xsec_token": "token",
                "xsec_source": "pc_user",
                "type": "video",
                "title": "摘要",
                "liked_count": "1",
            },
            {
                "作品标题": "详情",
                "作品描述": "正文",
                "作品标签": "标签A 标签B",
                "下载地址": ["https://temporary.invalid/video.mp4"],
            },
        )
        self.assertEqual(result["title"], "详情")
        self.assertEqual(result["tags"], ["标签A", "标签B"])
        self.assertNotIn("下载地址", result)
        self.assertNotIn("xsec_token", result)
        self.assertEqual(result["url"], "https://www.xiaohongshu.com/explore/note-1")

    def test_stable_detail_accepts_explicit_failure_status(self):
        result = stable_detail(
            {"note_id": "note-1", "type": "normal", "title": "标题"},
            None,
            status="risk_control_captcha",
        )
        self.assertEqual(result["detail_status"], "risk_control_captcha")


class ExporterTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_export_writes_json_and_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            exporter = CreatorExporter(FakeCrawler())
            messages = []
            json_path, csv_path, document = await exporter.export(
                "https://www.xiaohongshu.com/user/profile/user123",
                data_dir=Path(directory),
                with_details=False,
                progress=messages.append,
            )
            self.assertEqual(document["note_count"], 1)
            self.assertEqual(
                json.loads(json_path.read_text())["notes"][0]["note_id"], "abc123"
            )
            with csv_path.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(rows[0]["title"], "示例作品")
            self.assertEqual(
                messages,
                [
                    "主页清单抓取完成，共 1 条作品。",
                    "正在写入摘要 JSON/CSV……",
                    "JSON/CSV 写入完成。",
                ],
            )


if __name__ == "__main__":
    unittest.main()
