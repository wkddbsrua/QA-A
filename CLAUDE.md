# 화면주석-QA — Claude 작업 규칙

비개발자 QA/기획자가 아무 사이트에서나 화면을 클릭해 지적을 남기는 범용 도구.
UI·기능은 agentation 원본, 화면 문구만 한글. **CMS 프로젝트와 무관한 독립 도구다.**

## 착수 전 필독
- `README.md` — 특히 **"iframe(셸 구조)에서 지킬 것" 7개 항목**. 사고가 거기서 다 터졌다.
- 사용자용 문서는 `docs/사용법.src.html` → `python docs/make_help.py` 로 생성(생성물 직접 수정 금지).

## 되돌아가지 말 것 (이미 실패한 경로)
주입은 **CDP** 로만 한다 — `Page.addScriptToEvaluateOnNewDocument` + `Runtime.addBinding`.
북마크릿 · 브라우저 확장 · 유저스크립트 · 로컬 싱크서버로 되돌아가지 않는다(이유는 README).

## 범위 규칙
- **VRT(해상도 순회·캡처 비교)를 넣지 않는다.** 해상도는 기록·분리만 한다.
  화면 하나의 정체성은 `(주소, 해상도)` — URL 로만 묶으면 1920/1366 결과가 합쳐져 증거가 사라진다.
- 새 기능은 붙이기 전에 "이게 이 도구의 일인가" 를 먼저 판단한다.
  증상마다 버튼을 늘리는 것이 사용자가 가장 싫어한 결과다.

## 모달 규칙
**z-index 를 올려서 모달 위로 가려 하지 않는다.** 페이지가 `2147483647` 을 쓰면 진다.
top layer(`popover`) 로 올라가고, native 모달이 열리면 그 `<dialog>` 안으로 옮긴다(README 참조).
`popover` 승격이 실패하면 **속성을 반드시 뗀다** — 붙은 채 실패하면 툴바가 사라진다.
페이지의 `showModal` 을 `show` 로 바꿔치기하지 않는다(백드롭·포커스 트랩이 사라져 화면이 실제와 달라진다).

## 스크린샷 금지
**도구가 주석마다 스크린샷을 찍지 않는다.** 스크린샷 첨부를 없애려고 만든 도구라 넣으면 회귀다.
"그림을 보내 달라" 가 나오면 먼저 **텍스트를 고친다** — 색 기준·묶음 기하(정렬·간격)·배치 속성·
기대·우선순위·항목 목차가 그 답이다(README 참조).
★사람이 메모창에 직접 붙인 그림(브8, `Ctrl+V`/드래그)은 예외다 — **도구가 찍는 것과 사람이
붙이는 것을 구분한다.** 자동 캡처 버튼은 추가하지 않는다.

## 클릭·Esc 정책 (실사용자 요구로 정해진 것)
요소 선택은 **더블클릭**, 모드 토글은 **Esc**, 주석 모드에서 페이지는 클릭을 **전혀** 못 받는다.
구현은 `src/inject.jsx` 의 캡처단계 리스너 하나에 모여 있다 —
`stopImmediatePropagation`(첫 클릭, agentation 까지 차단)과 `stopPropagation`(두 번째, 페이지만 차단)을
나눠 쓰는 것이 핵심이다. **우리 리스너가 agentation 보다 먼저 등록된다는 전제**가 깨지면 둘 다 깨진다.
모달이 열려 있으면 Esc 로 모드를 켜지 않는다(그 Esc 는 모달을 닫는 것이다).

## 툴바 위치·와이어프레임
`iframe` 규칙 1(툴바는 탭마다 하나)을 **이중 마운트로 되돌리지 않는다.**
"좌측 메뉴 포함" 과 "iframe 내부 선택" 은 동시에 성립하지 않으므로 **사람이 고르게** 한다
(프로그램 창 체크박스 → `window.__qaForceTop`). 전환은 `__qaRemount`(저장소 무해)로 한다 —
`__qaClear` 는 주석까지 지운다.
동작 기본값을 바꿔야 하면 `ko.strings.json` 의 **`patches`** 절에 사유와 함께 넣는다(번역 rules 와 섞지 않는다).

## 진단 규칙
"안 되는데?" 를 들으면 추측하지 말고 **그 브라우저에 CDP 로 붙어 DOM 을 본다** —
`document.__qaDoc` · `window.__qaInjected` · `window.__qaReady` · `<style>` 개수 ·
툴바 computed `position`. 스냅샷 한 장으로는 계속 헛짚었고, **시간에 따라 추적**해야 갈린다.

## 한글화
`ko.strings.json` 한 파일 + `python build_ko.py` 게이트(못찾음·개수불일치·잔존영문 전부 0).
**allow 목록에 넣어 조용히 면제하지 말 것** — 그렇게 `Webhooks` 를 놓쳤다.

## 다른 PC 전제
배포 PC 에는 파이썬·node·esbuild 가 없다. 해상도·DPI·Windows 언어·설치 브라우저도 다르다.
이 PC 기준으로 짜지 말 것(README "기기마다 다른 것" 표).

## 빌드
```
python build_ko.py      # 한글 문구 주입 → vendor/agentation.ko.mjs
python build_inject.py  # esbuild 번들 → dist/inject.js
python build_exe.py     # PyInstaller → release/화면주석-QA.exe (산출물 존재 검사 포함)
```
로컬 확인 도구: python 3.14 · node 25 · esbuild 0.28.2 · PyInstaller 6.20.

## 테스트 격리
★**테스트 주석을 `%LOCALAPPDATA%\qa-annotator\out` 에 남기지 말 것** — 사용자 결과에 섞인 사고가 있다.
스크래치패드 전용 폴더를 쓴다.

## 커밋
`git commit -m "…" -- <경로…>` 로 경로 지정. `--amend`/`--no-verify` 금지(전역 규칙과 동일).
