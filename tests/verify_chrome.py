# @file verify_chrome.py
# @author: rudals252
# @brief 작업 폴더의 로컬 영상 및 전용 Chrome만 사용하는 통합 검증.
import argparse
import json
import queue
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hls_downloader as h
from patchright.sync_api import sync_playwright


def main(work_dir):
    work_dir.mkdir(parents=True, exist_ok=True)
    fixture = work_dir / "fixture"
    fixture.mkdir(exist_ok=True)
    ffmpeg = h.find_ffmpeg()
    subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                    '-i', 'testsrc=size=160x90:rate=12', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                    '-t', '3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-g', '12', '-c:a', 'aac',
                    '-f', 'hls', '-hls_time', '1', '-hls_list_size', '0',
                    '-hls_segment_filename', str(fixture / 'seg%02d.png'), str(fixture / 'index.m3u8')], check=True)
    state = {"pages": 0, "fail_segment": False, "headers": [], "paths": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            state['paths'].append(path)
            content_type = 'application/vnd.apple.mpegurl'
            if path in ('/video', '/network', '/network-fresh', '/iframe', '/frame', '/hls-page', '/hls-mime',
                        '/embedded', '/embedded-player'):
                state['pages'] += 1
                content_type = 'text/html; charset=utf-8'
                if path == '/video':
                    body = '<button id="play" onclick="fetch(\'/config.js\').then(r=>r.text()).then(t=>{let s=document.createElement(\'script\');s.textContent=t;document.body.appendChild(s);})">Play</button>'
                elif path == '/network':
                    body = '<button id="play" onclick="fetch(\'/stream/playlist.html\')">Play</button>'
                elif path == '/network-fresh':
                    body = '<button id="play" onclick="fetch(\'/stream/video.html\')">Play</button>'
                elif path == '/iframe':
                    body = '<iframe src="/frame"></iframe>'
                elif path == '/hls-page':
                    body = '<button id="play" onclick="Promise.all([fetch(\'/generic/master.m3u8\'),fetch(\'/ads/ad.m3u8\')])">Play</button>'
                elif path == '/hls-mime':
                    body = '<button id="play" onclick="fetch(\'/generic/manifest\')">Play</button>'
                elif path == '/embedded':
                    body = '<iframe src="' + player_origin + '/embedded-player"></iframe>'
                elif path == '/embedded-player':
                    body = '<script>fetch("' + server_url + '/embedded/master.m3u8")</script>'
                else:
                    config = {'streamId': 'stream', 'cdnHosts': [server_url]}
                    body = '<script>var videoPlayerConfig = ' + json.dumps(config) + ';</script>'
                body = body.encode()
            elif path == '/config.js':
                content_type = 'application/javascript'
                # 단일 인용부호 JavaScript 객체와 조합된 변수명: HTML JSON 추출로는 찾을 수 없다.
                body = ("window['video'+'PlayerConfig']={streamId:'stream',cdnHosts:['" + server_url + "']};").encode()
            elif path == '/stream/playlist.html':
                body = b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nvideo.html?test=1\n'
            elif path == '/generic/master.m3u8':
                body = (b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=640x360\nlow/index.m3u8\n'
                        b'#EXT-X-STREAM-INF:BANDWIDTH=1500000,RESOLUTION=1280x720\nhigh/index.m3u8?token=fixture\n')
            elif path == '/embedded/master.m3u8':
                if self.headers.get('Origin') != player_origin or self.headers.get('Referer') != player_origin + '/':
                    self.send_response(403)
                    self.end_headers()
                    return
                body = (b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1500000\n'
                        b'/generic/high/index.m3u8?token=fixture\n')
            elif path in ('/generic/low/index.m3u8', '/generic/high/index.m3u8', '/generic/manifest', '/ads/ad.m3u8'):
                body = (fixture / 'index.m3u8').read_text()
                for number in range(3):
                    body = body.replace(f'seg{number:02d}.png', f'/assets/seg{number:02d}.png')
                body = body.encode()
            elif path == '/stream/video.html':
                body = (fixture / 'index.m3u8').read_text().replace('seg00.png', server_url + '/assets/seg00.png?token=fixture').replace('seg01.png', '/assets/seg01.png').replace('seg02.png', '../assets/seg02.png?token=fixture').encode()
            elif path.startswith('/assets/seg'):
                if state['fail_segment'] and path.endswith('seg01.png'):
                    self.send_response(503)
                    self.end_headers()
                    return
                content_type = 'image/png'  # 확장자와 MIME도 영상 데이터를 결정하지 않는다.
                body = (fixture / Path(path).name).read_bytes()
            else:
                self.send_response(404)
                self.end_headers()
                return
            if path.startswith(('/stream/', '/variants/', '/assets/')):
                state['headers'].append((self.headers.get('Origin'), self.headers.get('Referer')))
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', player_origin)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_url = f'http://127.0.0.1:{server.server_port}'
    player_origin = f'http://localhost:{server.server_port}'
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # 기존 사용자 Chrome/9222와 별개의 포트 및 프로필만 사용한다.
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        h.CDP_PORT = probe.getsockname()[1]
    h.CDP_URL = f'http://127.0.0.1:{h.CDP_PORT}'
    h.CHROME_PROFILE = work_dir / 'chrome-profile'
    output = work_dir / 'output'
    headers = h.make_headers(server_url, server_url + '/')
    log = lambda text: print(text, flush=True)
    no_challenge = lambda: (_ for _ in ()).throw(AssertionError('Unexpected manual wait'))
    browser_launched = False

    def prepare(path, click=False):
        # 테스트에서만 사용자 탐색/재생을 모사한다. 다운로드 함수는 이 동작을 하지 않는다.
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(h.CDP_URL)
            page = browser.contexts[0].pages[0]
            page.goto(server_url + path)
            if click:
                page.click('#play')
                for _ in range(50):
                    if page.evaluate("!!window['video'+'PlayerConfig']", isolated_context=False):
                        break
                    page.wait_for_timeout(50)
                else:
                    raise AssertionError('Local playback did not create the runtime config')

    def user_replays(path='/network', resource='/stream/playlist.html'):
        errors = []
        def click():
            try:
                with sync_playwright() as p:
                    browser = p.chromium.connect_over_cdp(h.CDP_URL)
                    page = h.find_video_page(browser, server_url + path)
                    with page.expect_response(lambda r: r.url.endswith(resource)):
                        page.click('#play')
                    page.wait_for_timeout(100)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=click)
        thread.start()
        thread.join(timeout=15)
        assert not thread.is_alive(), 'Replaying test video timed out'
        if errors:
            raise errors[0]

    def download(path, wait=no_challenge, progress=lambda value, total: None, mode='site', choose=None, direct_hls=False):
        pages_before = state['pages']
        files_before = set(output.iterdir()) if output.exists() else set()
        expected = h.final_output_path(output, server_url + path)
        result = h.run_download(server_url + path, output, headers, threading.Event(), log, progress, wait,
                                mode=mode, choose=choose, direct_hls=direct_hls)
        assert state['pages'] == pages_before, 'Downloader navigated/reloaded a page'
        assert h.read_cdp(), 'Downloader closed user browser'
        assert result == expected
        assert result.parent == output, 'Result is wrapped in an extra folder'
        assert len(result.stem) <= 10
        assert set(output.iterdir()) == files_before | {result}, 'Intermediate files remain after success'
        return result

    try:
        h.launch_chrome(log)
        browser_launched = True
        deadline = time.monotonic() + 25
        while not h.read_cdp():
            assert time.monotonic() < deadline, 'Test Chrome did not open CDP'
            time.sleep(0.3)
        assert state['pages'] == 0, 'Chrome launch unexpectedly visited a website'
        prepare('/video', click=True)
        mp4 = download('/video')
        ffprobe = str(Path(ffmpeg).resolve().with_name('ffprobe.exe'))
        metadata = json.loads(subprocess.check_output([ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(mp4)]))
        assert {'video', 'audio'} <= {stream['codec_type'] for stream in metadata['streams']}
        assert 2.9 <= float(metadata['format']['duration']) <= 3.3
        assert state['headers'] and all(pair == (server_url, server_url + '/') for pair in state['headers'])
        print('PASS manual playback / runtime config / no navigation / PNG-named TS / MP4 with audio.', flush=True)
        prepare('/iframe')
        assert download('/iframe').exists()
        print('PASS iframe config.', flush=True)
        prepare('/network')
        assert download('/network', wait=user_replays).exists()
        print('PASS manual replay / real playlist.html response fallback.', flush=True)
        # 이 시점의 playlist 요청은 완료된 상태다. 설정 없이 연결 후 주소를 복원한다.
        assert download('/network').exists()
        print('PASS already completed playlist request is recovered without replay.', flush=True)
        assert download('/stream/playlist.html').exists()
        print('PASS direct playlist.html URL -> video.html -> TS -> MP4.', flush=True)
        assert download('/stream/video.html').exists()
        print('PASS direct video.html URL -> TS -> MP4.', flush=True)
        prepare('/network-fresh')
        assert download('/network-fresh', wait=lambda: user_replays('/network-fresh', '/stream/video.html')).exists()
        print('PASS only video.html is requested during playback.', flush=True)
        # 실제 다운로드 함수를 대기열로 실행: 중간 404 이후에도 다음 링크를 처리한다.
        app = h.DownloaderApp.__new__(h.DownloaderApp)
        app.events = queue.Queue()
        app.cancel = threading.Event()
        app.wait_for_user = no_challenge
        files_before = set(output.glob('*.mp4'))
        pages_before = state['pages']
        app.work([server_url + '/stream/playlist.html', server_url + '/missing/playlist.html',
                  server_url + '/stream/video.html'], output, headers)
        events = list(app.events.queue)
        assert ('item', (0, '완료')) in events and ('item', (1, '실패')) in events and ('item', (2, '완료')) in events, events
        assert events[-1][0] == 'done' and '성공 2, 실패 1' in events[-1][1]
        assert len(set(output.glob('*.mp4')) - files_before) == 2
        assert state['pages'] == pages_before and h.read_cdp()
        print('PASS real sequential queue / failure continues / direct output / Chrome remains.', flush=True)
        selections = []
        def select_hls(title, options):
            selections.append((title, options))
            if title.startswith('재생목록'):
                return next(option['url'] for option in options if '/generic/' in option['url'])
            return next(option['url'] for option in options if '/high/' in option['url'])
        prepare('/hls-page')
        user_replays('/hls-page', '/generic/master.m3u8')
        paths_before = len(state['paths'])
        assert download('/hls-page', mode='hls', choose=select_hls).exists()
        assert any('/ads/' in option['url'] for option in selections[0][1])
        assert '/generic/high/index.m3u8' in state['paths'][paths_before:]
        assert '/generic/low/index.m3u8' not in state['paths'][paths_before:]
        assert '/ads/ad.m3u8' not in state['paths'][paths_before:]
        print('PASS general HLS discovery / explicit ad-vs-content choice / selected quality only.', flush=True)
        assert download('/generic/master.m3u8', mode='hls', choose=select_hls).exists()
        assert download('/generic/low/index.m3u8', mode='hls').exists()
        print('PASS arbitrary master.m3u8 and index.m3u8 direct inputs.', flush=True)
        pages_before = state['pages']
        found = h.find_hls_playlist(server_url + '/hls-page', headers, threading.Event(), log, no_challenge, select_hls)
        assert found.endswith('/generic/high/index.m3u8?token=fixture')
        assert state['pages'] == pages_before
        prepare('/hls-mime')
        found = h.find_hls_playlist(server_url + '/hls-mime', headers, threading.Event(), log,
                                    lambda: user_replays('/hls-mime', '/generic/manifest'), select_hls)
        assert found == server_url + '/generic/manifest'
        assert download('/generic/manifest', mode='hls', direct_hls=True).exists()
        print('PASS find-only quality selection / new HLS MIME response / extensionless selected playlist.', flush=True)

        prepare('/embedded')
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(h.CDP_URL)
            page = h.find_video_page(browser, server_url + '/embedded')
            page.wait_for_timeout(300)
            response = page.request.get(server_url + '/embedded/master.m3u8', headers=headers)
            assert response.status == 403, 'Fixture must reject outer-page headers'
            response.dispose()
        paths_before = len(state['paths'])
        player_headers = h.find_hls_headers(server_url + '/embedded', threading.Event(), None)
        assert player_headers == {'Origin': player_origin, 'Referer': player_origin + '/'}
        direct_headers = h.find_hls_headers(server_url + '/embedded/master.m3u8', threading.Event(), None)
        assert direct_headers == player_headers
        assert len(state['paths']) == paths_before, 'Header detection must not send new HTTP requests'
        outer_headers = headers
        headers = h.make_headers(**dict(origin=player_headers['Origin'], referer=player_headers['Referer']))
        assert download('/embedded/master.m3u8', mode='hls').exists()
        headers = outer_headers
        print('PASS iframe header mismatch reproduced as 403 / passive header detection / direct MP4 with iframe headers.', flush=True)

        # 외부 사용자 창 조작 없이 Tk 위젯·콜백과 실제 localhost 다운로드를 함께 검증한다.
        root = h.tk.Tk()
        root.withdraw()
        try:
            (work_dir / 'gui-settings.json').write_text(json.dumps({
                'Origin': '', 'Referer': '', 'OutputDir': str(output)}), encoding='utf-8')
            with patch.object(h, 'SETTINGS_PATH', work_dir / 'gui-settings.json'):
                gui = h.DownloaderApp(root)
                assert gui.origin.get() == '' and gui.referer.get() == ''
                assert gui.output.get() == str(output), 'Empty headers lost the saved output folder'
                gui.mode.set('hls')
                gui.update_mode()
                gui.output.set(str(output))
                gui.origin.set(server_url)
                gui.referer.set(server_url + '/')
                gui.video_entry.insert('1.0', server_url + '/generic/master.m3u8\n' + server_url + '/missing/index.m3u8\n'
                                       + server_url + '/generic/low/index.m3u8')
                gui.start()
                deadline = time.monotonic() + 60
                while gui.worker.is_alive() or not gui.events.empty():
                    assert time.monotonic() < deadline, 'GUI queue or selection deadlocked'
                    root.update()
                    if gui.selector_frame.grid_info() and gui.selection_options:
                        choice = next(i for i, option in enumerate(gui.selection_options) if '/high/' in option['url'])
                        gui.option_view.selection_set(str(choice))
                        root.update()
                        gui.confirm_selection()
                    time.sleep(0.05)
                assert [gui.queue_view.set(str(i), 'state') for i in range(3)] == ['완료', '실패', '완료']
                assert '성공 2, 실패 1' in gui.status.get()
                assert str(gui.find_button['state']) == 'normal'
                assert not gui.selector_frame.grid_info()
                prepare('/hls-page')
                user_replays('/hls-page', '/generic/master.m3u8')
                gui.video_entry.delete('1.0', 'end')
                gui.video_entry.insert('1.0', server_url + '/hls-page')
                gui.use_page_headers()
                deadline = time.monotonic() + 15
                while gui.worker.is_alive() or not gui.events.empty():
                    assert time.monotonic() < deadline, 'GUI header detection deadlocked'
                    root.update()
                    time.sleep(0.05)
                assert gui.origin.get() == server_url and gui.referer.get() == server_url + '/hls-page'
                assert json.loads((work_dir / 'gui-settings.json').read_text())['Referer'] == server_url + '/hls-page'
                assets_before = sum(path.startswith('/assets/') for path in state['paths'])
                gui.find_playlists()
                deadline = time.monotonic() + 30
                while gui.worker.is_alive() or not gui.events.empty():
                    assert time.monotonic() < deadline, 'GUI finder deadlocked'
                    root.update()
                    if gui.selector_frame.grid_info() and gui.selection_options:
                        wanted = '/master.m3u8' if gui.selector_frame['text'].startswith('재생목록') else '/high/'
                        choice = next(i for i, option in enumerate(gui.selection_options) if wanted in option['url'])
                        gui.option_view.selection_set(str(choice))
                        root.update()
                        gui.confirm_selection()
                    time.sleep(0.05)
                found_url = gui.video_entry.get('1.0', 'end-1c')
                assert found_url == server_url + '/generic/high/index.m3u8?token=fixture'
                assert found_url in gui.detected_playlists
                assert sum(path.startswith('/assets/') for path in state['paths']) == assets_before
                # 후보 선택을 기다리는 worker를 중지하면 대기 이벤트도 해제되어야 한다.
                gui.cancel.clear()
                cancelled = []
                def wait_for_choice():
                    try:
                        gui.choose_option('화질 선택', [{'url': found_url, 'label': 'test'}])
                    except h.Cancelled:
                        cancelled.append(True)
                gui.set_running(True)
                gui.worker = threading.Thread(target=wait_for_choice)
                gui.worker.start()
                deadline = time.monotonic() + 5
                while gui.events.empty():
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                root.update()
                gui.stop()
                gui.worker.join(timeout=2)
                assert not gui.worker.is_alive() and cancelled == [True]
                gui.set_running(False)
                gui.mode.set('site')
                gui.update_mode()
                assert str(gui.find_button['state']) == 'disabled'
                print('PASS Tk mode buttons / finder without segments / page headers saved / quality selection / queue / stop waiting.', flush=True)
        finally:
            root.destroy()
        prepare('/video', click=True)
        state['fail_segment'] = True
        before = set(output.glob('*.mp4'))
        jobs_before = set(output.iterdir())
        try:
            download('/video')
        except RuntimeError as error:
            assert '503' in str(error)
        else:
            raise AssertionError('Failed segment was accepted')
        assert set(output.glob('*.mp4')) == before
        failed_job, = set(output.iterdir()) - jobs_before
        assert list((failed_job / 'segments').glob('*.ts')), 'Failed job lost its segments'
        print('PASS missing segment fails without an incomplete MP4.', flush=True)
        state['fail_segment'] = False
        cancel = threading.Event()
        jobs_before = set(output.iterdir())
        try:
            h.run_download(server_url + '/video', output, headers, cancel, log,
                           lambda value, total: cancel.set(), no_challenge)
        except h.Cancelled:
            pass
        else:
            raise AssertionError('Cancel was ignored')
        assert h.read_cdp()
        cancelled_job, = set(output.iterdir()) - jobs_before
        assert list((cancelled_job / 'segments').glob('*.ts')), 'Cancelled job lost its segments'
        print('PASS cancel disconnects without closing Chrome.', flush=True)
    finally:
        server.shutdown()
        server.server_close()
        if browser_launched and h.read_cdp():
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(h.CDP_URL)
                browser.new_browser_cdp_session().send('Browser.close')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', required=True, type=Path)
    main(parser.parse_args().work_dir.resolve())
