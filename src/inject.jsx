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

    /* ── 타이머 ────────────────────────────────────────────────
     * ★문서가 바뀌면(셸이 iframe 에 새 문서를 받으면) 옛 인스턴스의 타이머를 전부 끊는다.
     *   예전에는 '촘촘한 구간' 타이머 하나만 끊었고 느린 구간·팝업 감시는 그대로 남아,
     *   화면을 옮길 때마다 죽은 인스턴스의 타이머가 쌓였다. */
    var timers = [];

    function stopTimers() {
        for (var i = 0; i < timers.length; i++) {
            try { clearInterval(timers[i]); } catch (e) { /* 무시 */ }
        }
        timers = [];
    }

    function every(fn, ms) {
        var id = setInterval(function () {
            if (!isMine()) { stopTimers(); return; }     // 문서가 바뀌었다 - 손을 뗀다
            fn();
        }, ms);
        timers.push(id);
        return id;
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

    /* 우리가 들여다볼 수 없는(교차출처) 큰 iframe. 그 안은 그 문서에 뜬 툴바가 담당한다 -
     * "이 문서에서 무엇을 고를 수 있나" 를 말할 때 이것을 빼면 거짓말이 된다. */
    function foreignFrameExists() {
        try {
            var area = window.innerWidth * window.innerHeight;
            if (!area) return false;
            var frames = document.querySelectorAll('iframe');
            for (var i = 0; i < frames.length; i++) {
                var seen = true;
                try { seen = !!frames[i].contentDocument; } catch (e) { seen = false; }
                if (seen) continue;                     // 같은 출처는 위에서 판단했다
                var r = frames[i].getBoundingClientRect();
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

    function bigEnough() {
        // 광고·트래킹용 초소형 프레임과 '부모에 비해 작은' 프레임은 제외한다.
        if (window.top === window.self) return true;
        if (window.innerWidth < MIN_SIZE || window.innerHeight < MIN_SIZE) return false;
        var pa = parentArea();
        if (pa && (window.innerWidth * window.innerHeight) < pa * SHARE) return false;
        return true;
    }

    function shouldMount() {
        if (!document.body) return false;
        /* ★스위치가 켜져 있으면 최상위와 콘텐츠 iframe 에 '둘 다' 띄운다.
         *   예전에는 최상위에만 띄웠다 - "상단바·좌측 메뉴 포함" 과 "iframe 안쪽 선택" 이
         *   동시에 성립하지 않는다고 보았기 때문이다(부모의 전체화면 오버레이가 콘텐츠
         *   클릭을 가로챈다는 관찰). 그러나 실측하니 지금 구조에서는 가로채지 않는다:
         *   둘 다 띄우고 모드를 함께 켠 상태에서 GNB·LNB 는 최상위 문서가, 본문은 iframe
         *   문서가 각각 정확히 잡았다(같은 출처 셸 · 교차출처 셸 모두).
         *   대신 사람에게는 툴바를 하나만 보인다(hideBar - 본문 쪽은 감춘다). */
        if (forceTop()) return bigEnough();
        if (window.top === window.self) return !dominantFrameExists();
        if (!bigEnough()) return false;
        return !dominantFrameExists();                      // 중첩 셸까지 재귀적으로 성립
    }

    /* ── 툴바는 하나만 보이게 ─────────────────────────────────
     * ★'둘 다 고르기' 에서는 문서마다 툴바가 하나씩 있어야 한다(브라우저가 문서 경계를
     *   넘겨주지 않으므로 - agentation 도 iframe 안으로 들어가지 않는다: 번들에
     *   contentDocument/contentWindow 사용 0곳). 그러나 '사람에게 버튼이 둘로 보일'
     *   이유는 없다(실사용 불만: "왜 두 개를 써야 하나").
     *   그래서 바깥(최상위) 툴바만 남기고 본문 쪽 툴바는 감춘다 - 감춰도 요소 고르기·핀·
     *   메모창은 그대로 동작한다. 모드는 프로그램이 두 문서에 함께 걸어 준다.
     *   ★예전에는 겹치지 않게 위로 밀었는데(translateY), 새로고침 직후 몇 초 동안은
     *     밀리기 전이라 두 툴바가 화면상 같은 좌표에 정확히 포개졌다(실측: 최상위
     *     (892,1213) ≡ 본문 (224+668, 56+1157)). 그래서 "버튼이 사라져 하나가 됐다"
     *     로 보였다. 자리를 옮기는 대신 아예 하나만 보이게 한다. */
    function barHidden() {
        return forceTop() && window.top !== window.self;
    }

    function hideBar() {
        try {
            var t = document.querySelector('[data-feedback-toolbar]');
            if (!t) return;
            var want = barHidden() ? '0' : '';
            if (t.style.opacity === want) return;
            t.style.opacity = want;
            // 보이지 않는 것이 클릭을 먹으면 안 된다(그 자리에 페이지가 있다).
            t.style.pointerEvents = barHidden() ? 'none' : '';
        } catch (e) { /* 무시 */ }
    }

    /* ── 요소 속성 ──────────────────────────────────────────────
     * ★서버가 요소에 실어 보낸 값(data-*·title·href…)이다. 화면에 무엇이 "표시되는가" 와
     *   달리 무엇이 "와 있는가" 를 말해 주므로, 화면 탓인지 서버 탓인지가 여기서 갈린다.
     *   (실측: DOM Tree 가 콘텐츠명 대신 영역라벨만 보이던 건 - 값이 안 온 것인지,
     *    와 있는데 표시 우선순위가 가린 것인지를 결과 문서만으로는 가릴 수 없었다.) */
    var lastPick = null;                // 마지막으로 더블클릭해 고른 요소
    var SKIP_ATTRS = { 'class': 1, 'style': 1 };    // 이미 따로 나가는 것

    function classKey(el) {
        try { return Array.prototype.slice.call(el.classList).join(', '); }
        catch (e) { return ''; }
    }

    function attrsOf(el) {
        var out = [];
        try {
            var list = el.attributes || [];
            for (var i = 0; i < list.length && out.length < 8; i++) {
                var name = list[i].name;
                if (SKIP_ATTRS[name]) continue;
                var v = String(list[i].value || '');
                if (v.length > 60) v = v.slice(0, 60) + '…';
                out.push(v ? name + '="' + v + '"' : name);
            }
        } catch (e) { return ''; }
        return out.join(' ');
    }

    /* 주석이 가리키는 요소를 되찾는다. ★못 찾으면 붙이지 않는다 -
     * 엉뚱한 요소의 속성을 붙이는 것은 아무것도 안 붙이는 것보다 나쁘다. */
    function pickFor(a) {
        var want = a && a.cssClasses, el = lastPick;
        for (var i = 0; el && i < 6; i++) {         // 고른 지점에서 위로 훑는다
            if (classKey(el) === want) return el;   //   (agentation 이 조상을 고를 수 있다)
            el = el.parentElement;
        }
        try {
            var hit = document.querySelectorAll(a.elementPath);
            if (hit.length === 1) return hit[0];    // 경로가 유일할 때만 믿는다
        } catch (e) { /* 경로가 선택자로 성립하지 않는 경우 */ }
        return null;
    }

    function withAttrs(kind, annotations) {
        var src = annotations || [];
        if (kind === 'delete' || kind === 'clear') return src;   // 사라진 요소는 찾지 않는다
        var out = [];
        for (var i = 0; i < src.length; i++) {
            var a = src[i], el = null, at = '';
            try { el = pickFor(a); at = el ? attrsOf(el) : ''; } catch (e) { at = ''; }
            if (!at) { out.push(a); continue; }
            var copy = {};                          // agentation 의 객체는 건드리지 않는다
            for (var k in a) {
                if (Object.prototype.hasOwnProperty.call(a, k)) copy[k] = a[k];
            }
            copy.attrs = at;
            out.push(copy);
        }
        return out;
    }

    /* ── 프로그램으로 보내기 ─────────────────────────────────── */
    function push(kind, output, annotations, extra) {
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
            annotations: withAttrs(kind, annotations)
        };
        if (extra) {
            for (var k in extra) {
                if (Object.prototype.hasOwnProperty.call(extra, k)) payload[k] = extra[k];
            }
        }
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
        var badge = document.getElementById(BADGE_ID);
        if (badge && badge.parentNode) badge.parentNode.removeChild(badge);
        var mode = document.getElementById(MODE_ID);
        if (mode && mode.parentNode) mode.parentNode.removeChild(mode);
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
                // 브8: 그 메모창에 붙여 둔 그림이 있으면 함께 보낸다.
                onAnnotationAdd: function (a) {
                    var imgs = imagesToSend();
                    push('add', '', [a], imgs ? { images: imgs } : null);
                },
                onAnnotationUpdate: function (a) {
                    var imgs = imagesToSend();
                    push('update', '', [a], imgs ? { images: imgs } : null);
                },
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
        /* ★대소문자를 가리지 않는다(`i` 플래그). vendor 번들을 갱신하면서 클래스 이름이
         *   Overlay 로 바뀌기만 해도 여기가 조용히 false 가 되고, 그러면 클릭 차단까지
         *   같이 풀려 페이지가 그냥 눌린다 - 가장 알아채기 어려운 고장이다. */
        return !!document.querySelector('[data-agentation-root] [class*="overlay" i]');
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
                /* ★메모창이 열려 있는데 포커스가 그 안에 없으면 agentation 은 Esc 에
                 *   아무 반응도 하지 않는다(실측: 모드도 안 꺼지고 메모창도 안 닫힘).
                 *   사람에게는 "Esc 가 안 먹는다" 로 보인다 - 우리가 대신 닫아 준다.
                 *   글자가 들어 있으면 건드리지 않는다(쓰던 글을 잃게 하지 않는다). */
                var pop = openPopup();
                if (pop && !pop.contains(document.activeElement) && !popupBusy() &&
                        !pop.hasAttribute('data-qa-escd')) {
                    pop.setAttribute('data-qa-escd', '1');   // ★같은 노드에 두 번 하지 않는다
                    esc(pop.querySelector('textarea') || pop);
                    e.stopPropagation();
                    e.preventDefault();
                    return;
                }
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
                      el.closest('#' + HOST_ID) || el.closest('#' + SNIP_ID) ||
                      el.closest('#' + PINS_ID));
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
                if (!barHidden() && getComputedStyle(e).pointerEvents !== 'auto') continue;
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
                    if (type === 'dblclick') lastPick = e.target;    // 속성 수집용
                    e.stopPropagation();        // agentation 은 받고 페이지는 못 받는다
                }, true);
            });

        /* ★`e.detail >= 2` 하나로 판정하지 않는다. 브라우저는 두 클릭 사이에 마우스가
         *   몇 px 만 움직이거나 간격이 벌어지면 detail 을 1 로 되돌린다 - 그러면 아무리
         *   더블클릭해도 영영 아무것도 잡히지 않는다("클릭이 안 먹는다" 의 한 갈래).
         *   그래서 우리가 직접 시간·거리로 두 번째 클릭을 센다(사람 손에 맞춰 넉넉히). */
        var PAIR_MS = 800, PAIR_PX = 24;
        var firstAt = 0, firstX = 0, firstY = 0;

        function isSecond(e) {
            if ((e.detail || 1) >= 2) return true;
            if (!firstAt) return false;
            if (Date.now() - firstAt > PAIR_MS) return false;
            return Math.abs(e.clientX - firstX) <= PAIR_PX &&
                   Math.abs(e.clientY - firstY) <= PAIR_PX;
        }

        document.addEventListener('click', function (e) {
            if (!mounted() || !modeOn() || isOurs(e.target)) return;
            if (!isSecond(e)) {
                // 첫 클릭: 아무 일도 일어나지 않게 한다(agentation 까지 차단).
                firstAt = Date.now();
                firstX = e.clientX;
                firstY = e.clientY;
                e.stopImmediatePropagation();
                e.preventDefault();
                return;
            }
            firstAt = 0;                        // 한 쌍을 썼다 - 다음 쌍은 처음부터
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
            /* ★두 가지가 겹쳐 화면 경계에서 팝업이 좌우로 흔들렸다(실사용 보고).
             *   ①agentation 의 스타일시트가 이 팝업에 `transform: translateX(-50%)`
             *     로 가운데 정렬을 걸어 둔다. 여기서 `style.transform` 을 직접 쓰면
             *     그 값을 통째로 덮어써 -50% 가 사라지고 팝업이 폭의 절반만큼
             *     오른쪽으로 튄다. `transform` 과 별개로 합성되는 CSS `translate`
             *     속성을 쓰면 가운데 정렬은 그대로 두고 보정만 더할 수 있다.
             *   ②보정값을 적용한 뒤의 rect 로 또 보정량을 계산하면, 매 tick 마다
             *     '이미 당겨진 자리' 를 기준으로 다시 당겨 진동한다. 그래서 지금
             *     적용해 둔 보정값을 먼저 빼서 '보정 전 자리' 를 되살린 뒤 새
             *     보정량을 계산한다(멱등 - 같은 상태면 같은 결과). */
            var prevDx = parseFloat(p.getAttribute('data-qa-dx') || '0') || 0;
            var prevDy = parseFloat(p.getAttribute('data-qa-dy') || '0') || 0;
            var pr = p.getBoundingClientRect();
            var baseLeft = pr.left - prevDx, baseRight = pr.right - prevDx;
            var baseTop = pr.top - prevDy, baseBottom = pr.bottom - prevDy;
            var dx = 0, dy = 0;
            if (baseRight > window.innerWidth - 8) dx = window.innerWidth - 8 - baseRight;
            if (baseLeft + dx < 8) dx += 8 - (baseLeft + dx);
            if (baseBottom > window.innerHeight - 8) dy = window.innerHeight - 8 - baseBottom;
            if (baseTop + dy < 8) dy += 8 - (baseTop + dy);
            dx = Math.round(dx);
            dy = Math.round(dy);
            if (dx !== prevDx || dy !== prevDy) {
                p.style.translate = (dx || dy) ? (dx + 'px ' + dy + 'px') : '';
                p.setAttribute('data-qa-dx', String(dx));
                p.setAttribute('data-qa-dy', String(dy));
            }
        } catch (e) { /* 무시 */ }
    }

    function watchPopup() {
        try {
            new MutationObserver(growPopup).observe(document.body, {
                childList: true, subtree: true
            });
        } catch (e) { /* 무시 */ }
        every(growPopup, 400);                  // 관찰이 막히는 문서에서도 되게
    }

    /* ── 브9: 레이아웃 변경 개수를 화면에도 보여 준다 ─────────
     * 요구는 "주석 모드에서도 레이아웃 모드의 변경사항 개수를 알 수 있으면" 이었다.
     * 프로그램 창 배지에도 뜨지만, 화면을 보는 중에 알아야 하므로 여기에도 둔다.
     * ★버튼이 아니다 - 누를 수 없는 표시다(pointer-events:none). 툴바를 늘리지 않는다.
     */
    var BADGE_ID = '__qa_moved_badge';

    function rearrangeCount() {
        var n = 0;
        try {
            for (var i = 0; i < localStorage.length; i++) {
                var k = localStorage.key(i);
                if (!k || k.indexOf('agentation-rearrange-') !== 0) continue;
                var v = null;
                try { v = JSON.parse(localStorage.getItem(k)); } catch (e) { /* 무시 */ }
                if (Object.prototype.toString.call(v) === '[object Array]') n += v.length;
                else if (v && typeof v === 'object') n += Object.keys(v).length;
                else n += 1;
            }
        } catch (e) { /* 저장소 접근 불가 */ }
        return n;
    }

    function showMovedBadge() {
        try {
            if (!isMine() || barHidden()) return;   // 감춘 툴바 쪽에는 배지도 띄우지 않는다
            var n = mounted() ? rearrangeCount() : 0;
            var el = document.getElementById(BADGE_ID);
            if (!n) {
                if (el && el.parentNode) el.parentNode.removeChild(el);
                return;
            }
            var frame = topFrame();
            if (!el) {
                el = document.createElement('div');
                el.id = BADGE_ID;
                el.style.cssText = 'position:fixed;right:1.25rem;bottom:4.6rem;' +
                    'padding:4px 10px;border-radius:999px;background:#111;color:#fff;' +
                    'font:600 12px/1.5 "Malgun Gothic",system-ui,sans-serif;' +
                    'box-shadow:0 2px 8px rgba(0,0,0,.28);pointer-events:none;' +
                    'white-space:nowrap;';
            }
            if (el.parentNode !== frame) frame.appendChild(el);
            var text = '레이아웃 변경 ' + n;
            if (el.textContent !== text) el.textContent = text;
        } catch (e) { /* 무시 */ }
    }

    /* ── 브3: 레이아웃 모드에서 옮긴 것을 목록에도 보인다 ──────────
     * agentation 의 레이아웃(rearrange·design) 변경은 우리 콜백으로 나오지 않고
     * localStorage 에만 남는다 - 그래서 지금까지는 개수만 세고(브9) 내용은
     * 몰랐다(실사용 보고: "레이아웃 기능 주석 작성시 목록에서 해당 주석이 안
     * 보입니다"). 내용을 읽어 프로그램에 보낸다 - 새 기능이 아니라 agentation
     * 이 이미 저장해 둔 것을 마저 전달하는 것이다. */
    function rearrangeState() {
        try {
            var raw = localStorage.getItem('agentation-rearrange-' + location.pathname);
            return raw ? JSON.parse(raw) : null;
        } catch (e) { return null; }
    }

    function designPlacements() {
        /* ★agentation-design-<pathname> 은 배열이 그대로 저장된다
         *   ({placements:[...]} 이 아니다 - vendor 번들 실측). 배열이 아니면 버린다. */
        try {
            var raw = localStorage.getItem('agentation-design-' + location.pathname);
            var v = raw ? JSON.parse(raw) : null;
            return (Object.prototype.toString.call(v) === '[object Array]') ? v : [];
        } catch (e) { return []; }
    }

    function roundRect(r) {
        if (!r) return null;
        return { x: Math.round(r.x), y: Math.round(r.y),
                 w: Math.round(r.width), h: Math.round(r.height) };
    }

    function rectMoved(a, b) {
        if (!a || !b) return false;
        return Math.abs(a.x - b.x) > 1 || Math.abs(a.y - b.y) > 1 ||
               Math.abs(a.width - b.width) > 1 || Math.abs(a.height - b.height) > 1;
    }

    function layoutDiff() {
        var out = { moved: [], order: null, placements: [] };
        var st = rearrangeState();
        if (st && st.sections) {
            for (var i = 0; i < st.sections.length; i++) {
                var s = st.sections[i];
                if (s && rectMoved(s.originalRect, s.currentRect)) {
                    out.moved.push({ id: s.id, label: s.label || s.tagName || '?',
                        tag: s.tagName || '', sel: s.selector || '',
                        from: roundRect(s.originalRect), to: roundRect(s.currentRect) });
                }
            }
            if (st.originalOrder) {
                var now = st.sections.map(function (s) { return s.id; });
                var before = st.originalOrder;
                var changed = now.length !== before.length ||
                    now.some(function (id, j) { return id !== before[j]; });
                if (changed) out.order = { from: before, to: now };
            }
        }
        var pl = designPlacements();
        for (var k = 0; k < pl.length; k++) {
            var p = pl[k];
            if (!p) continue;
            out.placements.push({ id: p.id, type: p.type || '?',
                x: Math.round(p.x || 0), y: Math.round(p.y || 0),
                w: Math.round(p.width || 0), h: Math.round(p.height || 0),
                text: p.text || '' });
        }
        if (!out.moved.length && !out.order && !out.placements.length) return null;
        return out;
    }

    // 손대지 않은 화면은 아예 보내지 않는다 - 초기값도 '변화 없음' 과 같은 값으로 둔다.
    var lastLayoutSig = 'null';

    function watchLayout() {
        if (!isMine() || !mounted()) return;
        var d;
        try { d = layoutDiff(); } catch (e) { d = null; }
        var sig;
        try { sig = JSON.stringify(d); } catch (e) { sig = 'null'; }
        if (sig === lastLayoutSig) return;
        lastLayoutSig = sig;
        push('layout', '', [], { layout: d });
    }

    /* ── [브10] 지금 어느 모드인가 ─────────────────
     * 요구: "내가 지금 정확히 어느 모드인지 툴바에서 알 수 있게".
     * 툴바 위치(자동 / 최상위 고정)에 따라 고를 수 있는 범위가 갈리는데, 화면에는 그
     * 단서가 하나도 없었다. 그래서 "gnb·lnb 만 잡히고 나머지는 안 잡힌다" 는 보고가
     * 고장인지 모드인지 사람도 프로그램도 가릴 수 없었다(프로그램의 주입 배지는
     * 프레임 하나만 떠 있어도 초록이라 이 둘을 구분하지 못한다).
     * ★버튼이 아니다 - 누를 수 없는 표시다(pointer-events:none). 툴바를 늘리지 않는다.
     * ★모드가 꺼진 동안은 한 줄로 접는다 - 평소 보는 화면에 큰 배지를 올려놓지 않는다.
     */
    var MODE_ID = '__qa_mode_badge';

    function modeBadgeText() {
        var top = window.top === window.self;
        var head = (forceTop() ? '둘 다 고르기' : '자동') + ' · ' +
            (top ? '최상위 문서' : 'iframe 문서');
        try { if (window.__qaVer) head += ' · ' + window.__qaVer; } catch (e) { /* 판 모름 */ }
        if (!modeOn()) return head;                     // 접힌 상태 - 한 줄만
        var can;
        if (!top) {
            can = forceTop()
                ? '이 본문을 고를 수 있습니다 (상단바·좌측 메뉴는 위쪽 툴바로)'
                : '이 iframe 안쪽을 고를 수 있습니다 (상단바·좌측 메뉴는 불가)';
        } else if (dominantFrameExists()) {
            can = forceTop()
                ? '상단바·좌측 메뉴와 본문을 모두 고를 수 있습니다'
                : '상단바·좌측 메뉴를 고를 수 있습니다 (iframe 안쪽은 불가)';
        } else if (foreignFrameExists()) {
            can = '이 문서를 고를 수 있습니다 '
                + '(다른 출처 iframe 안쪽은 그 안의 툴바가 맡습니다 - 모드는 같이 켜집니다)';
        } else {
            can = '이 화면 전부를 고를 수 있습니다';
        }
        return head + '\n' + can;
    }

    function showModeBadge() {
        try {
            if (!isMine() || barHidden()) return;   // 배지도 하나만
            var el = document.getElementById(MODE_ID);
            if (!mounted()) {
                if (el && el.parentNode) el.parentNode.removeChild(el);
                return;
            }
            var frame = topFrame();
            if (!el) {
                el = document.createElement('div');
                el.id = MODE_ID;
                // 툴바 바로 위. '레이아웃 변경' 배지 자리(4.6rem)는 그대로 둔다.
                el.style.cssText = 'position:fixed;right:1.25rem;bottom:7.2rem;' +
                    'padding:5px 11px;border-radius:10px;background:rgba(17,17,17,.88);' +
                    'color:#fff;font:600 11px/1.45 "Malgun Gothic",system-ui,sans-serif;' +
                    'box-shadow:0 2px 8px rgba(0,0,0,.28);pointer-events:none;' +
                    'white-space:pre;text-align:right;';
            }
            if (el.parentNode !== frame) frame.appendChild(el);
            var text = modeBadgeText();
            if (el.textContent !== text) el.textContent = text;
        } catch (e) { /* 배지 하나 때문에 도구가 서면 안 된다 */ }
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

    /* ★스타일이 안 들어간 채로 렌더된 툴바는 position:static 으로 문서 맨 아래에
     *   깔려 '없는 것' 과 같다(실측: 요소 128개가 렌더됐는데 style 태그는 0개).
     *   프로그램의 주입 점검은 이것을 잡아내지만, 잡아낸 뒤에 할 수 있는 일이 없었다.
     *   여기서 한 번 다시 붙여 본다(무한 반복은 하지 않는다 - 두 번까지). */
    var healed = 0;

    function healStyles() {
        if (healed >= 2) return;
        try {
            var t = document.querySelector('[data-feedback-toolbar]');
            if (!t) return;
            if (getComputedStyle(t).position === 'fixed') return;
            healed++;
            if (window.console) console.info('[화면주석] 툴바 스타일이 빠져 다시 붙입니다.');
            unmount();
            mount();
        } catch (e) { /* 무시 */ }
    }

    /* ★주소만 바뀌고 문서는 그대로인 화면(SPA·해시 이동)에서는 로드 통지가 오지 않는다.
     *   그러면 뒤이어 남긴 주석이 '직전 화면' 에 붙어, 결과 문서의 화면 목록이 실제와
     *   어긋난다. 주소가 바뀌면 그 사실만 다시 알린다(추가 기능이 아니라 귀속 교정이다). */
    var lastUrl = '';

    function watchUrl() {
        try {
            if (location.href === lastUrl) return;
            lastUrl = location.href;
            announce();
        } catch (e) { /* 무시 */ }
    }

    function watchFrames() {
        // 셸은 문서가 로드된 뒤 JS 로 iframe 을 만들고 탭마다 src 를 갈아치운다.
        // 그래서 "지금 지배 프레임이 있는가" 를 한 번만 보고 끝내면 안 된다.
        // 처음에는 촘촘히, 그 뒤에는 느리게 계속 본다(구조가 나중에 또 바뀐다).
        var until = Date.now() + FAST_MS;
        var fast = null;

        function tick() {
            watchUrl();                      // 주소만 바뀌는 화면(SPA)도 화면으로 센다
            if (mounted()) {
                if (!shouldMount()) unmount();
                else {
                    keepOnTop();             // 모달이 열렸다 닫혔을 수 있다
                    healStyles();            // 스타일 없이 렌더된 툴바를 되살린다
                    hideBar();               // 본문 쪽 툴바는 감춘다(버튼은 하나만)
                    showMovedBadge();        // 레이아웃 변경 개수(브9)
                    showModeBadge();         // 지금 어느 모드인가
                    watchLayout();           // 레이아웃 변경 내용을 목록에도(브3)
                }
            } else {
                mount();
            }
            if (fast && Date.now() > until) {               // 촘촘한 구간이 끝나면 느리게
                clearInterval(fast);
                fast = null;
                every(tick, SLOW_TICK);
            }
        }
        fast = every(tick, FAST_TICK);
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
        /* ★켜기만 하지 않는다. 같은 출처 셸에서는 툴바가 iframe 에 있고 Esc 는
         *   최상위가 받는데, 여기서 켜기만 하면 사람이 Esc 로 모드를 끌 수 없다 -
         *   모드가 켜진 동안 페이지는 클릭을 받지 못하므로 화면이 멈춘 것처럼 보인다
         *   (실측: 같은 출처 셸에서 Esc 를 몇 번을 눌러도 계속 켜진 채였다). */
        if (!mounted()) return false;
        return modeOn() ? turnOff() : turnOn();
    };

    /* 모드를 켠다. ★툴바가 등장 애니메이션 중이면 클릭이 안 먹는 경우가 있다(실측:
     * Esc 를 눌렀는데 아무 일도 안 일어남). 사람이 두 번 누르지 않게 한 번만 더 시도한다. */
    function turnOn() {
        var btn = toggleButton();
        if (btn) btn.click();
        setTimeout(function () {
            try {
                if (modeOn()) return;
                var b2 = toggleButton();
                if (b2) b2.click();
                /* ★둥근 버튼을 '크기' 로 찾는다(문구로 찾으면 한글화 표에 묶인다).
                 *   그래서 못 찾거나 엉뚱한 것을 눌렀을 수 있다 - agentation 자체
                 *   단축키(Ctrl+Shift+F)로 한 번 더 시도한다. 길이 둘이면 하나가 막혀도 켜진다. */
                setTimeout(function () {
                    try {
                        if (modeOn()) return;
                        document.dispatchEvent(new KeyboardEvent('keydown', {
                            key: 'f', code: 'KeyF', ctrlKey: true, shiftKey: true,
                            bubbles: true, cancelable: true
                        }));
                    } catch (e2) { /* 무시 */ }
                }, 320);
            } catch (e) { /* 무시 */ }
        }, 320);
        return !!btn;
    }

    /* ── 프레임 사이 모드 맞추기 ──────────────────────────────
     * ★셸 구조에서 상단바·좌측 메뉴(최상위 문서)와 본문(iframe)은 서로 다른 문서다.
     *   교차출처면 서로를 JS 로 부를 수 없어 두 툴바의 모드가 따로 논다 - Esc 를 받은
     *   문서만 켜지고 나머지는 꺼진 채라 그쪽은 페이지가 평소대로 동작한다.
     *   그것이 "gnb·lnb 만 클릭된다" 의 정체다(재현: 셸 + 교차출처 iframe →
     *   최상위 mode=on / iframe mode=off, 본문을 더블클릭해도 메모창이 열리지 않았다.
     *   반대로 iframe 에 포커스를 두고 Esc 를 누르면 본문만 잡히고 gnb·lnb 가 안 잡힌다 -
     *   어느 쪽이 켜지는지는 그때 포커스가 어디 있었나로 갈려서 '간헐적' 으로 보였다).
     *   교차출처를 넘는 다리는 프로그램(CDP)뿐이다. 바뀐 쪽이 알리면 프로그램이 같은
     *   탭의 나머지 문서에 그대로 전달한다(다른 탭에는 보내지 않는다). */
    var lastMode = null;        // 마지막으로 확인한 모드. null = 툴바 없음/아직 모름
    var muteUntil = 0;          // 프로그램이 맞춰 주는 동안은 되받아 알리지 않는다
    var wantMode = null;        // 프로그램이 이 문서에 걸어 달라고 한 값
    var wantTries = 0;
    var lastBeat = 0;      // 마지막으로 상태를 알린 시각(어긋남 자동 교정용)

    /* ★[나가기] 버튼을 문구로 찾지 않는다(한글화 표가 바뀌면 조용히 깨진다).
     *   agentation 은 document 의 keydown 으로 Esc 를 받아 모드를 끈다 -
     *   사람이 누르는 것과 같은 길로 끈다. */
    /* 지금 이 문서에서 메모를 쓰는 중인가(글자가 들어간 메모창이 열려 있다).
     * ★쓰던 글을 잃게 하지 않는다 - 이 상태에서는 남이 시켜도 모드를 끄지 않는다. */
    /* 지금 '실제로 떠 있는' 메모창. ★사라지는 중(퇴장 애니메이션)인 노드가 DOM 에
     *   잠시 남는다 - 그것을 열린 것으로 세면, 우리가 Esc 를 계속 그쪽으로 돌려
     *   모드를 영영 못 끄게 된다(실측: Esc 를 세 번 눌러도 안 꺼졌다). */
    function openPopup() {
        try {
            var p = document.querySelector('[data-annotation-popup]');
            if (!p) return null;
            var st = getComputedStyle(p);
            if (st.display === 'none' || st.visibility === 'hidden') return null;
            if (parseFloat(st.opacity || '1') < 0.1) return null;
            return p;
        } catch (e) {
            return null;
        }
    }

    function popupBusy() {
        try {
            var p = openPopup();
            if (!p) return false;
            var ta = p.querySelector('textarea');
            return !!(ta && String(ta.value || '').trim());
        } catch (e) {
            return false;
        }
    }

    function esc(el) {
        el.dispatchEvent(new KeyboardEvent('keydown', {
            key: 'Escape', code: 'Escape', keyCode: 27, which: 27,
            bubbles: true, cancelable: true
        }));
    }

    /* ★끄기에 Esc 를 쓰지 않는다. agentation 의 Esc 처리는 메모창 상태에 얽혀 있어
     *   ("메모가 대기 중이면 아무것도 하지 않는다"), 메모창을 열어 둔 프레임은 Esc 를
     *   몇 번을 보내도 모드가 꺼지지 않았다(실측: 세 번 눌러도 링크가 계속 안 눌렸다).
     *   agentation 자체 단축키(Ctrl+Shift+F)는 그 상태와 무관하게 모드를 끈다 -
     *   그 길로 끈다. 꺼질 때까지 짧게 여러 번 시도한다.
     *   ★글자가 들어간 메모창이 있으면 그대로 둔다 - 쓰던 글을 잃게 하지 않는다. */
    function turnOff() {
        var tries = 0;
        (function step() {
            try {
                if (!isMine() || !mounted() || !modeOn()) return;    // 다 됐다
                if (popupBusy()) return;                             // 쓰는 중 - 기다린다
                document.dispatchEvent(new KeyboardEvent('keydown', {
                    key: 'f', code: 'KeyF', ctrlKey: true, shiftKey: true,
                    bubbles: true, cancelable: true
                }));
            } catch (e) { /* 무시 */ }
            if (++tries < 4) setTimeout(step, 220);
        })();
        return true;
    }

    function apply(on) {
        return on ? turnOn() : turnOff();
    }

    function reportMode() {
        var on = mounted() ? modeOn() : null;
        if (on === null) {                       // 툴바가 없다 - 알릴 것도 걸 것도 없다
            lastMode = null;
            wantMode = null;
            return;
        }
        /* ★프로그램이 걸어 달라고 한 값과 다르면, 그것은 '알릴 일' 이 아니라 '다시 걸
         *   일' 이다. 여기서 반대로 알려 버리면 - 예컨대 페이지가 Esc 를 가로채서 이
         *   프레임만 안 꺼졌을 때 - 그 보고가 탭 전체를 도로 켠다(끄려는데 다시 켜지는
         *   진동). 몇 번 더 해 보고, 그래도 안 되면 그때는 사실대로 알린다:
         *   한쪽이 안 꺼진 채로 남는 것이, 전체가 제멋대로 켜지는 것보다 낫다. */
        if (wantMode !== null && on !== wantMode) {
            /* ★메모를 쓰는 중이면 다투지 않는다. 다시 걸지도, 반대로 알리지도 않는다 -
             *   글을 저장하거나 취소하는 순간 아래 재시도가 이어서 맞춘다. */
            if (popupBusy()) return;
            if (Date.now() < muteUntil) return;
            if (wantTries < 3) {
                wantTries++;
                muteUntil = Date.now() + 900;
                apply(wantMode);
                return;
            }
            /* ★포기하더라도 '내가 바꿨다' 로 알리지 않는다. 그렇게 알리면 프로그램이
             *   그 값을 탭의 기준으로 삼아 나머지 문서까지 따라 바꾼다 - 두 문서가
             *   서로 자기 상태를 주장하며 번갈아 뒤집혔다(실측: [꺼짐,켜짐] 에서
             *   off→on 보고가 오가며 영영 안 맞았다).
             *   대신 '참고 보고' 로 알린다 - 프로그램이 기억한 값으로 다시 걸어 준다. */
            wantMode = null;
            lastMode = on;
            lastBeat = Date.now();
            push('mode', on ? 'sync-on' : 'sync-off', []);
            if (window.console) {
                console.info('[화면주석] 이 문서의 모드를 요청대로 바꾸지 못했습니다 - 다시 시도합니다.');
            }
            return;
        }
        if (on === wantMode) wantMode = null;    // 맞춰졌다
        if (on === lastMode) {
            /* ★변한 게 없어도 5초에 한 번은 상태를 알린다. 어떤 이유로든 두 문서가
             *   어긋나면(한쪽만 켜진 채로 남으면) 사람에게는 "한쪽만 잡힌다" 로 보이는데,
             *   변화가 없으면 아무도 그 사실을 모른다. 프로그램이 이 보고를 받아
             *   탭이 기억하는 값으로 되돌린다. */
            if (Date.now() - lastBeat > 5000 && Date.now() >= muteUntil) {
                lastBeat = Date.now();
                push('mode', on ? 'sync-on' : 'sync-off', []);
            }
            return;
        }
        lastBeat = Date.now();
        /* ★프로그램이 맞춰 주는 동안에는 '판단' 자체를 미룬다. 여기서 lastMode 를
         *   적어 버리면 그 사이에 사람이 실제로 끈 것까지 삼켜, 한쪽만 꺼진 채로
         *   남는다(실측: 최상위는 꺼졌는데 iframe 은 켜진 채였다). */
        if (Date.now() < muteUntil) return;
        var first = (lastMode === null);
        lastMode = on;
        /* 방금 툴바가 붙었으면 "이 탭이 지금 주석 모드인가" 를 프로그램에 묻는다.
         * 셸이 iframe 을 갈아끼우면 새 문서는 항상 꺼진 채로 시작하기 때문이다. */
        push('mode', first ? 'ask' : (on ? 'on' : 'off'), []);
    }

    /* 프로그램이 같은 탭의 다른 문서에 모드를 맞춰 줄 때 부른다. */
    window.__qaSetMode = function (on) {
        on = !!on;
        if (!isMine() || !mounted()) return false;
        lastMode = on;                          // ★먼저 적는다 - 되받아 다시 알리지 않게
        muteUntil = Date.now() + 900;   // 켜기 재시도(320ms)와 렌더까지만 덮는다
        wantTries = 0;
        if (modeOn() === on) {
            wantMode = null;
            return true;
        }
        wantMode = on;                          // 안 걸리면 reportMode 가 다시 시도한다
        return apply(on);
    };

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
                // ★design(레이아웃의 배치 상자)도 함께 지운다 - 이걸 빼먹으면
                //   [비우기]/[추출] 뒤에도 배치 상자가 브라우저에 남아 다음 회차의
                //   레이아웃 변경 개수에 섞인다.
                else if (k && k.indexOf('agentation-design-') === 0) kill.push(k);
            }
            for (var j = 0; j < kill.length; j++) {
                localStorage.removeItem(kill[j]);
                removed++;
            }
        } catch (e) { /* 저장소 접근 불가 - 무시 */ }
        lastLayoutSig = 'null';        // 지웠으니 다음 tick 은 '변화 없음' 부터 다시 본다
        try {
            if (mounted()) {
                unmount();
                mount();
            }
        } catch (e) { /* 무시 */ }
        return removed;
    };

    /* ── 브8: 메모창에 그림을 붙인다 ─────────────────────────────
     * ★도구가 스크린샷을 찍지 않는다(README) - 사람이 무엇을 얼마나 보여줄지
     *   직접 잘라 오는 것만 붙는다(Win+Shift+S 후 Ctrl+V, 또는 파일을 끌어다 놓기).
     *   메모를 쓰는 자리에서 바로 끝나야 하므로 프로그램 창으로 옮겨 갈 필요가
     *   없다. 저장은 기존 push 통로를 그대로 쓴다(네트워크를 타지 않는다). */
    var attachMap = (typeof WeakMap !== 'undefined') ? new WeakMap() : null;
    var ATTACH_ROW_ID = '__qa_attach_row';
    var MAX_IMAGES = 5, MAX_IMAGE_BYTES = 4 * 1024 * 1024;

    // ★openPopup() 은 Esc 가드용(안 보이면 null) - 여기서는 손대지 않는다.
    //   제출 순간 팝업이 사라지는 애니메이션(opacity 감소) 중일 수 있어 그 엄격한
    //   기준을 쓰면 방금 붙인 그림을 놓친다. DOM 에 있으면 그대로 믿는다.
    function anyPopupNode() {
        try { return document.querySelector('[data-annotation-popup]'); }
        catch (e) { return null; }
    }

    function popupImages(pop) {
        if (!attachMap || !pop) return [];
        return attachMap.get(pop) || [];
    }

    function setPopupImages(pop, list) {
        if (attachMap && pop) attachMap.set(pop, list);
    }

    function renderAttachRow(pop) {
        try {
            var list = popupImages(pop);
            var row = pop.querySelector('#' + ATTACH_ROW_ID);
            if (!list.length) {
                if (row && row.parentNode) row.parentNode.removeChild(row);
                return;
            }
            if (!row) {
                row = document.createElement('div');
                row.id = ATTACH_ROW_ID;
                row.style.cssText = 'display:flex;flex-wrap:wrap;gap:4px;margin:6px 0;' +
                    'align-items:center;';
                var ta = pop.querySelector('textarea');
                if (ta && ta.parentNode) ta.parentNode.insertBefore(row, ta.nextSibling);
                else pop.appendChild(row);
            }
            row.innerHTML = '';
            var label = document.createElement('div');
            label.style.cssText = 'font:11px system-ui,sans-serif;' +
                'color:rgba(255,255,255,.7);width:100%;';
            label.textContent = '그림 ' + list.length + '장 - 확정하면 함께 저장됩니다';
            row.appendChild(label);
            list.forEach(function (img, i) {
                var wrap = document.createElement('div');
                wrap.style.cssText = 'position:relative;width:48px;height:48px;flex:none;';
                var im = document.createElement('img');
                im.src = img.data;
                im.style.cssText = 'width:48px;height:48px;object-fit:cover;' +
                    'border-radius:4px;display:block;';
                wrap.appendChild(im);
                var x = document.createElement('button');
                x.type = 'button';
                x.textContent = '×';
                x.title = '빼기';
                x.style.cssText = 'position:absolute;top:-6px;right:-6px;width:16px;' +
                    'height:16px;border-radius:50%;border:0;background:#c0392b;color:#fff;' +
                    'font:11px/16px sans-serif;cursor:pointer;padding:0;';
                x.addEventListener('click', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    var l2 = popupImages(pop).slice();
                    l2.splice(i, 1);
                    setPopupImages(pop, l2);
                    renderAttachRow(pop);
                });
                wrap.appendChild(x);
                row.appendChild(wrap);
            });
        } catch (e) { /* 그림 한 줄 때문에 도구가 서면 안 된다 */ }
    }

    function addPopupImage(pop, file) {
        if (!file || !pop || file.type.indexOf('image/') !== 0) return;
        var list = popupImages(pop);
        if (list.length >= MAX_IMAGES || file.size > MAX_IMAGE_BYTES) return;
        var reader = new FileReader();
        reader.onload = function () {
            var l2 = popupImages(pop).slice();
            l2.push({ mime: file.type, data: String(reader.result || '') });
            setPopupImages(pop, l2);
            renderAttachRow(pop);
        };
        try { reader.readAsDataURL(file); } catch (e) { /* 무시 */ }
    }

    function imageFilesOf(list) {
        var out = [];
        for (var i = 0; list && i < list.length; i++) {
            if (list[i] && list[i].type && list[i].type.indexOf('image/') === 0) {
                out.push(list[i]);
            }
        }
        return out;
    }

    function watchAttach() {
        // 글자를 붙여넣을 때는 손대지 않는다 - 그림 파일이 있을 때만 가로챈다.
        document.addEventListener('paste', function (e) {
            var pop = anyPopupNode();
            if (!pop || !pop.contains(e.target)) return;
            var imgs = imageFilesOf(e.clipboardData && e.clipboardData.files);
            if (!imgs.length) return;
            e.preventDefault();
            imgs.forEach(function (f) { addPopupImage(pop, f); });
        }, true);

        document.addEventListener('dragover', function (e) {
            var pop = anyPopupNode();
            if (pop && pop.contains(e.target)) e.preventDefault();   // 드롭을 허용한다
        }, true);

        document.addEventListener('drop', function (e) {
            var pop = anyPopupNode();
            if (!pop || !pop.contains(e.target)) return;
            var imgs = imageFilesOf(e.dataTransfer && e.dataTransfer.files);
            if (!imgs.length) return;
            e.preventDefault();
            imgs.forEach(function (f) { addPopupImage(pop, f); });
        }, true);
    }

    // 확정(add/update) 할 때 그 팝업에 쌓여 있던 그림을 함께 보낸다.
    function imagesToSend() {
        var pop = anyPopupNode();
        var imgs = pop ? popupImages(pop) : [];
        return imgs.length ? imgs : null;
    }

    /* ── 1.2: 우리 UI 를 잠시 숨긴다(캡처에 툴바·핀·메모창이 찍히지 않게) ──────
     * visibility 로 숨긴다 - display:none 은 React 레이아웃·popover 상태를 건드린다. */
    var hiddenUI = null;

    function hideUI() {
        if (hiddenUI) return;
        hiddenUI = [];
        try {
            var els = [].slice.call(document.querySelectorAll(
                '#' + HOST_ID + ', #' + TOP_ID + ', [data-agentation-root], #' + BADGE_ID +
                ', #' + MODE_ID));
            for (var i = 0; i < els.length; i++) {
                var el = els[i];
                hiddenUI.push([el, el.style.visibility, el.style.opacity]);
                el.style.visibility = 'hidden';
                el.style.opacity = '0';
            }
        } catch (e) { /* 무시 */ }
    }

    function showUI() {
        if (!hiddenUI) return;
        for (var i = 0; i < hiddenUI.length; i++) {
            try {
                hiddenUI[i][0].style.visibility = hiddenUI[i][1];
                hiddenUI[i][0].style.opacity = hiddenUI[i][2];
            } catch (e) { /* 무시 */ }
        }
        hiddenUI = null;
    }

    /* 이 문서가 최상위 문서 안에서 차지하는 자리(같은 출처 iframe 사슬을 따라 합산).
     * 교차출처가 끼면 null - 그때는 프로그램이 CDP(DOM.getFrameOwner)로 구한다. */
    function frameOffset() {
        var x = 0, y = 0, w = window;
        try {
            while (w !== w.parent) {
                var fe = w.frameElement;                 // 교차출처면 여기서 throw 또는 null
                if (!fe) return null;
                var r = fe.getBoundingClientRect();
                x += r.left + (fe.clientLeft || 0);
                y += r.top + (fe.clientTop || 0);
                w = w.parent;
            }
        } catch (e) {
            return null;
        }
        return { x: Math.round(x), y: Math.round(y) };
    }

    /* 주석이 가리키는 요소의 지금 자리(뷰포트 좌표). 경로가 유일할 때만 믿는다(pickFor 와 같다). */
    function rectOfPath(path) {
        try {
            if (!path) return null;
            var hit = document.querySelectorAll(path);
            if (hit.length !== 1) return null;
            var r = hit[0].getBoundingClientRect();
            if (!r.width && !r.height) return null;
            return { x: r.left, y: r.top, w: r.width, h: r.height };
        } catch (e) {
            return null;
        }
    }

    /* ── 1.2: 화면 단위 캡처 훅 (window.__qaCapture) ─────────────────────
     * ★주석마다 찍지 않는다. 화면(주소·해상도)당 1장을 프로그램이 [캡처 갱신] 때 찍고,
     *   이 문서는 그 화면의 항목 번호를 핀으로 겹쳐 그려 줄 뿐이다. 찍는 것은 CDP(파이썬).
     *   핀은 주석을 소유한 문서 자신에 그린다(좌표 변환 없음) - iframe 규칙과 같다. */
    var PINS_ID = '__qa_pins';
    var expanded = null;                 // iframe 높이를 늘렸을 때의 원래 값
    var savedScroll = null;              // begin() 이 맨 위로 올리기 전의 스크롤 위치

    function docSize() {
        var de = document.documentElement, b = document.body || de;
        return {
            sw: Math.max(de.scrollWidth, b.scrollWidth, de.clientWidth),
            sh: Math.max(de.scrollHeight, b.scrollHeight, de.clientHeight)
        };
    }

    window.__qaCapture = {
        where: function () {
            var s = docSize();
            return { url: location.href, vp: window.innerWidth + 'x' + window.innerHeight,
                     top: window.top === window.self, dpr: window.devicePixelRatio || 1,
                     sw: s.sw, sh: s.sh, sx: window.scrollX || 0, sy: window.scrollY || 0,
                     offset: frameOffset() };
        },
        /* pins = {aid: {label, box:{x,y,width,height}|null, fixed, path}}
         * ★실측(크롬 151, Page.captureScreenshot): clip 은 문서 좌표이고, captureBeyondViewport
         *   전체 캡처에서도 position:fixed 요소는 '지금 스크롤 위치' 에 찍힌다. 그래서 찍기 전에
         *   맨 위로 스크롤한다(고정 요소 = 문서 맨 위, 저장 좌표와 일치) - end() 가 되돌린다.
         *   핀 컨테이너는 absolute(문서 원점) 다 - fixed 로 두면 스크롤만큼 밀린다. */
        begin: function (pins) {
            this.end();
            hideUI();
            savedScroll = { x: window.scrollX || 0, y: window.scrollY || 0 };
            try { window.scrollTo(0, 0); } catch (e) { /* 무시 */ }
            var wrap = document.createElement('div');
            wrap.id = PINS_ID;
            // popover(top layer)로 올려 페이지의 z-index 위에 그린다. 안 되면 속성을 떼고 보통 요소로.
            wrap.style.cssText = 'position:absolute;left:0;top:0;inset:auto;margin:0;border:0;padding:0;' +
                'width:0;height:0;max-width:none;max-height:none;background:none;overflow:visible;' +
                'pointer-events:none;z-index:2147483647;';
            var n = 0;
            for (var aid in (pins || {})) {
                if (!Object.prototype.hasOwnProperty.call(pins, aid)) continue;
                var p = pins[aid] || {}, r = rectOfPath(p.path), x, y, w, h;
                if (r) {                                     // 지금 자리 (스크롤 0 이라 문서 좌표)
                    x = r.x; y = r.y; w = r.w; h = r.h;
                } else if (p.box && typeof p.box.x === 'number') {
                    // 저장 좌표: agentation 은 y 에 scrollY 를 더해 두고(fixed 는 뷰포트 y), x 는 뷰포트 기준.
                    // 스크롤 0 기준이면 둘 다 그대로 문서 좌표다.
                    x = p.box.x; y = p.box.y; w = p.box.width || 0; h = p.box.height || 0;
                } else {
                    continue;
                }
                var pin = document.createElement('div');
                pin.style.cssText = 'position:absolute;left:' + Math.round(x) + 'px;top:' +
                    Math.round(y) + 'px;width:' + Math.max(4, Math.round(w)) + 'px;height:' +
                    Math.max(4, Math.round(h)) + 'px;border:2px solid #e1251b;' +
                    'box-shadow:0 0 0 2px rgba(255,255,255,.85);box-sizing:border-box;';
                var badge = document.createElement('div');
                badge.textContent = String(p.label || '');
                badge.style.cssText = 'position:absolute;left:-2px;top:-24px;min-width:22px;' +
                    'padding:0 6px;height:22px;border-radius:11px;background:#e1251b;color:#fff;' +
                    'font:700 13px/22px "Malgun Gothic",system-ui,sans-serif;text-align:center;' +
                    'box-shadow:0 1px 4px rgba(0,0,0,.4);white-space:nowrap;';
                if (y < 26) badge.style.top = '2px';
                pin.appendChild(badge);
                wrap.appendChild(pin);
                n++;
            }
            (document.documentElement || document.body).appendChild(wrap);
            promote(wrap);                                   // 실패하면 promote 가 속성을 뗀다
            var s = docSize();
            return { n: n, sw: s.sw, sh: s.sh, dpr: window.devicePixelRatio || 1,
                     popover: !!(wrap.hasAttribute('popover')) };
        },
        end: function () {
            var el = document.getElementById(PINS_ID);
            if (el) {
                try { el.hidePopover(); } catch (e) { /* 무시 */ }
                if (el.parentNode) el.parentNode.removeChild(el);
            }
            if (savedScroll) {
                try { window.scrollTo(savedScroll.x, savedScroll.y); } catch (e) { /* 무시 */ }
                savedScroll = null;
            }
            showUI();
            return true;
        },
        /* iframe 문서: 같은 출처 부모라면 이 프레임의 높이를 문서 전체로 늘린다(전체 캡처용). */
        expand: function (h) {
            try {
                var fe = window.frameElement;
                if (!fe) return false;
                if (!expanded) {
                    expanded = { el: fe, height: fe.style.height, maxHeight: fe.style.maxHeight,
                                 minHeight: fe.style.minHeight };
                }
                fe.style.height = Math.ceil(h) + 'px';
                fe.style.maxHeight = 'none';
                fe.style.minHeight = '0';
                return true;
            } catch (e) {
                return false;
            }
        },
        restore: function () {
            if (!expanded) return false;
            try {
                expanded.el.style.height = expanded.height;
                expanded.el.style.maxHeight = expanded.maxHeight;
                expanded.el.style.minHeight = expanded.minHeight;
            } catch (e) { /* 무시 */ }
            expanded = null;
            return true;
        }
    };

    /* ── 1.2: 영역 잘라 붙이기 (window.__qaSnip) ─────────────────────────
     * "부분 스크린샷도 따로 붙일 수 있게" - 사람이 **영역을 골라** 자른다. 도구가 주석마다
     * 스스로 찍는 것이 아니라 Win+Shift+S 를 대신하는 것이다(브8 '사람이 붙인 그림' 과 같은
     * 자리에 저장). 이 문서는 좌표만 정하고, 찍는 것은 프로그램(CDP)이다.
     *   시작: 메모창의 ✂ 버튼(target='popup' → 확정 전 그림 목록에 쌓임) 또는
     *         보강 창 [화면에서 잘라 붙이기](target=aid → 프로그램이 곧장 첨부).
     *   조작: 드래그로 영역 · Enter 로 제안 영역(그 주석의 요소) · Esc 로 취소.
     * ★포인터 이벤트는 document 캡처 단계에서 가로챈다(guardClicks 보다 먼저 등록) -
     *   오버레이 위 드래그가 agentation 의 요소 고르기나 페이지로 새지 않게. */
    var SNIP_ID = '__qa_snip';
    var snip = null;                     // {token, target, el, box, hint, suggest, sx, sy, active}
    var snipCoolUntil = 0;               // 끝난 직후의 click 잔향을 삼킨다

    function snipRect(a, b) {
        var x = Math.min(a.x, b.x), y = Math.min(a.y, b.y);
        return { x: x, y: y, w: Math.abs(a.x - b.x), h: Math.abs(a.y - b.y) };
    }

    function drawSnip(r) {
        if (!snip || !snip.box) return;
        var b = snip.box;
        if (!r || r.w < 1 || r.h < 1) {
            // 상자가 없을 땐 오버레이 자체를 어둡게, 있을 땐 상자 그림자로 바깥만 어둡게
            b.style.display = 'none';
            snip.el.style.background = 'rgba(0,0,0,.28)';
            return;
        }
        snip.el.style.background = 'transparent';
        b.style.display = 'block';
        b.style.left = r.x + 'px';
        b.style.top = r.y + 'px';
        b.style.width = r.w + 'px';
        b.style.height = r.h + 'px';
        if (snip.size) snip.size.textContent = Math.round(r.w) + ' × ' + Math.round(r.h);
    }

    function startSnip(token, opts) {
        opts = opts || {};
        cancelSnip();
        var el = document.createElement('div');
        el.id = SNIP_ID;
        el.style.cssText = 'position:fixed;inset:0;margin:0;border:0;padding:0;width:auto;height:auto;' +
            'max-width:none;max-height:none;background:rgba(0,0,0,.28);cursor:crosshair;' +
            'z-index:2147483647;user-select:none;-webkit-user-select:none;overflow:visible;';
        var box = document.createElement('div');
        box.style.cssText = 'position:absolute;display:none;border:2px solid #e1251b;' +
            'background:rgba(255,255,255,.12);box-shadow:0 0 0 9999px rgba(0,0,0,.28);' +
            'box-sizing:border-box;pointer-events:none;';
        var size = document.createElement('div');
        size.style.cssText = 'position:absolute;right:0;bottom:-22px;background:#e1251b;color:#fff;' +
            'font:600 12px/18px "Malgun Gothic",system-ui,sans-serif;padding:0 6px;border-radius:4px;';
        box.appendChild(size);
        var hint = document.createElement('div');
        hint.style.cssText = 'position:absolute;left:50%;top:16px;transform:translateX(-50%);' +
            'background:rgba(17,17,17,.92);color:#fff;padding:8px 14px;border-radius:10px;' +
            'font:600 13px/1.5 "Malgun Gothic",system-ui,sans-serif;white-space:nowrap;' +
            'box-shadow:0 2px 10px rgba(0,0,0,.35);pointer-events:none;';
        var suggest = null;
        if (opts.suggest && opts.suggest.w > 0 && opts.suggest.h > 0) {
            suggest = { x: Math.max(0, opts.suggest.x - 8), y: Math.max(0, opts.suggest.y - 8),
                        w: Math.min(window.innerWidth, opts.suggest.w + 16),
                        h: Math.min(window.innerHeight, opts.suggest.h + 16) };
        } else if (opts.path) {
            var r = rectOfPath(opts.path);
            if (r) suggest = { x: Math.max(0, r.x - 8), y: Math.max(0, r.y - 8),
                               w: Math.min(window.innerWidth, r.w + 16),
                               h: Math.min(window.innerHeight, r.h + 16) };
        }
        hint.textContent = '드래그해서 잘라 붙일 영역을 고르세요' +
            (suggest ? '  ·  Enter: 표시된 영역 그대로' : '') + '  ·  Esc: 취소';
        el.appendChild(box);
        el.appendChild(hint);
        (document.documentElement || document.body).appendChild(el);
        snip = { token: token, target: opts.target || 'popup', el: el, box: box, size: size,
                 hint: hint, suggest: suggest, start: null, rect: null, active: true };
        if (!promote(el)) el.style.zIndex = '2147483647';
        drawSnip(suggest);                 // 제안 영역이 있으면 미리 그린다(Enter 로 확정)
        return true;
    }

    function cancelSnip() {
        if (!snip) return;
        var el = snip.el;
        try { el.hidePopover(); } catch (e) { /* 무시 */ }
        if (el && el.parentNode) el.parentNode.removeChild(el);
        snip = null;
        snipCoolUntil = Date.now() + 400;
        showUI();
    }

    function finishSnip(r) {
        if (!snip || !r || r.w < 4 || r.h < 4) return;
        var s = snip;
        snip = null;
        snipCoolUntil = Date.now() + 400;
        try { s.el.hidePopover(); } catch (e) { /* 무시 */ }
        if (s.el && s.el.parentNode) s.el.parentNode.removeChild(s.el);
        hideUI();                                       // 찍히면 안 되는 것들을 숨기고
        // 두 프레임 기다린 뒤 좌표를 보낸다(숨김이 화면에 반영될 시간).
        requestAnimationFrame(function () {
            requestAnimationFrame(function () {
                push('snip', '', [], {
                    token: s.token, target: s.target,
                    rect: { x: Math.round(r.x), y: Math.round(r.y),
                            w: Math.round(r.w), h: Math.round(r.h) },
                    sx: window.scrollX || 0, sy: window.scrollY || 0,
                    dpr: window.devicePixelRatio || 1,
                    offset: frameOffset(), top: window.top === window.self
                });
                // 프로그램이 응답하지 않아도 UI 가 영영 숨어 있지 않게
                setTimeout(function () { if (hiddenUI) showUI(); }, 8000);
            });
        });
    }

    function guardSnip() {
        ['pointerdown', 'pointermove', 'pointerup', 'mousedown', 'mouseup', 'mousemove',
         'click', 'dblclick', 'contextmenu', 'auxclick'].forEach(function (type) {
            document.addEventListener(type, function (e) {
                if (!snip) {
                    if (Date.now() < snipCoolUntil && type !== 'mousemove' && type !== 'pointermove') {
                        e.stopImmediatePropagation();
                        e.preventDefault();
                    }
                    return;
                }
                e.stopImmediatePropagation();
                e.preventDefault();
                if (type === 'pointerdown' && (e.button === 0 || e.button === undefined)) {
                    snip.start = { x: e.clientX, y: e.clientY };
                    snip.rect = null;
                    drawSnip(null);
                } else if (type === 'pointermove' && snip.start) {
                    snip.rect = snipRect(snip.start, { x: e.clientX, y: e.clientY });
                    drawSnip(snip.rect);
                } else if (type === 'pointerup' && snip.start) {
                    var r = snipRect(snip.start, { x: e.clientX, y: e.clientY });
                    snip.start = null;
                    if (r.w >= 4 && r.h >= 4) finishSnip(r);
                    else if (snip.suggest) drawSnip(snip.suggest);
                } else if (type === 'contextmenu') {
                    cancelSnip();
                }
            }, true);
        });
        document.addEventListener('keydown', function (e) {
            if (!snip) return;
            e.stopImmediatePropagation();
            e.preventDefault();
            if (e.key === 'Escape') cancelSnip();
            else if (e.key === 'Enter' && snip.suggest) finishSnip(snip.suggest);
        }, true);
    }

    /* 프로그램(보강 창)이 부른다. opts = {target: aid, path, box:{x,y,width,height}, fixed} */
    window.__qaSnip = function (token, opts) {
        if (!isMine()) return false;
        opts = opts || {};
        if (!opts.suggest && !rectOfPath(opts.path) && opts.box && typeof opts.box.x === 'number') {
            // 경로로 못 찾으면 저장 좌표를 쓴다: x 는 뷰포트 기준, y 는 문서 기준(fixed 면 뷰포트)
            var sy = window.scrollY || 0;
            opts.suggest = { x: opts.box.x, y: opts.box.y - (opts.fixed ? 0 : sy),
                             w: opts.box.width || 0, h: opts.box.height || 0 };
        }
        return startSnip(token, opts);
    };

    /* 프로그램이 찍은 결과를 돌려준다. dataUrl 은 target 이 'popup' 일 때만 온다. */
    window.__qaSnipDone = function (token, ok, dataUrl) {
        showUI();
        if (!ok || !dataUrl) return !!ok;
        var pop = anyPopupNode();
        if (!pop) return false;
        var l2 = popupImages(pop).slice();
        if (l2.length >= MAX_IMAGES) return false;
        l2.push({ mime: 'image/png', data: String(dataUrl) });
        setPopupImages(pop, l2);
        renderAttachRow(pop);
        return true;
    };

    /* 메모창 안의 ✂ 버튼. 한 노드에 한 번만 붙인다. */
    var SNIP_BTN_ID = '__qa_snip_btn';

    function ensureSnipButton() {
        try {
            var pop = anyPopupNode();
            if (!pop || pop.querySelector('#' + SNIP_BTN_ID)) return;
            var ta = pop.querySelector('textarea');
            if (!ta) return;
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.id = SNIP_BTN_ID;
            btn.textContent = '✂ 화면 잘라 붙이기';
            btn.title = '화면에서 영역을 드래그해 이 메모에 그림으로 붙입니다 (Win+Shift+S 대신)';
            btn.style.cssText = 'display:inline-block;margin:4px 0 2px;padding:2px 9px;border-radius:6px;' +
                'border:1px solid rgba(255,255,255,.35);background:rgba(255,255,255,.08);color:inherit;' +
                'font:600 11px/1.6 "Malgun Gothic",system-ui,sans-serif;cursor:pointer;';
            btn.addEventListener('click', function (e) {
                e.preventDefault();
                e.stopPropagation();
                var token = 'p' + Date.now();
                startSnip(token, { target: 'popup' });
            });
            if (ta.parentNode) ta.parentNode.insertBefore(btn, ta.nextSibling);
        } catch (e) { /* 버튼 하나 때문에 도구가 서면 안 된다 */ }
    }

    function start() {
        mount();
        lastUrl = location.href;
        announce();
        watchFrames();
        every(reportMode, 250);         // 모드가 바뀌면 같은 탭의 다른 문서에도 맞춘다
        watchTop();
        guardSnip();                    // ★guardClicks 보다 먼저 - 잘라 붙이기 드래그가 새지 않게
        guardEsc();
        guardClicks();
        watchPopup();
        watchAttach();                  // 브8: 메모창에 붙인 그림
        every(ensureSnipButton, 400);   // 1.2: 메모창의 ✂ 버튼
        window.addEventListener('resize', onResize);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start, { once: true });
    } else {
        start();
    }
})();
