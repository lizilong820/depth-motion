from __future__ import annotations

import argparse
from http.cookiejar import MozillaCookieJar
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, unquote, urlsplit


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _save_cookies(cookies: list[dict], path: Path) -> None:
    jar = MozillaCookieJar(str(path))
    for item in cookies:
        domain = str(item.get("domain") or "").lower()
        normalized_domain = domain.lstrip(".")
        if normalized_domain != "douyin.com" and not normalized_domain.endswith(".douyin.com"):
            continue
        from http.cookiejar import Cookie

        jar.set_cookie(
            Cookie(
                version=0,
                name=str(item["name"]),
                value=str(item["value"]),
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path=str(item.get("path") or "/"),
                path_specified=True,
                secure=bool(item.get("secure")),
                expires=int(item["expires"]) if float(item.get("expires") or 0) > 0 else None,
                discard=float(item.get("expires") or 0) <= 0,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": None} if item.get("httpOnly") else {},
                rfc2109=False,
            )
        )
    jar.save(ignore_discard=True, ignore_expires=True)
    path.chmod(0o600)


def _matches_aweme_id(value: dict, aweme_id: str) -> bool:
    return any(str(value.get(key)) == aweme_id for key in ("aweme_id", "awemeId"))


def _find_aweme_detail(value: object, aweme_id: str) -> dict | None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            if _matches_aweme_id(current, aweme_id) and isinstance(current.get("video"), dict):
                return current
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return None


def _aweme_diagnostics(value: object, aweme_id: str) -> dict:
    matching_keys: list[list[str]] = []
    pending = [value]
    while pending and len(matching_keys) < 5:
        current = pending.pop()
        if isinstance(current, dict):
            if any(str(item) == aweme_id for item in current.values() if not isinstance(item, (dict, list))):
                matching_keys.append(sorted(str(key) for key in current.keys())[:40])
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return {"target_found": bool(matching_keys), "matching_keys": matching_keys}


def _aweme_result(payload: object, aweme_id: str, page_url: str) -> dict | None:
    detail = payload.get("aweme_detail") if isinstance(payload, dict) else None
    if not isinstance(detail, dict) or not _matches_aweme_id(detail, aweme_id):
        detail = _find_aweme_detail(payload, aweme_id)
    if not isinstance(detail, dict):
        return None
    video = detail.get("video")
    if not isinstance(video, dict):
        return None
    media_urls: list[str] = []
    for key in ("play_addr", "download_addr", "playAddr", "downloadAddr"):
        address = video.get(key)
        urls = (address.get("url_list") or address.get("urlList")) if isinstance(address, dict) else None
        if isinstance(urls, list):
            for url in urls:
                if isinstance(url, str) and url.startswith(("http://", "https://")) and url not in media_urls:
                    media_urls.append(url)
    if not media_urls:
        return None
    return {
        "page_url": page_url,
        "title": str(detail.get("desc") or detail.get("aweme_id") or detail.get("awemeId") or "douyin-video")[:120],
        "media_urls": media_urls[:8],
    }


def collect_session(page_url: str, proxy_url: str, cookie_path: Path, result_path: Path) -> None:
    from playwright.sync_api import Response, sync_playwright

    captured: dict | None = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            proxy={"server": proxy_url, "bypass": "<-loopback>"},
            args=[
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            ],
        )
        context = None
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1280, "height": 720},
            )
            page = context.new_page()
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"image", "font", "media"}
                else route.continue_(),
            )

            expected_aweme_id: str | None = None

            def capture(response: Response) -> None:
                nonlocal captured
                parsed = urlsplit(response.url)
                query_aweme_id = parse_qs(parsed.query).get("aweme_id", [None])[0]
                if (
                    captured is not None
                    or expected_aweme_id is None
                    or response.status != 200
                    or parsed.hostname != "www.douyin.com"
                    or parsed.path != "/aweme/v1/web/aweme/detail/"
                    or query_aweme_id != expected_aweme_id
                ):
                    return
                try:
                    payload = response.json()
                    detail = payload.get("aweme_detail") if isinstance(payload, dict) else None
                    if isinstance(detail, dict) and str(detail.get("aweme_id")) == expected_aweme_id:
                        captured = payload
                except Exception:
                    return

            page.goto(page_url, wait_until="domcontentloaded", timeout=60_000)
            match = re.search(r"/video/(\d+)(?:/|$)", urlsplit(page.url).path)
            if match:
                expected_aweme_id = match.group(1)
                page.on("response", capture)
            deadline = time.monotonic() + 20
            cookies: list[dict] = []
            render_data: object = {}
            while time.monotonic() < deadline:
                page.wait_for_timeout(500)
                cookies = context.cookies()
                names = {cookie.get("name") for cookie in cookies}
                render_locator = page.locator("#RENDER_DATA")
                if render_locator.count():
                    try:
                        encoded = render_locator.text_content(timeout=1_000) or ""
                        render_data = json.loads(unquote(encoded))
                    except (json.JSONDecodeError, UnicodeError):
                        render_data = {}
                render_ready = (
                    expected_aweme_id is not None
                    and _find_aweme_detail(render_data, expected_aweme_id) is not None
                )
                if captured is not None or ("s_v_web_id" in names and render_ready):
                    break
            _save_cookies(cookies, cookie_path)
            payload = captured or render_data
            final_match = re.search(r"/video/(\d+)(?:/|$)", urlsplit(page.url).path)
            final_aweme_id = final_match.group(1) if final_match else ""
            result = _aweme_result(payload, final_aweme_id, page.url)
            if result is None:
                result = {
                    "page_url": page.url,
                    "diagnostics": _aweme_diagnostics(payload, final_aweme_id),
                }
            result_path.write_text(json.dumps(result), encoding="utf-8")
            result_path.chmod(0o600)
        finally:
            try:
                if context is not None:
                    context.close()
            finally:
                browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--cookies", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("url")
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit("invalid URL")
    collect_session(args.url, args.proxy, args.cookies, args.result)


if __name__ == "__main__":
    main()
