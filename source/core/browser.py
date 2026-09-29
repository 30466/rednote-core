from __future__ import annotations

import asyncio
from collections.abc import Iterable
from os import environ
from pathlib import Path
from re import search
from time import monotonic
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlparse

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from source.application.explore import Explore
from source.expansion import Converter, Namespace
from source.module import VOLUME
from source.module.tools import sleep_time

LOGIN_URL = "https://www.xiaohongshu.com/login"
EXPLORE_URL = "https://www.xiaohongshu.com/explore"
USER_POSTED_API = "/api/sns/web/v1/user_posted"
SEARCH_URL = "https://www.xiaohongshu.com/search_result"
DEFAULT_PROFILE_DIR = VOLUME / "playwright-profile"
DIRECT_ENV_VAR = "REDNOTE_DIRECT"
PROXY_ENV_VARS = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "all_proxy",
    "https_proxy",
    "http_proxy",
)


class BrowserAutomationError(RuntimeError):
    """Raised when the isolated browser cannot complete a crawl operation."""


class LoginRequired(BrowserAutomationError):
    """Raised when a creator crawl needs an authenticated browser profile."""


def direct_connection_enabled() -> bool:
    """Use a direct connection unless the user explicitly opts into proxies."""

    return environ.get(DIRECT_ENV_VAR, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def browser_environment() -> dict[str, str]:
    """Return a browser environment without inherited shell proxy variables."""

    browser_env = dict(environ)
    if direct_connection_enabled():
        for name in PROXY_ENV_VARS:
            browser_env.pop(name, None)
        browser_env["NO_PROXY"] = "*"
        browser_env["no_proxy"] = "*"
    return browser_env


def configure_direct_connection() -> None:
    """Prevent this CLI process and its children from inheriting proxy variables."""

    if not direct_connection_enabled():
        return
    for name in PROXY_ENV_VARS:
        environ.pop(name, None)
    environ["NO_PROXY"] = "*"
    environ["no_proxy"] = "*"


def extract_user_id(profile_url: str) -> str:
    parts = [part for part in urlparse(profile_url).path.split("/") if part]
    try:
        index = parts.index("profile")
        user_id = parts[index + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(f"无效的小红书作者主页链接: {profile_url}") from exc
    if not user_id:
        raise ValueError(f"无效的小红书作者主页链接: {profile_url}")
    return user_id


def is_profile_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.netloc.endswith("xiaohongshu.com") and "/user/profile/" in parsed.path


def profile_candidates_from_links(
    links: Iterable[dict[str, str]],
) -> list[dict[str, str]]:
    """Extract deduplicated account cards from user-search result links."""

    candidates: dict[str, dict[str, str]] = {}
    for link in links:
        href = str(link.get("href") or "")
        text = str(link.get("text") or "").strip()
        if not is_profile_url(href) or not text:
            continue
        red_id_match = search(r"小红书号[：:]\s*([^\s]+)", text)
        if not red_id_match:
            continue
        try:
            user_id = extract_user_id(href)
        except ValueError:
            continue
        nickname = text.splitlines()[0].strip()
        candidates[user_id] = {
            "user_id": user_id,
            "nickname": nickname,
            "red_id": red_id_match.group(1).strip(),
            "profile_url": href,
        }
    return list(candidates.values())


def cookie_string(cookies: Iterable[dict[str, Any]]) -> str:
    return "; ".join(
        f"{cookie['name']}={cookie['value']}"
        for cookie in cookies
        if cookie.get("name") and cookie.get("value")
    )


def _first(mapping: dict[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return default


def normalize_note_summary(note: dict[str, Any]) -> dict[str, Any]:
    interact = _first(note, "interact_info", "interactInfo", default={}) or {}
    cover = note.get("cover") or {}
    note_id = str(_first(note, "note_id", "noteId"))
    return {
        "note_id": note_id,
        "xsec_token": str(_first(note, "xsec_token", "xsecToken")),
        "xsec_source": str(
            _first(note, "xsec_source", "xsecSource", default="pc_user")
        ),
        "type": str(note.get("type") or ""),
        "title": str(_first(note, "display_title", "displayTitle", "title")),
        "liked_count": str(_first(interact, "liked_count", "likedCount", default="")),
        "cover_url": str(
            _first(cover, "url_default", "urlDefault", "url_pre", "urlPre", default="")
        ),
    }


def _walk_note_items(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        note_card = value.get("noteCard") or value.get("note_card")
        if isinstance(note_card, dict):
            item = note_card.copy()
            item.setdefault(
                "xsecToken", _first(value, "xsecToken", "xsec_token", default="")
            )
            yield item
            return
        if _first(value, "note_id", "noteId"):
            yield value
            return
        for item in value.values():
            yield from _walk_note_items(item)
        return
    if isinstance(value, list):
        for item in value:
            yield from _walk_note_items(item)


def notes_from_initial_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    user = state.get("user") or {}
    tabs = user.get("notes") or []
    posted_tab = tabs[0] if tabs else []
    return [normalize_note_summary(note) for note in _walk_note_items(posted_tab)]


def notes_from_api_payload(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    notes = data.get("notes") or []
    return (
        [normalize_note_summary(note) for note in notes if isinstance(note, dict)],
        bool(_first(data, "has_more", "hasMore", default=False)),
    )


def profile_from_initial_state(state: dict[str, Any], user_id: str) -> dict[str, Any]:
    user_page = (state.get("user") or {}).get("userPageData") or {}
    basic = user_page.get("basicInfo") or {}
    raw_result = user_page.get("result") or {}
    result = raw_result if isinstance(raw_result, dict) else {}
    return {
        "user_id": user_id,
        "nickname": str(
            _first(basic, "nickname", "nickName", default=result.get("nickname", ""))
        ),
        "red_id": str(
            _first(basic, "redId", "red_id", default=result.get("redId", ""))
        ),
        "description": str(
            _first(basic, "desc", "description", default=result.get("desc", ""))
        ),
        "avatar": str(_first(basic, "imageb", "images", "avatar", default="")),
    }


def logged_in_from_state(state: dict[str, Any]) -> bool:
    """Interpret Xiaohongshu's boolean or reactive-ref login flag strictly."""

    value = (state.get("user") or {}).get("loggedIn")
    if value is True:
        return True
    return isinstance(value, dict) and value.get("value") is True


async def initial_state_from_page(page: Page) -> dict[str, Any]:
    """Return the small, serializable part of Xiaohongshu's user state.

    After hydration several fields become Vue refs containing dependency graphs.
    Returning the entire object can then fail with "object reference chain is too
    long", so unwrap refs in the page and copy only fields used by this crawler.
    """

    state = await page.evaluate(
        """() => {
            let state = window.__INITIAL_STATE__ || null;
            if (!state) {
                const script = [...document.scripts]
                    .map(element => element.textContent || "")
                    .find(text => text.startsWith("window.__INITIAL_STATE__="));
                if (script) {
                    try {
                        state = (new Function(
                            `${script}; return window.__INITIAL_STATE__`
                        ))();
                    } catch (_) {
                        state = null;
                    }
                }
            }
            const seen = new WeakSet();
            const plain = (input, depth = 0) => {
                if (input === null || input === undefined) return input;
                if (["string", "number", "boolean"].includes(typeof input)) {
                    return input;
                }
                if (typeof input !== "object" || depth > 20) return null;
                let value = input;
                if (value.__v_isRef === true) {
                    try {
                        value = value.value;
                    } catch (_) {
                        value = value._value;
                    }
                }
                if (value === null || value === undefined) return value;
                if (["string", "number", "boolean"].includes(typeof value)) {
                    return value;
                }
                if (typeof value !== "object" || seen.has(value)) return null;
                seen.add(value);
                if (Array.isArray(value)) {
                    return value.map(item => plain(item, depth + 1));
                }
                const result = {};
                for (const key of Object.keys(value)) {
                    if (key === "dep" || key.startsWith("__v_")) continue;
                    const child = plain(value[key], depth + 1);
                    if (child !== null && child !== undefined) result[key] = child;
                }
                return result;
            };
            const user = state?.user || {};
            return {
                user: {
                    loggedIn: plain(user.loggedIn),
                    notes: plain(user.notes),
                    userPageData: plain(user.userPageData),
                    noteQueries: plain(user.noteQueries),
                },
            };
        }"""
    )
    return state if isinstance(state, dict) else {}


def initial_posted_has_more(state: dict[str, Any]) -> bool | None:
    """Read the posted-tab pagination flag from the initial page state."""

    queries = (state.get("user") or {}).get("noteQueries")
    if not isinstance(queries, list) or not queries:
        return None
    posted = queries[0]
    if not isinstance(posted, dict):
        return None
    value = _first(posted, "hasMore", "has_more", default=None)
    return value if isinstance(value, bool) else None


async def resolve_profile_reference(
    reference: str,
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    *,
    _empty_retry: bool = True,
) -> str:
    """Resolve an exact nickname or Red ID to a tokenized creator URL."""

    reference = reference.strip()
    if is_profile_url(reference):
        return reference
    if not reference:
        raise BrowserAutomationError("作者名称或小红书号不能为空")

    search_url = (
        f"{SEARCH_URL}?keyword={quote(reference)}"
        "&source=web_search_result_notes&type=51"
    )
    async with IsolatedBrowser(profile_dir, headless=False) as browser:
        page = await browser.new_or_existing_page()
        try:
            await page.goto(EXPLORE_URL, wait_until="commit", timeout=20_000)
            await page.wait_for_timeout(2_000)
            if not logged_in_from_state(await initial_state_from_page(page)):
                raise LoginRequired("当前隔离浏览器登录态已失效，请重新扫码登录")
            # Let the already authenticated page initiate navigation. Waiting for
            # the search page's load lifecycle is unreliable because its network
            # requests remain active after useful results are rendered.
            await page.evaluate("url => location.assign(url)", search_url)
        except PlaywrightTimeoutError as exc:
            raise BrowserAutomationError(
                "小红书首页加载超时，无法开始用户搜索"
            ) from exc
        except PlaywrightError as exc:
            raise BrowserAutomationError("用户搜索窗口已关闭") from exc

        user_tab = page.get_by_text("用户", exact=True)
        for _ in range(30):
            await page.wait_for_timeout(500)
            try:
                if await user_tab.count():
                    break
            except PlaywrightError:
                continue
        try:
            user_tab_count = await user_tab.count()
        except PlaywrightError:
            user_tab_count = 0
        if user_tab_count:
            try:
                await user_tab.first.click(timeout=5_000, no_wait_after=True)
            except PlaywrightError:
                pass

        candidates: list[dict[str, str]] = []
        for _ in range(30):
            await page.wait_for_timeout(500)
            try:
                links = await page.locator('a[href*="/user/profile/"]').evaluate_all(
                    """elements => elements.map(element => ({
                        href: element.href,
                        text: (element.innerText || element.textContent || "").trim(),
                    }))"""
                )
            except PlaywrightError:
                continue
            candidates = profile_candidates_from_links(links)
            if any(
                item["nickname"] == reference or item["red_id"] == reference
                for item in candidates
            ):
                break
        if "error_code=300012" in page.url:
            raise BrowserAutomationError(
                "小红书用户搜索触发 IP 风险限制（300012）；请检查直连网络后重试"
            )
        if "/login" in page.url:
            raise LoginRequired("当前隔离浏览器尚未登录，请先运行 login 命令扫码")

    if not candidates and _empty_retry:
        return await resolve_profile_reference(
            reference,
            profile_dir,
            _empty_retry=False,
        )

    exact = [
        item
        for item in candidates
        if item["nickname"] == reference or item["red_id"] == reference
    ]
    if len(exact) == 1:
        return exact[0]["profile_url"]
    if len(exact) > 1:
        choices = "、".join(
            f"{item['nickname']}（小红书号 {item['red_id']}）" for item in exact
        )
        raise BrowserAutomationError(
            f"存在多个同名账号：{choices}；请改用小红书号精确导出"
        )
    suggestions = "、".join(
        f"{item['nickname']}（{item['red_id']}）" for item in candidates[:5]
    )
    suffix = f"；搜索到的候选为：{suggestions}" if suggestions else ""
    raise BrowserAutomationError(f"未找到完全匹配的账号“{reference}”{suffix}")


class IsolatedBrowser:
    """Project-local Playwright context that never reads the user's browser profile."""

    def __init__(self, profile_dir: Path = DEFAULT_PROFILE_DIR, *, headless: bool):
        self.profile_dir = profile_dir
        self.headless = headless
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None

    async def __aenter__(self) -> "IsolatedBrowser":
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        launch_args = ["--disable-blink-features=AutomationControlled"]
        if direct_connection_enabled():
            launch_args.extend(["--no-proxy-server", "--proxy-bypass-list=*"])
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=self.headless,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 1000},
            args=launch_args,
            env=browser_environment(),
            ignore_default_args=["--enable-automation"],
        )
        await self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        if self.context:
            try:
                await asyncio.wait_for(self.context.close(), timeout=10)
            except (PlaywrightError, TimeoutError):
                pass
        if self.playwright:
            try:
                await asyncio.wait_for(self.playwright.stop(), timeout=10)
            except (PlaywrightError, TimeoutError):
                pass

    async def new_or_existing_page(self) -> Page:
        if not self.context:
            raise BrowserAutomationError("Playwright 浏览器尚未启动")
        return (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )

    async def cookies(self) -> list[dict[str, Any]]:
        if not self.context:
            raise BrowserAutomationError("Playwright 浏览器尚未启动")
        return await self.context.cookies()


async def login_with_browser(
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    *,
    timeout_seconds: int = 180,
) -> None:
    """Validate the saved session, or wait until QR login creates a valid one."""

    async with IsolatedBrowser(profile_dir, headless=False) as browser:
        page = await browser.new_or_existing_page()

        async def navigate(url: str, timeout_message: str) -> None:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                await page.wait_for_timeout(1_000)
            except PlaywrightTimeoutError as exc:
                raise BrowserAutomationError(timeout_message) from exc
            except PlaywrightError as exc:
                raise BrowserAutomationError("登录窗口已关闭，未完成扫码登录") from exc

        async def server_reports_logged_in() -> bool:
            if page.is_closed() or "/login" in page.url:
                return False
            try:
                return logged_in_from_state(await initial_state_from_page(page))
            except PlaywrightError:
                return False

        # Cookie presence alone is insufficient: the server may already have revoked it.
        await navigate(EXPLORE_URL, "小红书登录态验证页面加载超时")
        if "error_code=300012" in page.url:
            raise BrowserAutomationError(
                "当前网络出口被小红书判定为高风险（300012）；请关闭 VPN 或切换网络后重试"
            )
        if await server_reports_logged_in():
            return

        await navigate(LOGIN_URL, "小红书登录页加载超时")
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            if page.is_closed():
                raise BrowserAutomationError("登录窗口已关闭，未完成扫码登录")
            if "error_code=300012" in page.url:
                raise BrowserAutomationError(
                    "当前网络出口被小红书判定为高风险（300012）；请关闭 VPN 或切换网络后重试"
                )
            # Never navigate or reload the QR page here. Xiaohongshu writes
            # several cookies during one scan; reacting to each cookie update
            # caused repeated /explore -> /login refreshes and rate limits.
            if await server_reports_logged_in():
                return
            try:
                await page.wait_for_timeout(1_000)
            except PlaywrightError as exc:
                raise BrowserAutomationError("登录窗口已关闭，未完成扫码登录") from exc
    raise BrowserAutomationError("扫码登录等待超时")


class CreatorBrowserCrawler:
    """Collect a creator's full note list from structured browser responses."""

    def __init__(self, profile_dir: Path = DEFAULT_PROFILE_DIR):
        self.profile_dir = profile_dir

    @staticmethod
    async def _read_raw_detail(
        page: Page,
        url: str,
        note_id: str,
        interactive_timeout: int,
    ) -> tuple[dict[str, Any], str]:
        converter = Converter()
        try:
            await page.goto(url, wait_until="commit", timeout=20_000)
            await page.wait_for_timeout(1_500)
        except PlaywrightTimeoutError:
            return {}, "navigation_timeout"
        except PlaywrightError:
            return {}, "browser_closed"

        data = converter.run(await page.content())
        if data and str(data.get("noteId") or "") == note_id:
            return data, "complete"

        state = await initial_state_from_page(page)
        needs_interaction = (
            "/website-login/captcha" in page.url or not logged_in_from_state(state)
        )
        if not needs_interaction:
            return {}, "detail_unavailable"

        print(
            "检测到登录或验证码页面，请在当前隔离浏览器中完成操作；"
            f"最多等待 {interactive_timeout} 秒。"
        )
        deadline = monotonic() + interactive_timeout
        while monotonic() < deadline and not page.is_closed():
            await page.wait_for_timeout(1_000)
            if "/website-login/captcha" in page.url:
                continue
            state = await initial_state_from_page(page)
            if not logged_in_from_state(state):
                continue
            try:
                await page.goto(url, wait_until="commit", timeout=20_000)
                await page.wait_for_timeout(1_500)
                data = converter.run(await page.content())
            except PlaywrightError:
                data = {}
            if data and str(data.get("noteId") or "") == note_id:
                return data, "complete"
        return {}, "interactive_timeout"

    async def raw_detail(
        self,
        note_url: str,
        *,
        interactive_timeout: int = 300,
    ) -> tuple[dict[str, Any], str]:
        """Read one note's raw structured data in the isolated browser."""

        note_id = urlparse(note_url).path.rstrip("/").split("/")[-1]
        if not note_id:
            raise BrowserAutomationError("无效的小红书作品链接")
        async with IsolatedBrowser(self.profile_dir, headless=False) as browser:
            page = await browser.new_or_existing_page()
            return await self._read_raw_detail(
                page,
                note_url,
                note_id,
                interactive_timeout,
            )

    async def details(
        self,
        summaries: list[dict[str, Any]],
        *,
        interactive_timeout: int = 300,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str], str]:
        """Read note details through one visible, authenticated browser context."""

        details: dict[str, dict[str, Any]] = {}
        statuses: dict[str, str] = {}
        explorer = Explore()

        async with IsolatedBrowser(self.profile_dir, headless=False) as browser:
            page = await browser.new_or_existing_page()
            for index, summary in enumerate(summaries):
                note_id = summary["note_id"]
                url = f"https://www.xiaohongshu.com/explore/{note_id}"
                if token := summary.get("xsec_token"):
                    source = summary.get("xsec_source") or "pc_user"
                    url += f"?xsec_token={token}&xsec_source={source}"

                print(f"浏览器读取作品详情：{note_id}（{index + 1}/{len(summaries)}）")
                data, status = await self._read_raw_detail(
                    page,
                    url,
                    note_id,
                    interactive_timeout,
                )
                statuses[note_id] = status
                if status == "complete":
                    details[note_id] = explorer.run(Namespace(data))
                elif status in {"browser_closed", "interactive_timeout"}:
                    return details, statuses, status

                if index + 1 < len(summaries):
                    await sleep_time()

        return details, statuses, ""

    async def crawl(
        self,
        profile_url: str,
        *,
        max_notes: int = 0,
        headless: bool = True,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        user_id = extract_user_id(profile_url)
        notes_by_id: dict[str, dict[str, Any]] = {}
        latest_has_more = True
        terminal_page_received = False
        response_tasks: set[asyncio.Task] = set()
        response_event = asyncio.Event()

        async with IsolatedBrowser(self.profile_dir, headless=headless) as browser:
            page = await browser.new_or_existing_page()

            async def capture_response(response) -> None:
                nonlocal latest_has_more, terminal_page_received
                if USER_POSTED_API not in response.url or response.status != 200:
                    return
                query = parse_qs(urlparse(response.url).query)
                response_user_id = (query.get("user_id") or [""])[0]
                if response_user_id and response_user_id != user_id:
                    return
                try:
                    payload = await response.json()
                except Exception:
                    return
                previous_count = len(notes_by_id)
                items, latest_has_more = notes_from_api_payload(payload)
                terminal_page_received = terminal_page_received or not latest_has_more
                for item in items:
                    if item["note_id"]:
                        notes_by_id[item["note_id"]] = item
                if progress and len(notes_by_id) != previous_count:
                    progress(f"主页分页已取得 {len(notes_by_id)} 条作品……")
                response_event.set()

            def schedule_capture(response) -> None:
                task = asyncio.create_task(capture_response(response))
                response_tasks.add(task)
                task.add_done_callback(response_tasks.discard)

            page.on("response", schedule_capture)
            try:
                response = await page.goto(
                    profile_url,
                    wait_until="commit",
                    timeout=15_000,
                )
            except PlaywrightTimeoutError as exc:
                raise BrowserAutomationError("作者主页加载超时") from exc
            if response and response.status >= 400:
                raise BrowserAutomationError(
                    f"作者主页请求失败: HTTP {response.status}"
                )
            await page.wait_for_timeout(1_500)
            if "error_code=300012" in page.url:
                raise BrowserAutomationError(
                    "当前网络出口被小红书判定为高风险（300012）；请关闭 VPN 或切换网络后重试"
                )
            if "/login" in page.url:
                raise LoginRequired("当前隔离浏览器尚未登录，请先运行 login 命令扫码")

            state = await initial_state_from_page(page)
            if not logged_in_from_state(state):
                raise LoginRequired("当前隔离浏览器尚未登录，请先运行 login 命令扫码")
            for item in notes_from_initial_state(state):
                if item["note_id"]:
                    notes_by_id[item["note_id"]] = item
            initial_has_more = initial_posted_has_more(state)
            if initial_has_more is False:
                terminal_page_received = True
            if progress:
                progress(f"主页首屏已取得 {len(notes_by_id)} 条作品……")
            profile = profile_from_initial_state(state, user_id)

            if response_tasks:
                await asyncio.gather(*list(response_tasks), return_exceptions=True)

            stalled_rounds = 0
            while not terminal_page_received and stalled_rounds < 12:
                if max_notes and len(notes_by_id) >= max_notes:
                    break
                response_event.clear()
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.mouse.wheel(0, 2_500)
                try:
                    await asyncio.wait_for(response_event.wait(), timeout=4.0)
                    stalled_rounds = 0
                except TimeoutError:
                    stalled_rounds += 1

            if response_tasks:
                await asyncio.gather(*list(response_tasks), return_exceptions=True)
            cookies = cookie_string(await browser.cookies())

            reached_requested_limit = bool(max_notes and len(notes_by_id) >= max_notes)
            if not reached_requested_limit and not terminal_page_received:
                raise BrowserAutomationError(
                    f"作者作品分页未完成：当前仅取得 {len(notes_by_id)} 条，"
                    "且未收到 has_more=false；已停止导出以避免生成残缺清单"
                )
            if progress:
                progress("主页数据收集完成，正在关闭主页浏览器……")

        notes = list(notes_by_id.values())
        if progress:
            progress("主页浏览器已关闭，正在整理作品清单……")
        if max_notes:
            notes = notes[:max_notes]
        if not notes:
            raise BrowserAutomationError(
                "未从作者主页初始状态或 user_posted 响应取得作品"
            )
        return {
            "profile": profile,
            "notes": notes,
            "cookie": cookies,
        }
