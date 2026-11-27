/*
 * PostHog 방문/행동 분석 로더.
 * - 키는 코드에 두지 않고 서버의 /api/config가 POSTHOG_KEY 환경변수에서 내려준다.
 *   키가 없으면(로컬 개발, 테스트) 아무것도 불러오지 않고 dmTrack은 무시된다.
 * - 셀러 페이지 URL에는 ?token=이 붙으므로, 전송 전에 모든 문자열에서 token 값을 지운다.
 * - 페이지에서는 dmTrack('이벤트명', { ...속성 })만 호출하면 된다. SDK 로딩 전 호출은 큐에 쌓였다가 보내진다.
 */
(function () {
  var queue = [];
  var state = 'loading'; // loading | ready | off

  window.dmTrack = function (event, props) {
    if (state === 'ready' && window.posthog) {
      window.posthog.capture(event, props || {});
    } else if (state === 'loading' && queue.length < 50) {
      queue.push([event, props || {}]);
    }
  };

  var TOKEN_PARAM = /([?&#](?:buyer_token|token)=)[^&#\s"']*/gi;

  function scrub(value) {
    if (typeof value === 'string') return value.replace(TOKEN_PARAM, '$1[redacted]');
    if (Array.isArray(value)) return value.map(scrub);
    if (value && typeof value === 'object') {
      var out = {};
      for (var k in value) {
        if (Object.prototype.hasOwnProperty.call(value, k)) out[k] = scrub(value[k]);
      }
      return out;
    }
    return value;
  }

  function scrubEvent(event) {
    if (!event) return event;
    if (event.properties) event.properties = scrub(event.properties);
    if (event.$set) event.$set = scrub(event.$set);
    if (event.$set_once) event.$set_once = scrub(event.$set_once);
    return event;
  }

  function turnOff() {
    state = 'off';
    queue = [];
  }

  fetch('/api/config')
    .then(function (res) { return res.ok ? res.json() : {}; })
    .then(function (cfg) {
      if (!cfg || !cfg.posthog_key) return turnOff();
      var host = cfg.posthog_host;
      var script = document.createElement('script');
      script.async = true;
      script.crossOrigin = 'anonymous';
      script.src = host.replace('.i.posthog.com', '-assets.i.posthog.com') + '/static/array.js';
      script.onerror = turnOff;
      script.onload = function () {
        if (!window.posthog || typeof window.posthog.init !== 'function') return turnOff();
        window.posthog.init(cfg.posthog_key, {
          api_host: host,
          defaults: '2026-01-30',
          person_profiles: 'identified_only',
          disable_session_recording: true,
          before_send: scrubEvent
        });
        state = 'ready';
        queue.forEach(function (e) { window.posthog.capture(e[0], e[1]); });
        queue = [];
      };
      document.head.appendChild(script);
    })
    .catch(turnOff);
})();
