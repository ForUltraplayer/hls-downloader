# @file hls_downloader.py
# @author: rudals252
# @brief 실제 Chrome CDP에 연결하는 입력 UI와 HLS 다운로드/MP4 변환.
"""원본 흐름: Chrome CDP → videoPlayerConfig → playlist → 조각 → TS → MP4."""

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import unicodedata
from pathlib import Path
from tkinter import filedialog, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.parse import quote, unquote, urljoin, urlsplit
from urllib.request import ProxyHandler, build_opener


APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
SETTINGS_PATH = APP_DIR / "headers.json"
CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
CHROME_PROFILE = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "chrome-debug"
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
HIDDEN_PROCESS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

HEADERS_TEMPLATE = {
    "Origin": "",
    "Referer": "",
    "Accept": "*/*",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="154", "Google Chrome";v="154", "Not A(Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "cross-site",
}


class Cancelled(Exception):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled("사용자가 작업을 중지했습니다.")


def validate_url(value, label):
    if any(character.isspace() for character in value):
        raise ValueError(f"{label}: 공백이나 줄바꿈을 포함할 수 없습니다.")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{label}: 올바른 HTTP/HTTPS URL을 입력하세요.")
    _ = parsed.port
    return parsed


def parse_video_urls(text):
    urls = []
    for line_number, line in enumerate(text.splitlines(), 1):
        value = line.strip()
        if value:
            validate_url(value, f"영상 URL ({line_number}번째 줄)")
            urls.append(value)
    if not urls:
        raise ValueError("영상 URL을 한 줄에 하나씩 입력하세요.")
    return urls


def make_headers(origin, referer, allow_empty=False):
    if origin or not allow_empty:
        parsed = validate_url(origin, "Origin")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Origin에는 경로 없이 사이트 주소만 입력하세요.")
    ref = validate_url(referer, "Referer") if referer or not allow_empty else None
    headers = HEADERS_TEMPLATE.copy()
    headers.update(Origin=origin.rstrip("/"), Referer=ref._replace(path=ref.path or "/").geturl() if ref else "")
    return headers


def save_settings(settings):
    temporary = SETTINGS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(SETTINGS_PATH)


def safe_error(error):
    # 요청 오류에 포함될 수 있는 URL의 인증정보/쿼리/fragment는 로그에서 제거한다.
    def clean(match):
        try:
            parsed = urlsplit(match.group())
            host = parsed.netloc.rsplit("@", 1)[-1]
            return f"{parsed.scheme}://{host}{parsed.path}" + ("?[숨김]" if parsed.query else "")
        except ValueError:
            return "[URL 숨김]"
    return re.sub(r"https?://[^\s<>\"']+", clean, str(error))


def usable_config(config):
    return (isinstance(config, dict) and isinstance(config.get("streamId"), (str, int))
            and bool(str(config["streamId"])) and isinstance(config.get("cdnHosts"), list)
            and bool(config["cdnHosts"])
            and all(isinstance(host, str) and host for host in config["cdnHosts"]))


def extract_config(html: str) -> dict:
    pattern = r'''(?:\bvideoPlayerConfig\b|["']videoPlayerConfig["'])\s*\]?\s*[=:]\s*'''
    for match in re.finditer(pattern, html):
        try:
            config, _ = json.JSONDecoder().raw_decode(html[match.end():].lstrip())
        except ValueError:
            continue
        if usable_config(config):
            return config
    raise RuntimeError("HTML에서 사용할 수 있는 videoPlayerConfig를 찾지 못했습니다.")


def page_key(url):
    parsed = urlsplit(url)
    port = parsed.port
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return (parsed.scheme.lower(), parsed.hostname, port or default_port,
            quote(parsed.path, safe="/%:@!$&'()*+,;=-._~").rstrip("/"), parsed.query)


def find_video_page(browser, video_page):
    wanted = page_key(video_page)
    matches = [page for context in browser.contexts for page in context.pages
               if page_key(page.url) == wanted]
    if not matches:
        raise RuntimeError("입력 URL과 일치하는 열린 탭이 없습니다. Chrome에서 해당 상세 페이지를 열고 "
                           "챌린지·광고를 처리한 뒤 영상을 재생하세요. 프로그램은 페이지를 이동하지 않습니다.")
    if len(matches) != 1:
        raise RuntimeError("같은 영상 URL의 탭이 여러 개 열려 있습니다. 사용할 탭 하나만 남겨주세요.")
    return matches[0]


def read_page_config(page, cancel, log):
    from patchright.sync_api import Error
    # 한 번씩만 읽는다. 챌린지 동안 DOM을 반복 조회하지 않는다.
    frames = list(page.frames)
    log(f"[*] 영상 설정 확인: 본문 및 iframe {max(0, len(frames) - 1)}개")
    for frame in frames:
        check_cancel(cancel)
        try:
            config = extract_config(frame.content())
        except (RuntimeError, Error):
            continue
        log("[*] HTML에서 영상 설정을 찾았습니다.")
        return config
    # Patchright의 기본 isolated context에서는 페이지 전역 설정이 보이지 않는다.
    expression = """() => {
        const config = typeof videoPlayerConfig !== 'undefined'
            ? videoPlayerConfig : window.videoPlayerConfig;
        if (!config || typeof config !== 'object') return null;
        return {streamId: config.streamId, playlistExt: config.playlistExt || 'html',
                cdnHosts: config.cdnHosts};
    }"""
    for frame in frames:
        check_cancel(cancel)
        try:
            config = frame.evaluate(expression, isolated_context=False)
        except Error:
            continue
        if usable_config(config):
            log("[*] 실행 중인 페이지에서 영상 설정을 찾았습니다.")
            return config
    return None


class PlaylistCapture:
    """선택한 탭의 playlist 응답만 관찰한다. 요청을 가로채거나 변경하지 않는다."""
    def __init__(self, general=False):
        self.urls = set()
        self.general = general

    def on_response(self, response):
        from patchright.sync_api import Error
        # 광고의 일반 m3u8 주소까지 무작위로 선택하지 않는다.
        matched = is_hls_url(response.url) if self.general else is_playlist_url(response.url)
        if self.general and not matched:
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            matched = content_type in ("application/vnd.apple.mpegurl", "application/x-mpegurl")
        if response.status != 200 or not matched:
            return
        try:
            if response.text().lstrip().startswith("#EXTM3U"):
                self.urls.add(response.url)
        except Error:
            pass

    def get_url(self):
        return select_playlist_url(self.urls)


