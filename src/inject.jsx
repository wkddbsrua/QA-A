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

    function shouldMount() {
        if (!document.body) return false;
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
        window.addEventListener('resize', onResize);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start, { once: true });
    } else {
        start();
    }
})();
