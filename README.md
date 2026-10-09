# HLS 다운로더

사용자가 Chrome에서 준비한 영상 세션에 CDP로 연결해 HLS 재생목록과 TS 조각을 받고, FFmpeg로 MP4를 만듭니다.

## 실행

Windows, 설치된 Google Chrome, FFmpeg가 필요합니다. `HLS_Downloader_v11.exe`로 실행하면 Python 설치는 필요하지 않습니다.

1. **Chrome 열기**를 누르거나 이미 CDP 9222로 실행한 Chrome을 사용합니다.
2. Chrome에서 직접 영상 페이지를 열고 필요한 확인 절차와 광고를 처리한 뒤 실제 영상을 재생합니다.
3. 영상 페이지 URL 또는 재생목록 URL을 한 줄에 하나씩 입력합니다.
4. **기존 사이트 방식** 또는 **일반 HLS · m3u8**를 선택합니다.
5. Origin·Referer를 입력합니다. 일반 HLS에서는 **페이지 헤더 적용**으로 재생목록을 요청한 본문·iframe 기준 값을 찾을 수 있습니다.
6. 저장 폴더를 정하고 **대기열 다운로드**를 누릅니다.

Origin·Referer 기본값은 비어 있습니다. 유효한 값을 입력하거나 적용한 뒤 다운로드할 수 있습니다.
사이트 주소가 기본값으로 포함되지 않으며 사용자 설정 파일은 배포 ZIP과 Git에서 제외합니다.
Origin은 `https://player.example`, 루트 Referer는 `https://player.example/` 형식입니다.
Referer의 상세 경로는 입력한 대로 유지합니다. 문서와 테스트의 `.example` 주소는 가상 예제입니다.

```text
영상 페이지: https://video.example/watch/sample
재생목록: https://cdn.example/sample/master.m3u8
Origin: https://player.example
Referer: https://player.example/
저장 폴더: D:\Videos
결과 파일: D:\Videos\sample.mp4
```

위 주소들은 사용 방법을 설명하는 가상 값입니다. 실제 입력할 때는 영상 페이지와 플레이어의 요청 기록에 맞춥니다.

## 다운로드 방식

- 기존 사이트 방식: 본문·iframe의 `videoPlayerConfig` 설정 또는 `playlist.html` / `video.html` 요청 기록을 사용합니다.
- 일반 HLS: `.m3u8` URL, 완료된 요청 주소 기록, 연결 후 수신한 HLS MIME 응답을 확인합니다. 광고·본영상의 재생목록 후보는 사용자가 선택하며, 같은 영상의 화질은 자동으로 선택합니다.
- 최고 화질 자동 선택: 마스터 재생목록의 해상도(가로×세로)를 우선하고, 같으면 `BANDWIDTH`가 높은 항목을 사용합니다. 해상도가 없으면 비트레이트를 비교하며, `BANDWIDTH`가 없거나 유효하지 않으면 `AVERAGE-BANDWIDTH`를 사용합니다. 화질 정보가 모두 없으면 첫 항목을 사용하고 로그로 알립니다. 직접 입력한 미디어 재생목록에는 다른 화질 목록이 없으므로 해당 화질로 받습니다.
- 페이지 헤더 적용: 실제 m3u8 요청 기록을 가진 프레임을 찾습니다. 여러 플레이어가 있으면 선택하며, 기록이 없으면 로그로 안내합니다.
- 별도 프로세스 iframe은 CDP 부모 대상 식별자를 확인해 해당 탭에 속한 프레임만 읽습니다. 헤더 찾기 자체는 새로운 HTTP 요청을 보내지 않습니다.
- 교차 출처 Referer는 Chrome 기본 정책에 맞춰 플레이어 출처와 `/`를 사용합니다. 같은 출처는 상세 경로를 유지합니다. 별도 정책이나 추가 인증 조건은 별도 대응이 필요합니다.
- 상세 URL은 Chrome에 같은 URL의 탭이 열려 있어야 합니다. 직접 재생목록 URL은 Origin에 해당하는 본문·iframe이 열린 브라우저 컨텍스트를 사용합니다.
- 프로그램은 영상 페이지를 이동·새로고침하거나 재생·확인 버튼을 클릭하지 않습니다. 수동 재생이 필요하면 **재생 후 계속**으로 이어갑니다.
- 다운로드 요청은 브라우저 컨텍스트의 쿠키를 공유하는 `page.request`를 사용합니다. Chrome의 네트워크 엔진과 동일한 요청 방식은 아닙니다.
- URL 쿼리는 실제 요청에 유지하고 오류 로그에서는 숨깁니다. 재생목록 확장자로 조각의 형식을 결정하지 않습니다.

종료된 비암호화 MPEG-TS HLS를 지원합니다. 암호화, 별도 오디오, fMP4, 바이트 범위 및 라이브 스트림은 지원하지 않습니다.
모든 사이트의 다운로드 성공을 보장하지 않습니다.

## 대기열·저장

링크를 입력 순서대로 하나씩 처리합니다. 개별 실패는 로그에 남기고 다음 링크로 넘어갑니다.
**중지**는 현재 작업과 남은 대기열을 취소합니다. 진행 중인 요청 종료까지 최대 60초가 걸릴 수 있습니다.
한 대기열은 방식·헤더·저장 폴더를 공유합니다. 서로 다른 설정이 필요한 링크는 대기열을 나눕니다.

선택한 폴더에 MP4를 직접 저장합니다. 파일 이름은 입력 URL을 기준으로 확장자를 제외한 최대 10자이며,
중복 이름에는 번호를 붙여 기존 파일을 보존합니다. 성공 후 임시 조각과 TS를 제거하여 MP4만 남깁니다.
실패·중지한 작업의 중간 파일은 보존합니다. **폴더 열기**로 저장 위치를 탐색기에서 엽니다.

Origin·Referer·저장 폴더는 실행 파일 옆 `headers.json`에 저장하고 다음 실행에서 복원합니다.
헤더가 비어 있어도 저장 폴더를 기억합니다. 완료·오류는 로그와 상태 표시로만 알립니다.

## Chrome 프로필

Chrome 열기는 설치된 Chrome을 `--remote-debugging-port=9222`와 별도 `%USERPROFILE%\chrome-debug` 데이터 디렉터리로 실행합니다.
이 프로필을 재사용해 로그인·설정을 유지합니다. Chrome은 프로그램의 작업 중지로 종료하지 않습니다.
기본 Chrome 데이터 디렉터리에 직접 CDP를 붙이는 기능은 제공하지 않습니다.
[Chrome 공식 설명](https://developer.chrome.com/blog/remote-debugging-port)

## 개발·검증

Python 3.10 이상, Tkinter, FFmpeg가 필요하며 Chrome 통합 검증에는 FFprobe도 필요합니다.

```powershell
python -m pip install -r requirements.txt
python hls_downloader.py
python -m unittest discover -s tests
python tests/verify_chrome.py --work-dir C:\Temp\hls-fixture
```

통합 검증은 localhost 테스트 서버와 사용자 Chrome과 분리된 전용 포트·프로필을 사용합니다.
영상·음성 MP4 생성, iframe 헤더, 순차 처리, 실패·중지, 설정 저장을 확인하며 외부 영상 사이트에는 접속하지 않습니다.

```powershell
python -m pip install pyinstaller
powershell -NoProfile -File build.ps1 -BuildDirectory C:\Temp\hls-build
```

Git에 사용자 설정, 브라우저 프로필, 쿠키, 로그, 다운로드 파일 및 빌드 산출물을 포함하지 않습니다.
