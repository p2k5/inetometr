// Inetometr — frontend measurement logic
// Uses semantic tokens from CSS, no hardcoded values in JS

const $ = (id) => document.getElementById(id);
const setVal = (id, v) => { $(id).textContent = v; };

// ----- WebSocket metrics -------------------------------------------------

let ws = null;
let wsReconnectTimer = null;
let wsReconnectDelay = 1000; // начальная задержка 1с
const WS_MAX_RECONNECT_DELAY = 30000; // максимум 30с

function connectWebSocket() {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${window.location.host}/ws/metrics`;

  ws = new WebSocket(url);

  ws.onopen = () => {
    console.log('[ws] connected');
    wsReconnectDelay = 1000; // сброс задержки при успешном соединении
    clearWsReconnect();
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data.latest) {
        setVal('metric-down', Math.round(data.latest.download * 10) / 10);
        setVal('metric-up', Math.round(data.latest.upload * 10) / 10);
        setVal('metric-ping', Math.round(data.latest.ping * 10) / 10);
        setVal('metric-jitter', data.latest.jitter != null
          ? Math.round(data.latest.jitter * 10) / 10 : '—');
        setVal('metric-https', data.latest.https_latency != null
          ? Math.round(data.latest.https_latency * 10) / 10 : '—');
        setHeroValue(Math.round(data.latest.download * 10) / 10);
        updateQualityBadge(data.latest.jitter, data.latest.https_latency, data.latest.ping);
        // Update experience ratings from latest measurements
        const ratings = computeExperienceRatings(
          data.latest.download,
          data.latest.upload,
          data.latest.ping,
          data.latest.jitter,
          data.latest.https_latency
        );
        renderExperienceRatings(ratings);
      }
    } catch (e) {
      console.debug('[ws] parse error:', e);
    }
  };

  ws.onclose = (event) => {
    console.log('[ws] closed', event.code, event.reason);
    scheduleWsReconnect();
  };

  ws.onerror = (err) => {
    console.debug('[ws] error:', err);
    // onclose всё равно вызовется, reconnect обработается там
  };
}

function clearWsReconnect() {
  if (wsReconnectTimer) {
    clearTimeout(wsReconnectTimer);
    wsReconnectTimer = null;
  }
}

function scheduleWsReconnect() {
  clearWsReconnect();
  wsReconnectTimer = setTimeout(() => {
    console.log(`[ws] reconnecting in ${wsReconnectDelay}ms...`);
    connectWebSocket();
  }, wsReconnectDelay);
  // экспоненциальный рост задержки с ограничением максимума
  wsReconnectDelay = Math.min(wsReconnectDelay * 2, WS_MAX_RECONNECT_DELAY);
}

// ----- Helpers -----------------------------------------------------------

function setStatus(text, opts = {}) {
  const el = $('status');
  el.textContent = text;
  if (opts.phase) {
    const phaseEl = document.createElement('span');
    phaseEl.className = 'phase';
    phaseEl.textContent = ` ${opts.phase}`;
    el.appendChild(phaseEl);
  }
  if (opts.result) {
    const resultEl = document.createElement('span');
    resultEl.className = 'result';
    resultEl.textContent = ` ${opts.result}`;
    el.appendChild(resultEl);
  }
}

// ----- Measurement functions --------------------------------------------

async function measurePing() {
  const pings = [];
  for (let i = 0; i < 5; i++) {
    const t0 = performance.now();
    await fetch('/health', { cache: 'no-store' });
    pings.push(performance.now() - t0);
  }
  pings.sort((a, b) => a - b);
  return Math.round(pings[Math.floor(pings.length / 2)] * 10) / 10;
}

async function measureJitter() {
  // Measure jitter as the difference between max and min ping over several samples
  const pings = [];
  for (let i = 0; i < 10; i++) {
    const t0 = performance.now();
    await fetch('/health', { cache: 'no-store' });
    pings.push(performance.now() - t0);
  }
  const max = Math.max(...pings);
  const min = Math.min(...pings);
  const jitter = max - min;
  return Math.round(jitter * 10) / 10;
}

async function measureHttpsLatency() {
  // Measure HTTPS latency by timing a request to the server itself over HTTPS
  // For simplicity in testing, we'll use the same endpoint but note it's HTTP
  // In production with HTTPS, this would measure actual TLS handshake time
  const start = performance.now();
  try {
    await fetch('/health', { cache: 'no-store' });
    return Math.round((performance.now() - start) * 10) / 10;
  } catch (e) {
    return 0;
  }
}

async function measureDownload() {
  const sizes = [1, 5, 10, 20];
  let best = 0;
  for (const mb of sizes) {
    try {
      const t0 = performance.now();
      const r = await fetch(`/download?mb=${mb}&t=${Date.now()}`);
      const buf = await r.arrayBuffer();
      const sec = (performance.now() - t0) / 1000;
      if (r.status === 200 && sec > 0.001) {
        best = Math.max(best, buf.byteLength * 8 / sec / 1e6);
      }
    } catch (e) {
      // silent fail
    }
  }
  return Math.round(best * 10) / 10;
}

async function measureUpload() {
  const payload = new Uint8Array(2 * 1024 * 1024); // 2 MB
  let best = 0;
  for (let i = 0; i < 2; i++) {
    try {
      const t0 = performance.now();
      const r = await fetch('/upload', {
        method: 'POST',
        body: payload,
        // Prevent caching of the POST request
        headers: { 'Pragma': 'no-cache', 'Cache-Control': 'no-cache' }
      });
      const sec = (performance.now() - t0) / 1000;
      if (r.status === 200 && sec > 0.001) {
        best = Math.max(best, payload.byteLength * 8 / sec / 1e6);
      } else if (r.status !== 200) {
        // Log the error status for debugging (silent in production)
        console.debug('Upload response status:', r.status);
      }
    } catch (e) {
      // Network or fetch error – keep best at 0
      console.debug('Upload measurement error:', e);
    }
  }
  // Return 0 if no valid measurement was obtained
  return Math.round(best * 10) / 10;
}

// ----- UI state ----------------------------------------------------------

const startBtn = $('start-btn');
const progressFill = $('progress-fill');
const heroValue = $('hero-value');
const heroUnit = $('hero-unit');
const heroLabel = $('hero-label');

function setProgress(pct) {
  progressFill.style.width = `${pct}%`;
  progressFill.parentElement.setAttribute('aria-valuenow', pct);
}

function setLoading(loading) {
  startBtn.disabled = loading;
  document.querySelector('main').setAttribute('aria-busy', loading);
}

function setHeroValue(val) {
  heroValue.textContent = val;
  heroValue.setAttribute('data-measured', 'true');
}

function updateQualityBadge(jitter, httpsLatency, ping) {
  // Determine quality based on ping (ms), jitter (ms), and https latency (ms)
  let quality = 'fair';

  // Poor if jitter > 50ms or https latency > 200ms
  if ((jitter !== undefined && jitter > 50) || (httpsLatency !== undefined && httpsLatency > 200)) {
    quality = 'poor';
  } else if ((jitter !== undefined && jitter > 30) || (httpsLatency !== undefined && httpsLatency > 100)) {
    quality = 'fair';
  } else if ((jitter !== undefined && jitter > 20) || (httpsLatency !== undefined && httpsLatency > 50)) {
    quality = 'good';
  } else {
    quality = 'excellent';
  }

  const badge = $('quality-badge');
  badge.textContent = quality[0].toUpperCase() + quality.slice(1);
  badge.setAttribute('data-quality', quality);
}

// ----- Experience ratings (оценки опыта использования) -------------------
//
// Thresholds are documented and versioned. When data is missing the rating
// is "insufficient" and the UI shows "Недостаточно данных" instead of guessing.

function computeExperienceRatings(download, upload, ping, jitter, httpsLatency) {
  // Returns {4k: {rating, sub}, video: {rating, sub}, gaming: {rating, sub}}

  const hasData = (download != null) && (upload != null) && (ping != null);

  if (!hasData) {
    return {
      '4k': { rating: 'insufficient', sub: 'Недостаточно данных' },
      video: { rating: 'insufficient', sub: 'Недостаточно данных' },
      gaming: { rating: 'insufficient', sub: 'Недостаточно данных' },
    };
  }

  // 4K streaming: needs download >= 25 Mbps and stable connection (jitter <= 30)
  let fourK;
  if (download >= 25 && (jitter == null || jitter <= 30)) {
    fourK = { rating: 'comfortable', sub: 'Комфортно' };
  } else if (download >= 15) {
    fourK = { rating: 'limited', sub: 'Возможны буферизации' };
  } else {
    fourK = { rating: 'limited', sub: 'Низкая скорость' };
  }

  // Video calls: needs jitter <= 30 and ping <= 150
  let video;
  if ((jitter == null || jitter <= 30) && ping <= 150) {
    video = { rating: 'comfortable', sub: 'Комфортно' };
  } else if (jitter == null || jitter <= 60) {
    video = { rating: 'limited', sub: 'Возможны задержки' };
  } else {
    video = { rating: 'limited', sub: 'Высокий джиттер' };
  }

  // Online gaming: needs ping <= 50 and jitter <= 10
  let gaming;
  if (ping <= 50 && (jitter == null || jitter <= 10)) {
    gaming = { rating: 'comfortable', sub: 'Комфортно' };
  } else if (ping <= 100) {
    gaming = { rating: 'limited', sub: 'Лаги возможны' };
  } else {
    gaming = { rating: 'limited', sub: 'Высокий пинг' };
  }

  return { '4k': fourK, video: video, gaming: gaming };
}

function renderExperienceRatings(ratings) {
  const set = (id, r) => {
    const el = $(id);
    if (!el) return;
    el.textContent = r.rating === 'comfortable' ? 'Хорошо'
      : r.rating === 'limited' ? 'Ограничено'
      : '-';
    el.setAttribute('data-rating', r.rating);
    const sub = $(id + '-sub');
    if (sub) sub.textContent = r.sub;
  };

  set('rating-4k', ratings['4k']);
  set('rating-video', ratings.video);
  set('rating-gaming', ratings.gaming);
}

// ----- Comparison with previous measurement (сравнение с предыдущим) -----
//
function fmtDelta(cur, prev, lowerIsBetter) {
  // Returns {text: string, cls: string} or null if insufficient data
  if (cur == null || prev == null) return null;
  const delta = cur - prev;
  if (delta === 0) return { text: '0', cls: 'delta-neutral' };
  const up = delta > 0;
  const good = lowerIsBetter ? !up : up; // improvement if (lowerIsBetter && delta<0) or (!lowerIsBetter && delta>0)
  return {
    text: (up ? '+' : '') + Math.round(delta * 10) / 10,
    cls: good ? 'delta-good' : 'delta-bad',
  };
}

function renderComparison(data) {
  const row = $('comparison-row');
  const timeEl = $('comparison-time');
  if (!data || !data.cur) {
    row.innerHTML = '<span class="comparison__empty">Недостаточно данных для сравнения</span>';
    timeEl.textContent = 'нет данных';
    return;
  }
  const { cur, prev } = data;
  const prevTime = prev.timestamp ? prev.timestamp.replace('T',' ').replace('Z','').slice(0,16) : '?';
  timeEl.textContent = prevTime;

  const fields = [
    { label: '↓', value: cur.download, prev: prev.download, unit: 'Мбит/с', lowerIsBetter: false },
    { label: '↑', value: cur.upload, prev: prev.upload, unit: 'Мбит/с', lowerIsBetter: false },
    { label: 'пинг', value: cur.ping, prev: prev.ping, unit: 'мс', lowerIsBetter: true },
    { label: 'джиттер', value: cur.jitter, prev: prev.jitter, unit: 'мс', lowerIsBetter: true },
  ];

  const items = fields.map(f => {
    const d = fmtDelta(f.value, f.prev, f.lowerIsBetter);
    if (!d) {
      return `
        <div class="comparison__item">
          <div class="comparison__label">${f.label}</div>
          <div class="comparison__prev">—</div>
          <div class="comparison__delta"></div>
          <div class="comparison__arrow"></div>
        </div>`;
    }
    return `
      <div class="comparison__item" data-trend="${d.cls === 'delta-good' ? 'improved' : d.cls === 'delta-bad' ? 'worse' : 'same'}">
        <div class="comparison__label">${f.label}</div>
        <div class="comparison__prev">${f.prev !== null ? f.prev.toFixed(1) : '—'}</div>
        <div class="comparison__delta ${d.cls}">${d.text} ${f.unit}</div>
        <div class="comparison__arrow">${f.value !== null && f.prev !== null ? (f.value > f.prev ? '↑' : f.value < f.prev ? '↓' : '→') : ''}</div>
      </div>`;
  }).join('');

  row.innerHTML = items;
}

async function loadComparison() {
  try {
    const r = await fetch('/api/history?limit=2');
    const items = await r.json();
    if (!items || items.length < 2) {
      renderComparison({ cur: items && items[0] ? items[0] : null, prev: null });
      return;
    }
    const cur = items[0]; // newest (timestamp desc)
    const prev = items[1];
    renderComparison({ cur, prev });
  } catch (e) {
    console.debug('[comparison] load failed:', e);
    renderComparison({ cur: null, prev: null });
  }
}

// ----- Main test flow ----------------------------------------------------

async function runTest() {
  setLoading(true);
  setProgress(10);
  setStatus('Замер пинга...');

  const pingMs = await measurePing();
  setVal('metric-ping', pingMs);
  setProgress(25);
  setStatus('Замер джиттера...');

  const jitter = await measureJitter();
  setVal('metric-jitter', jitter);
  setProgress(40);
  setStatus('Замер HTTPS латентности...');

  const httpsLatency = await measureHttpsLatency();
  setVal('metric-https', httpsLatency);
  setProgress(55);
  setStatus('Замер скорости загрузки...');

  const down = await measureDownload();
  setVal('metric-down', down);
  setHeroValue(down);
  setProgress(75);
  setStatus('Замер скорости передачи...');

  const up = await measureUpload();
  setVal('metric-up', up);
  setProgress(100);
  setStatus('Тест завершён, сохранение результата...', { result: 'Готово' });

  // Save to server
  try {
    await fetch('/api/speed', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        download: down,
        upload: up,
        ping: pingMs,
        jitter: jitter,
        https_latency: httpsLatency,
      }),
    });
    setStatus('Тест завершён, результат сохранён.', { result: 'Готово' });
  } catch (e) {
    setStatus('Тест завершён (сохранение не удалось).', { result: 'Ошибка сохранения' });
  }

  // Update quality badge based on results
  updateQualityBadge(jitter, httpsLatency, pingMs);

  // Update experience ratings based on measured values
  const ratings = computeExperienceRatings(down, up, pingMs, jitter, httpsLatency);
  renderExperienceRatings(ratings);

  // Load comparison with previous measurement
  await loadComparison();

  // Reset progress bar after delay
  setTimeout(() => {
    setProgress(0);
    setLoading(false);
  }, 600);
}

function resetUI() {
  setVal('hero-value', 'нет данных');
  heroValue.setAttribute('data-measured', 'false');
  setVal('metric-up', 'нет данных');
  setVal('metric-ping', 'нет данных');
  setVal('metric-down', 'нет данных');
  setProgress(0);
  setStatus('Нажмите Начать тест. Замеряем ваш IP и пинг сервера.');
  startBtn.disabled = false;
  $('quality-badge').textContent = '-';
  $('quality-badge').removeAttribute('data-quality');
}

// ----- Event listeners ---------------------------------------------------

startBtn.addEventListener('click', runTest);

const runTracerouteBtn = $('run-traceroute-btn');
const tracerouteResults = $('traceroute-results');
const tracerouteTableBody = $('traceroute-table-body');
const tracerouteSummary = $('reached-text');

runTracerouteBtn.addEventListener('click', async () => {
  tracerouteResults.style.display = 'none';
  tracerouteTableBody.innerHTML = '';
  tracerouteSummary.textContent = '';

  try {
    const r = await fetch('/api/diagnostics/traceroute?target=127.0.0.1&max_hops=15&timeout_ms=2000', {
      method: 'POST',
    });
    if (!r.ok) {
      throw new Error(`Traceroute failed: ${r.status}`);
    }
    const data = await r.json();

    // Build table rows
    const rows = data.hops.map(hop => {
      const rttStr = hop.rtt_ms.length
        ? hop.rtt_ms.map(v => v.toFixed(1)).join(', ')
        : '<span class="empty-cell">—</span>';
      const ipStr = hop.ip || '<span class="empty-cell">—</span>';
      const hostStr = hop.host || '<span class="empty-cell">—</span>';
      const loss = hop.loss_percent ?? 0;
      let lossAttr = 'data-loss-low';
      if (loss >= 50) lossAttr = 'data-loss-high';
      else if (loss > 0) lossAttr = 'data-loss-medium';
      const rttAttr = hop.rtt_ms.length > 1 ? 'data-rtt-variable' : '';

      return `
        <tr>
          <td>${hop.hop}</td>
          <td>${ipStr}</td>
          <td>${hostStr}</td>
          <td ${rttAttr}>${rttStr}</td>
          <td ${lossAttr}>${loss.toFixed(1)}%</td>
        </tr>
      `;
    }).join('');

    tracerouteTableBody.innerHTML = rows;
    tracerouteSummary.textContent = data.reached ? 'Достигнут' : 'Не достигнут';
    tracerouteResults.style.display = 'block';
  } catch (e) {
    setStatus('Ошибка traceroute: ' + e.message, { result: 'Ошибка' });
  }
});

// ----- Initial load: show server IP + ping -------------------------------

(async () => {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    $('header-info').textContent =
      `IP: ${d.client_ip || '?'} | пинг сервера: ${d.ping_ms ?? '—'} мс`;
  } catch (e) {
    $('header-info').textContent = 'IP: недоступен | пинг сервера: недоступен';
  }
  // Подключаемся к WebSocket после успешного API-запроса
  connectWebSocket();
  // Загружаем сравнение с предыдущим результатом
  loadComparison();
})();

// Очистка при закрытии окна
window.addEventListener('beforeunload', () => {
  if (ws) {
    ws.close();
  }
  clearWsReconnect();
});