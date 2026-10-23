from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .browser import CreatorBrowserCrawler

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _reserve_snapshot_paths(output_dir: Path, account_id: str, saved_at: datetime):
    stamp = saved_at.strftime("%Y%m%d_%H%M%S_") + f"{saved_at.microsecond // 1000:03d}"
    index = 0
    while True:
        stem = f"{account_id}_{stamp}" + (f"_{index}" if index else "")
        json_path = output_dir / f"{stem}.json"
        csv_path = output_dir / f"{stem}.csv"
        index += 1
        try:
            with json_path.open("x", encoding="utf-8"):
                pass
        except FileExistsError:
            continue
        try:
            with csv_path.open("x", encoding="utf-8"):
                pass
        except FileExistsError:
            json_path.unlink()
            continue
        except OSError:
            json_path.unlink()
            raise
        return json_path, csv_path


SCHEMA_VERSION = 1
CSV_FIELDS = (
    "note_id",
    "published_at",
    "type",
    "title",
    "description",
    "tags",
    "liked_count",
    "collected_count",
    "comment_count",
    "share_count",
    "author_id",
    "author_name",
    "url",
)


def _safe_name(value: str, default: str) -> str:
    cleaned = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", value).strip("_")
    return cleaned or default


def canonical_note_url(note_id: str) -> str:
    return f"https://www.xiaohongshu.com/explore/{note_id}"


def stable_detail(
    summary: dict[str, Any],
    detail: dict[str, Any] | None,
    *,
    status: str | None = None,
) -> dict[str, Any]:
    detail = detail or {}
    tags = detail.get("作品标签") or ""
    return {
        "note_id": summary["note_id"],
        "published_at": detail.get("发布时间", ""),
        "updated_at": detail.get("最后更新时间", ""),
        "type": detail.get("作品类型") or summary.get("type", ""),
        "title": detail.get("作品标题") or summary.get("title", ""),
        "description": detail.get("作品描述", ""),
        "tags": tags.split() if isinstance(tags, str) else tags,
        "liked_count": detail.get("点赞数量") or summary.get("liked_count", ""),
        "collected_count": detail.get("收藏数量", ""),
        "comment_count": detail.get("评论数量", ""),
        "share_count": detail.get("分享数量", ""),
        "author_id": detail.get("作者ID", ""),
        "author_name": detail.get("作者昵称", ""),
        "url": canonical_note_url(summary["note_id"]),
        "detail_status": status or ("complete" if detail else "summary_only"),
    }


class CreatorExporter:
    def __init__(self, crawler: CreatorBrowserCrawler | None = None):
        self.crawler = crawler or CreatorBrowserCrawler()

    async def export(
        self,
        profile_url: str,
        *,
        data_dir: Path,
        max_notes: int = 0,
        with_details: bool = True,
        headless: bool = True,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[Path, Path, dict[str, Any]]:
        result = await self.crawler.crawl(
            profile_url,
            max_notes=max_notes,
            headless=headless,
            progress=progress,
        )
        profile = result["profile"]
        summaries = result["notes"]
        if progress:
            progress(f"主页清单抓取完成，共 {len(summaries)} 条作品。")
        details: dict[str, dict[str, Any]] = {}
        detail_statuses: dict[str, str] = {}
        detail_aborted_reason = ""

        if with_details:
            if progress:
                progress("主页浏览器阶段已结束，正在启动详情浏览器……")
            (
                details,
                detail_statuses,
                detail_aborted_reason,
            ) = await self.crawler.details(summaries)
            if detail_aborted_reason:
                for summary in summaries:
                    detail_statuses.setdefault(summary["note_id"], "not_attempted")
        elif progress:
            progress("正在写入摘要 JSON/CSV……")

        notes = [
            stable_detail(
                item,
                details.get(item["note_id"]),
                status=detail_statuses.get(item["note_id"]),
            )
            for item in summaries
        ]
        if notes and not profile.get("nickname"):
            profile["nickname"] = notes[0].get("author_name", "")
        if notes and not profile.get("user_id"):
            profile["user_id"] = notes[0].get("author_id", "")

        saved_at = datetime.now(BEIJING_TZ)
        document = {
            "schema_version": SCHEMA_VERSION,
            "platform": "xiaohongshu",
            "exported_at": saved_at.isoformat(timespec="milliseconds"),
            "source_profile_url": (
                f"https://www.xiaohongshu.com/user/profile/{profile.get('user_id', '')}"
            ),
            "profile": profile,
            "note_count": len(notes),
            "detail_run": {
                "requested": with_details,
                "complete": sum(item["detail_status"] == "complete" for item in notes),
                "failed": sum(
                    item["detail_status"]
                    in {
                        "failed",
                        "navigation_timeout",
                        "browser_closed",
                        "detail_unavailable",
                        "interactive_timeout",
                    }
                    for item in notes
                ),
                "risk_control_captcha": sum(
                    item["detail_status"] == "risk_control_captcha" for item in notes
                ),
                "not_attempted": sum(
                    item["detail_status"] == "not_attempted" for item in notes
                ),
                "aborted_reason": detail_aborted_reason,
            },
            "notes": notes,
        }
        author_id = profile.get("user_id") or "unknown"
        author_name = _safe_name(profile.get("nickname", ""), author_id)
        output_dir = data_dir / author_name
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path, csv_path = _reserve_snapshot_paths(output_dir, author_id, saved_at)
        json_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for item in notes:
                row = {key: item.get(key, "") for key in CSV_FIELDS}
                row["tags"] = " ".join(item.get("tags") or [])
                writer.writerow(row)
        if progress:
            progress("JSON/CSV 写入完成。")
        return json_path, csv_path, document
