# @file test_downloader.py
# @author: rudals252
# @brief 수동 재생한 탭 선택, 설정 추출, 수동 대기 및 중지 회귀 검증.
import json
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hls_downloader as h


CONFIG = {"streamId": "stream", "cdnHosts": ["https://cdn.example.com"]}


class DownloaderTests(unittest.TestCase):
    def test_blank_defaults_require_headers_only_when_requesting(self):
        self.assertEqual(h.HEADERS_TEMPLATE["Origin"], "")
        self.assertEqual(h.HEADERS_TEMPLATE["Referer"], "")
        with self.assertRaisesRegex(ValueError, "Origin"):
            h.make_headers("", "")
        saved = h.make_headers("", "", allow_empty=True)
        self.assertEqual((saved["Origin"], saved["Referer"]), ("", ""))
        partial = h.make_headers("", "https://player.example", allow_empty=True)
        self.assertEqual((partial["Origin"], partial["Referer"]), ("", "https://player.example/"))

    def test_empty_headers_still_save_last_output_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "headers.json"
            app = self.queue_app()
            app.origin = Mock(get=lambda: "")
            app.referer = Mock(get=lambda: "")
            app.output = Mock(get=lambda: temporary)
            app.append_log = Mock()
            with patch.object(h, "SETTINGS_PATH", settings_path):
                app.remember_settings()
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(saved, {"Origin": "", "Referer": "", "OutputDir": str(Path(temporary).resolve())})
            app.append_log.assert_not_called()

    def queue_app(self):
        app = h.DownloaderApp.__new__(h.DownloaderApp)
        app.events = queue.Queue()
        app.cancel = threading.Event()
        app.wait_for_user = Mock()
        return app

    def test_general_hls_recognizes_arbitrary_names_and_signed_query(self):
        for name in ("master.m3u8", "index.m3u8", "720p.M3U8"):
            self.assertTrue(h.is_hls_url("https://cdn.example/id/" + name + "?key=test"))
        self.assertFalse(h.is_hls_url("https://site.example/video/a?playlist=index.m3u8"))
        self.assertFalse(h.is_playlist_url("https://cdn.example/id/index.m3u8"))

    def test_general_capture_validates_body_and_accepts_hls_mime(self):
        capture = h.PlaylistCapture(general=True)
        for url, body, content_type in (
                ("https://cdn.example/index.m3u8", "<html>Denied</html>", "text/html"),
                ("https://cdn.example/manifest", "#EXTM3U\n", "application/vnd.apple.mpegurl; charset=utf-8"),
                ("https://cdn.example/720p.m3u8?key=test", "#EXTM3U\n", "text/plain")):
            capture.on_response(SimpleNamespace(status=200, url=url, text=lambda: body,
                                                headers={"content-type": content_type}))
        self.assertEqual(capture.urls, {"https://cdn.example/manifest", "https://cdn.example/720p.m3u8?key=test"})

    def test_general_discovery_requires_explicit_choice_and_detaches(self):
        page = Mock()
        urls = {"https://cdn.example/master.m3u8", "https://ads.example/ad.m3u8"}
        choose = Mock(return_value="https://cdn.example/master.m3u8")
        with patch.object(h, "resource_playlist_urls", return_value=urls):
            result = h.discover_hls(page, threading.Event(), Mock(), Mock(), choose)
        self.assertEqual(result, "https://cdn.example/master.m3u8")
        self.assertEqual({option["url"] for option in choose.call_args.args[1]}, urls)
        page.remove_listener.assert_called_once()
        page.goto.assert_not_called()

    def test_general_discovery_waits_for_manual_response(self):
        page = Mock()
        def replay():
            callback = page.on.call_args.args[1]
            callback(SimpleNamespace(status=200, url="https://cdn.example/master.m3u8", text=lambda: "#EXTM3U\n"))
        with patch.object(h, "resource_playlist_urls", return_value=set()):
            result = h.discover_hls(page, threading.Event(), Mock(), replay,
                                    lambda title, options: options[0]["url"])
        self.assertEqual(result, "https://cdn.example/master.m3u8")
        page.remove_listener.assert_called_once()

    def test_variants_keep_quality_labels_and_relative_signed_paths(self):
        master = ('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=640x360,CODECS="avc1,mp4a"\n'
                  'low/index.m3u8?key=test\n#EXT-X-STREAM-INF:BANDWIDTH=1500000,RESOLUTION=1280x720\n'
                  '/high.m3u8\n')
        variants = h.parse_variants(master, "https://cdn.example/id/master.m3u8")
        self.assertEqual(variants[0]["url"], "https://cdn.example/id/low/index.m3u8?key=test")
        self.assertEqual(variants[1]["label"], "1280x720 / 1500 kbps")
        self.assertEqual(h.parse_master(master, "https://cdn.example/id/master.m3u8"), variants[0]["url"])

    def test_general_quality_selection_requests_selected_variant_and_disposes(self):
        master = Mock(status=200, url="https://cdn.example/master.m3u8")
        master.text.return_value = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nlow.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=200000\nhigh.m3u8?key=test\n'
        media = Mock(status=200, url="https://cdn.example/high.m3u8?key=test")
        media.text.return_value = '#EXTM3U\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n'
        page = Mock()
        page.request.get.side_effect = [master, media]
        url, text = h.load_media_playlist(page, master.url, h.HEADERS_TEMPLATE, threading.Event(), "hls",
                                         lambda title, options: options[1]["url"])
        self.assertEqual(url, media.url)
        self.assertEqual(page.request.get.call_args.args[0], media.url)
        self.assertEqual(page.request.get.call_args.kwargs["headers"], h.HEADERS_TEMPLATE)
        master.dispose.assert_called_once()
        media.dispose.assert_called_once()

    def test_hls_choice_cannot_return_foreign_url_or_ignore_cancel(self):
        options = [{"url": "https://cdn.example/low.m3u8", "label": "low"},
                   {"url": "https://cdn.example/high.m3u8", "label": "high"}]
        with self.assertRaisesRegex(RuntimeError, "목록에 없는"):
            h.choose_hls_option(options, "화질 선택", threading.Event(), lambda *args: "https://other.example/file")
        cancel = threading.Event()
        def choose(*args):
            cancel.set()
            return options[0]["url"]
        with self.assertRaises(h.Cancelled):
            h.choose_hls_option(options, "화질 선택", cancel, choose)

    def test_unsupported_hls_and_html_response_are_rejected(self):
        for tag in ('#EXT-X-KEY:METHOD=AES-128,URI="key"', '#EXT-X-MAP:URI="init.mp4"', '#EXT-X-BYTERANGE:10@0'):
            with self.subTest(tag=tag), self.assertRaises(RuntimeError):
                h.parse_media('#EXTM3U\n' + tag + '\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n')
        response = Mock(status=200, url="https://cdn.example/index.m3u8")
        response.text.return_value = "<html>Denied</html>"
        page = Mock()
        page.request.get.return_value = response
        with self.assertRaisesRegex(RuntimeError, "HLS 재생목록이 아닙니다"):
            h.request_playlist(page, response.url, h.HEADERS_TEMPLATE, threading.Event())
        response.dispose.assert_called_once()

    def test_generic_master_filename_uses_parent_id(self):
        self.assertEqual(h.output_name("https://cdn.example/abcdefghijk/master.m3u8?key=test"), "abcdefghij.mp4")
        self.assertEqual(h.output_name("https://cdn.example/id/index.m3u8"), "id.mp4")

    def test_external_audio_attribute_order_and_session_keys_are_rejected(self):
        for tag in ('#EXT-X-MEDIA:URI="audio.m3u8",GROUP-ID="audio",TYPE=AUDIO',
                    '#EXT-X-SESSION-KEY:METHOD=AES-128,URI="key"'):
            response = Mock(status=200, url="https://cdn.example/master.m3u8")
            response.text.return_value = '#EXTM3U\n' + tag + '\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nvideo.m3u8\n'
            page = Mock()
            page.request.get.return_value = response
            with self.subTest(tag=tag), self.assertRaises(RuntimeError):
                h.load_media_playlist(page, response.url, h.HEADERS_TEMPLATE, threading.Event(), "hls", Mock())
            page.request.get.assert_called_once()
            response.dispose.assert_called_once()

    def test_queue_freezes_mode_and_known_playlist_per_run(self):
        app = self.queue_app()
        url = "https://cdn.example/manifest"
        with patch.object(h, "run_download", return_value=Path("result.mp4")) as download:
            app.work([url], Path("output"), h.HEADERS_TEMPLATE, "hls", {url})
        self.assertEqual(download.call_args.kwargs["mode"], "hls")
        self.assertTrue(download.call_args.kwargs["direct_hls"])

    def test_multiline_urls_validate_all_lines_before_starting(self):
        urls = ["https://site.example/video/a", "https://cdn.example/id/playlist.html?key=test"]
        self.assertEqual(h.parse_video_urls("  " + urls[0] + "\r\n\n" + urls[1] + "  \n"), urls)
        with self.assertRaisesRegex(ValueError, "한 줄에 하나"):
            h.parse_video_urls(" \n\n")
        with self.assertRaisesRegex(ValueError, "2번째 줄"):
            h.parse_video_urls(urls[0] + "\ninvalid-url")

    def test_queue_processes_in_input_order_and_finishes_once(self):
        app = self.queue_app()
        urls = ["https://site.example/video/a", "https://site.example/video/b"]
        order = []
        def download(url, *args, **kwargs):
            order.append(("start", url))
            order.append(("finish", url))
            return Path("result.mp4")
        with patch.object(h, "run_download", side_effect=download):
            app.work(urls, Path("output"), h.HEADERS_TEMPLATE)
        self.assertEqual(order, [("start", urls[0]), ("finish", urls[0]), ("start", urls[1]), ("finish", urls[1])])
        events = list(app.events.queue)
        self.assertEqual([value for kind, value in events if kind == "item"],
                         [(0, "다운로드 중"), (0, "완료"), (1, "다운로드 중"), (1, "완료")])
        self.assertEqual([kind for kind, value in events if kind in ("done", "error", "cancel")], ["done"])
        self.assertIn("성공 2, 실패 0", events[-1][1])

    def test_queue_continues_after_failure_and_redacts_tokens(self):
        app = self.queue_app()
        urls = ["https://site.example/video/a", "https://site.example/video/b"]
        with patch.object(h, "run_download", side_effect=[RuntimeError("HTTP 403 https://cdn.example/video.html?secret=hidden"), Path("b.mp4")]) as download:
            app.work(urls, Path("output"), h.HEADERS_TEMPLATE)
        self.assertEqual(download.call_count, 2)
        events = list(app.events.queue)
        self.assertIn(("item", (0, "실패")), events)
        self.assertIn(("item", (1, "완료")), events)
        self.assertIn("성공 1, 실패 1", events[-1][1])
        self.assertNotIn("hidden", str(events))

    def test_queue_cancel_between_jobs_leaves_remaining_unstarted(self):
        app = self.queue_app()
        urls = ["https://site.example/video/a", "https://site.example/video/b"]
        def download(*args, **kwargs):
            app.cancel.set()
            return Path("a.mp4")
        with patch.object(h, "run_download", side_effect=download) as download_mock:
            app.work(urls, Path("output"), h.HEADERS_TEMPLATE)
        download_mock.assert_called_once()
        self.assertIn(("item", (1, "취소")), list(app.events.queue))
        self.assertEqual(list(app.events.queue)[-1][0], "cancel")

    def test_queue_cancel_during_job_stops_current_and_remaining(self):
        app = self.queue_app()
        urls = ["https://site.example/video/a", "https://site.example/video/b"]
        with patch.object(h, "run_download", side_effect=h.Cancelled()) as download:
            app.work(urls, Path("output"), h.HEADERS_TEMPLATE)
        download.assert_called_once()
        events = list(app.events.queue)
        self.assertIn(("item", (0, "중지")), events)
        self.assertIn(("item", (1, "취소")), events)
        self.assertEqual(events[-1][0], "cancel")

    def test_root_header_slashes_and_referer_path(self):
        headers = h.make_headers("https://site.example/", "https://site.example")
        self.assertEqual(headers["Origin"], "https://site.example")
        self.assertEqual(headers["Referer"], "https://site.example/")
        headers = h.make_headers("https://site.example", "https://site.example/video/a?x=1")
        self.assertEqual(headers["Referer"], "https://site.example/video/a?x=1")

    def test_header_edits_persist_without_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "headers.json"
            app = h.DownloaderApp.__new__(h.DownloaderApp)
            app.output = SimpleNamespace(get=lambda: temporary)
            app.origin = Mock(get=lambda: "https://next.example/")
            app.referer = Mock(get=lambda: "https://next.example")
            app.saved_settings = {"Origin": "https://previous.example", "Referer": "https://previous.example/"}
            app.append_log = Mock()
            with patch.object(h, "SETTINGS_PATH", settings_path):
                app.remember_settings()
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["Origin"], "https://next.example")
            self.assertEqual(saved["Referer"], "https://next.example/")
            self.assertEqual(saved["OutputDir"], str(Path(temporary).resolve()))
            app.origin.set.assert_called_once_with(saved["Origin"])
            app.referer.set.assert_called_once_with(saved["Referer"])
            app.append_log.assert_not_called()

    def test_direct_output_collision_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            first = folder / "abcdefghij.mp4"
            first.write_bytes(b"existing")
            second = folder / "abcdefgh_1.mp4"
            second.write_bytes(b"also existing")
            candidate = h.final_output_path(folder, "https://site.example/video/abcdefghijklm")
            self.assertEqual(candidate, folder / "abcdefgh_2.mp4")
            self.assertLessEqual(len(candidate.stem), 10)
            self.assertEqual(first.read_bytes(), b"existing")
            self.assertEqual(second.read_bytes(), b"also existing")

    def test_completion_and_error_are_logged_without_dialogs(self):
        app = h.DownloaderApp.__new__(h.DownloaderApp)
        app.events = queue.Queue()
        app.events.put(("done", "video.mp4"))
        app.events.put(("error", "HTTP 503"))
        app.cancel = threading.Event()
        app.status = Mock()
        app.root = Mock()
        app.closing = False
        app.set_running = Mock()
        app.append_log = Mock()
        with patch("tkinter.messagebox.showinfo") as info, patch("tkinter.messagebox.showerror") as error:
            app.poll()
        info.assert_not_called()
        error.assert_not_called()
        self.assertEqual([call.args[0] for call in app.append_log.call_args_list], ["완료: video.mp4", "HTTP 503"])

    def test_output_names_follow_input_and_limit_stem_to_ten(self):
        cases = {
            "https://site.example/video/아주긴영상이름열글자보다길어요": "아주긴영상이름열글자.mp4",
            "https://site.example/video/%EC%98%81%EC%83%81?token=x#play": "영상.mp4",
            "https://cdn.example/abcdefghijklm/playlist.html": "abcdefghij.mp4",
            "https://cdn.example/abcdefghijklm/video.html?token=x": "abcdefghij.mp4",
            "https://site.example/video/movie.html": "movie.mp4",
            "https://site.example/": "video.mp4",
            "https://site.example/video/%2E%2E": "video.mp4",
            "https://site.example/video/CON": "_CON.mp4",
            "https://site.example/video/LPT1.txt": "_LPT1.txt.mp4",
            "https://site.example/video/a%2Fb%5Cc%3Ad%3F": "a_b_c_d_.mp4",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                result = h.output_name(url)
                self.assertEqual(result, expected)
                self.assertLessEqual(len(Path(result).stem), 10)

    def test_cleanup_preserves_final_mp4_and_other_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = root / "current"
            segments = job / "segments"
            segments.mkdir(parents=True)
            piece = segments / "000000.ts"
            piece.write_bytes(b"segment")
            ts = job / "output.ts"
            ts.write_bytes(b"merged")
            mp4 = job / "영상.mp4"
            mp4.write_bytes(b"final")
            other = root / "other.ts"
            other.write_bytes(b"unrelated")
            h.cleanup_intermediates(job, [piece], ts)
            self.assertEqual(list(job.iterdir()), [mp4])
            self.assertEqual(other.read_bytes(), b"unrelated")

    def test_cleanup_rejects_external_path_before_deleting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = root / "current"
            (job / "segments").mkdir(parents=True)
            piece = job / "segments" / "000000.ts"
            piece.write_bytes(b"segment")
            ts = job / "output.ts"
            ts.write_bytes(b"merged")
            other = root / "other.ts"
            other.write_bytes(b"unrelated")
            with self.assertRaisesRegex(RuntimeError, "벗어났습니다"):
                h.cleanup_intermediates(job, [piece, other], ts)
            self.assertTrue(piece.exists())
            self.assertTrue(ts.exists())
            self.assertTrue(other.exists())

    def test_html_assignment_forms_and_nested_json(self):
        config = {**CONFIG, "nested": {"value": True}}
        for prefix in ("var videoPlayerConfig = ", 'window["videoPlayerConfig"] = ', '"videoPlayerConfig": '):
            with self.subTest(prefix=prefix):
                self.assertEqual(h.extract_config(prefix + json.dumps(config) + ";"), config)

    def test_bad_first_assignment_does_not_hide_valid_config(self):
        html = "videoPlayerConfig = null; videoPlayerConfig = " + json.dumps(CONFIG)
        self.assertEqual(h.extract_config(html), CONFIG)

    def test_exact_tab_across_contexts_without_navigation(self):
        wrong = SimpleNamespace(url="https://site.example/video/other")
        right = SimpleNamespace(url="https://site.example/video/%EC%98%81%EC%83%81#player")
        browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[wrong]), SimpleNamespace(pages=[right])])
        self.assertIs(h.find_video_page(browser, "https://site.example/video/영상/"), right)
        with self.assertRaisesRegex(RuntimeError, "일치하는 열린 탭"):
            h.find_video_page(browser, "https://site.example/video/missing")

    def test_duplicate_tabs_are_not_selected_arbitrarily(self):
        page = SimpleNamespace(url="https://site.example/video/a")
        browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[page, page])])
        with self.assertRaisesRegex(RuntimeError, "여러 개"):
            h.find_video_page(browser, page.url)

    def test_iframe_config(self):
        frame = Mock()
        frame.content.return_value = "videoPlayerConfig = " + json.dumps(CONFIG)
        page = SimpleNamespace(frames=[frame])
        self.assertEqual(h.read_page_config(page, threading.Event(), Mock()), CONFIG)
        frame.evaluate.assert_not_called()

    def test_runtime_config_uses_page_world(self):
        frame = Mock()
        frame.content.return_value = "<html>No inline config</html>"
        frame.evaluate.return_value = CONFIG
        page = SimpleNamespace(frames=[frame])
        self.assertEqual(h.read_page_config(page, threading.Event(), Mock()), CONFIG)
        self.assertFalse(frame.evaluate.call_args.kwargs["isolated_context"])

    def test_network_capture_ignores_ads_and_html_errors(self):
        capture = h.PlaylistCapture()
        capture.on_response(SimpleNamespace(status=200, url="https://ads.example.com/ad.m3u8",
                                            text=lambda: "#EXTM3U\n"))
        capture.on_response(SimpleNamespace(status=200, url="https://cdn.example.com/a/playlist.html",
                                            text=lambda: "<html>Blocked</html>"))
        self.assertIsNone(capture.get_url())
        capture.on_response(SimpleNamespace(status=200, url="https://cdn.example.com/a/playlist.html?key=test",
                                            text=lambda: "#EXTM3U\n"))
        self.assertEqual(capture.get_url(), "https://cdn.example.com/a/playlist.html?key=test")

    def test_capture_ambiguity_fails(self):
        capture = h.PlaylistCapture()
        capture.urls.update(["https://cdn.example.com/1/playlist.html", "https://cdn.example.com/2/playlist.html"])
        with self.assertRaisesRegex(RuntimeError, "여러 개"):
            capture.get_url()

    def test_manual_wait_and_network_fallback(self):
        page = Mock()
        def wait():
            callback = page.on.call_args.args[1]
            callback(SimpleNamespace(status=200, url="https://cdn.example.com/s/playlist.html",
                                     text=lambda: "#EXTM3U\n"))
        with patch.object(h, "read_page_config", return_value=None) as read_config, \
                patch.object(h, "read_resource_playlist", return_value=None):
            self.assertEqual(h.resolve_playlist(page, threading.Event(), Mock(), wait),
                             "https://cdn.example.com/s/playlist.html")
            # 수동 대기 동안 추가 DOM 조회가 없다.
            read_config.assert_called_once()
        page.remove_listener.assert_called_once()
        page.goto.assert_not_called()
        page.reload.assert_not_called()
        page.click.assert_not_called()

    def test_cancel_during_manual_wait_detaches_listener(self):
        page = Mock()
        cancel = threading.Event()
        with patch.object(h, "read_page_config", return_value=None), \
                patch.object(h, "read_resource_playlist", return_value=None):
            with self.assertRaises(h.Cancelled):
                h.resolve_playlist(page, cancel, Mock(), cancel.set)
        page.remove_listener.assert_called_once()
        page.wait_for_timeout.assert_not_called()

    def test_download_does_not_launch_chrome(self):
        with patch.object(h, "read_cdp", return_value=None), patch.object(h, "find_ffmpeg", return_value="ffmpeg"), \
                patch.object(h.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "열린 Chrome이 없습니다"):
                h.run_download("https://site.example/video/a", Path("unused"), h.HEADERS_TEMPLATE,
                               threading.Event(), Mock(), Mock(), Mock())
            popen.assert_not_called()

    def test_only_two_headers_change(self):
        headers = h.make_headers("https://site.example", "https://site.example/")
        self.assertEqual([key for key in headers if headers[key] != h.HEADERS_TEMPLATE[key]], ["Origin", "Referer"])

    def test_master_and_video_are_one_pipeline(self):
        capture = h.PlaylistCapture()
        for url in ("https://cdn.example.com/s/playlist.html", "https://cdn.example.com/s/video.html"):
            capture.on_response(SimpleNamespace(status=200, url=url, text=lambda: "#EXTM3U\n"))
        self.assertEqual(capture.get_url(), "https://cdn.example.com/s/playlist.html")

    def test_video_response_alone_is_usable(self):
        capture = h.PlaylistCapture()
        capture.on_response(SimpleNamespace(status=200, url="https://cdn.example.com/s/video.html",
                                            text=lambda: "#EXTM3U\n"))
        self.assertEqual(capture.get_url(), "https://cdn.example.com/s/video.html")

    def test_completed_request_address_is_recovered(self):
        frame = Mock()
        frame.url = "https://site.example/video/a"
        frame.evaluate.return_value = ["https://cdn.example.com/s/playlist.html", "https://cdn.example.com/s/video.html",
                                       "https://ads.example.com/ad.m3u8"]
        page = SimpleNamespace(frames=[frame])
        self.assertEqual(h.read_resource_playlist(page, threading.Event(), Mock()),
                         "https://cdn.example.com/s/playlist.html")

    def test_remembered_request_does_not_require_replay(self):
        page = Mock()
        wait = Mock()
        with patch.object(h, "read_page_config", return_value=None), \
                patch.object(h, "read_resource_playlist", return_value="https://cdn.example.com/s/playlist.html"):
            self.assertEqual(h.resolve_playlist(page, threading.Event(), Mock(), wait),
                             "https://cdn.example.com/s/playlist.html")
        wait.assert_not_called()
        page.remove_listener.assert_called_once()

    def test_direct_url_uses_site_context_not_cdn_tab(self):
        site_page = SimpleNamespace(url="https://site.example/video/a")
        browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[site_page])])
        self.assertTrue(h.is_playlist_url("https://cdn-other.example/s/playlist.html"))
        self.assertTrue(h.is_playlist_url("https://cdn-other.example/s/video.html?token=test"))
        self.assertIs(h.find_request_page(browser, "https://site.example"), site_page)
        with self.assertRaisesRegex(RuntimeError, "Origin"):
            h.find_request_page(browser, "https://cdn-other.example")

    def test_direct_url_accepts_player_origin_inside_iframe(self):
        page = SimpleNamespace(url="https://site.example/video", frames=[SimpleNamespace(url="https://player.example/embed/id")])
        browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])
        self.assertIs(h.find_request_page(browser, "https://player.example"), page)
        browser.contexts.append(SimpleNamespace(pages=[page]))
        with self.assertRaisesRegex(RuntimeError, "Origin"):
            h.find_request_page(browser, "https://player.example")

    def test_header_source_uses_iframe_with_hls_history_not_outer_page(self):
        main = Mock(url="https://site.example/video")
        main.evaluate.return_value = ["https://ads.example/banner.js"]
        player = Mock(url="https://player.example/embed/id?private=secret")
        player.evaluate.return_value = ["https://cdn.example/master.m3u8"]
        options = h.hls_header_options([SimpleNamespace(frames=[main, player])], None, threading.Event())
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0]["Origin"], "https://player.example")
        self.assertEqual(options[0]["Referer"], "https://player.example/")
        main.goto.assert_not_called()
        player.goto.assert_not_called()

    def test_direct_header_source_matches_exact_signed_playlist(self):
        playlist = "https://cdn.example/manifest?key=selected"
        ad = Mock(url="https://ads.example/embed")
        ad.evaluate.return_value = ["https://cdn.example/manifest?key=advertisement"]
        player = Mock(url="https://player.example/embed")
        player.evaluate.return_value = [playlist]
        options = h.hls_header_options([SimpleNamespace(frames=[ad, player])], playlist, threading.Event())
        self.assertEqual([option["url"] for option in options], [player.url])

    def test_header_source_keeps_same_origin_path_and_does_not_guess_missing(self):
        frame = Mock(url="https://player.example/embed/id#fragment")
        frame.evaluate.return_value = ["https://player.example/master.m3u8"]
        pages = [SimpleNamespace(frames=[frame])]
        self.assertEqual(h.hls_header_options(pages, None, threading.Event())[0]["Referer"],
                         "https://player.example/embed/id")
        frame.evaluate.return_value = []
        with self.assertRaisesRegex(RuntimeError, "기록이 없습니다"):
            h.hls_header_options(pages, None, threading.Event())
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(h.Cancelled):
            h.hls_header_options(pages, None, cancel)

    def test_header_worker_only_posts_settings_and_reports_failures(self):
        app = self.queue_app()
        app.choose_option = Mock()
        headers = {"Origin": "https://player.example", "Referer": "https://player.example/"}
        with patch.object(h, "find_hls_headers", return_value=headers):
            app.headers_work("https://site.example/video")
        self.assertEqual(list(app.events.queue), [("headers", headers)])
        app.events = queue.Queue()
        with patch.object(h, "find_hls_headers", side_effect=RuntimeError("no history")):
            app.headers_work("https://site.example/video")
        self.assertEqual(app.events.get()[0], "error")


if __name__ == "__main__":
    unittest.main()