def is_playlist_url(url):
    return bool(re.search(r"/(?:playlist|video)\.(?:html|m3u8)$", urlsplit(url).path, re.I))


def is_hls_url(url):
    return urlsplit(url).path.lower().endswith(".m3u8")


def select_playlist_url(urls):
    # 마스터와 그 하위 video.html은 한 영상의 정상적인 요청 순서다.
    masters = {url for url in urls if re.search(r"/playlist\.", urlsplit(url).path, re.I)}
    candidates = masters or set(urls)
    if len(candidates) > 1:
        raise RuntimeError("서로 다른 재생목록이 여러 개 감지되었습니다. 사용할 playlist.html 또는 "
                           "video.html URL을 입력란에 직접 붙여 넣어주세요.")
    return next(iter(candidates), None)


def resource_playlist_urls(page, cancel, predicate=is_playlist_url):
    from patchright.sync_api import Error, Page
    urls = set()
    for frame in list(page.frames):
        check_cancel(cancel)
        if predicate(frame.url):
            urls.add(frame.url)
        try:
            # 완료된 응답 본문을 복원하는 대신, 브라우저가 보관한 요청 주소를 읽는다.
            resources = frame.evaluate("() => performance.getEntriesByType('resource').map(entry => entry.name)")
        except Error:
            continue
        urls.update(url for url in resources if isinstance(url, str) and predicate(url))
    if isinstance(page, Page):
        for record in cdp_iframe_records(page, cancel):
            urls.update(url for url in record["resources"] if isinstance(url, str) and predicate(url))
    return urls


def cdp_iframe_records(page, cancel, resources=True):
    """Chrome의 분리된 iframe 중 이 탭 소유의 대상만 읽는다. 요청/탐색은 하지 않는다."""
    from patchright.sync_api import Error
    browser_session = page.context.browser.new_browser_cdp_session()
    page_session = page.context.new_cdp_session(page)
    records = []
    try:
        root_id = page_session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
        targets = browser_session.send("Target.getTargets")["targetInfos"]
        by_id = {target["targetId"]: target for target in targets}
        known_urls = {frame.url for frame in page.frames}
        for target in targets:
            check_cancel(cancel)
            if target["type"] != "iframe" or target["url"] in known_urls:
                continue
            parent = target.get("parentId", target.get("parentFrameId"))
            visited = set()
            while parent and parent != root_id and parent not in visited:
                visited.add(parent)
                ancestor = by_id.get(parent, {})
                parent = ancestor.get("parentId", ancestor.get("parentFrameId"))
            if parent != root_id:
                continue
            if not resources:
                records.append({"url": target["url"], "resources": []})
                continue
            session_id = browser_session.send("Target.attachToTarget", {"targetId": target["targetId"], "flatten": False})["sessionId"]
            replies = []

            def received(event):
                if event.get("sessionId") == session_id:
                    reply = json.loads(event["message"])
                    if reply.get("id") == 1:
                        replies.append(reply)

            browser_session.on("Target.receivedMessageFromTarget", received)
            try:
                expression = "JSON.stringify({url:location.href,resources:performance.getEntriesByType('resource').map(e=>e.name)})"
                message = {"id": 1, "method": "Runtime.evaluate", "params": {"expression": expression, "returnByValue": True}}
                browser_session.send("Target.sendMessageToTarget", {"sessionId": session_id, "message": json.dumps(message)})
                deadline = time.monotonic() + 5
                while not replies and time.monotonic() < deadline:
                    check_cancel(cancel)
                    page.wait_for_timeout(50)
                if replies and "result" in replies[0]:
                    value = replies[0]["result"].get("result", {}).get("value")
                    if isinstance(value, str):
                        records.append(json.loads(value))
            except Error:
                pass
            finally:
                browser_session.remove_listener("Target.receivedMessageFromTarget", received)
                browser_session.send("Target.detachFromTarget", {"sessionId": session_id})
    finally:
        page_session.detach()
        browser_session.detach()
    return records


def read_resource_playlist(page, cancel, log):
    result = select_playlist_url(resource_playlist_urls(page, cancel))
    if result:
        log("[*] 브라우저에 남아 있는 재생목록 요청 주소를 찾았습니다. 헤더를 넣어 내용을 다시 요청합니다.")
    return result


def find_request_page(browser, origin):
    from patchright.sync_api import Page
    wanted = page_key(origin)[:3]
    contexts = []
    for context in browser.contexts:
        pages = []
        for page in context.pages:
            matched = page_key(page.url)[:3] == wanted or any(
                page_key(frame.url)[:3] == wanted for frame in getattr(page, "frames", []))
            if not matched and isinstance(page, Page):
                matched = any(page_key(record["url"])[:3] == wanted for record in
                              cdp_iframe_records(page, threading.Event(), resources=False))
            if matched:
                pages.append(page)
        if pages:
            contexts.append(pages)
    if len(contexts) != 1:
        raise RuntimeError("Origin에 해당하는 Chrome 사이트 탭이 필요합니다. 영상을 선택한 상세 탭을 "
                           "열어 두고 Origin·Referer에 실제 사이트 주소를 입력하세요.")
    # page.request는 같은 컨텍스트의 쿠키를 공유한다. 탭 자체는 조작하지 않는다.
    return contexts[0][0]


def hls_header_options(pages, playlist_url, cancel):
    from patchright.sync_api import Error, Page
    options = {}
    for page in pages:
        records = []
        for frame in list(page.frames):
            check_cancel(cancel)
            try:
                resources = frame.evaluate("() => performance.getEntriesByType('resource').map(entry => entry.name)")
            except (Error, ValueError):
                continue
            records.append({"url": frame.url, "resources": resources})
        if isinstance(page, Page):
            records.extend(cdp_iframe_records(page, cancel))
        for record in records:
            try:
                source = validate_url(record["url"], "플레이어 페이지")
            except ValueError:
                continue
            matches = [url for url in record["resources"] if isinstance(url, str) and
                       (url == playlist_url if playlist_url else is_hls_url(url))]
            if not matches:
                continue
            origin = source._replace(path="", query="", fragment="").geturl()
            # Chrome 기본 referrer policy의 교차 출처 요청은 출처 주소만 전송한다.
            cross_origin = any(page_key(url)[:3] != page_key(origin)[:3] for url in matches)
            referer = origin + "/" if cross_origin else source._replace(fragment="").geturl()
            options[record["url"]] = {"url": record["url"], "label": "재생목록을 요청한 페이지",
                                  "Origin": origin, "Referer": referer}
    if not options:
        raise RuntimeError("재생목록을 요청한 페이지 기록이 없습니다. 연결한 Chrome에서 실제 영상을 "
                           "재생한 뒤 다시 헤더를 적용하거나 Origin·Referer를 직접 입력하세요.")
    return list(options.values())


