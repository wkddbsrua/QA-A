/* 화면 주석 - 주입 스크립트 (도메인 무관)
 *
 * 이 파일은 어떤 사이트에도 올라가지 않는다. 프로그램(launcher.py)이 CDP 로
 * Page.addScriptToEvaluateOnNewDocument 에 등록해 두면, 그 브라우저에서 열리는
 * 모든 문서와 하위 프레임에 문서가 뜰 때마다 자동으로 실행된다.
 *   · 사이트에 파일을 심지 않는다 → 어느 도메인에서나 그대로 동작
 *   · 북마크·확장·유저스크립트가 없다 → 페이지마다 사람이 누를 것이 없다
 *   · 결과는 fetch 가 아니라 CDP 바인딩(window.__qaPush)으로 나간다
 *     → https 페이지에서도 mixed content 가 성립하지 않는다
 */
import React from 'react';
import { createRoot } from 'react-dom/client';
import { Agentation } from '../vendor/agentation.ko.mjs';

(function boot() {
    var HOST_ID = '__qa_annotator_host';
    var TOP_ID = '__qa_top_layer';      // 우리 UI 를 담아 top layer 로 올리는 투명한 틀

    /* ★이 인스턴스가 담당하는 문서. iframe 은 보통 빈 문서(about:blank)로 먼저
     *   만들어지고 곧바로 실제 주소로 바뀌는데, 그때 크롬은 window 를 재사용한다.
     *   그러면 옛 문서에서 돌던 타이머가 살아남아 새 문서에 '마크업만' 다시 그린다 -
     *   스타일은 옛 문서에 이미 넣었다고 여겨 다시 넣지 않으므로, 툴바가 CSS 없이
     *   position:static 으로 문서 맨 아래에 깔려 보이지 않는다(실측: ww2 /shell -
     *   요소 128개가 렌더됐는데 style 태그는 0개, 최상위 문서는 9개였다).
     *   그래서 문서가 바뀌면 옛 인스턴스는 즉시 손을 뗀다. 새 문서는 CDP 가 새로
     *   주입한 인스턴스(스타일을 가진 쪽)가 담당한다. */
    var myDoc = document;

    function isMine() {
        return document === myDoc;
    }

    var MIN_SIZE = 200;          // 광고·트래킹용 초소형 iframe 에는 띄우지 않는다
    var SHARE = 0.5;             // 부모 화면의 이 비율 이상을 차지하면 '지배 프레임'
    var FAST_MS = 6000;          // 처음 6초는 촘촘히 본다(iframe 을 나중에 만드는 셸)
    var FAST_TICK = 500;
    var SLOW_TICK = 2000;        // 그 뒤에도 느리게 계속 본다(셸이 구조를 바꾼다)

    /* ── 툴바는 탭마다 하나만 ────────────────────────────────────
     * ★셸 구조(관리자 화면이 <iframe> 안에 있는 형태)에서 부모와 자식이 각각 툴바를
     *   띄우면 둘이 화면상 같은 자리에 겹치고, 게다가 부모의 전체화면 오버레이가
     *   콘텐츠 영역 클릭을 가로채서 iframe 안 요소를 아예 고를 수 없다
     *   (실측: ww2 /shell - 툴바 2개가 x=877,y=1213 에 정확히 겹쳤다).
     *
     * 규칙: 화면을 지배하는 프레임이 툴바를 갖는다.
     *   · 부모: 화면 절반 이상을 차지하는 같은 출처 iframe 이 있으면 양보한다
     *   · 자식: 부모 화면의 절반 이상을 차지할 때만 갖는다
     *   같은 출처일 때 이 둘이 맞물려 정확히 하나만 뜬다. 교차출처는 서로를 볼 수
     *   없으므로 각자 띄운다(광고 iframe 때문에 호스트 툴바가 사라지는 쪽이 더 나쁘다).
     */
    function dominantFrameExists() {
        try {
            var area = window.innerWidth * window.innerHeight;
            if (!area) return false;
            var frames = document.querySelectorAll('iframe');
            for (var i = 0; i < frames.length; i++) {
                var f = frames[i];
                try {
                    if (!f.contentDocument) continue;       // 교차출처 → 판단하지 않는다
                } catch (e) {
                    continue;
                }
                var r = f.getBoundingClientRect();
                if (r.width * r.height >= area * SHARE) return true;
            }
        } catch (e) { /* 무시 */ }
        return false;
    }

    function parentArea() {
        // 같은 출처 부모의 화면 크기. 교차출처면 null(판단 불가 → 내가 띄운다).
        try {
            if (window.top === window.self) return null;
            var p = window.parent;
            void p.location.href;                           // 교차출처면 여기서 throw
            return p.innerWidth * p.innerHeight;
        } catch (e) {
            return null;
        }
    }

    /* 프로그램이 켜 주는 스위치(브4). 최상위 화면에 툴바를 고정한다 -
     * 좌측 메뉴·상단바에도 주석을 달 수 있다. 대신 iframe 내부 요소는 고를 수 없다
     * (문서가 다르면 히트테스트가 안 된다 - 이 둘은 동시에 성립하지 않는다). */
    function forceTop() {
        try { return !!window.__qaForceTop; } catch (e) { return false; }
    }

    function shouldMount() {
        if (!document.body) return false;
        if (forceTop()) return window.top === window.self;
        if (window.top === window.self) return !dominantFrameExists();
        if (window.innerWidth < MIN_SIZE || window.innerHeight < MIN_SIZE) return false;
        var pa = parentArea();
        if (pa && (window.innerWidth * window.innerHeight) < pa * SHARE) return false;
        return !dominantFrameExists();                      // 중첩 셸까지 재귀적으로 성립
    }

    /* ── 프로그램으로 보내기 ─────────────────────────────────── */
    function push(kind, output, annotations) {
        var payload = {
            kind: kind,
            // 화면 정보: 페이지를 옮길 때마다 이 값들이 주석과 함께 누적된다.
            // 해상도는 기기마다 다르고 화면 정체성의 일부다(같은 주소라도 따로 센다).
            url: location.href,
            title: document.title,
            viewport: window.innerWidth + 'x' + window.innerHeight,
            dpr: window.devicePixelRatio || 1,
            referrer: document.referrer || '',
            inIframe: window.top !== window.self,
            /* ★색 기준. 다크 화면에서 캡처한 색값을 라이트 기준으로 읽어 지적이 어긋난
             *   일이 있었다(SC-295 QA 회신). 강제할 수는 없으니 무엇을 보고 있었는지 남긴다. */
            bg: (function () {
                try { return getComputedStyle(document.body).backgroundColor || ''; }
                catch (e) { return ''; }
            })(),
            scheme: (function () {
                try { return getComputedStyle(document.documentElement).colorScheme || ''; }
                catch (e) { return ''; }
            })(),
            prefersDark: (function () {
                try {
                    return !!(window.matchMedia &&
                              window.matchMedia('(prefers-color-scheme: dark)').matches);
                } catch (e) { return null; }
            })(),
            ts: new Date().toISOString(),
            output: output || '',
            annotations: annotations || []
        };
        try {
            // CDP Runtime.addBinding 으로 프로그램이 만들어 둔 함수. 네트워크를 쓰지 않는다.
            window.__qaPush(JSON.stringify(payload));
        } catch (e) {
            // 프로그램 없이 이 스크립트만 실행된 경우(수동 테스트). 주석은
            // agentation 자체 localStorage 에 남으므로 잃지 않는다.
            if (window.console) console.info('[화면주석] 수집 채널 없음 - 주석은 브라우저에 저장됩니다.');
        }
    }

    /* ── 마운트 ────────────────────────────────────────────── */
    var root = null;

    /* ★"떠 있는가" 는 window 플래그가 아니라 실제 DOM 으로 판단한다.
     *   iframe 은 보통 빈 문서(about:blank)로 먼저 만들어지고 곧바로 실제 주소로
     *   바뀐다. 그때 크롬은 같은 window 를 재사용하므로 문서(DOM)는 날아가는데
     *   플래그만 남는다 → 새 문서에 다시 주입돼도 "이미 떴다" 로 보고 그냥 나가서
     *   툴바가 영원히 안 뜬다(실측: 셸 구조에서 항상 이렇게 됐다). */
    function mounted() {
        return !!document.getElementById(HOST_ID);
    }

    function unmount() {
        try { if (root) root.unmount(); } catch (e) { /* 무시 */ }
        root = null;
        var host = document.getElementById(HOST_ID);
        if (host && host.parentNode) host.parentNode.removeChild(host);
        var frame = document.getElementById(TOP_ID);
        if (frame) {
            try { frame.hidePopover(); } catch (e) { /* 무시 */ }
            if (frame.parentNode) frame.parentNode.removeChild(frame);
        }
        window.__QA_ANNOTATOR__ = false;
    }

    function mount() {
        if (!isMine()) return;
        if (mounted()) return;
        if (!shouldMount()) return;
        window.__QA_ANNOTATOR__ = true;

        var host = document.createElement('div');
        host.id = HOST_ID;
        document.body.appendChild(host);

        root = createRoot(host);
        // 렌더 뒤에 UI 가 body 로 포털되므로, 그다음 틱에 틀로 모아 올린다.
        setTimeout(keepOnTop, 0);
        root.render(
            React.createElement(Agentation, {
                // 메모를 다는 즉시 넘긴다. Copy/Send 를 눌러야만 전송되던 것이
                // "코멘트를 달았는데 목록에 없다" 의 원인이었다.
                onAnnotationAdd: function (a) { push('add', '', [a]); },
                onAnnotationUpdate: function (a) { push('update', '', [a]); },
                onAnnotationDelete: function (a) { push('delete', '', [a]); },
                onAnnotationsClear: function (as) { push('clear', '', as || []); },
                onCopy: function (md) { push('copy', md, null); },
                onSubmit: function (out, as) { push('submit', out, as); }
            })
        );
    }

    /* ── 모달 위로 올라가기 ───────────────────────────────────
     * ★z-index 로는 이길 수 없다. 우리 최대값은 100020 인데 페이지는 2147483647 을
     *   쓸 수 있고, 그러면 툴바가 모달 밑에 깔려 아무것도 고를 수 없다.
     *   "어떤 사이트에서나" 도구이므로 사이트별 숫자를 맞추는 길은 없다.
     *
     * 실측(Chrome 151, CDP 로 진짜 마우스 클릭을 보내 카운터로 확인):
     *   · 보통 요소(z=100020) vs 페이지 오버레이 z=2147483647 → 우리가 진다
     *   · popover(top layer)  vs 같은 오버레이               → 우리가 이긴다
     *   · popover vs dialog.showModal()                     → 막힌다(순서 무관).
     *     top layer 안에서도 모달이 최상위고 나머지는 inert 가 된다
     *   · UI 를 그 모달 dialog '안' 으로 옮기면                → 클릭·React 이벤트
     *     위임·position:fixed 좌표가 모두 정상. 페이지 모달은 손대지 않는다
     * 그래서: 평소엔 popover 로 올리고, native 모달이 열리면 그 안으로 옮긴다.
     */
    function topFrame() {
        var el = document.getElementById(TOP_ID);
        if (el) return el;
        el = document.createElement('div');
        el.id = TOP_ID;
        // UA 의 popover 기본 스타일(inset:0·margin:auto·테두리·배경·overflow)을 전부
        // 되돌린다. 우리는 '보이지 않는 틀' 만 필요하다. 클릭은 안 가로챈다
        // (자식 중 pointer-events:auto 인 것만 받는다 - 기존 동작과 같다).
        el.style.cssText = 'position:fixed;inset:0;display:block;margin:0;border:0;' +
            'padding:0;width:auto;height:auto;max-width:none;max-height:none;' +
            'min-width:0;min-height:0;background:none;overflow:visible;' +
            'pointer-events:none;color:inherit;';
        document.body.appendChild(el);
        return el;
    }

    /* ★실패하면 popover 속성을 반드시 떼야 한다. 속성이 붙은 채 showPopover() 가
     *   안 되면 UA 스타일이 display:none 으로 만들어 툴바가 통째로 사라진다 -
     *   이 도구에서 가장 나쁜 결과다(그래서 성공을 :popover-open 으로 확인한다). */
    function promote(el) {
        if (typeof el.showPopover !== 'function') return false;
        try {
            if (!el.hasAttribute('popover')) el.setAttribute('popover', 'manual');
            if (!el.matches(':popover-open')) el.showPopover();
            if (el.matches(':popover-open')) return true;
        } catch (e) { /* 아래에서 되돌린다 */ }
        try { el.removeAttribute('popover'); } catch (e) { /* 무시 */ }
        return false;
    }

    function topmostModalDialog() {
        // 열려 있는 native 모달. top layer 순서는 노출되지 않으므로 DOM 순서상 마지막을 쓴다.
        try {
            var ds = document.querySelectorAll('dialog[open]');
            var found = null;
            for (var i = 0; i < ds.length; i++) {
                if (ds[i].matches(':modal')) found = ds[i];
            }
            return found;
        } catch (e) {
            return null;
        }
    }

    /* 진단은 숫자로(README 7). "안 올라갔다" 를 추측하지 않게 센다. */
    var topStats = { calls: 0, moves: 0, intoModal: 0, promoted: 0, err: '' };

    function keepOnTop() {
        topStats.calls++;
        if (!isMine() || !mounted()) return;
        try {
            var frame = topFrame();
            // ①agentation 은 UI 를 document.body 로 포털한다. 그 래퍼를 틀 안으로 모은다.
            var nodes = document.querySelectorAll('body > [data-agentation-root]');
            for (var i = 0; i < nodes.length; i++) {
                if (nodes[i] !== frame) {
                    frame.appendChild(nodes[i]);
                    topStats.moves++;
                }
            }
            // ②native 모달이 열려 있으면 그 안이 inert 를 면제받는 유일한 자리다(실측).
            var want = topmostModalDialog() || document.body;
            if (frame.parentNode !== want) {
                want.appendChild(frame);
                if (want !== document.body) topStats.intoModal++;
            }
            // ③DOM 을 옮기면 popover 는 닫힌다 - 매번 다시 올린다.
            if (promote(frame)) topStats.promoted++;
        } catch (e) {
            topStats.err = String(e && e.message || e);
        }
    }

    /* 모달은 예고 없이 열린다. 주기 검사(2초)만으로는 늦으므로 두 가지를 더 본다:
     *   · dialog 의 open 속성 변화(native 모달)
     *   · 클릭 직후(대부분의 모달이 클릭으로 열린다) */
    function watchTop() {
        var pending = null;
        function soon() {
            if (pending) clearTimeout(pending);
            pending = setTimeout(function () { pending = null; keepOnTop(); }, 60);
        }
        try {
            new MutationObserver(soon).observe(document.documentElement, {
                subtree: true, attributes: true, attributeFilter: ['open']
            });
        } catch (e) { /* 관찰 불가 - 주기 검사로 버틴다 */ }
        document.addEventListener('click', soon, true);
    }

    /* ── Esc 보호 ─────────────────────────────────────────────
     * ★사용법이 안내하는 "Esc 로 모드 끄기" 가 페이지의 모달까지 닫아 버린다.
     *   지적하려던 모달이 사라지므로 주석을 남길 수 없다. 모달이 열려 있는 동안에는
     *   Esc 를 페이지로 넘기지 않는다(주석 도구 자신은 그대로 받는다 - 우리보다
     *   먼저 걸리는 리스너가 없도록 capture 단계에서 페이지 쪽만 끊는다).
     *   모달 판정은 표준 신호(dialog[open]·aria-modal·role=dialog)로만 한다.
     *   그 표시가 없는 모달은 가려낼 수 없다 - 그때는 툴바의 [나가기] 로 끈다.
     */
    /* 같은 출처 자식 프레임 중 툴바를 가진 쪽을 토글한다(없으면 false). */
    function toggleChild() {
        try {
            var fs = document.querySelectorAll('iframe');
            for (var i = 0; i < fs.length; i++) {
                var w = null;
                try { w = fs[i].contentWindow; } catch (e) { continue; }
                if (!w) continue;
                try {
                    if (typeof w.__qaToggleMode === 'function' &&
                        w.document.getElementById(HOST_ID)) {
                        if (w.__qaToggleMode()) return true;
                    }
                } catch (e) { continue; }   // 교차출처 - 건너뛴다
            }
        } catch (e) { /* 무시 */ }
        return false;
    }

    /* 같은 출처 부모가 툴바를 가졌으면 부모를 토글한다.
     * ★셸 구조에서 사람이 iframe 안을 한 번 클릭하면 포커스가 그 문서로 옮겨간다.
     *   그 뒤 Esc 는 iframe 이 받는데 툴바는 최상위에 있을 수 있다 - 위로도 넘겨야
     *   한다(실측: 아래로만 넘겨서 Esc 가 안 먹었다). */
    function toggleParent() {
        try {
            if (window.top === window.self) return false;
            var w = window.parent;
            void w.location.href;                 // 교차출처면 여기서 throw
            if (typeof w.__qaToggleMode === 'function' &&
                w.document.getElementById(HOST_ID)) {
                return !!w.__qaToggleMode();
            }
        } catch (e) { /* 교차출처 - 넘길 수 없다 */ }
        return false;
    }

    function pageModalOpen() {
        try {
            if (topmostModalDialog()) return true;
            var cands = document.querySelectorAll('[aria-modal="true"], [role="dialog"]');
            for (var i = 0; i < cands.length; i++) {
                var el = cands[i];
                if (el.id === TOP_ID || el.closest('#' + TOP_ID)) continue;   // 우리 UI 제외
                if (el.closest('[data-agentation-root]')) continue;
                var r = el.getBoundingClientRect();
                if (r.width < 40 || r.height < 40) continue;
                var st = getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                if (parseFloat(st.opacity || '1') < 0.05) continue;
                return true;
            }
        } catch (e) { /* 무시 */ }
        return false;
    }

    /* 주석 모드가 켜져 있는가.
     * ★`#feedback-cursor-styles` 로 판단하지 말 것. 그 <style> 은 모드를 끈 뒤에도
     *   문서에 남는다(실측: 모드 OFF 인데 styleHasCrosshair=true). 그걸 쓰면 한 번
     *   켠 뒤에는 영원히 '켜짐' 으로 읽혀, 모드를 끈 뒤에도 Esc 가드가 계속 발동해
     *   **사용자가 모달을 닫을 수 없게 된다.**
     *   모드와 함께 생겼다 사라지는 것은 '고르기 오버레이' 하나뿐이다(실측 0 ↔ 1).
     *   우리 UI 안으로 범위를 좁혀야 한다 - 페이지에도 overlay 가 15개씩 있다. */
    function modeOn() {
        return !!document.querySelector('[data-agentation-root] [class*="overlay"]');
    }

    function guardEsc() {
        /* ①페이지 스크립트가 닫는 모달(div 오버레이 계열) - 전파를 끊는다.
         * ②모드가 꺼져 있으면 Esc 로 켠다 - 화면을 옮길 때마다 둥근 버튼을 찾아
         *   누르는 것이 불편하다는 실사용 보고(브3).
         *   ★단 모달이 열려 있을 때는 켜지 않는다. 그 Esc 는 모달을 닫으려는 것이다
         *     (모달 위에서 모드를 끄려면 툴바 [나가기] 를 쓴다 - 사용법 3절). */
        document.addEventListener('keydown', function (e) {
            if (e.key !== 'Escape') return;
            /* ★내 문서에 툴바가 없어도 빠져나가지 않는다. 셸 구조에서는 툴바가 내용
             *   iframe 에 있고 Esc 는 포커스된(보통 최상위) 문서가 받는다 - 여기서 물러나면
             *   Esc 로 모드를 켤 수 없다(실측: 위임 코드는 멀쩡한데 여기서 막혀 있었다). */
            if (!mounted()) {
                if (pageModalOpen()) return;    // 모달 닫기가 우선
                if (toggleChild() || toggleParent()) {
                    e.stopPropagation();
                    e.preventDefault();
                }
                return;
            }
            if (modeOn()) {
                if (!pageModalOpen()) return;   // 평소대로 agentation 이 모드를 끈다
                e.stopPropagation();
                if (window.console) {
                    console.info('[화면주석] 모달이 열려 있어 Esc 를 페이지로 넘기지 않았습니다.');
                }
                return;
            }
            if (pageModalOpen()) return;         // 모달 닫기가 우선
            if (turnOn()) {
                e.stopPropagation();
                e.preventDefault();
                if (window.console) console.info('[화면주석] Esc 로 주석 모드를 켰습니다.');
                return;
            }
            /* 내 문서에 툴바가 없다 = 셸 구조에서 툴바가 내용 iframe 에 있다.
             * Esc 는 포커스된 문서만 받으므로, 같은 출처 자식에게 대신 넘긴다. */
            if (toggleChild()) {
                e.stopPropagation();
                e.preventDefault();
            }
        }, true);

        /* ★②native <dialog> 는 페이지가 아니라 브라우저가 닫는다(close request).
         *   전파를 끊어도 닫히므로 cancel 을 막아야 한다(실측: stopPropagation 만
         *   걸었을 때 모달이 그대로 닫혔다). */
        document.addEventListener('cancel', function (e) {
            if (!mounted() || !modeOn()) return;
            var t = e.target;
            if (!t || !t.tagName || t.tagName !== 'DIALOG') return;
            e.preventDefault();
            if (window.console) {
                console.info('[화면주석] 주석 모드라서 Esc 로 모달을 닫지 않았습니다.');
            }
        }, true);
    }

    /* ── 우리 UI 안인가 ─────────────────────────────────────── */
    function isOurs(el) {
        try {
            if (!el || !el.closest) return false;
            return !!(el.closest('[data-agentation-root]') || el.closest('#' + TOP_ID) ||
                      el.closest('#' + HOST_ID));
        } catch (e) {
            return false;
        }
    }

    /* 접힌 둥근 버튼(= 모드 켜기). 툴바 안에서 클릭을 받는 60px 이하 요소가 그것이다.
     * ★문구(aria-label)로 찾지 않는다 - 한글화 표가 바뀌면 조용히 깨진다. */
    function toggleButton() {
        try {
            var t = document.querySelector('[data-feedback-toolbar]');
            if (!t) return null;
            var all = [t].concat([].slice.call(t.querySelectorAll('*')));
            for (var i = 0; i < all.length; i++) {
                var e = all[i], r = e.getBoundingClientRect();
                if (getComputedStyle(e).pointerEvents !== 'auto') continue;
                if (r.width >= 24 && r.height >= 24 && r.width <= 60) return e;
            }
        } catch (e) { /* 무시 */ }
        return null;
    }

    /* ── 브2·브8: 클릭 정책 ───────────────────────────────────
     * · 주석 모드가 꺼져 있으면 아무것도 하지 않는다(페이지가 평소대로 동작).
     * · 켜져 있으면 페이지는 클릭을 못 받는다 - '페이지 클릭 차단' 설정이
     *   button·a·input 등 상호작용 태그만 막아서 썸네일(img/div + 위임 핸들러)이
     *   새어 나가 미리보기가 열렸다(실사용 보고).
     * · 첫 클릭은 우리도 삼킨다 → 두 번째 클릭(더블클릭)에만 요소가 잡힌다.
     */
    function guardClicks() {
        // 드래그·글자 선택은 mousedown 기반이라 preventDefault 하지 않는다.
        ['mousedown', 'mouseup', 'pointerdown', 'pointerup', 'dblclick', 'auxclick'].forEach(
            function (type) {
                document.addEventListener(type, function (e) {
                    if (!mounted() || !modeOn() || isOurs(e.target)) return;
                    e.stopPropagation();        // agentation 은 받고 페이지는 못 받는다
                }, true);
            });

        document.addEventListener('click', function (e) {
            if (!mounted() || !modeOn() || isOurs(e.target)) return;
            if ((e.detail || 1) < 2) {
                // 첫 클릭: 아무 일도 일어나지 않게 한다(agentation 까지 차단).
                e.stopImmediatePropagation();
                e.preventDefault();
                return;
            }
            // 두 번째 클릭만 선택으로 넘긴다. ★기본 동작도 막아야 한다 -
            // stopPropagation 만 하면 href="#" 링크가 실제로 눌려 주소에 해시가 붙었다(실측).
            e.preventDefault();
            e.stopPropagation();
        }, true);
    }

    /* ── 브6: 메모 입력 영역을 2배로 · 팝업을 화면 안으로 ───── */
    function growPopup() {
        try {
            var p = document.querySelector('[data-annotation-popup]');
            if (!p) return;
            var ta = p.querySelector('textarea');
            /* ★한 번만 키운다. 팝업이 닫혔다 '같은 노드' 로 다시 열리면 이미 2배가 된
             *   크기를 또 2배로 잡아 폭주한다(실측: 280x52 → 1169x456).
             *   그래서 노드에 표식을 남긴다 - 노드가 살아 있는 동안 상태도 살아 있다. */
            if (ta && !ta.hasAttribute('data-qa-grown')) {
                var r = ta.getBoundingClientRect();
                var pr0 = p.getBoundingClientRect();
                if (r.width && r.height) {
                    ta.setAttribute('data-qa-grown', '1');
                    // 실측값의 2배. 숫자를 박지 않고 그때그때 두 배로 만든다
                    // (기기·글꼴에 따라 원래 크기가 다르다).
                    var w = Math.min(Math.round(r.width * 2), window.innerWidth - 80);
                    var h = Math.min(Math.round(r.height * 2), window.innerHeight - 160);
                    ta.style.width = w + 'px';
                    ta.style.height = h + 'px';
                    /* ★팝업 통(패널)도 같이 넓혀야 한다. 입력칸만 키우면 테두리가 통
                     *   밖으로 튀어나온다(실측: 캡처에서 파란 테두리가 패널을 뚫고 나갔다).
                     *   통과 입력칸의 폭 차이(=여백)를 유지한 채 넓힌다. */
                    var pad = Math.max(0, Math.round(pr0.width - r.width));
                    var pw = Math.min(w + pad, window.innerWidth - 24);
                    p.style.minWidth = pw + 'px';
                    p.style.maxWidth = 'none';
                    p.style.boxSizing = 'border-box';
                }
            }
            // 커진 팝업이 화면을 벗어나면 안으로 당긴다.
            // ★'버튼이 아래로 넘어가서 안 눌러진다' 는 실사용 보고가 이 경우다.
            var pr = p.getBoundingClientRect();
            var dx = 0, dy = 0;
            if (pr.right > window.innerWidth - 8) dx = window.innerWidth - 8 - pr.right;
            if (pr.left + dx < 8) dx += 8 - (pr.left + dx);
            if (pr.bottom > window.innerHeight - 8) dy = window.innerHeight - 8 - pr.bottom;
            if (pr.top + dy < 8) dy += 8 - (pr.top + dy);
            var want = (dx || dy) ? 'translate(' + Math.round(dx) + 'px,' + Math.round(dy) + 'px)'
                                  : '';
            if (p.style.transform !== want) p.style.transform = want;
        } catch (e) { /* 무시 */ }
    }

    function watchPopup() {
        try {
            new MutationObserver(growPopup).observe(document.body, {
                childList: true, subtree: true
            });
        } catch (e) { /* 무시 */ }
        setInterval(growPopup, 400);            // 관찰이 막히는 문서에서도 되게
    }

    /* ── 화면 정보 통지 · 감시 ───────────────────────────────── */
    var resizeTimer = null;

    function announce() {
        // 최상위 문서만 "이 탭은 지금 이 주소를 이 해상도로 본다" 를 알린다.
        // 프로그램이 콘솔 에러·실패 요청을 올바른 화면에 붙이는 근거가 된다.
        if (!isMine()) return;
        if (window.top !== window.self) return;
        push('page', '', []);
    }

    function onResize() {
        if (resizeTimer) clearTimeout(resizeTimer);
        resizeTimer = setTimeout(announce, 400);            // 끌어 조절하는 동안 쏟아지지 않게
    }

    function watchFrames() {
        // 셸은 문서가 로드된 뒤 JS 로 iframe 을 만들고 탭마다 src 를 갈아치운다.
        // 그래서 "지금 지배 프레임이 있는가" 를 한 번만 보고 끝내면 안 된다.
        // 처음에는 촘촘히, 그 뒤에는 느리게 계속 본다(구조가 나중에 또 바뀐다).
        var until = Date.now() + FAST_MS;
        var timer = null;

        function tick() {
            if (!isMine()) {                                // 문서가 바뀌었다 - 손을 뗀다
                if (timer) clearInterval(timer);
                return;
            }
            if (mounted()) {
                if (!shouldMount()) unmount();
                else keepOnTop();            // 모달이 열렸다 닫혔을 수 있다
            } else {
                mount();
            }
            if (timer && Date.now() > until) {              // 촘촘한 구간이 끝나면 느리게
                clearInterval(timer);
                timer = null;
                setInterval(tick, SLOW_TICK);
            }
        }
        timer = setInterval(tick, FAST_TICK);
    }

    /* ── 프로그램이 부르는 비우기 ─────────────────────────────
     * ★이력은 두 곳에 있다. ①프로그램의 파일 ②브라우저 localStorage(agentation
     *   자체 기능, 화면별 7일 보관). 프로그램에서 [비우기]/[추출] 을 눌러도 ②가
     *   남아 있으면 그 화면에 다시 갔을 때 하단 툴바에 옛 주석이 그대로 보인다
     *   (실측: feedback-annotations-/dashboard 3건이 남아 있었다).
     *   그래서 프로그램이 이 함수를 불러 ②까지 비운다. 설정(핀 색상·테마)은 남긴다.
     *   비운 뒤 툴바를 다시 붙여야 React 가 빈 저장소를 다시 읽는다(새로고침 불필요). */
    /* 진단용. "왜 모달 위로 안 올라갔나" 를 추측하지 말고 이 값을 읽는다. */
    window.__qaTop = function () {
        var frame = document.getElementById(TOP_ID);
        var modal = topmostModalDialog();
        return {
            frame: !!frame,
            popoverOpen: !!(frame && frame.hasAttribute('popover') &&
                            frame.matches(':popover-open')),
            parent: frame && frame.parentNode
                ? (frame.parentNode.tagName + (frame.parentNode.id ? '#' + frame.parentNode.id : ''))
                : null,
            uiInFrame: frame ? frame.querySelectorAll('[data-agentation-root]').length : 0,
            uiInBody: document.querySelectorAll('body > [data-agentation-root]').length,
            modalDialogOpen: !!modal,
            pageModalOpen: pageModalOpen(),
            modeOn: modeOn(),
            toggleFound: !!toggleButton(),
            stats: topStats
        };
    };

    /* 프로그램이 설정을 바꿨을 때 다시 붙이기 위한 것. __qaClear 와 달리
     * 저장된 주석을 지우지 않는다 - 스위치를 켜다가 주석을 잃으면 안 된다. */
    /* 이 문서의 툴바를 토글한다. 부모 문서가 자식 프레임을 대신 켜 줄 때 쓴다 -
     * 셸 구조에서는 툴바가 iframe 에 있는데 Esc 는 포커스된 문서만 받는다(실측). */
    window.__qaToggleMode = function () {
        return turnOn();
    };

    /* 모드를 켠다. ★툴바가 등장 애니메이션 중이면 클릭이 안 먹는 경우가 있다(실측:
     * Esc 를 눌렀는데 아무 일도 안 일어남). 사람이 두 번 누르지 않게 한 번만 더 시도한다. */
    function turnOn() {
        var btn = toggleButton();
        if (!btn) return false;
        btn.click();
        setTimeout(function () {
            try {
                if (modeOn()) return;
                var b2 = toggleButton();
                if (b2) b2.click();
            } catch (e) { /* 무시 */ }
        }, 320);
        return true;
    }

    window.__qaRemount = function () {
        try {
            unmount();
            mount();
        } catch (e) { /* 무시 */ }
        return mounted();
    };

    window.__qaClear = function () {
        var removed = 0;
        try {
            var kill = [];
            for (var i = 0; i < localStorage.length; i++) {
                var k = localStorage.key(i);
                if (k && k.indexOf('feedback-annotations-') === 0) kill.push(k);
                else if (k && k.indexOf('agentation-rearrange-') === 0) kill.push(k);
            }
            for (var j = 0; j < kill.length; j++) {
                localStorage.removeItem(kill[j]);
                removed++;
            }
        } catch (e) { /* 저장소 접근 불가 - 무시 */ }
        try {
            if (mounted()) {
                unmount();
                mount();
            }
        } catch (e) { /* 무시 */ }
        return removed;
    };

    function start() {
        mount();
        announce();
        watchFrames();
        watchTop();
        guardEsc();
        guardClicks();
        watchPopup();
        window.addEventListener('resize', onResize);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start, { once: true });
    } else {
        start();
    }
})();
