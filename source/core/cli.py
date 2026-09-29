from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import perf_counter

import click

from source.application import XHS
from source.application.explore import Explore
from source.expansion import Namespace
from source.module import VOLUME

from .browser import (
    DEFAULT_PROFILE_DIR,
    BrowserAutomationError,
    CreatorBrowserCrawler,
    LoginRequired,
    configure_direct_connection,
    login_with_browser,
    resolve_profile_reference,
)
from .exporter import CreatorExporter


def run(coroutine):
    return asyncio.run(coroutine)


def format_elapsed(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分 {seconds} 秒"
    if minutes:
        return f"{minutes} 分 {seconds} 秒"
    return f"{seconds} 秒"


@click.group(help="小红书作者作品导出与单篇媒体下载 CLI")
def cli() -> None:
    configure_direct_connection()


@cli.command(help="在项目隔离的 Playwright 浏览器中扫码登录")
@click.option(
    "--profile-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=DEFAULT_PROFILE_DIR,
    show_default=True,
)
@click.option("--timeout", type=int, default=180, show_default=True)
def login(profile_dir: Path, timeout: int) -> None:
    try:
        run(login_with_browser(profile_dir, timeout_seconds=timeout))
    except BrowserAutomationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("登录成功，会话已保存在项目隔离 profile 中。")


@cli.command(
    "export",
    help="按精确昵称、小红书号或主页 URL 导出作者作品 JSON/CSV",
)
@click.argument("profile_reference")
@click.option(
    "--profile-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=DEFAULT_PROFILE_DIR,
    show_default=True,
)
@click.option(
    "--data-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("data"),
    show_default=True,
)
@click.option("--limit", type=int, default=0, help="0 表示不限制")
@click.option("--details/--summary-only", default=False, show_default=True)
@click.option("--headed", is_flag=True, help="以可见模式运行作者主页抓取")
def export_creator(
    profile_reference: str,
    profile_dir: Path,
    data_dir: Path,
    limit: int,
    details: bool,
    headed: bool,
) -> None:
    started_at = perf_counter()

    async def execute():
        crawler = CreatorBrowserCrawler(profile_dir)
        exporter = CreatorExporter(crawler)

        async def perform_export():
            click.echo(f"正在解析作者：{profile_reference}")
            profile_url = await resolve_profile_reference(
                profile_reference,
                profile_dir,
            )
            click.echo("作者定位完成，正在抓取主页作品清单……")
            return await exporter.export(
                profile_url,
                data_dir=data_dir,
                max_notes=max(0, limit),
                with_details=details,
                headless=not headed,
                progress=click.echo,
            )

        try:
            return await perform_export()
        except LoginRequired:
            click.echo("隔离浏览器尚未登录，正在打开扫码窗口……")
            await login_with_browser(profile_dir)
            return await perform_export()

    try:
        json_path, csv_path, document = run(execute())
    except BrowserAutomationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"已导出 {document['note_count']} 条作品")
    click.echo(f"JSON: {json_path.resolve()}")
    click.echo(f"CSV:  {csv_path.resolve()}")
    detail_run = document.get("detail_run") or {}
    if detail_run.get("requested"):
        click.echo(
            "详情结果："
            f"完成 {detail_run.get('complete', 0)}，"
            f"失败 {detail_run.get('failed', 0)}，"
            f"未执行 {detail_run.get('not_attempted', 0)}"
        )
    click.echo(f"总耗时：{format_elapsed(perf_counter() - started_at)}")
    if detail_run.get("requested"):
        if reason := detail_run.get("aborted_reason"):
            raise click.ClickException(
                f"详情阶段已中止（{reason}）；已完成的数据已保存在上述文件中"
            )


@cli.command(help="解析单篇作品详情，不下载媒体")
@click.argument("note_url")
@click.option(
    "--profile-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=DEFAULT_PROFILE_DIR,
    show_default=True,
)
def detail(note_url: str, profile_dir: Path) -> None:
    async def execute():
        data, status = await CreatorBrowserCrawler(profile_dir).raw_detail(note_url)
        if status != "complete":
            raise BrowserAutomationError(f"作品详情读取失败：{status}")
        return Explore().run(Namespace(data))

    try:
        result = run(execute())
    except BrowserAutomationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@cli.command(help="下载一篇作品的图片、视频或 Live Photo")
@click.argument("note_url")
@click.option("--cookie", default="", help="可选的小红书网页版 Cookie")
@click.option(
    "--profile-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=DEFAULT_PROFILE_DIR,
    show_default=True,
)
@click.option(
    "--output",
    type=click.Path(path_type=Path, file_okay=False),
    default=VOLUME,
    show_default=True,
)
def download(note_url: str, cookie: str, profile_dir: Path, output: Path) -> None:
    async def execute():
        data, status = await CreatorBrowserCrawler(profile_dir).raw_detail(note_url)
        if status != "complete":
            raise BrowserAutomationError(f"作品详情读取失败：{status}")
        async with XHS(cookie=cookie, work_path=str(output), record_data=False) as app:
            return await app.deal_script_tasks(data, index=None)

    try:
        result = run(execute())
    except BrowserAutomationError as exc:
        raise click.ClickException(str(exc)) from exc
    if not result:
        raise click.ClickException("作品解析或下载失败")
    click.echo("下载完成。")


if __name__ == "__main__":
    cli()