def find_hls_headers(url, cancel, choose, direct_hls=False):
    from patchright.sync_api import sync_playwright
    check_cancel(cancel)
    if not read_cdp():
        raise RuntimeError("CDP로 열린 Chrome이 없습니다. 'Chrome 열기' 후 직접 영상을 준비하세요.")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL, timeout=15000)
        direct = direct_hls or is_hls_url(url)
        pages = [page for context in browser.contexts for page in context.pages] if direct else [find_video_page(browser, url)]
        options = hls_header_options(pages, url if direct else None, cancel)
        selected = choose_hls_option(options, "헤더 기준 플레이어 선택", cancel, choose)
        option = next(option for option in options if option["url"] == selected)
        return {key: option[key] for key in ("Origin", "Referer")}


def resolve_playlist(page, cancel, log, wait_for_user):
    capture = PlaylistCapture()
    page.on("response", capture.on_response)
    try:
        config = read_page_config(page, cancel, log)
        if config:
            return build_playlist_url(config, config["cdnHosts"][0])
        remembered_url = read_resource_playlist(page, cancel, log)
        if remembered_url:
            return remembered_url
        log("[*] 설정과 요청 주소가 아직 없습니다. Chrome에서 영상을 다시 선택하거나 재생한 뒤 '재생 후 계속'을 누르세요.")
        log("[*] 이 대기 중에는 페이지를 조회하거나 조작하지 않습니다.")
        wait_for_user()
        check_cancel(cancel)
        # 사용자가 재생하는 동안 수신한 응답 이벤트를 한 번 처리한다.
        page.wait_for_timeout(100)
        observed_url = capture.get_url()
        if observed_url:
            log("[*] 실제 재생에서 발생한 HLS 재생목록을 확인했습니다.")
            return observed_url
        config = read_page_config(page, cancel, log)
        if config:
            return build_playlist_url(config, config["cdnHosts"][0])
        remembered_url = read_resource_playlist(page, cancel, log)
        if remembered_url:
            return remembered_url
        raise RuntimeError("재생목록 주소를 찾지 못했습니다. 영상 선택 때 나온 playlist.html 또는 "
                           "재생 때 나온 video.html URL을 입력란에 직접 붙여 넣어 다운로드하세요. "
                           "Origin·Referer는 영상 사이트 주소를 유지하세요.")
    finally:
        page.remove_listener("response", capture.on_response)


def build_playlist_url(config: dict, host: str) -> str:
    stream_id = config["streamId"]
    ext = config.get("playlistExt", "html")
    return f"{host.rstrip('/')}/{stream_id}/playlist.{ext}"


def parse_master(text: str, base_url: str) -> str:
    return parse_variants(text, base_url)[0]["url"]


def playlist_attributes(text):
    return {match[0]: (match[1] or match[2]).strip() for match in
            re.findall(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))', text)}


def parse_variants(text, base_url):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants = []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                attributes = playlist_attributes(line.split(":", 1)[1])
                resolution = attributes.get("RESOLUTION", "해상도 정보 없음")
                bandwidth = attributes.get("AVERAGE-BANDWIDTH", attributes.get("BANDWIDTH", ""))
                label = resolution + (f" / {int(bandwidth) / 1000:g} kbps" if bandwidth.isdigit() else "")
                dimensions = re.fullmatch(r"([0-9]+)x([0-9]+)", resolution)
                resolution_area = int(dimensions[1]) * int(dimensions[2]) if dimensions else 0
                peak = attributes.get("BANDWIDTH", "")
                rank_bandwidth = int(peak) if peak.isdigit() and int(peak) > 0 else (
                    int(bandwidth) if bandwidth.isdigit() else 0)
                variants.append({"url": urljoin(base_url, lines[i + 1]), "label": label,
                                 "resolution_area": resolution_area, "bandwidth": rank_bandwidth})
    if not variants:
        raise RuntimeError("하위 재생목록을 찾지 못했습니다.")
    return variants


def choose_hls_option(options, title, cancel, choose, confirm_single=False):
    check_cancel(cancel)
    if len(options) == 1 and not confirm_single:
        return options[0]["url"]
    if choose is None:
        raise RuntimeError(f"{title}이 필요합니다. 사용할 m3u8 주소를 직접 입력하세요.")
    result = choose(title, options)
    check_cancel(cancel)
    if result not in {option["url"] for option in options}:
        raise RuntimeError("목록에 없는 재생목록을 선택했습니다.")
    return result


def discover_hls(page, cancel, log, wait_for_user, choose):
    capture = PlaylistCapture(general=True)
    page.on("response", capture.on_response)
    try:
        urls = resource_playlist_urls(page, cancel, is_hls_url)
        if not urls:
            log("[*] m3u8 기록이 없습니다. Chrome에서 실제 영상을 재생한 뒤 '재생 후 계속'을 누르세요.")
            wait_for_user()
            check_cancel(cancel)
            page.wait_for_timeout(100)
            urls = capture.urls | resource_playlist_urls(page, cancel, is_hls_url)
        if not urls:
            raise RuntimeError("m3u8 재생목록을 찾지 못했습니다. 직접 URL을 입력하거나 연결 후 다시 재생하세요.")
        options = [{"url": url, "label": "재생목록 후보"} for url in sorted(urls)]
        return choose_hls_option(options, "재생목록 선택 — 광고·본영상을 확인하세요", cancel, choose, confirm_single=True)
    finally:
        page.remove_listener("response", capture.on_response)


def request_playlist(page, url, headers, cancel):
    check_cancel(cancel)
    validate_url(url, "재생목록 URL")
    response = page.request.get(url, headers=headers, timeout=60000)
    try:
        if response.status != 200:
            raise RuntimeError(f"재생목록 요청 실패 (HTTP {response.status}).")
        text, final_url = response.text(), response.url
        if not text.lstrip().startswith("#EXTM3U"):
            raise RuntimeError("서버 응답이 HLS 재생목록이 아닙니다.")
        return final_url, text
    finally:
        response.dispose()


def load_media_playlist(page, playlist_url, headers, cancel, mode, choose, log=None):
    playlist_url, text = request_playlist(page, playlist_url, headers, cancel)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-SESSION-KEY:"):
            if playlist_attributes(line.split(":", 1)[1]).get("METHOD") != "NONE":
                raise RuntimeError("원본 방식이 지원하지 않는 암호화 HLS입니다.")
        if line.startswith("#EXT-X-MEDIA:"):
            attributes = playlist_attributes(line.split(":", 1)[1])
            if attributes.get("TYPE") == "AUDIO" and attributes.get("URI"):
                raise RuntimeError("분리 오디오 HLS는 원본 병합 방식으로 지원하지 않습니다.")
    if "#EXT-X-STREAM-INF:" not in text:
        return playlist_url, text
    if mode == "hls":
        variants = parse_variants(text, playlist_url)
        best = max(variants, key=lambda variant: (variant["resolution_area"], variant["bandwidth"]))
        media_url = best["url"]
        if log:
            if best["resolution_area"] or best["bandwidth"]:
                log(f"[*] 최고 화질 자동 선택: {best['label']}")
            else:
                log("[*] 화질 정보가 없어 첫 번째 하위 재생목록을 사용합니다.")
    else:
        media_url = parse_master(text, playlist_url)
    return request_playlist(page, media_url, headers, cancel)


def find_hls_playlist(video_page, headers, cancel, log, wait_for_user, choose, direct_hls=False):
    from patchright.sync_api import sync_playwright
    check_cancel(cancel)
    if not read_cdp():
        raise RuntimeError("CDP로 열린 Chrome이 없습니다. 'Chrome 열기' 후 직접 영상을 준비하세요.")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL, timeout=15000)
        if direct_hls or is_hls_url(video_page):
            page = find_request_page(browser, headers["Origin"])
            playlist_url = video_page
        else:
            page = find_video_page(browser, video_page)
            playlist_url = discover_hls(page, cancel, log, wait_for_user, choose)
        media_url, media_text = load_media_playlist(page, playlist_url, headers, cancel, "hls", choose, log)
        parse_media(media_text)
        return media_url


def parse_media(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("#EXT-X-KEY:") and not re.match(r"#EXT-X-KEY:METHOD=NONE(?:,|$)", line):
            raise RuntimeError("원본 방식이 지원하지 않는 암호화 HLS입니다.")
        if line.startswith(("#EXT-X-MAP:", "#EXT-X-BYTERANGE:")):
            raise RuntimeError("원본 TS 병합 방식이 지원하지 않는 HLS 형식입니다.")
    if "#EXT-X-ENDLIST" not in lines:
        raise RuntimeError("종료되지 않은 라이브 재생목록은 지원하지 않습니다.")
    segments = []
    for i, line in enumerate(lines):
        if line.startswith("#EXTINF:"):
            j = i + 1
            while j < len(lines) and lines[j].startswith("#"):
                if lines[j].startswith(("#EXTINF:", "#EXT-X-ENDLIST")):
                    break
                j += 1
            if j >= len(lines) or lines[j].startswith("#"):
                raise RuntimeError("재생목록에 조각 주소가 누락되었습니다.")
            segments.append(lines[j])
    if not segments:
        raise RuntimeError("다운로드할 조각이 없습니다.")
    return segments


def read_cdp():
    try:
        with build_opener(ProxyHandler({})).open(CDP_URL + "/json/version", timeout=1) as response:
            data = json.load(response)
        if data.get("webSocketDebuggerUrl") and "Chrome/" in data.get("Browser", ""):
            return data
    except (OSError, ValueError):
        pass
    return None


def launch_chrome(log):
    if read_cdp():
        log(f"[*] CDP({CDP_PORT})로 실행 중인 Chrome을 사용하세요. 아직 자동화에 연결하지 않았습니다.")
        return
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", CDP_PORT)) == 0:
            raise RuntimeError("9222 포트를 다른 프로그램이 사용하고 있습니다.")
    if not CHROME_PATH.is_file():
        raise RuntimeError(f"Chrome을 찾지 못했습니다: {CHROME_PATH}")
    log("[*] 디버깅용 Chrome을 자동 실행합니다.")
    subprocess.Popen([
        str(CHROME_PATH), f"--remote-debugging-port={CDP_PORT}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={CHROME_PROFILE}", "--no-first-run", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=HIDDEN_PROCESS)
    log("[*] Chrome 실행 명령을 전달했습니다. 직접 사이트에 접속해 챌린지·광고를 처리하고 영상을 재생하세요.")
    log("[*] 다운로드 버튼을 누르기 전에는 CDP에 연결하거나 페이지를 조회하지 않습니다.")


def find_ffmpeg():
    for candidate in (APP_DIR / "ffmpeg.exe", APP_DIR / "bin" / "ffmpeg.exe"):
        if candidate.is_file():
            return str(candidate)
    result = shutil.which("ffmpeg")
    if not result:
        raise RuntimeError("FFmpeg가 없습니다. ffmpeg.exe를 프로그램 폴더에 넣거나 PATH에 등록하세요.")
    return result


def output_name(video_page):
    parts = urlsplit(video_page).path.rstrip("/").split("/")
    generic_hls = is_hls_url(video_page) and parts[-1].lower() in ("master.m3u8", "index.m3u8")
    name = parts[-2] if (is_playlist_url(video_page) or generic_hls) and len(parts) > 1 else parts[-1]
    name = unicodedata.normalize("NFC", unquote(name))
    name = re.sub(r"\.(?:html?|mp4|m3u8)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    name = name[:10].rstrip(" .") or "video"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]", name.split(".")[0], re.IGNORECASE):
        name = ("_" + name)[:10].rstrip(" .")
    return name + ".mp4"


def cleanup_intermediates(job_dir, files, ts_path):
    # 현재 작업이 직접 생성한 파일만 제거한다. 다른 작업이나 외부 경로는 건드리지 않는다.
    job = job_dir.resolve()
    segments = job / "segments"
    if job_dir.is_symlink() or segments.resolve() != segments:
        raise RuntimeError("중간 파일 정리 경로가 현재 작업 폴더를 벗어났습니다.")
    if ts_path.resolve() != job / "output.ts" or any(target.resolve().parent != segments for target in files):
        raise RuntimeError("중간 파일 정리 경로가 현재 작업 폴더를 벗어났습니다.")
    for target in files:
        target.unlink()
    segments.rmdir()
    ts_path.unlink()


def final_output_path(output_dir, video_page):
    name = output_name(video_page)
    candidate = output_dir / name
    number = 1
    while candidate.exists():
        suffix = f"_{number}"
        if len(suffix) >= 10:
            raise RuntimeError("같은 이름의 파일이 너무 많습니다. 저장 폴더를 변경하세요.")
        candidate = output_dir / (Path(name).stem[:10 - len(suffix)] + suffix + ".mp4")
        number += 1
    return candidate


def run_download(video_page, output_dir, headers, cancel, log, progress, wait_for_user, mode="site", choose=None,
                 direct_hls=False):
    if mode not in ("site", "hls"):
        raise ValueError("지원하지 않는 다운로드 방식입니다.")
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError("Patchright가 없습니다. python -m pip install -r requirements.txt 를 실행하세요.") from error
    check_cancel(cancel)
    ffmpeg = find_ffmpeg()
    if not read_cdp():
        raise RuntimeError(f"CDP({CDP_PORT})로 열린 Chrome이 없습니다. 'Chrome 열기' 후 직접 영상을 "
                           "재생하고 다운로드하세요. 일반 기본 프로필에는 디버깅 포트를 붙일 수 없습니다.")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL, timeout=15000)
        direct = (direct_hls or is_hls_url(video_page)) if mode == "hls" else is_playlist_url(video_page)
        if direct:
            page = find_request_page(browser, headers["Origin"])
            playlist_url = video_page
            log("[*] 직접 입력한 재생목록을 사용합니다. 열린 사이트 탭의 쿠키와 설정한 헤더로 요청합니다.")
        else:
            page = find_video_page(browser, video_page)
            log("[*] 사용자가 열어 둔 상세 탭을 사용합니다. 이동·새로고침·클릭을 수행하지 않습니다.")
            playlist_url = (discover_hls(page, cancel, log, wait_for_user, choose) if mode == "hls"
                            else resolve_playlist(page, cancel, log, wait_for_user))
        check_cancel(cancel)
        output_dir.mkdir(parents=True, exist_ok=True)
        job_dir = Path(tempfile.mkdtemp(prefix=".hls-work-", dir=output_dir))
        segment_dir = job_dir / "segments"
        segment_dir.mkdir()
        log(f"[*] 임시 작업 폴더: {job_dir}")

        def get(url, label):
            check_cancel(cancel)
            validate_url(url, label)
            response = page.request.get(url, headers=headers, timeout=60000)
            if response.status != 200:
                status = response.status
                response.dispose()
                raise RuntimeError(f"{label} 요청 실패 (HTTP {status}).")
            return response

        media_url, media_text = load_media_playlist(page, playlist_url, headers, cancel, mode, choose, log)
        segments = parse_media(media_text)
        log(f"[*] 조각 수: {len(segments)}")
        files = []
        for i, segment in enumerate(segments):
            # URL의 query/경로를 파일명으로 쓰지 않아 경로 이탈 및 충돌을 방지한다.
            target = segment_dir / f"{i:06d}.ts"
            response = get(urljoin(media_url, segment), f"조각 {i + 1}/{len(segments)}")
            body = response.body()
            response.dispose()
            if not body:
                raise RuntimeError(f"조각 {i + 1}의 응답이 비어 있습니다.")
            target.write_bytes(body)
            files.append(target)
            progress(i + 1, len(segments))
            log(f"[{i + 1}/{len(segments)}] 저장 ({len(body):,} bytes)")
    check_cancel(cancel)
    ts_path = job_dir / "output.ts"
    log("[*] TS 병합 중...")
    with ts_path.open("wb") as merged:
        for target in files:
            check_cancel(cancel)
            with target.open("rb") as source:
                shutil.copyfileobj(source, merged)
    partial_path = job_dir / "output.partial.mp4"
    log("[*] FFmpeg MP4 변환 중...")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-n",
           "-i", str(ts_path), "-c", "copy", "-bsf:a", "aac_adtstoasc",
           "-movflags", "+faststart", str(partial_path)]
    # PIPE를 읽지 않아 FFmpeg가 멈추는 일을 방지하고, 변환 중 중지를 지원한다.
    with tempfile.TemporaryFile() as error_log:
        process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=error_log,
                                   creationflags=HIDDEN_PROCESS)
        try:
            while process.poll() is None:
                check_cancel(cancel)
                cancel.wait(0.2)
            check_cancel(cancel)
            if process.returncode != 0:
                error_log.seek(0)
                detail = error_log.read().decode("utf-8", errors="replace")[-4000:]
                raise RuntimeError(f"FFmpeg 변환 실패 ({process.returncode}):\n{detail}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    if not partial_path.exists() or partial_path.stat().st_size == 0:
        raise RuntimeError("MP4 결과가 비어 있습니다.")
    mp4_path = final_output_path(output_dir, video_page)
    # Windows rename은 기존 파일을 덮어쓰지 않는다. 동시 생성 시에도 원본을 보호한다.
    partial_path.rename(mp4_path)
    try:
        cleanup_intermediates(job_dir, files, ts_path)
        job_dir.rmdir()
    except (OSError, RuntimeError) as error:
        raise RuntimeError(f"MP4는 저장했지만 중간 파일 정리에 실패했습니다: {mp4_path}\n{error}") from error
    log("[*] 중간 조각과 TS 정리 완료. MP4만 남겼습니다.")
    log(f"[*] MP4: {mp4_path} ({mp4_path.stat().st_size:,} bytes)")
    return mp4_path


class DownloaderApp:
    def __init__(self, root):
        self.root = root
        root.title("HLS 다운로더 — 수동 재생한 Chrome 탭 사용")
        root.geometry("900x900")
        root.minsize(780, 740)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.resume = threading.Event()
        self.worker = None
        self.closing = False
        self.active_item = None
        self.selection_ready = threading.Event()
        self.selection_value = None
        self.selection_options = []
        self.detected_playlists = set()
        self.mode = tk.StringVar(value="site")
        saved = {}
        settings_error = None
        try:
            if SETTINGS_PATH.exists():
                saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                if not isinstance(saved, dict):
                    raise ValueError("설정 파일은 JSON 객체여야 합니다.")
                normalized = make_headers(saved.get("Origin", HEADERS_TEMPLATE["Origin"]),
                                          saved.get("Referer", HEADERS_TEMPLATE["Referer"]), allow_empty=True)
                saved.update({key: normalized[key] for key in ("Origin", "Referer")})
        except (OSError, ValueError, TypeError) as error:
            saved = {}
            settings_error = safe_error(error)
        last_output = saved.get("OutputDir")
        self.output = tk.StringVar(value=last_output if isinstance(last_output, str) and last_output.strip()
                                   else str(APP_DIR / "output"))
        self.saved_settings = {key: saved.get(key, HEADERS_TEMPLATE[key]) for key in ("Origin", "Referer")}
        self.origin = tk.StringVar(value=saved.get("Origin", HEADERS_TEMPLATE["Origin"]))
        self.referer = tk.StringVar(value=saved.get("Referer", HEADERS_TEMPLATE["Referer"]))
        self.status = tk.StringVar(value="Chrome에서 영상을 준비하고 URL을 한 줄에 하나씩 입력하세요.")
        frame = ttk.Frame(root, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(8, weight=1)
        ttk.Label(frame, text="영상 URL\n한 줄에 하나").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.video_entry = ScrolledText(frame, height=4, wrap="none")
        self.video_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=5)
        ttk.Label(frame, text="저장 폴더").grid(row=1, column=0, sticky="w")
        self.output_entry = ttk.Entry(frame, textvariable=self.output)
        self.output_entry.grid(row=1, column=1, sticky="ew", pady=5)
        self.output_entry.bind("<FocusOut>", lambda event: self.remember_settings())
        self.output_entry.bind("<Return>", lambda event: self.remember_settings())
        folder_buttons = ttk.Frame(frame)
        folder_buttons.grid(row=1, column=2, padx=(8, 0))
        self.browse = ttk.Button(folder_buttons, text="찾아보기", command=self.choose_output)
        self.browse.pack(side="left")
        self.open_folder_button = ttk.Button(folder_buttons, text="폴더 열기", command=self.open_output)
        self.open_folder_button.pack(side="left", padx=(6, 0))
        headers_frame = ttk.LabelFrame(frame, text="다운로드 방식 / 헤더 설정", padding=10)
        headers_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=10)
        headers_frame.columnconfigure(1, weight=1)
        mode_frame = ttk.Frame(headers_frame)
        mode_frame.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.mode_buttons = []
        for label, value in (("기존 사이트 방식", "site"), ("일반 HLS · m3u8", "hls")):
            button = ttk.Radiobutton(mode_frame, text=label, variable=self.mode, value=value, command=self.update_mode)
            button.pack(side="left", padx=(0, 12))
            self.mode_buttons.append(button)
        self.find_button = ttk.Button(mode_frame, text="재생목록 찾기", command=self.find_playlists, state="disabled")
        self.find_button.pack(side="left", padx=(0, 8))
        self.page_headers_button = ttk.Button(mode_frame, text="페이지 헤더 적용", command=self.use_page_headers, state="disabled")
        self.page_headers_button.pack(side="left")
        self.header_entries = []
        for i, (label, variable) in enumerate((("Origin", self.origin), ("Referer", self.referer))):
            ttk.Label(headers_frame, text=label).grid(row=i + 1, column=0, sticky="w", padx=(0, 12))
            entry = ttk.Entry(headers_frame, textvariable=variable)
            entry.grid(row=i + 1, column=1, sticky="ew", pady=4)
            entry.bind("<FocusOut>", lambda event: self.remember_settings())
            entry.bind("<Return>", lambda event: self.remember_settings())
            self.header_entries.append(entry)
        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=3, sticky="w", pady=5)
        self.chrome_button = ttk.Button(buttons, text="Chrome 열기", command=self.open_chrome)
        self.chrome_button.pack(side="left", padx=(0, 8))
        self.start_button = ttk.Button(buttons, text="대기열 다운로드", command=self.start)
        self.start_button.pack(side="left")
        self.resume_button = ttk.Button(buttons, text="재생 후 계속", command=self.resume.set, state="disabled")
        self.resume_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(buttons, text="중지", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        self.queue_view = ttk.Treeview(frame, columns=("number", "state", "url"), show="headings", height=5)
        for column, title, width in (("number", "순서", 50), ("state", "상태", 90), ("url", "입력 URL", 560)):
            self.queue_view.heading(column, text=title)
            self.queue_view.column(column, width=width, stretch=column == "url")
        self.queue_view.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.grid(row=5, column=0, columnspan=3, sticky="ew", pady=8)
        ttk.Label(frame, textvariable=self.status, wraplength=790).grid(row=6, column=0, columnspan=3, sticky="w")
        self.selector_frame = ttk.LabelFrame(frame, text="재생목록 선택", padding=6)
        self.selector_frame.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.selector_frame.columnconfigure(0, weight=1)
        self.option_view = ttk.Treeview(self.selector_frame, columns=("label", "url"), show="headings", height=3)
        self.option_view.heading("label", text="후보 / 화질")
        self.option_view.heading("url", text="주소")
        self.option_view.column("label", width=200, stretch=False)
        self.option_view.column("url", width=580)
        self.option_view.grid(row=0, column=0, sticky="ew")
        self.confirm_button = ttk.Button(self.selector_frame, text="선택 완료", command=self.confirm_selection, state="disabled")
        self.confirm_button.grid(row=0, column=1, padx=(8, 0))
        self.option_view.bind("<<TreeviewSelect>>", lambda event: self.confirm_button.configure(
            state="normal" if self.option_view.selection() and not self.cancel.is_set() else "disabled"))
        self.selector_frame.grid_remove()
        self.log_box = ScrolledText(frame, height=15, state="disabled", wrap="word")
        self.log_box.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        ttk.Label(frame, text="Chrome 열기: chrome-debug 프로필 사용. 이미 CDP(9222)로 열린 Chrome은 그대로 사용합니다.",
                  wraplength=790).grid(row=9, column=0, columnspan=3, sticky="w", pady=(8, 0))
        if settings_error:
            self.append_log(f"설정을 읽지 못해 기본값을 표시합니다: {settings_error}")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        self.video_entry.focus_set()

    def append_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def choose_output(self):
        folder = filedialog.askdirectory(title="저장 폴더 선택", initialdir=self.output.get())
        if folder:
            self.output.set(folder)
            self.remember_settings()

    def remember_settings(self):
        if not self.output.get().strip():
            return
        try:
            folder = Path(self.output.get().strip()).expanduser().resolve()
            headers = make_headers(self.origin.get().strip(), self.referer.get().strip(), allow_empty=True)
            settings = {key: headers[key] for key in ("Origin", "Referer")}
            settings["OutputDir"] = str(folder)
            save_settings(settings)
            self.saved_settings = settings
            self.origin.set(headers["Origin"])
            self.referer.set(headers["Referer"])
        except (OSError, ValueError) as error:
            self.append_log(f"설정 저장 오류: {safe_error(error)}")

    def open_output(self):
        try:
            if not self.output.get().strip():
                raise ValueError("저장 폴더를 지정하세요.")
            folder = Path(self.output.get().strip()).expanduser().resolve()
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
            self.remember_settings()
        except (OSError, ValueError) as error:
            self.append_log(f"폴더 열기 실패: {safe_error(error)}")

    def open_chrome(self):
        try:
            launch_chrome(self.append_log)
            self.status.set("Chrome에서 직접 챌린지·영상 선택을 마친 뒤 상세 URL 또는 재생목록 URL을 입력하세요.")
        except (OSError, RuntimeError) as error:
            self.append_log(f"Chrome 실행 실패: {safe_error(error)}")

    def update_mode(self):
        general = self.mode.get() == "hls"
        for button in (self.find_button, self.page_headers_button):
            button.configure(state="normal" if general else "disabled")
        self.status.set("일반 HLS: 실제 영상 사이트의 Origin·Referer를 설정하고 m3u8 또는 페이지 URL을 입력하세요."
                        if general else "기존 사이트: 상세 URL 또는 playlist.html / video.html을 입력하세요.")

    def use_page_headers(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            url = parse_video_urls(self.video_entry.get("1.0", "end-1c"))[0]
        except ValueError as error:
            self.append_log(f"헤더 적용 오류: {safe_error(error)}")
            return
        self.cancel.clear()
        self.resume.clear()
        self.active_item = None
        self.set_running(True)
        self.status.set("Chrome에서 재생목록을 요청한 본문·iframe을 확인하는 중...")
        self.worker = threading.Thread(target=self.headers_work,
                                       args=(url, url in self.detected_playlists), daemon=True)
        self.worker.start()

    def headers_work(self, url, direct_hls=False):
        try:
            headers = find_hls_headers(url, self.cancel, self.choose_option, direct_hls)
            check_cancel(self.cancel)
            self.events.put(("headers", headers))
        except Cancelled as error:
            self.events.put(("cancel", str(error)))
        except Exception as error:
            self.events.put(("error", f"헤더 적용 오류: {type(error).__name__}: {safe_error(error)}"))

    def set_running(self, running):
        for widget in (self.video_entry, self.output_entry, self.browse, *self.header_entries,
                       *self.mode_buttons, self.chrome_button, self.start_button):
            widget.configure(state="disabled" if running else "normal")
        for button in (self.find_button, self.page_headers_button):
            button.configure(state="normal" if not running and self.mode.get() == "hls" else "disabled")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.resume_button.configure(state="disabled")
        if not running:
            self.selector_frame.grid_remove()
            self.confirm_button.configure(state="disabled")

    def find_playlists(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            urls = parse_video_urls(self.video_entry.get("1.0", "end-1c"))
            if len(urls) != 1:
                raise ValueError("재생목록 찾기는 영상 페이지 또는 m3u8 URL 한 개씩 사용하세요. 여러 링크는 대기열 다운로드로 처리하세요.")
            headers = make_headers(self.origin.get().strip(), self.referer.get().strip())
            self.remember_settings()
        except ValueError as error:
            self.append_log(f"재생목록 찾기 오류: {safe_error(error)}")
            return
        self.cancel.clear()
        self.resume.clear()
        self.active_item = None
        self.set_running(True)
        self.progress.configure(value=0, maximum=1)
        self.status.set("준비된 Chrome 탭에서 재생목록을 찾는 중...")
        self.worker = threading.Thread(target=self.find_work,
                                       args=(urls[0], headers, urls[0] in self.detected_playlists), daemon=True)
        self.worker.start()

    def find_work(self, url, headers, direct_hls=False):
        try:
            result = find_hls_playlist(url, headers, self.cancel,
                                       lambda text: self.events.put(("log", text)),
                                       self.wait_for_user, self.choose_option, direct_hls=direct_hls)
            self.events.put(("found", result))
        except Cancelled as error:
            self.events.put(("cancel", str(error)))
        except Exception as error:
            self.events.put(("error", f"{type(error).__name__}: {safe_error(error)}"))

    def choose_option(self, title, options):
        self.selection_value = None
        self.selection_ready.clear()
        self.events.put(("choose", (title, options)))
        while not self.selection_ready.wait(0.2):
            check_cancel(self.cancel)
        check_cancel(self.cancel)
        return self.selection_value

    def confirm_selection(self):
        selected = self.option_view.selection()
        if not selected or self.cancel.is_set():
            return
        self.selection_value = self.selection_options[int(selected[0])]["url"]
        self.selector_frame.grid_remove()
        self.confirm_button.configure(state="disabled")
        self.selection_ready.set()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            video_pages = parse_video_urls(self.video_entry.get("1.0", "end-1c"))
            headers = make_headers(self.origin.get().strip(), self.referer.get().strip())
            if not self.output.get().strip():
                raise ValueError("저장 폴더를 지정하세요.")
            output_dir = Path(self.output.get().strip()).expanduser().resolve()
            # 헤더 두 개와 마지막 저장 폴더만 저장한다.
            settings = {key: headers[key] for key in ("Origin", "Referer")}
            settings["OutputDir"] = str(output_dir)
            save_settings(settings)
            self.saved_settings = settings
            self.origin.set(headers["Origin"])
            self.referer.set(headers["Referer"])
        except (OSError, ValueError) as error:
            self.append_log(f"입력/설정 오류: {safe_error(error)}")
            return
        self.cancel.clear()
        self.resume.clear()
        self.active_item = None
        for row in self.queue_view.get_children():
            self.queue_view.delete(row)
        for index, url in enumerate(video_pages):
            self.queue_view.insert("", "end", iid=str(index), values=(index + 1, "대기", url))
        self.set_running(True)
        self.progress.configure(value=0)
        self.status.set("재생한 Chrome 탭에 연결 중...")
        self.append_log(f"\n=== 대기열 시작: {len(video_pages)}개, 순차 다운로드 ===")
        mode = self.mode.get()
        self.worker = threading.Thread(target=self.work,
                                       args=(video_pages, output_dir, headers, mode, frozenset(self.detected_playlists)), daemon=True)
        self.worker.start()

    def wait_for_user(self):
        self.resume.clear()
        self.events.put(("wait", None))
        while not self.resume.wait(0.2):
            check_cancel(self.cancel)
        check_cancel(self.cancel)
        self.events.put(("resume", None))

    def work(self, video_pages, output_dir, headers, mode="site", known_playlists=()):
        succeeded = failed = 0

        def stop_remaining(first):
            for remaining in range(first, len(video_pages)):
                self.events.put(("item", (remaining, "취소")))
            self.events.put(("cancel", f"대기열 중지: 성공 {succeeded}, 실패 {failed}, "
                             f"미처리 {len(video_pages) - succeeded - failed} / 전체 {len(video_pages)}"))

        for index, video_page in enumerate(video_pages):
            if self.cancel.is_set():
                stop_remaining(index)
                return
            self.events.put(("current", (index + 1, len(video_pages))))
            self.events.put(("item", (index, "다운로드 중")))
            self.events.put(("log", f"\n[{index + 1}/{len(video_pages)}] 시작: {safe_error(video_page)}"))
            try:
                result = run_download(video_page, output_dir, headers, self.cancel,
                                      lambda text: self.events.put(("log", text)),
                                      lambda value, total: self.events.put(("progress", (value, total))),
                                      self.wait_for_user, mode=mode, choose=self.choose_option,
                                      direct_hls=video_page in known_playlists)
            except Cancelled:
                self.events.put(("item", (index, "중지")))
                stop_remaining(index + 1)
                return
            except Exception as error:
                failed += 1
                self.events.put(("item", (index, "실패")))
                self.events.put(("log", f"[{index + 1}/{len(video_pages)}] 실패: "
                                 f"{type(error).__name__}: {safe_error(error)}"))
            else:
                succeeded += 1
                self.events.put(("item", (index, "완료")))
                self.events.put(("log", f"[{index + 1}/{len(video_pages)}] 완료: {result}"))
        self.events.put(("done", f"대기열 완료: 성공 {succeeded}, 실패 {failed} / 전체 {len(video_pages)}"))

    def stop(self):
        self.cancel.set()
        self.resume.set()
        self.selection_ready.set()
        self.confirm_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.resume_button.configure(state="disabled")
        self.status.set("중지 처리 중... 진행 중인 요청 종료까지 최대 60초가 걸릴 수 있습니다.")

    def close(self):
        self.remember_settings()
        if self.worker and self.worker.is_alive():
            self.closing = True
            self.stop()
        else:
            self.root.destroy()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "current":
                    self.active_item = value
                    self.progress.configure(value=0, maximum=1)
                    self.resume_button.configure(state="disabled")
                    self.status.set(f"[{value[0]}/{value[1]}] 재생한 Chrome 탭에 연결 중...")
                elif kind == "choose" and not self.cancel.is_set():
                    title, self.selection_options = value
                    self.selector_frame.configure(text=title)
                    for row in self.option_view.get_children():
                        self.option_view.delete(row)
                    for index, option in enumerate(self.selection_options):
                        self.option_view.insert("", "end", iid=str(index), values=(option["label"], safe_error(option["url"])))
                    self.confirm_button.configure(state="disabled")
                    self.selector_frame.grid()
                    self.status.set(title + " — 목록에서 선택 후 '선택 완료'를 누르세요.")
                elif kind == "item":
                    self.queue_view.set(str(value[0]), "state", value[1])
                    self.queue_view.see(str(value[0]))
                elif kind == "log":
                    self.append_log(value)
                    if value.startswith("[*]") and not self.cancel.is_set():
                        prefix = f"[{self.active_item[0]}/{self.active_item[1]}] " if self.active_item else ""
                        self.status.set(prefix + value[4:])
                elif kind == "progress":
                    self.progress.configure(value=value[0], maximum=value[1])
                    if not self.cancel.is_set():
                        prefix = f"[{self.active_item[0]}/{self.active_item[1]}] " if self.active_item else ""
                        self.status.set(f"{prefix}조각 다운로드: {value[0]} / {value[1]}")
                elif kind == "wait" and not self.cancel.is_set():
                    self.resume_button.configure(state="normal")
                    self.status.set("Chrome에서 영상을 다시 선택하거나 실제 영상을 재생한 뒤 '재생 후 계속'을 누르세요.")
                elif kind == "resume":
                    self.resume_button.configure(state="disabled")
                    if not self.cancel.is_set():
                        self.status.set("다운로드를 계속합니다.")
                elif kind == "headers":
                    self.set_running(False)
                    if not self.cancel.is_set():
                        self.origin.set(value["Origin"])
                        self.referer.set(value["Referer"])
                        self.remember_settings()
                        self.status.set("실제 플레이어 기준 Origin·Referer를 적용했습니다.")
                        self.append_log(f"[*] 플레이어 헤더 적용: Origin={safe_error(value['Origin'])}, "
                                        f"Referer={safe_error(value['Referer'])}")
                elif kind == "found":
                    self.set_running(False)
                    self.detected_playlists.add(value)
                    self.video_entry.delete("1.0", "end")
                    self.video_entry.insert("1.0", value)
                    self.status.set("선택한 재생목록을 입력창에 넣었습니다. 대기열 다운로드를 누르세요.")
                    self.append_log(f"재생목록 선택 완료: {safe_error(value)}")
                elif kind in ("done", "error", "cancel"):
                    self.set_running(False)
                    self.status.set(value if kind == "done" else ("중지됨" if kind == "cancel" else "실패 — 로그 확인"))
                    self.append_log(("완료: " if kind == "done" else "") + value)
        except queue.Empty:
            pass
        if self.closing and self.worker and not self.worker.is_alive():
            self.root.destroy()
            return
        self.root.after(100, self.poll)


def main():
    root = tk.Tk()
    DownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
