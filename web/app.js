'use strict';
/* HomeRepo — интерфейс панели (без сборки и внешних библиотек) */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const S = { me: null, timers: [], acts: {}, on: {}, assignPre: null, up: [], upBusy: false };

const SHIELD = (w = 26, h = 30) => `<svg class="logo" width="${w}" height="${h}" viewBox="0 0 84 96" aria-hidden="true"><path d="M42 6 74 18v26c0 22-14 38-32 46C24 82 10 66 10 44V18z" fill="none" stroke="currentColor" stroke-width="8" stroke-linejoin="round"/><path d="M42 14v68C27 74 18 61 18 44V23z" fill="currentColor"/></svg>`;

/* ------------------------------------------------------------------ тема */
function curTheme() { return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'; }
function applyTheme(t) { document.documentElement.dataset.theme = t; $$('[data-act=theme]').forEach((b) => { b.textContent = t === 'light' ? '☾' : '☀'; b.title = t === 'light' ? 'тёмная тема' : 'светлая тема'; }); }
function toggleTheme() { const t = curTheme() === 'light' ? 'dark' : 'light'; applyTheme(t); try { localStorage.setItem('hr-theme', t); } catch (e) { } }
(function () {
  let t = null;
  try { t = localStorage.getItem('hr-theme'); } catch (e) { }
  if (!t) t = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  document.documentElement.dataset.theme = t;
})();
const themeBtn = () => `<button class="theme-btn" data-act="theme" title="${curTheme() === 'light' ? 'тёмная тема' : 'светлая тема'}">${curTheme() === 'light' ? '☾' : '☀'}</button>`;

/* ------------------------------------------------------------------ утилиты */
class ApiError extends Error { constructor(status, msg, data) { super(msg); this.status = status; this.data = data; } }

async function api(method, path, body, raw) {
  const h = {};
  if (S.me && S.me.csrf) h['X-CSRF'] = S.me.csrf;
  let b;
  if (raw !== undefined) { b = raw; h['Content-Type'] = 'application/octet-stream'; }
  else if (body !== undefined) { b = JSON.stringify(body); h['Content-Type'] = 'application/json'; }
  let r;
  try { r = await fetch(path, { method, headers: h, body: b, credentials: 'same-origin' }); }
  catch (e) { throw new ApiError(0, 'нет связи с сервером'); }
  let data = {};
  try { data = await r.json(); } catch (e) { data = {}; }
  if (r.status === 401 && path !== '/api/login') { const first = !S.me; S.me = null; showLogin(first ? '' : data.error); throw new ApiError(401, data.error || 'требуется вход'); }
  if (!r.ok) throw new ApiError(r.status, data.error || ('ошибка ' + r.status), data);
  return data;
}
const GET = (p) => api('GET', p);
const POST = (p, b) => api('POST', p, b || {});

function toast(msg, kind = 'ok', ms = 4500) {
  const d = document.createElement('div');
  d.className = kind; d.textContent = msg;
  $('#toast').appendChild(d);
  setTimeout(() => d.remove(), ms);
}
function fail(e) { if (e && e.status !== 401) toast(e.message || String(e), 'err', 7000); }

function fmtSize(n) {
  n = Number(n || 0);
  if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(1).replace('.', ',') + ' ГБ';
  if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(n >= 100 * 1024 ** 2 ? 0 : 1).replace('.', ',') + ' МБ';
  if (n >= 1024) return Math.round(n / 1024) + ' КБ';
  return n + ' Б';
}
const pad = (x) => String(x).padStart(2, '0');
function fmtDate(ts) { if (!ts) return '—'; const d = new Date(ts * 1000); return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}`; }
function fmtDT(ts) { if (!ts) return '—'; const d = new Date(ts * 1000); return `${fmtDate(ts)} ${pad(d.getHours())}:${pad(d.getMinutes())}`; }
function ago(ts) {
  if (!ts) return 'никогда';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return 'только что';
  if (s < 3600) return Math.floor(s / 60) + ' мин назад';
  if (s < 86400) return Math.floor(s / 3600) + ' ч назад';
  return Math.floor(s / 86400) + ' дн назад';
}
const canOp = () => S.me && (S.me.role === 'operator' || S.me.role === 'admin');
const isAdmin = () => S.me && S.me.role === 'admin';
const ROLE_RU = { viewer: 'наблюдатель', operator: 'оператор', admin: 'администратор' };

function every(ms, fn) { const t = setInterval(() => { if (!document.hidden) fn(); }, ms); S.timers.push(t); }

function modal(html, narrow, onClose) {
  const m = document.createElement('div');
  m.className = 'modal';
  m.innerHTML = `<div class="dlg${narrow ? ' narrow' : ''}" role="dialog" aria-modal="true">${html}</div>`;
  document.body.appendChild(m);
  let closed = false, downOverlay = false;
  const close = () => { if (closed) return; closed = true; m.remove(); document.removeEventListener('keydown', onKey); if (onClose) onClose(); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  // выделение текста мышью в поле, отпущенное за краем окна, не должно закрывать диалог
  m.addEventListener('mousedown', (e) => { downOverlay = e.target === m; });
  m.addEventListener('click', (e) => {
    if ((e.target === m && downOverlay) || e.target.closest('[data-close]')) close();
    downOverlay = false;
  });
  const f = m.querySelector('input,select,textarea,button.p');
  if (f) setTimeout(() => f.focus(), 30);
  return { el: m, close };
}
function confirmDlg(title, text, ok = 'Подтвердить', danger = false) {
  return new Promise((res) => {
    let answer = false;
    const m = modal(`<h2 class="t">${esc(title)}</h2><div class="note" style="font-size:13px;color:var(--text2)">${text}</div>
      <div class="row" style="justify-content:flex-end"><button class="btn" data-close>Отмена</button>
      <button class="btn ${danger ? 'd' : 'p'}" id="cfok">${esc(ok)}</button></div>`, true, () => res(answer));
    m.el.querySelector('#cfok').onclick = () => { answer = true; m.close(); };
  });
}
function copyText(t) {
  if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(t).then(() => toast('скопировано')); return; }
  const ta = document.createElement('textarea'); ta.value = t; document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); toast('скопировано'); } catch (e) { toast('не удалось скопировать', 'err'); }
  ta.remove();
}

/* делегирование событий: data-act (клик), data-ch (change), data-in (input) */
document.addEventListener('click', (e) => {
  const a = e.target.closest('[data-act]');
  if (!a) return;
  const fn = S.acts[a.dataset.act];
  if (fn) { if (a.tagName === 'A' && !a.getAttribute('href')?.startsWith('#/')) e.preventDefault(); fn(a, e); }
});
document.addEventListener('change', (e) => { const a = e.target.closest('[data-ch]'); if (a && S.on[a.dataset.ch]) S.on[a.dataset.ch](a, e); });
document.addEventListener('input', (e) => { const a = e.target.closest('[data-in]'); if (a && S.on[a.dataset.in]) S.on[a.dataset.in](a, e); });

/* ------------------------------------------------------------------ вход */
function showLogin(err, mustChange) {
  S.timers.forEach(clearInterval); S.timers = [];
  S.acts = { theme: toggleTheme }; S.on = {};
  const app = $('#app');
  const form = mustChange ? `
      <form id="pwform" autocomplete="off">
        <div style="display:flex;flex-direction:column;align-items:center;gap:12px">${SHIELD(70, 80)}<h1>HomeRepo</h1>
        <p class="muted" style="margin:0">Смена пароля для ${esc(S.me ? S.me.user : '')}</p></div>
        <div class="hint">Пароль, выданный администратором, нужно сменить. Не короче 10 символов, без имени пользователя; пароли старой панели не принимаются.</div>
        ${err ? `<div class="err">${esc(err)}</div>` : ''}
        <label>Текущий пароль<input class="inp" type="password" name="old" required autocomplete="current-password"></label>
        <label>Новый пароль<input class="inp" type="password" name="new" required autocomplete="new-password" minlength="10"></label>
        <label>Повторите<input class="inp" type="password" name="new2" required autocomplete="new-password"></label>
        <button class="btn p" style="min-height:50px;justify-content:center;font-size:15px">Сменить пароль</button>
        <button type="button" class="btn link" id="lgout">выйти</button>
      </form>` : `
      <form id="lform">
        <div style="display:flex;flex-direction:column;align-items:center;gap:12px">${SHIELD(80, 92)}<h1>HomeRepo</h1>
        <p class="muted" style="margin:0">Репозиторий пакетов и устройства</p></div>
        ${err ? `<div class="err">${esc(err)}</div>` : ''}
        <label>Имя пользователя<input class="inp" name="user" required autocomplete="username" autofocus></label>
        <label>Пароль<input class="inp" type="password" name="password" required autocomplete="current-password"></label>
        <button class="btn p" style="min-height:50px;justify-content:center;font-size:15px">Войти</button>
        <div class="hint">Учётную запись выдаёт администратор панели. Первый вход — с временным паролем, его нужно сменить.</div>
      </form>`;
  app.innerHTML = `<div class="login"><div class="in">${form}
      <section>
        <div><div class="row"><h2 class="grow" style="font-size:26px;font-weight:800;letter-spacing:.08em;color:var(--cyan)">ЗАЩИТА</h2>${themeBtn()}</div>
        <p class="muted small" style="margin:8px 0 0">HomeRepo · домашний RPM-репозиторий</p></div>
        <div class="prot"><h3>КАК УСТРОЕНО</h3><ul>
          <li>Пароли хранятся как scrypt-хеши в базе вне веб-каталога</li>
          <li>5 неверных попыток — вход блокируется на 15 минут</li>
          <li>Ключ подписи хранит отдельная служба homerepo-signer — у панели к нему доступа нет</li>
          <li>SSH-пароли для установки агентов используются один раз и нигде не сохраняются</li>
          <li>Панель не работает от root и не использует sudo</li>
          <li>Все действия записываются в журнал: кто, что, когда, откуда</li>
        </ul></div>
        <p class="note">Роли: наблюдатель — только просмотр; оператор — пакеты, подпись, назначения; администратор — ещё и пользователи.</p>
      </section></div></div>`;
  if (mustChange) {
    $('#lgout').onclick = async () => { try { await POST('/api/logout'); } catch (e) { } S.me = null; showLogin(); };
    $('#pwform').onsubmit = async (e) => {
      e.preventDefault();
      const f = e.target;
      if (f.new.value !== f.new2.value) return showLogin('новые пароли не совпадают', true);
      try { await POST('/api/password', { old: f.old.value, new: f.new.value }); toast('пароль изменён'); boot(); }
      catch (er) { if (er.status !== 401) showLogin(er.message, true); }
    };
    return;
  }
  $('#lform').onsubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    try { await api('POST', '/api/login', { user: f.user.value, password: f.password.value }); boot(); }
    catch (er) { showLogin(er.message); }
  };
}

/* ------------------------------------------------------------------ каркас */
const NAV = [['packages', 'Пакеты'], ['signing', 'Подпись'], ['devices', 'Устройства'], ['scanner', 'Сканер'], ['assign', 'Назначения'], ['connect', 'Подключение'], ['log', 'Журнал'], ['users', 'Пользователи']];

function layout(active) {
  const nav = NAV.filter(([k]) => k !== 'users' || isAdmin()).map(([k, t]) => `<a href="#/${k}" class="${active === k ? 'on' : ''}">${t}</a>`).join('');
  $('#app').innerHTML = `<header class="top">
      <a class="brand" href="#/packages">${SHIELD()}HomeRepo</a>
      <nav class="nav">${nav}</nav>
      <div class="who"><button class="errbtn hidden" id="errbtn" data-act="errors" title="ошибки">⚠ <b>0</b></button>${themeBtn()}<b>${esc(S.me.user)}</b><span class="role">${esc(ROLE_RU[S.me.role] || S.me.role)}</span>
        <a href="#/account">пароль</a><a href="#" data-act="logout">выйти</a></div>
    </header><main class="wrap" id="main"><div class="empty">загрузка…</div></main>`;
  return $('#main');
}

async function boot() {
  try { S.me = await GET('/api/me'); }
  catch (e) { if (e.status !== 401) $('#app').innerHTML = `<div class="login"><div class="err">${esc(e.message)}</div></div>`; return; }
  if (S.me.must_change) return showLogin('', true);
  route();
}
window.addEventListener('hashchange', route);

function route() {
  if (!S.me || S.me.must_change) return;
  S.timers.forEach(clearInterval); S.timers = []; S.acts = { logout: doLogout, theme: toggleTheme, errors: showErrors }; S.on = {};
  const h = (location.hash || '#/packages').slice(2).split('/');
  const v = VIEWS[h[0]] || VIEWS.packages;
  const main = layout(VIEWS[h[0]] ? (h[0] === 'device' ? 'devices' : h[0]) : 'packages');
  v(main, h[1]).catch((e) => { fail(e); main.innerHTML = `<div class="alert bad">${esc(e.message)}</div>`; });
  refreshErr();
  every(30000, refreshErr);
}

/* ------------------------------------------------------------------ центр ошибок */
async function refreshErr() {
  const b = $('#errbtn');
  if (!b) return;
  try {
    const r = await GET('/api/errors?count=1');
    b.querySelector('b').textContent = r.count;
    b.classList.toggle('hidden', !r.count);
    b.title = r.count ? `ошибок: ${r.count} — открыть` : 'ошибок нет';
  } catch (e) { }
}
async function showErrors() {
  let d;
  const m = modal('<div class="empty">загрузка…</div>', false, refreshErr);
  m.el.querySelector('.dlg').classList.add('wide');
  async function load() { d = await GET('/api/errors'); draw(); }
  const item = (kind, x, title, sub, text, extra) => `<div class="erritem">
      <div class="jh"><b>${title}</b><span class="nowrap muted small">${fmtDT(x.ts)}</span></div>
      <div class="note">${sub}</div>
      ${text ? `<pre class="out">${esc(text)}</pre>` : ''}
      ${x.hint ? `<div class="note c-hb">→ ${esc(x.hint)}</div>` : ''}
      ${canOp() ? `<div class="row" style="justify-content:flex-end">${extra || ''}<button class="btn s" data-e="ack" data-k="${kind}" data-id="${x.id}">Скрыть</button></div>` : ''}</div>`;
  function draw() {
    const n = d.system.length + d.jobs.length + d.tasks.length;
    m.el.querySelector('.dlg').innerHTML = `<div class="row"><h2 class="t grow">Ошибки · ${n}</h2>
        <a class="btn s" href="/api/report/errors.html" target="_blank" rel="noopener">Отчёт HTML / PDF</a>
        ${canOp() && (d.jobs.length || d.tasks.length) ? '<button class="btn s" data-e="ackall">Скрыть все</button>' : ''}
        <button class="btn s" data-close>Закрыть</button></div>
      ${!n ? '<div class="alert ok"><div>Ошибок нет — всё работает.</div></div>' : ''}
      ${d.system.length ? `<div class="lbl">Службы</div>${d.system.map((x) => `<div class="erritem"><div class="jh"><b class="c-bad">${esc(x.title)}</b></div><div class="note">${esc(x.message)}</div><div class="note c-hb">→ ${esc(x.hint)}</div></div>`).join('')}` : ''}
      ${d.jobs.length ? `<div class="lbl">Задания службы подписи и панели · ${d.jobs.length}</div>${d.jobs.map((x) => item('jobs', x, esc(x.title || x.kind),
        `${esc(x.kind)} · ${esc(x.who || '')} · #${x.id}`, x.message + (x.log ? '\n' + x.log.slice(-1500) : ''),
        x.retry && canOp() ? `<button class="btn s p" data-e="jretry" data-id="${x.id}">Повторить</button>` : '')).join('')}` : ''}
      ${d.tasks.length ? `<div class="lbl">Назначения на устройствах · ${d.tasks.length}</div>${d.tasks.map((x) => item('tasks', x,
        `${esc(x.host)}: ${esc(x.action)} ${esc(x.name)} ${esc(x.evr)}`, `<a href="#/device/${x.host_id}" data-close>карточка устройства</a> · ${esc(x.who || '')} · #${x.id}`,
        x.output.slice(-1500), canOp() ? `<button class="btn s p" data-e="tretry" data-id="${x.id}">Повторить</button>` : '')).join('')}` : ''}
      <div class="note">Скрытая ошибка больше не показывается, но остаётся в отчёте «все ошибки» (<a href="/api/report/errors.html?all=1" target="_blank" rel="noopener">открыть</a>). Повтор создаёт новое задание; если оно пройдёт, ошибка исчезнет сама.</div>`;
  }
  m.el.addEventListener('click', async (ev) => {
    const b = ev.target.closest('[data-e]');
    if (!b) return;
    const id = +b.dataset.id;
    try {
      if (b.dataset.e === 'ack') await POST('/api/errors/ack', { [b.dataset.k]: [id] });
      if (b.dataset.e === 'ackall') await POST('/api/errors/ack', { all: true });
      if (b.dataset.e === 'jretry') { const r = await POST(`/api/jobs/${id}/retry`); toast(`задание повторено (#${r.job})`); }
      if (b.dataset.e === 'tretry') { await POST(`/api/tasks/${id}/retry`); await POST('/api/errors/ack', { tasks: [id] }); toast('назначение повторено'); }
      await load(); refreshErr();
    } catch (e) { fail(e); }
  });
  try { await load(); } catch (e) { fail(e); m.close(); }
}
async function doLogout() { try { await POST('/api/logout'); } catch (e) { } S.me = null; location.hash = ''; showLogin(); }

/* ------------------------------------------------------------------ пакеты */
const SIG = {
  ours: '<span class="tag ok">HomeRepo</span>',
  foreign: (k) => `<span class="tag warn">чужая · ${esc((k || '').slice(-8))}</span>`,
  none: '<span class="tag bad">без подписи</span>',
};
function sigTag(p) { return p.sigclass === 'ours' ? SIG.ours : p.sigclass === 'foreign' ? SIG.foreign(p.sigkey) : SIG.none; }

function signerAlert(sm) {
  if (sm.signer && !sm.signer.alive) return `<div class="alert bad"><div><b>Служба подписи не отвечает.</b> Задания встанут в очередь и выполнятся после запуска: <code>systemctl status homerepo-signer</code></div></div>`;
  if (sm.key && sm.key.error) return `<div class="alert bad"><div><b>Ключ подписи недоступен:</b> ${esc(sm.key.error)}</div></div>`;
  return '';
}

async function vPackages(main) {
  const st = { q: '', arch: '', branch: '', sig: '', all: false, sel: new Set(), list: [], sm: null };
  async function load() {
    const [sm, pk] = await Promise.all([GET('/api/summary'), GET('/api/packages')]);
    st.sm = sm; st.list = pk.packages;
    const ids = new Set(st.list.map((p) => p.id));
    st.sel.forEach((i) => { if (!ids.has(i)) st.sel.delete(i); });
  }
  await load();
  function visible() {
    const q = st.q.trim().toLowerCase();
    return st.list.filter((p) => (st.all || p.latest)
      && (!q || p.name.toLowerCase().includes(q) || p.filename.toLowerCase().includes(q) || (p.summary || '').toLowerCase().includes(q))
      && (!st.arch || (st.arch === 'noarch' ? p.arch === 'noarch' : p.arches.split(' ').includes(st.arch)))
      && (!st.branch || p.branch === st.branch) && (!st.sig || p.sigclass === st.sig));
  }
  function draw() {
    const sm = st.sm;
    const bad = sm.sig.foreign + sm.sig.none;
    const badList = st.list.filter((p) => p.sigclass !== 'ours');
    const pa = Object.entries(sm.per_arch).map(([a, n]) => `${a} ${n}`).join(' · ');
    const opts = (arr, cur, all) => `<option value="">${all}</option>` + arr.map((x) => `<option ${x === cur ? 'selected' : ''}>${esc(x)}</option>`).join('');
    main.innerHTML = `
      ${signerAlert(sm)}
      <div class="tiles">
        <div class="tile"><b>${sm.packages}</b><span>пакетов</span></div>
        <div class="tile click" data-act="fsig" data-v="ours"><b>${sm.sig.ours}</b><span>ключом HomeRepo</span></div>
        <div class="tile click" data-act="fsig" data-v="foreign"><b style="color:var(--warn)">${sm.sig.foreign}</b><span>чужая подпись</span></div>
        <div class="tile click" data-act="fsig" data-v="none"><b style="color:${sm.sig.none ? 'var(--bad)' : 'var(--cyan)'}">${sm.sig.none}</b><span>без подписи</span></div>
        <div class="tile"><b>${fmtSize(sm.size)}</b><span>${esc(pa)}</span></div>
      </div>
      ${bad ? `<div class="alert warn"><div><b>${bad} ${bad === 1 ? 'пакет подписан' : 'пакета(ов) подписаны'} не ключом HomeRepo</b> — ${badList.slice(0, 5).map((p) => esc(p.name + ' ' + p.version)).join(', ')}${badList.length > 5 ? ' и др.' : ''}. Клиенты с gpgcheck=1 их не установят.</div>
        ${canOp() ? `<button class="btn w" data-act="resignall">Переподписать ${bad === 1 ? '' : 'все ' + bad}</button>` : ''}</div>` : ''}
      ${sm.junk.count ? `<div class="alert info"><div>В каталогах репозитория ${sm.junk.count} мусорных файлов/каталогов (${fmtSize(sm.junk.size)}) — остатки старой системы. <a href="#/signing">подробнее</a></div></div>` : ''}
      <section class="panel">
        <div class="bar">
          <input class="inp" style="flex:1 1 220px" placeholder="Поиск: имя, версия, файл" value="${esc(st.q)}" data-in="q">
          <select class="sel" data-ch="arch">${opts([...S.me.arches, 'noarch'], st.arch, 'Все архитектуры')}</select>
          <select class="sel" data-ch="branch">${opts(S.me.branches, st.branch, 'Все ветки')}</select>
          <select class="sel" data-ch="sig"><option value="">Любая подпись</option>
            ${[['ours', 'homerepo'], ['foreign', 'чужая'], ['none', 'без подписи']].map(([v, t]) => `<option value="${v}" ${st.sig === v ? 'selected' : ''}>${t}</option>`).join('')}</select>
          <label class="chk"><input type="checkbox" data-ch="all" ${st.all ? 'checked' : ''}>все версии</label>
          <div class="grow"></div>
          ${canOp() ? `<a class="btn p" href="#/signing">Загрузить</a><button class="btn" data-act="meta">Метаданные</button><button class="btn" data-act="rescan">Пересканировать</button>` : ''}
        </div>
        <div id="selbar"></div>
        <div class="tblwrap"><table class="t"><thead><tr>
          ${canOp() ? `<th><input type="checkbox" class="cb" data-ch="selall" aria-label="выбрать все"></th>` : ''}
          <th>Пакет</th><th>Версия</th><th>Арх.</th><th>Ветка</th><th>Размер</th><th>Подпись</th><th>На хостах</th><th>Добавлен</th></tr></thead>
          <tbody id="pkb"></tbody>
        </table></div>
        <div class="bar foot" id="pkf"></div>
      </section>`;
    drawRows();
  }
  // таблица перерисовывается отдельно от поля поиска — ввод, выделение и курсор в поле не сбиваются
  function drawRows() {
    const vis = visible();
    const hidden = st.list.length - st.list.filter((p) => p.latest).length;
    $('#pkb', main).innerHTML = vis.map((p) => `<tr class="clk ${st.sel.has(p.id) ? 's' : ''} ${p.latest ? '' : 'old'}" data-act="info" data-id="${p.id}">
            ${canOp() ? `<td><input type="checkbox" class="cb" data-ch="sel" data-id="${p.id}" ${st.sel.has(p.id) ? 'checked' : ''} aria-label="выбрать"></td>` : ''}
            <td class="nm">${esc(p.name)}<span class="sub" title="${esc(p.summary || '')}">${esc(p.summary || '')}</span></td><td class="fn" title="${esc(p.filename)}">${esc(p.evr)}</td>
            <td>${esc(p.arch === 'noarch' ? 'noarch → ' + p.arches.replace(' ', ' · ') : p.arch)}</td>
            <td class="br">${esc(p.branch)}</td><td>${fmtSize(p.size)}</td><td>${sigTag(p)}</td>
            <td>${p.hosts_eligible ? `${p.hosts_on} / ${p.hosts_eligible}` : '<span class="muted">—</span>'}</td>
            <td class="muted">${fmtDate(p.added)}</td></tr>`).join('') || `<tr><td colspan="9" class="empty">${st.list.length ? 'ничего не найдено' : 'репозиторий пуст — загрузите первые пакеты'}</td></tr>`;
    $('#pkf', main).innerHTML = `Показано ${vis.length} из ${st.list.length}${!st.all && hidden ? ` · старые версии скрыты (${hidden}) — <a href="#" data-act="showall">показать все версии</a>` : ''}
          <div class="grow"></div>noarch публикуется во все архитектуры автоматически`;
    drawSel();
  }
  function drawSel() {
    const el = $('#selbar', main);
    if (!el) return;
    if (!st.sel.size) { el.innerHTML = ''; return; }
    const rows = st.list.filter((p) => st.sel.has(p.id));
    el.innerHTML = `<div class="bar selbar"><b>Выбрано ${rows.length}</b> · ${fmtSize(rows.reduce((a, p) => a + p.size, 0))}
      <button class="btn link" data-act="selnone">снять выбор</button><div class="grow"></div>
      <button class="btn" data-act="resign">Переподписать</button>
      <button class="btn" data-act="move">Переместить в ветку…</button>
      <button class="btn p" data-act="assign">Назначить на хосты…</button>
      <button class="btn d" data-act="del">Удалить</button></div>`;
    $$('tbody tr', main).forEach((tr) => tr.classList.toggle('s', st.sel.has(+tr.dataset.id)));
  }
  const job = async (body, msg) => { try { const r = await POST('/api/packages/action', body); toast(msg + ` (задание #${r.job})`); st.sel.clear(); setTimeout(async () => { await load(); draw(); }, 1500); } catch (e) { fail(e); } };
  S.on.q = (el) => { st.q = el.value; drawRows(); };
  S.on.arch = (el) => { st.arch = el.value; drawRows(); };
  S.on.branch = (el) => { st.branch = el.value; drawRows(); };
  S.on.sig = (el) => { st.sig = el.value; drawRows(); };
  S.on.all = (el) => { st.all = el.checked; drawRows(); };
  S.on.sel = (el) => { const id = +el.dataset.id; el.checked ? st.sel.add(id) : st.sel.delete(id); drawSel(); };
  S.on.selall = (el) => { visible().forEach((p) => el.checked ? st.sel.add(p.id) : st.sel.delete(p.id)); drawRows(); };
  S.acts.selnone = () => { st.sel.clear(); drawRows(); };
  S.acts.showall = () => { st.all = true; draw(); };
  S.acts.fsig = (el) => { st.sig = st.sig === el.dataset.v ? '' : el.dataset.v; st.all = true; draw(); };
  S.acts.resignall = () => job({ op: 'resign_foreign' }, 'переподпись поставлена в очередь');
  S.acts.resign = () => job({ op: 'resign', ids: [...st.sel] }, 'переподпись поставлена в очередь');
  S.acts.del = async () => {
    const rows = st.list.filter((p) => st.sel.has(p.id));
    if (await confirmDlg('Удалить пакеты', `Будут удалены из репозитория:<br><b>${rows.map((p) => esc(p.filename)).join('<br>')}</b><br><br>На хостах они останутся установленными.`, 'Удалить', true))
      job({ op: 'delete', ids: [...st.sel] }, 'удаление поставлено в очередь');
  };
  S.acts.move = () => {
    const m = modal(`<h2 class="t">Переместить в ветку</h2><div class="note">Выбрано пакетов: ${st.sel.size}</div>
      <select class="sel" id="mvto">${S.me.branches.map((b) => `<option>${esc(b)}</option>`).join('')}</select>
      <div class="row" style="justify-content:flex-end"><button class="btn" data-close>Отмена</button><button class="btn p" id="mvok">Переместить</button></div>`, true);
    $('#mvok', m.el).onclick = () => { const to = $('#mvto', m.el).value; m.close(); job({ op: 'move', ids: [...st.sel], to }, 'перемещение в ' + to); };
  };
  S.acts.assign = () => { S.assignPre = [...new Set(st.list.filter((p) => st.sel.has(p.id)).map((p) => p.name))]; location.hash = '#/assign'; };
  S.acts.meta = async () => { try { const r = await POST('/api/repo/metadata'); toast(`пересборка метаданных (задание #${r.job})`); } catch (e) { fail(e); } };
  S.acts.rescan = async () => { try { const r = await POST('/api/repo/rescan'); toast(`сканирование (задание #${r.job})`); setTimeout(async () => { await load(); draw(); }, 2500); } catch (e) { fail(e); } };
  S.acts.info = (el, e) => { if (e.target.closest('input')) return; pkgInfo(+el.dataset.id); };
  draw();
  every(15000, async () => { if (document.activeElement && document.activeElement.dataset.in === 'q') return; if ($('.modal')) return; try { await load(); draw(); } catch (e) { } });
}

async function pkgInfo(id) {
  let p;
  try { p = await GET('/api/packages/' + id); } catch (e) { return fail(e); }
  const hs = p.hosts.map((h) => `<tr><td><a href="#/device/${h.id}" data-close>${esc(h.name)}</a></td><td>${esc(h.evr)}</td><td>${esc(h.parch)}</td>
    <td>${h.cmp === 0 ? '<span class="tag ok">эта версия</span>' : h.cmp < 0 ? '<span class="tag info">старее</span>' : '<span class="tag dim">новее</span>'}</td>
    <td class="muted">${esc(h.from_repo || '—')}</td></tr>`).join('');
  modal(`<div class="row"><h2 class="t grow">${esc(p.name)} ${esc(p.version)}-${esc(p.release)}</h2><button class="btn s" data-close>закрыть</button></div>
    <dl class="kv"><dt>Файл</dt><dd>${esc(p.filename)}</dd><dt>Описание</dt><dd>${esc(p.summary || '—')}</dd>
    <dt>Архитектура</dt><dd>${esc(p.arch)} → ${esc(p.arches)}</dd><dt>Ветка</dt><dd>${esc(p.branch)}</dd>
    <dt>Размер</dt><dd>${fmtSize(p.size)}</dd><dt>Подпись</dt><dd>${sigTag(p)} ${esc(p.sigkey || '')}</dd>
    <dt>Собран</dt><dd>${fmtDT(p.buildtime)}</dd><dt>Добавлен</dt><dd>${fmtDT(p.added)}</dd>
    <dt>Ссылка</dt><dd><a href="${esc(p.url)}">${esc(p.url)}</a></dd></dl>
    <h2 class="t">Установлен на устройствах</h2>
    ${hs ? `<div class="tblwrap"><table class="t"><thead><tr><th>Устройство</th><th>Версия</th><th>Арх.</th><th></th><th>Источник</th></tr></thead><tbody>${hs}</tbody></table></div>` : '<div class="note">ни на одном устройстве (по последним отчётам агентов)</div>'}`);
}

/* ------------------------------------------------------------------ подпись и загрузка */
const STEP_RU = { download: 'Скачивание', check: 'Проверка RPM', sign: 'Подпись', publish: 'Публикация', metadata: 'Метаданные', run: 'Выполнение' };
function jobSteps(j) {
  let steps;
  if (j.op === 'fetch') steps = ['download', 'check', 'sign', 'publish', 'metadata'];
  else if (j.op === 'add') steps = ['check', 'sign', 'publish', 'metadata'];
  else if (j.op === 'resign') steps = ['sign', 'publish', 'metadata'];
  else if (j.op === 'delete' || j.op === 'move') steps = ['publish', 'metadata'];
  else if (j.op === 'metadata') steps = ['metadata'];
  else steps = ['run'];
  let cur = steps.indexOf(j.step || '');
  if (cur < 0) cur = j.status === 'running' ? 0 : -1;
  return `<div class="steps">${steps.map((s, i) => {
    let c = '', mark = '';
    if (j.status === 'done') { c = 'd'; mark = '✓ '; }
    else if (j.status === 'error') { if (i < cur) { c = 'd'; mark = '✓ '; } else if (i === Math.max(cur, 0)) { c = 'e'; mark = '✕ '; } }
    else if (j.status === 'running') { if (i < cur) { c = 'd'; mark = '✓ '; } else if (i === cur) { c = 'a'; mark = '● '; } }
    return `<div class="st ${c}">${mark}${STEP_RU[s]}</div>`;
  }).join('')}</div>`;
}
function jobStatus(j) {
  const m = { queued: '<span class="muted">в очереди</span>', running: '<span class="c-hb">выполняется…</span>', done: `<span class="c-ok">готово · ${fmtDT(j.finished).slice(-5)}</span>`, error: '<span class="c-bad">ошибка</span>', cancelled: '<span class="muted">отменено</span>' };
  return m[j.status] || esc(j.status);
}

async function vSigning(main) {
  const st = { jobs: [], key: null, junk: [], dayDone: 0, dayErr: 0 };
  const br = S.me.default_branch;
  main.innerHTML = `<div class="cols2">
    <div class="colstack">
      ${canOp() ? `<section class="box"><h2 class="t">Добавить пакеты</h2>
        <div class="drop" id="drop" tabindex="0" role="button" aria-label="выбрать файлы">
          <svg class="logo" width="38" height="38" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M12 16V4m0 0-5 5m5-5 5 5M4 16v4h16v-4"/></svg>
          <div style="font-size:15px">Перетащите .rpm сюда или <span class="c-hb" style="text-decoration:underline">выберите файлы</span></div>
          <div class="note">До ${Math.round(S.me.upload_max / 1024 ** 3)} ГБ на файл, загрузка частями с докачкой · имя, версия и архитектура берутся из самого RPM</div>
          <input type="file" id="files" accept=".rpm" multiple class="hidden">
        </div>
        <div class="row"><span class="lbl">или по ссылке</span><input class="inp grow" id="url" placeholder="https://…/package.rpm"><button class="btn" data-act="fetch">Скачать</button></div>
        <div class="row">
          <label class="row"><span class="lbl">Ветка</span><select class="sel" id="br">${S.me.branches.map((b) => `<option ${b === br ? 'selected' : ''}>${esc(b)}</option>`).join('')}</select></label>
          <label class="chk"><input type="checkbox" id="sign" checked>Подписать ключом HomeRepo (чужая подпись заменяется)</label>
          <label class="row"><span class="lbl">Старые версии</span><select class="sel" id="keep"><option value="0">оставлять все</option><option value="1">оставить 1 прошлую</option><option value="2">оставить 2 прошлые</option><option value="3">оставить 3 прошлые</option></select></label>
        </div>
        <div id="uplist" class="colstack" style="gap:8px"></div>
      </section>` : ''}
      <section class="box"><div class="row"><h2 class="t grow">Очередь службы подписи</h2><span class="note" id="qstat"></span></div><div id="queue" class="colstack" style="gap:10px"></div></section>
    </div>
    <div class="colstack">
      <section class="box" id="keybox"><h2 class="t">Ключ подписи</h2><div class="empty">…</div></section>
      <section class="box" id="mdbox"><h2 class="t">Метаданные веток</h2><div class="empty">…</div></section>
      <section class="box" id="junkbox" style="display:none"></section>
    </div></div>`;

  function drawQueue() {
    $('#qstat', main).textContent = `за сутки: готово ${st.dayDone}, ошибок ${st.dayErr}`;
    $('#queue', main).innerHTML = st.jobs.map((j) => `<div class="job">
      <div class="jh"><b title="${esc(j.title)}">${esc(j.title || j.op)}</b><span class="nowrap">${jobStatus(j)}
        ${j.status === 'queued' && canOp() ? ` <button class="btn s" data-act="jcancel" data-id="${j.id}">отменить</button>` : ''}</span></div>
      ${jobSteps(j)}
      <div class="note">#${j.id} · ${esc(j.created_by || '')} · ${fmtDT(j.created)}${j.message ? ' · ' + esc(j.message) : ''}${j.progress != null && j.status === 'running' ? ` · ${Math.round(j.progress * 100)}%` : ''}</div></div>`).join('') || '<div class="empty">заданий ещё не было</div>';
  }
  function drawKey() {
    const k = st.key.key || {};
    const sg = st.key.signer || {};
    const fpr = (k.fpr || '').replace(/(.{4})/g, '$1 ').trim();
    $('#keybox', main).innerHTML = `<h2 class="t">Ключ подписи</h2>
      ${k.error ? `<div class="alert bad">${esc(k.error)}</div>` : ''}
      <dl class="kv"><dt>Владелец</dt><dd>${esc(k.uid || '—')}</dd>
      <dt>Отпечаток</dt><dd>${esc(fpr || '—')}</dd>
      <dt>Ключи</dt><dd>${(k.keyids || []).map((x, i) => esc(x) + (i ? ' (подключ)' : ' (основной)')).join('<br>') || '—'}</dd>
      <dt>Создан</dt><dd>${fmtDate(k.created)}${k.expires ? ', до ' + fmtDate(k.expires) : ', бессрочный'}</dd>
      <dt>Хранит</dt><dd>служба homerepo-signer ${sg.alive ? '<span class="tag ok">работает</span>' : '<span class="tag bad">не отвечает</span>'}</dd>
      <dt>Каталог</dt><dd>${esc(k.gnupg_home || '')} (0700)</dd></dl>
      <div class="alert ok" style="font-size:12px">Панель не имеет доступа к ключу: она только ставит задание в очередь. Подписывает отдельный пользователь без входа в систему и без sudo.</div>
      <div class="row"><a class="btn" href="${esc(st.key.pub_url)}" download>Открытый ключ</a>${canOp() ? '<button class="btn" data-act="keychk">Проверить ключ</button>' : ''}</div>`;
    const rs = st.key.repo_state || {};
    const dirty = new Set((st.key.dirty || []).map((d) => d.split('/').slice(-2).join('/')));
    const rows = Object.keys(rs).sort().map((d) => `<tr><td class="br">${esc(d)}</td><td>${rs[d].packages}</td><td>${fmtDT(rs[d].built)}</td>
      <td>${dirty.has(d) ? '<span class="tag run">пересборка</span>' : rs[d].signed ? '<span class="tag ok">подписаны</span>' : '<span class="tag dim">без подписи</span>'}</td></tr>`).join('');
    $('#mdbox', main).innerHTML = `<h2 class="t">Метаданные веток</h2>
      <div class="tblwrap"><table class="t"><thead><tr><th>Ветка</th><th>Пакетов</th><th>Собраны</th><th>repomd.xml</th></tr></thead><tbody>${rows || '<tr><td colspan="4" class="empty">ещё не собирались</td></tr>'}</tbody></table></div>
      <div class="note">После каждого изменения служба пересобирает метаданные только изменённых веток (createrepo_c --update) и подписывает repomd.xml.</div>
      ${canOp() ? '<div class="row"><button class="btn" data-act="meta">Пересобрать всё</button><button class="btn" data-act="rescan">Пересканировать файлы</button></div>' : ''}`;
    const jb = $('#junkbox', main);
    if (st.junk.length) {
      jb.style.display = '';
      jb.innerHTML = `<h2 class="t" style="color:var(--warn)">Мусор в репозитории · ${st.junk.length}</h2>
        <div class="tblwrap" style="max-height:260px"><table class="t"><tbody>${st.junk.map((j) => `<tr><td class="wrap">${esc(j.rel)}</td><td class="muted wrap">${esc(j.reason)}</td></tr>`).join('')}</tbody></table></div>
        ${canOp() ? '<div class="row"><button class="btn w" data-act="cleanup">Удалить мусор</button></div>' : ''}`;
    } else jb.style.display = 'none';
  }
  async function loadJobs() { const r = await GET('/api/jobs?limit=25'); st.jobs = r.jobs; st.dayDone = r.day_done; st.dayErr = r.day_error; drawQueue(); }
  async function loadKey() { const [k, j] = await Promise.all([GET('/api/key'), GET('/api/junk')]); st.key = k; st.junk = j.junk; drawKey(); }
  await Promise.all([loadJobs(), loadKey()]);
  every(2000, () => { const act = st.jobs.some((j) => j.status === 'queued' || j.status === 'running') || S.upBusy; if (act || Math.random() < 0.25) loadJobs().catch(() => { }); });
  every(8000, () => loadKey().catch(() => { }));

  const opts = () => ({ branch: $('#br', main).value, sign: $('#sign', main).checked, keep: +$('#keep', main).value });
  S.acts.jcancel = async (el) => { try { await POST(`/api/jobs/${el.dataset.id}/cancel`); loadJobs(); } catch (e) { fail(e); } };
  S.acts.keychk = async () => { try { const r = await POST('/api/repo/key'); toast(`проверка ключа (задание #${r.job})`); } catch (e) { fail(e); } };
  S.acts.meta = async () => { try { const r = await POST('/api/repo/metadata'); toast(`пересборка (задание #${r.job})`); loadJobs(); } catch (e) { fail(e); } };
  S.acts.rescan = async () => { try { const r = await POST('/api/repo/rescan'); toast(`сканирование (задание #${r.job})`); loadJobs(); } catch (e) { fail(e); } };
  S.acts.cleanup = async () => {
    if (await confirmDlg('Удалить мусор', `Будут удалены ${st.junk.length} файлов/каталогов, перечисленных в списке. RPM-пакеты не затрагиваются.`, 'Удалить', true)) {
      try { const r = await POST('/api/repo/cleanup'); toast(`очистка (задание #${r.job})`); loadJobs(); } catch (e) { fail(e); }
    }
  };
  if (!canOp()) return;
  S.acts.fetch = async () => {
    const u = $('#url', main).value.trim();
    if (!u) return toast('вставьте ссылку на .rpm', 'err');
    try { const r = await POST('/api/fetch', Object.assign({ url: u }, opts())); toast(`скачивание поставлено в очередь (#${r.job})`); $('#url', main).value = ''; loadJobs(); } catch (e) { fail(e); }
  };
  const drop = $('#drop', main), inp = $('#files', main);
  drop.onclick = () => inp.click();
  drop.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); inp.click(); } };
  inp.onchange = () => { addFiles(inp.files, opts()); inp.value = ''; };
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove('over'); addFiles(e.dataTransfer.files, opts()); };
  drawUploads();
}

function addFiles(files, o) {
  for (const f of files) {
    if (!f.name.toLowerCase().endsWith('.rpm')) { toast(`${f.name}: не .rpm`, 'err'); continue; }
    S.up.push({ f, name: f.name, size: f.size, sent: 0, status: 'wait', err: '', job: null, o });
  }
  drawUploads();
  runUploads();
}
function drawUploads() {
  const el = $('#uplist');
  if (!el) return;
  el.innerHTML = S.up.slice(-12).map((u, i) => {
    const pct = u.size ? Math.round(u.sent / u.size * 100) : 0;
    const stt = { wait: 'ожидает', up: `загрузка ${pct}%`, done: `передан в подпись · задание #${u.job}`, err: 'ошибка' }[u.status];
    return `<div class="upl"><div class="jh"><b>${esc(u.name)}</b><span class="nowrap ${u.status === 'err' ? 'c-bad' : u.status === 'done' ? 'c-ok' : 'muted'}">${fmtSize(u.size)} · ${stt}</span></div>
      <div class="pbar ${u.status === 'err' ? 'err' : ''}"><i style="width:${u.status === 'done' ? 100 : pct}%"></i></div>
      ${u.err ? `<div class="note c-bad">${esc(u.err)} ${u.uid ? '' : ''}<button class="btn s" data-act="upretry" data-i="${S.up.indexOf(u)}">повторить</button></div>` : ''}</div>`;
  }).join('');
  S.acts.upretry = (b) => { const u = S.up[+b.dataset.i]; if (u) { u.status = 'wait'; u.err = ''; drawUploads(); runUploads(); } };
}
async function runUploads() {
  if (S.upBusy) return;
  S.upBusy = true;
  try {
    let u;
    while ((u = S.up.find((x) => x.status === 'wait'))) {
      u.status = 'up'; drawUploads();
      try {
        if (!u.uid) { const r = await POST('/api/uploads', { filename: u.name, size: u.size }); u.uid = r.id; u.chunk = r.chunk; u.sent = 0; }
        else { const r = await GET('/api/uploads/' + u.uid); u.sent = r.received; }
        while (u.sent < u.size) {
          const blob = u.f.slice(u.sent, Math.min(u.size, u.sent + u.chunk));
          let tries = 0;
          for (;;) {
            try { const r = await api('PUT', `/api/uploads/${u.uid}?offset=${u.sent}`, undefined, blob); u.sent = r.received; break; }
            catch (e) {
              if (e.status === 409 && e.data && e.data.received != null) { u.sent = e.data.received; break; }
              if (++tries >= 4 || (e.status && e.status < 500 && e.status !== 0)) throw e;
              await new Promise((r) => setTimeout(r, 1500 * tries));
            }
          }
          drawUploads();
        }
        const r = await POST(`/api/uploads/${u.uid}/finish`, u.o);
        u.job = r.job; u.status = 'done'; u.f = null;
      } catch (e) {
        u.status = 'err'; u.err = e.message;
        if (e.status === 404) u.uid = null;
      }
      drawUploads();
    }
  } finally { S.upBusy = false; }
}



/* ------------------------------------------------------------------ значки систем (нейтральные символы, не логотипы) */
const OS_SVG = {
  linux: '<rect x="1.5" y="2.5" width="13" height="11" rx="1.5"/><path d="M4.5 6.2l2.2 1.8-2.2 1.8M8.5 10.5h3"/>',
  windows: '<rect x="1.5" y="2.5" width="13" height="8.5" rx="1"/><path d="M8 11v3M5 14h6"/>',
  macos: '<rect x="3" y="3.5" width="10" height="7" rx="1"/><path d="M1.5 12.5h13"/>',
  router: '<rect x="1.5" y="8.5" width="13" height="5" rx="1"/><path d="M4.5 8.5 3 3.5M11.5 8.5 13 3.5M4.5 11h.01M7 11h.01"/>',
  nas: '<rect x="2.5" y="2" width="11" height="5" rx="1"/><rect x="2.5" y="9" width="11" height="5" rx="1"/><path d="M5 4.5h.01M5 11.5h.01"/>',
  hypervisor: '<rect x="2" y="2" width="12" height="3.3" rx=".6"/><rect x="2" y="6.35" width="12" height="3.3" rx=".6"/><rect x="2" y="10.7" width="12" height="3.3" rx=".6"/>',
  printer: '<rect x="4" y="1.5" width="8" height="4"/><rect x="1.5" y="5.5" width="13" height="6" rx="1"/><rect x="4" y="9.5" width="8" height="5"/>',
  phone: '<rect x="4.5" y="1.5" width="7" height="13" rx="1.5"/><path d="M7 12.3h2"/>',
  iot: '<rect x="4.5" y="4.5" width="7" height="7" rx="1"/><path d="M6.5 2v2.5M9.5 2v2.5M6.5 11.5V14M9.5 11.5V14M2 6.5h2.5M2 9.5h2.5M11.5 6.5H14M11.5 9.5H14"/>',
  other: '<circle cx="8" cy="8" r="6.5"/><path d="M6.3 6.2a1.8 1.8 0 1 1 2.5 1.7c-.5.2-.8.6-.8 1.1v.4M8 11.6h.01"/>',
};
const FAM_RU = { linux: 'Linux', windows: 'Windows', macos: 'macOS', router: 'Роутер', nas: 'NAS / хранилище', hypervisor: 'Гипервизор', printer: 'Принтер', phone: 'Телефон / планшет', iot: 'Умное устройство', other: 'Другое' };
const FAM_COLOR = { linux: 'var(--cyan)', windows: 'var(--info)', macos: 'var(--text2)', router: 'var(--warn)', nas: 'var(--ok)', hypervisor: 'var(--cyan)' };
const OS_SUGGEST = {
  linux: ['РЕД ОС 8', 'РЕД ОС 7.3', 'Astra Linux', 'ALT Linux', 'ROSA', 'Ubuntu', 'Debian', 'Raspberry Pi OS', 'Rocky Linux', 'AlmaLinux', 'Fedora', 'CentOS', 'openSUSE', 'Arch Linux'],
  windows: ['Windows 11', 'Windows 10', 'Windows Server 2022', 'Windows Server 2019'],
  macos: ['macOS Sequoia', 'macOS Sonoma', 'macOS Ventura'],
  router: ['MikroTik RouterOS', 'Keenetic OS', 'OpenWrt', 'TP-Link', 'ASUSWRT', 'Ubiquiti EdgeOS', 'UniFi OS', 'pfSense', 'OPNsense'],
  nas: ['Synology DSM', 'TrueNAS', 'QNAP QTS', 'OpenMediaVault'],
  hypervisor: ['Proxmox VE', 'VMware ESXi', 'oVirt'],
  printer: ['HP', 'Canon', 'Epson', 'Brother', 'Kyocera'],
  phone: ['Android', 'iOS', 'iPadOS', 'HarmonyOS'],
  iot: ['Home Assistant', 'ESPHome', 'Tasmota', 'Яндекс Станция'],
  other: ['FreeBSD', 'OpenBSD', 'Solaris'],
};
function osIcon(fam) {
  const f = OS_SVG[fam] ? fam : 'other';
  return `<svg class="osi" viewBox="0 0 16 16" aria-hidden="true" style="color:${FAM_COLOR[f] || 'var(--dim)'}">${OS_SVG[f]}</svg>`;
}
function osBadge(fam, name) {
  if (!fam && !name) return '<span class="muted">—</span>';
  return `<span class="os" title="${esc(FAM_RU[fam] || '')}">${osIcon(fam)}${esc(name || FAM_RU[fam] || '')}</span>`;
}

/* ------------------------------------------------------------------ метрики */
function mbar(v, label) {
  if (v == null) return '<span class="muted small">—</span>';
  const c = v >= 90 ? 'b' : v >= 75 ? 'w' : '';
  return `<span class="nowrap small" title="${esc(label)}"><span class="mbar ${c}"><i style="width:${Math.min(100, Math.max(0, v))}%"></i></span>${Math.round(v)}%</span>`;
}
function fresh(h) { return h.metrics && h.metrics_ts && Date.now() / 1000 - h.metrics_ts < 900; }
function loadCell(h) {
  if (!fresh(h)) return '<span class="muted">—</span>';
  const m = h.metrics;
  return `<div style="display:grid;grid-template-columns:auto auto;gap:3px 8px;align-items:center"><span class="muted small">CPU</span>${mbar(m.cpu, 'загрузка CPU')}<span class="muted small">ОЗУ</span>${mbar(m.mem_pct, 'занято памяти')}<span class="muted small">диск</span>${mbar(m.disk_pct, 'заполнен корневой раздел')}</div>`;
}
function fmtUp(sec) {
  if (!sec) return '—';
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  return d ? `${d} дн ${h} ч` : h ? `${h} ч ${m} мин` : `${m} мин`;
}
function spark(vals, max) {
  const v = vals.filter((x) => x != null);
  if (v.length < 2) return '';
  const mx = max || Math.max(...v, 0.01) * 1.15;
  const n = vals.length, pts = [];
  vals.forEach((x, i) => { if (x != null) pts.push([(i / (n - 1)) * 100, 35 - (Math.min(x, mx) / mx) * 33]); });
  const d = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join('');
  const area = d + `L${pts[pts.length - 1][0].toFixed(1)},36L${pts[0][0].toFixed(1)},36Z`;
  return `<svg viewBox="0 0 100 36" preserveAspectRatio="none" aria-hidden="true"><path class="sparkf" d="${area}"/><path class="spark" d="${d}" vector-effect="non-scaling-stroke"/></svg>`;
}
const fmtLa = (x) => x == null ? '—' : (+x).toFixed(2);
function sysBox(h) {
  const hw = h.hw || {}, m = h.metrics || {}, hist = h.history || [];
  const col = (i) => hist.map((r) => r[i]);
  const ok = fresh(h);
  const tile = (label, val, sub, sp) => `<div class="mtile"><span class="l">${label}</span><span class="v">${val}</span><span class="s">${sub || '&nbsp;'}</span>${sp || ''}</div>`;
  const pct = (x) => x == null ? '—' : Math.round(x) + '%';
  const tiles = [
    tile('CPU', ok ? pct(m.cpu) : '—', `${m.cores || hw.cores || '?'} ядер`, spark(col(1), 100)),
    tile('Память', ok ? pct(m.mem_pct) : '—', m.mem_total ? `${fmtSize(m.mem_used)} из ${fmtSize(m.mem_total)}` : '', spark(col(2), 100)),
    tile('Нагрузка (LA)', ok && m.load ? m.load[0].toFixed(2) : '—', m.load ? `5 мин ${fmtLa(m.load[1])} · 15 мин ${fmtLa(m.load[2])}` : '', spark(col(4))),
    tile('Диск /', ok ? pct(m.disk_pct) : '—', '', spark(col(5), 100)),
    tile('Swap', ok ? (m.swap_total ? pct(m.swap_pct) : 'нет') : '—', m.swap_total ? `${fmtSize(m.swap_used)} из ${fmtSize(m.swap_total)}` : '', spark(col(3), 100)),
    tile('Температура', ok && m.temp != null ? `${Math.round(m.temp)}°` : '—', m.temp != null ? 'максимум по датчикам' : 'датчиков нет', spark(col(6))),
  ].join('');
  const ifs = (hw.interfaces || []).map((i) => `${esc(i.name)} ${esc(i.addr)}${i.mac ? ' <span class="muted">' + esc(i.mac) + '</span>' : ''}`).join('<br>');
  const dk = (m.disks || []).map((d) => `<tr><td>${esc(d.mount)}</td><td class="muted">${esc(d.fs)}</td><td>${fmtSize(d.size)}</td><td>${mbar(d.pct, d.dev)}</td></tr>`).join('');
  return `<section class="box"><div class="row"><h2 class="t grow">Система и нагрузка</h2>
      <span class="note">${ok ? 'метрики ' + ago(h.metrics_ts) + ' · графики за 24 ч' : h.metrics_ts ? 'метрики устарели: ' + ago(h.metrics_ts) : 'агент ещё не присылал метрики (нужен агент 1.1, обновится сам)'}</span></div>
    <div class="cols2" style="grid-template-columns:minmax(0,1fr) minmax(0,1.4fr)">
      <dl class="kv">
        <dt>Система</dt><dd>${esc(hw.pretty_name || ((h.os_name || '') + ' ' + (h.os_version || '')))}</dd>
        <dt>Ядро</dt><dd>${esc(h.kernel || '—')} · ${esc(h.arch || '')}</dd>
        <dt>Платформа</dt><dd>${esc(hw.model || '—')}${hw.virt && hw.virt !== 'none' ? ' <span class="tag info">' + esc(hw.virt) + '</span>' : ''}</dd>
        <dt>Процессор</dt><dd>${esc(hw.cpu_model || '—')}${hw.cores ? ' × ' + hw.cores : ''}</dd>
        <dt>Память</dt><dd>${hw.mem_total ? fmtSize(hw.mem_total) : '—'}${hw.swap_total ? ' · swap ' + fmtSize(hw.swap_total) : ''}</dd>
        <dt>Работает</dt><dd>${fmtUp(m.uptime)}${hw.boot ? ' <span class="muted">(с ' + fmtDT(hw.boot) + ')</span>' : ''}</dd>
        <dt>SELinux</dt><dd>${esc(hw.selinux || '—')}</dd>
        <dt>Сеть</dt><dd>${ifs || '—'}</dd>
      </dl>
      <div class="colstack" style="gap:12px"><div class="mtiles">${tiles}</div>
        ${dk ? `<div class="tblwrap"><table class="t"><thead><tr><th>Раздел</th><th>ФС</th><th>Размер</th><th>Занято</th></tr></thead><tbody>${dk}</tbody></table></div>` : ''}</div>
    </div></section>`;
}

/* ------------------------------------------------------------------ устройства */
const STATUS_RU = { online: 'на связи', late: 'давно не было', offline: 'нет связи' };
async function vDevices(main) {
  async function draw() {
    const r = await GET('/api/hosts');
    const hs = r.hosts;
    const sum = (k) => hs.reduce((a, h) => a + (h[k] || 0), 0);
    main.innerHTML = `
      <div class="tiles">
        <div class="tile"><b>${hs.length}</b><span>устройств</span></div>
        <div class="tile"><b style="color:var(--online)">${hs.filter((h) => h.status === 'online').length}</b><span>на связи</span></div>
        <div class="tile"><b style="color:var(--info)">${sum('n_updates')}</b><span>доступно обновлений</span></div>
        <div class="tile"><b style="color:var(--warn)">${sum('n_external')}</b><span>пакетов извне</span></div>
        <div class="tile click" data-act="toassign"><b>${sum('n_pending')}</b><span>назначения ждут</span></div>
      </div>
      <section class="panel">
        <div class="bar"><input class="inp" style="flex:1 1 240px" placeholder="Поиск по имени или адресу" data-in="hq" id="hq">
          <div class="grow"></div><a class="btn" href="/api/report/hosts.html" target="_blank" rel="noopener">Сводный отчёт</a>${canOp() ? '<a class="btn p" href="#/connect">Подключить устройство</a>' : ''}</div>
        <div class="tblwrap"><table class="t"><thead><tr><th>Устройство</th><th>ОС · арх.</th><th>Последний отчёт</th><th>Нагрузка</th><th>Из HomeRepo</th><th>Извне</th><th>Из ОС</th><th>Обновления</th><th>Назначения</th></tr></thead>
        <tbody>${hs.map((h) => `<tr class="clk" data-act="host" data-id="${h.id}" data-s="${esc((h.fqdn + ' ' + h.ip + ' ' + h.name).toLowerCase())}">
          <td class="nm"><span class="dot ${h.status}" title="${STATUS_RU[h.status]}"></span>${esc(h.name)}<span class="sub">${esc(h.fqdn || '')} · ${esc(h.ip || '')}</span></td>
          <td>${osBadge('linux', (h.os_name || '?') + ' ' + (h.os_version || ''))} <span class="muted">· ${esc(h.arch || '?')}</span></td>
          <td class="${h.status === 'online' ? '' : h.status === 'late' ? 'c-ex' : 'c-bad'}">${h.last_report ? ago(h.last_poll) : 'ждём первый отчёт'}${h.hw && h.hw.model ? '<span class="sub">' + esc(h.hw.model) + '</span>' : ''}</td>
          <td>${loadCell(h)}</td>
          <td class="num c-hb">${h.n_homerepo}</td><td class="num c-ex">${h.n_external}</td><td class="muted">${h.n_os}</td>
          <td class="num c-up">${h.n_updates == null ? '?' : h.n_updates}</td>
          <td><div style="display:flex;flex-wrap:wrap;gap:4px">${h.n_pending ? `<span class="tag info">${h.n_pending} ждёт</span>` : ''}${h.n_errors ? `<span class="tag bad">${h.n_errors} ошибк.</span>` : ''}${!h.n_pending && !h.n_errors ? '<span class="muted">—</span>' : ''}</div></td></tr>`).join('')
        || '<tr><td colspan="9" class="empty">устройств пока нет — подключите первое в разделе «Подключение»</td></tr>'}</tbody></table></div>
        <div class="bar foot"><span><b class="c-hb">Из HomeRepo</b> — пришёл из нашего репозитория · <b class="c-ex">Извне</b> — сторонний репозиторий или .rpm вручную · <b>Из ОС</b> — официальные репозитории системы</span></div>
      </section>`;
  }
  S.acts.host = (el) => { location.hash = '#/device/' + el.dataset.id; };
  S.acts.toassign = () => { location.hash = '#/assign'; };
  S.on.hq = (el) => { const q = el.value.toLowerCase(); $$('tbody tr[data-s]', main).forEach((tr) => { tr.style.display = tr.dataset.s.includes(q) ? '' : 'none'; }); };
  await draw();
  every(20000, () => { if (!$('#hq') || !$('#hq').value) draw().catch(() => { }); });
}

const ACT_RU = { install: 'установить / обновить', update: 'обновить', remove: 'удалить', hold: 'держать версию', unhold: 'снять фиксацию', collect: 'забрать в HomeRepo' };
const TST = {
  pending: (t) => t.schedule === 'night' ? '<span class="tag info">ночью</span>' : t.schedule === 'at' ? `<span class="tag info">${fmtDT(t.not_before)}</span>` : '<span class="tag info">ждёт агента</span>',
  sent: () => '<span class="tag run">выполняется</span>', processing: () => '<span class="tag run">в подписи</span>', done: () => '<span class="tag ok">выполнено</span>', error: () => '<span class="tag bad">ошибка</span>',
  skipped: () => '<span class="tag dim">пропущено</span>', cancelled: () => '<span class="tag dim">отменено</span>',
};
function taskRow(t, withHost) {
  const out = (t.output || '').trim();
  const first = out.split('\n')[0];
  return `<tr><td>${esc(t.name)}${t.evr ? ' <span class="muted">' + esc(t.evr) + '</span>' : ''}</td><td>${ACT_RU[t.action] || esc(t.action)}</td>
    ${withHost ? `<td><a href="#/device/${t.host_id}">${esc(t.host)}</a></td>` : ''}
    <td class="muted">${esc(t.created_by || '')} · ${fmtDT(t.created)}</td><td>${(TST[t.status] || (() => esc(t.status)))(t)}</td>
    <td class="wrap small" style="max-width:420px">${out ? `<details><summary>${esc(first.slice(0, 90))}</summary><pre class="out">${esc(out)}</pre></details>` : '<span class="muted">—</span>'}</td>
    <td class="nowrap">${canOp() && (t.status === 'pending' || t.status === 'sent') ? `<button class="btn s" data-act="tcancel" data-id="${t.id}">отменить</button>` : ''}
      ${canOp() && (t.status === 'error' || t.status === 'cancelled' || t.status === 'skipped') ? `<button class="btn s" data-act="tretry" data-id="${t.id}">повторить</button>` : ''}</td></tr>`;
}
function taskActs(reload) {
  S.acts.tcancel = async (el) => { try { await POST(`/api/tasks/${el.dataset.id}/cancel`); reload(); } catch (e) { fail(e); } };
  S.acts.tretry = async (el) => { try { await POST(`/api/tasks/${el.dataset.id}/retry`); reload(); } catch (e) { fail(e); } };
}

async function vDevice(main, id) {
  const st = { tab: 'homerepo', h: null, q: '' };
  async function load() { st.h = await GET('/api/hosts/' + id); }
  await load();
  function draw() {
    const h = st.h;
    const by = (c) => h.packages.filter((p) => p.cls === c);
    const hr = by('homerepo'), ex = by('external'), os = by('os');
    const pend = h.tasks.filter((t) => t.status === 'pending' || t.status === 'sent' || t.status === 'processing');
    const tabs = [['homerepo', 'Из HomeRepo', hr.length], ['external', 'Извне', ex.length], ['os', 'Из ОС', os.length], ['updates', 'Обновления', h.updates.length], ['tasks', 'Назначения', pend.length]];
    const isPk = st.tab === 'homerepo' || st.tab === 'external' || st.tab === 'os';
    const old = $('#dlist', main), keepScroll = old && old.dataset.tab === st.tab ? old.scrollTop : 0;
    let body = '';
    if (isPk) body = pkList();
    else if (st.tab === 'updates') body = `<table class="t"><thead><tr><th>Пакет</th><th>Установлено</th><th>В HomeRepo</th><th>Ветка</th><th>Сейчас из</th><th></th></tr></thead><tbody>${h.updates.map((u) => `<tr><td class="nm">${esc(u.name)}</td><td>${esc(u.installed)}</td><td class="c-up num">${esc(u.available)}</td><td class="br">${esc(u.branch)}</td><td class="muted">${esc(u.from_repo || u.cls)}</td><td>${canOp() ? `<button class="btn s" data-act="upd1" data-n="${esc(u.name)}">Обновить</button>` : ''}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">всё актуально</td></tr>'}</tbody></table>`;
    else body = `<table class="t"><thead><tr><th>Пакет</th><th>Действие</th><th>Создано</th><th>Статус</th><th>Результат</th><th></th></tr></thead><tbody>${h.tasks.map((t) => taskRow(t, false)).join('') || '<tr><td colspan="6" class="empty">назначений не было</td></tr>'}</tbody></table>`;
    const f = h.facts || {};
    main.innerHTML = `
      <div class="box" style="flex-direction:row;flex-wrap:wrap;justify-content:space-between;align-items:center">
        <div><div class="note"><a href="#/devices">Устройства</a> / ${esc(h.name)}</div>
          <h1 style="font-size:22px;font-weight:800;margin-top:4px;display:flex;align-items:center;gap:8px"><span class="dot ${h.status}" style="margin:0"></span>${osIcon('linux')}${esc(h.fqdn || h.name)}</h1>
          <div class="row note" style="gap:6px 20px;margin-top:8px;font-size:13px">
            <span>ОС <b style="color:var(--text)">${esc((h.os_name || '?') + ' ' + (h.os_version || ''))}</b></span><span>Арх. <b style="color:var(--text)">${esc(h.arch || '?')}</b></span>
            <span>IP <b style="color:var(--text)">${esc(h.ip || '?')}</b></span><span>Ядро <b style="color:var(--text)">${esc(h.kernel || '?')}</b></span>
            <span>Пакетов <b style="color:var(--text)">${h.pkg_total || 0}</b></span><span>Отчёт <b style="color:${h.status === 'online' ? 'var(--online)' : 'var(--warn)'}">${h.last_report ? ago(h.last_report) : 'нет'}</b></span>
            <span>Агент <b style="color:var(--text)">${esc(h.agent_version || '?')}</b></span></div></div>
        <div class="row"><a class="btn" href="/api/report/host/${h.id}.html" target="_blank" rel="noopener">Отчёт HTML / PDF</a>${canOp() ? `<button class="btn" data-act="rep">Запросить отчёт</button>${h.updates.length ? `<button class="btn p" data-act="updall">Обновить из HomeRepo (${h.updates.length})</button>` : ''}` : ''}
          ${isAdmin() ? '<button class="btn d" data-act="hdel">Удалить</button>' : ''}</div>
      </div>
      ${sysBox(h)}
      ${h.want_report ? '<div class="alert info"><div>Отчёт запрошен — агент пришлёт его при следующем опросе (раз в 2 минуты).</div></div>' : ''}
      <div class="cols2" style="grid-template-columns:minmax(0,2.3fr) minmax(0,1fr)">
        <section class="panel"><nav class="tabs">${tabs.map(([k, t, n]) => `<a data-act="tab" data-t="${k}" class="${st.tab === k ? 'on' : ''}">${t}<i>${n}</i></a>`).join('')}</nav>
          ${isPk ? `<div class="bar"><input class="inp" style="flex:1 1 240px" placeholder="Поиск пакета на хосте" value="${esc(st.q)}" data-in="dq" id="dq">
            <span class="note" style="margin:0">нажмите на пакет — откроется карточка с действиями</span></div>` : ''}
          <div class="tblwrap" id="dlist" data-tab="${st.tab}" style="max-height:640px">${body}</div></section>
        <div class="colstack">
          <section class="box${ex.length ? ' warn' : ''}"><h2 class="t"${ex.length ? ' style="color:var(--warn)"' : ''}>Источники пакетов</h2>
            <dl class="kv"><dt><a href="#" data-act="tab" data-t="homerepo">Из HomeRepo</a></dt><dd class="c-hb">${hr.length}</dd>
              <dt><a href="#" data-act="tab" data-t="external">Извне</a></dt><dd class="c-ex">${ex.length}${ex.length ? ' — можно забрать в HomeRepo' : ''}</dd>
              <dt><a href="#" data-act="tab" data-t="os">Из репозиториев ОС</a></dt><dd>${os.length}</dd></dl>
            <div class="note">Нажмите на пакет в списке слева: в карточке пакета есть кнопки «Скачать в HomeRepo» (агент скачает пакет из его источника, служба подпишет и опубликует его), «Перевести на HomeRepo», «Обновить», «Удалить с хоста» и «Держать версию».</div>
          </section>
          <section class="box"><h2 class="t">Репозитории на хосте</h2>
            <div class="note">${(f.repos || []).filter((r) => r.enabled).map((r) => `<span class="${(f.homerepo_ids || []).includes(r.id) ? 'c-hb' : (f.os_repos || []).includes(r.id) ? '' : 'c-ex'}">${esc(r.id)}</span>`).join(' · ') || '—'}</div>
            ${(f.repos || []).length && !(f.homerepo_ids || []).length ? '<div class="alert warn"><div>HomeRepo на этом хосте не подключён</div></div>' : ''}
            ${f.versionlock && f.versionlock.length ? `<div class="note">Зафиксированы версии: ${f.versionlock.map(esc).join(', ')}</div>` : ''}
          </section>
        </div></div>`;
    const w = $('#dlist', main); if (w) w.scrollTop = keepScroll;
  }
  function srcLabel(p) { const r = p.from_repo || ''; if (r === '@commandline') return 'вручную из .rpm'; if (!r) return 'неизвестно'; return r; }
  const CLS_RU = { homerepo: 'HomeRepo', external: 'извне', os: 'репозиторий ОС' };
  function pkList() {
    const q = st.q.trim().toLowerCase();
    const list = st.h.packages.filter((p) => p.cls === st.tab && (!q || (p.name + ' ' + p.evr + ' ' + (p.from_repo || '')).toLowerCase().includes(q)));
    if (!list.length) return `<div class="empty">${q ? 'ничего не найдено' : 'нет пакетов'}</div>`;
    const withRepo = st.tab !== 'os';
    return `<table class="t"><thead><tr><th>Пакет</th><th>Установлено</th>${withRepo ? '<th>В HomeRepo</th>' : ''}<th>Арх.</th><th>Источник</th><th>Установлен</th><th></th></tr></thead><tbody>
      ${list.map((p) => `<tr class="clk" data-act="pk" data-n="${esc(p.name)}" data-a="${esc(p.arch)}"><td class="nm">${esc(p.name)}${p.held ? ' <span class="tag dim">фикс.</span>' : ''}</td><td>${esc(p.evr)}</td>
        ${withRepo ? `<td class="${p.repo_cmp > 0 ? 'c-up num' : p.repo_cmp === 0 ? 'c-ok' : 'muted'}">${esc(p.repo_evr || '—')}</td>` : ''}
        <td>${esc(p.arch)}</td><td class="${p.cls === 'external' ? 'c-ex' : 'muted'}">${esc(srcLabel(p))}</td><td class="muted">${fmtDate(p.installtime)}</td>
        <td class="nowrap">${pkBadge(p)}</td></tr>`).join('')}</tbody></table>`;
  }
  function pkBadge(p) {
    if (pendingFor(p)) return '<span class="tag run">назначено</span>';
    if (p.cls === 'homerepo') return p.repo_cmp > 0 ? '<span class="tag info">есть новее</span>' : p.repo_cmp === 0 ? '<span class="tag ok">актуален</span>' : '';
    if (p.cls === 'external') return p.repo_evr ? '<span class="tag info">есть в HomeRepo</span>' : p.from_repo === '@commandline' ? '<span class="tag dim">из файла</span>' : '<span class="tag warn">не в HomeRepo</span>';
    return '';
  }
  function pendingFor(p) { return st.h.tasks.find((t) => t.name === p.name && (t.status === 'pending' || t.status === 'sent' || t.status === 'processing')); }
  function pkCard(p) {
    const h = st.h, pend = pendingFor(p), op = canOp();
    const btns = [];
    const ext = p.cls !== 'homerepo', file = p.from_repo === '@commandline';
    // забрать с хоста имеет смысл, если в HomeRepo этого пакета нет или там версия старше установленной
    const canCollect = op && !pend && ext && !file && (!p.repo_evr || p.repo_cmp < 0);
    if (op && !pend) {
      if (canCollect) btns.push('<button class="btn p" data-pc="collect">Скачать в HomeRepo</button>');
      if (ext && file && !p.repo_evr) btns.push('<a class="btn p" href="#/signing" data-close>Загрузить .rpm в панель</a>');
      if (ext && p.repo_cmp === 0) btns.push('<button class="btn p" data-pc="switch" title="переустановить ту же версию из HomeRepo вместо стороннего источника">Перевести на HomeRepo</button>');
      if (p.repo_cmp > 0) btns.push(`<button class="btn p" data-pc="update">Обновить до ${esc(p.repo_evr)}${ext ? ' из HomeRepo' : ''}</button>`);
      btns.push(p.held ? '<button class="btn" data-pc="unhold">Снять фиксацию</button>' : '<button class="btn" data-pc="hold">Держать версию</button>');
      btns.push('<button class="btn d" data-pc="remove">Удалить с хоста</button>');
    }
    if (op && pend && pend.status === 'pending') btns.push('<button class="btn" data-pc="cancel">Отменить назначение</button>');
    const m = modal(`<h2 class="t">${esc(p.name)} <span class="muted" style="font-weight:400">${esc(p.evr)}.${esc(p.arch)}</span></h2>
      <dl class="kv"><dt>Устройство</dt><dd>${esc(h.fqdn || h.name)}</dd>
        <dt>Откуда установлен</dt><dd class="${p.cls === 'external' ? 'c-ex' : p.cls === 'homerepo' ? 'c-hb' : ''}">${esc(CLS_RU[p.cls] || p.cls)} · ${esc(srcLabel(p))}</dd>
        <dt>Установлен</dt><dd>${fmtDate(p.installtime)}</dd>
        <dt>Подпись</dt><dd>${esc(p.sigkey ? p.sigkey.toUpperCase() : 'нет данных')}</dd>
        <dt>В HomeRepo</dt><dd>${p.repo_evr ? esc(p.repo_evr) + (p.repo_branch ? ' · ветка ' + esc(p.repo_branch) : '') + (p.repo_cmp > 0 ? ' — новее установленного' : p.repo_cmp === 0 ? ' — та же версия' : ' — старше установленного') : 'нет'}</dd>
        ${p.held ? '<dt>Версия</dt><dd>зафиксирована (versionlock)</dd>' : ''}</dl>
      ${pend ? `<div class="alert info"><div>Уже назначено: ${esc(ACT_RU[pend.action] || pend.action)} — ${(TST[pend.status] || (() => esc(pend.status)))(pend)}</div></div>` : ''}
      ${file && ext && !p.repo_evr ? '<div class="note">Пакет поставлен вручную из файла: скачать его с хоста неоткуда. Загрузите этот .rpm в разделе «Подпись» — после этого здесь появится кнопка «Перевести на HomeRepo».</div>' : ''}
      ${canCollect ? `<div class="note">«Скачать в HomeRepo» — агент скачает пакет из источника <b>${esc(srcLabel(p))}</b> (если точной версии там уже нет — возьмёт последнюю), служба подпишет его ключом HomeRepo и опубликует в выбранной ветке.</div>
        <label class="lbl">Ветка для публикации <select class="inp" id="pcbr" style="margin-top:6px">${(S.me.branches || ['extras']).map((b) => `<option${b === 'extras' ? ' selected' : ''}>${esc(b)}</option>`).join('')}</select></label>` : ''}
      <div class="row" style="justify-content:flex-end;flex-wrap:wrap">${btns.join('')}<button class="btn" data-close>Закрыть</button></div>`);
    m.el.addEventListener('click', async (e) => {
      const b = e.target.closest('[data-pc]'); if (!b) return;
      const a = b.dataset.pc;
      if (a === 'remove' && !await confirmDlg('Удалить пакет с хоста', `Агент на ${esc(h.name)} выполнит <code>dnf remove ${esc(p.name)}</code>. Зависимые от него пакеты тоже будут удалены.`, 'Удалить', true)) return;
      m.close();
      try {
        if (a === 'cancel') { await POST(`/api/tasks/${pend.id}/cancel`); toast('назначение отменено'); await load(); draw(); return; }
        if (a === 'collect') {
          const br = (m.el.querySelector('#pcbr') || {}).value || 'extras';
          await POST(`/api/hosts/${id}/collect`, { name: p.name, arch: p.arch, branch: br });
          toast('назначено: агент скачает пакет при следующем опросе');
          await load(); draw(); return;
        }
        const action = a === 'switch' || a === 'update' ? 'install' : a;
        await mk(action, [p.name], a === 'switch');
      } catch (err) { fail(err); }
    });
  }
  const mk = async (action, names, sw) => {
    try {
      const r = await POST('/api/tasks', { action, items: names.map((n) => ({ name: n, switch: !!sw })), hosts: [+id], schedule: 'now' });
      toast(`назначено: ${r.created}${r.skipped.length ? '; пропущено: ' + r.skipped.join('; ') : ''}`, r.created ? 'ok' : 'err');
      await load(); draw();
    } catch (e) { fail(e); }
  };
  S.acts.tab = (el) => { st.tab = el.dataset.t; draw(); };
  S.acts.pk = (el) => { const p = st.h.packages.find((x) => x.name === el.dataset.n && x.arch === el.dataset.a); if (p) pkCard(p); };
  S.on.dq = (el) => { st.q = el.value; const l = $('#dlist', main); if (l) { l.innerHTML = pkList(); l.scrollTop = 0; } };
  S.acts.upd1 = (el) => mk('install', [el.dataset.n]);
  S.acts.updall = () => mk('install', st.h.updates.map((u) => u.name));
  S.acts.rep = async () => { try { await POST(`/api/hosts/${id}/report`); await load(); draw(); toast('отчёт запрошен'); } catch (e) { fail(e); } };
  S.acts.hdel = async () => {
    if (!await confirmDlg('Удалить устройство', `Устройство <b>${esc(st.h.name)}</b> и его отчёты будут удалены из панели. Агент на хосте перестанет авторизоваться (удалите его: <code>setup.sh --uninstall</code>).`, 'Удалить', true)) return;
    try { await api('DELETE', '/api/hosts/' + id); location.hash = '#/devices'; } catch (e) { fail(e); }
  };
  taskActs(async () => { await load(); draw(); });
  draw();
  every(10000, async () => { if ($('.modal') || $('details[open]', main) || document.activeElement === $('#dq', main)) return; try { await load(); draw(); } catch (e) { } });
}


/* ------------------------------------------------------------------ сканер сети */
async function vScanner(main) {
  const st = { d: null, sel: new Set(), cidr: '', mode: 'quick', filter: 'all' };
  async function load() { st.d = await GET('/api/scanner'); if (!st.cidr) st.cidr = st.d.networks[0] || ''; }
  await load();
  const running = () => st.d.jobs.some((j) => j.status === 'queued' || j.status === 'running');
  function draw() {
    const d = st.d;
    const scan = d.jobs.find((j) => j.op === 'scan');
    const scanRun = scan && (scan.status === 'running' || scan.status === 'queued');
    const inst = d.jobs.filter((j) => j.op === 'ssh_install');
    let list = d.discovered;
    if (st.filter === 'new') list = list.filter((x) => !x.host_id && x.alive);
    if (st.filter === 'agent') list = list.filter((x) => x.host_id);
    if (st.filter === 'linux') list = list.filter((x) => x.family === 'linux');
    const nNew = d.discovered.filter((x) => !x.host_id && x.alive).length;
    main.innerHTML = `<div class="cols2">
      <section class="box"><h2 class="t">Сканирование сети</h2>
        ${isAdmin() ? `<div class="row"><input class="inp grow" id="cidr" list="nets" value="${esc(st.cidr)}" placeholder="192.168.10.0/24">
          <datalist id="nets">${d.networks.map((n) => `<option value="${esc(n)}">`).join('')}</datalist>
          <select class="sel" id="smode"><option value="quick" ${st.mode === 'quick' ? 'selected' : ''}>быстрое · ${d.quick_ports.length} портов</option><option value="full" ${st.mode === 'full' ? 'selected' : ''}>расширенное · ${d.full_ports.length} портов</option></select>
          <button class="btn p" data-act="scan" ${scanRun ? 'disabled' : ''}>Сканировать</button></div>` : '<div class="note">Запускать сканирование может администратор.</div>'}
        ${scan ? `<div class="job"><div class="jh"><b>${esc(scan.title)}</b><span class="nowrap">${jobStatus(scan)}</span></div>
          ${scanRun ? `<div class="pbar"><i style="width:${Math.round((scan.progress || 0) * 100)}%"></i></div>` : ''}
          <div class="note">${esc(scan.created_by || '')} · ${fmtDT(scan.created)}${scan.message ? ' · ' + esc(scan.message) : ''}</div></div>` : ''}
        <div class="note">Сканирует сама панель, без root и без nmap: пробует подключиться к типовым портам (ответ «закрыто» тоже значит, что устройство в сети), берёт MAC-адреса из ARP-таблицы сервера, имена — из DNS (FreeIPA), версию SSH — из приветствия сервера. Только частные сети, не больше /22.</div>
      </section>
      <section class="box"><h2 class="t">Ключ панели для SSH</h2>
        ${d.pubkey ? `<pre class="code">${esc(d.pubkey)}</pre><div class="row"><button class="btn s" data-act="copy" data-t="${esc(d.pubkey)}">скопировать</button></div>
          <div class="note">Добавьте ключ на хост — и агента можно ставить без пароля:<br><code>echo '${esc(d.pubkey)}' | sudo tee -a /root/.ssh/authorized_keys</code><br>Для обычного пользователя — в его <code>~/.ssh/authorized_keys</code> и с sudo без пароля.</div>`
        : `<div class="note">Можно ставить агентов по паролю (он используется один раз и нигде не сохраняется) или создать ключ панели и разложить его на хосты.</div>${isAdmin() ? '<div class="row"><button class="btn" data-act="mkkey">Создать ключ панели</button></div>' : ''}`}
      </section></div>
      <section class="panel">
        <div class="bar"><b style="font-size:14px;letter-spacing:.1em;color:var(--cyan)">НАЙДЕННЫЕ УСТРОЙСТВА · ${d.discovered.length}</b>
          <div class="row" style="gap:4px">${[['all', 'все'], ['new', 'без агента · ' + nNew], ['linux', 'Linux'], ['agent', 'с агентом']].map(([k, t]) => `<button class="btn s ${st.filter === k ? 'p' : ''}" data-act="sf" data-v="${k}">${t}</button>`).join('')}</div>
          <div class="grow"></div>${isAdmin() && d.discovered.length ? '<button class="btn s" data-act="sclear">очистить список</button>' : ''}</div>
        ${st.sel.size ? `<div class="bar selbar"><b>Выбрано ${st.sel.size}</b><button class="btn link" data-act="snone">снять выбор</button><div class="grow"></div><button class="btn p" data-act="inst">Установить агента…</button></div>` : ''}
        <div class="tblwrap"><table class="t"><thead><tr>${isAdmin() ? '<th></th>' : ''}<th>Адрес</th><th>Имя</th><th>MAC · производитель</th><th>Система</th><th>Открытые порты</th><th>Виден</th><th></th></tr></thead>
        <tbody>${list.map((x) => `<tr class="${x.alive ? '' : 'old'} ${st.sel.has(x.ip) ? 's' : ''}">
          ${isAdmin() ? `<td>${x.host_id ? '' : `<input type="checkbox" class="cb" data-ch="ssel" data-ip="${esc(x.ip)}" ${st.sel.has(x.ip) ? 'checked' : ''} aria-label="выбрать">`}</td>` : ''}
          <td><b style="color:var(--text)">${esc(x.ip)}</b></td>
          <td>${esc(x.hostname || '—')}${x.host_id ? `<span class="sub"><a href="#/device/${x.host_id}">агент: ${esc(x.host_name)}</a></span>` : ''}</td>
          <td class="small">${esc(x.mac || '—')}${x.vendor ? '<span class="sub">' + esc(x.vendor) + '</span>' : ''}</td>
          <td class="wrap" style="min-width:220px;max-width:350px"><div class="row" style="gap:6px;flex-wrap:nowrap">${osBadge(x.family, x.osname)}${canOp() ? `<button class="btn link" data-act="osedit" data-ip="${esc(x.ip)}" title="указать систему">✎</button>` : ''}</div>
            <span class="sub">${x.os_src === 'manual' ? 'указано вручную' : x.os_src === 'agent' ? 'по данным агента' : x.osname ? 'предположение' : ''}${x.banner ? ' · <span title="' + esc(x.banner) + '">' + esc(x.banner.slice(0, 32)) + '</span>' : ''}</span></td>
          <td><div class="small" style="display:flex;flex-wrap:wrap;gap:4px;max-width:210px">${x.ports.map((p, i) => `<span class="tag dim" title="${esc(x.port_names[i])}">${p} ${esc(x.port_names[i])}</span>`).join('') || '<span class="muted">открытых нет</span>'}</div></td>
          <td class="muted small">${x.alive ? ago(x.last_seen) : 'не отвечает<span class="sub">' + ago(x.last_seen) + '</span>'}</td>
          <td class="nowrap">${x.host_id ? '<span class="tag ok">агент</span>' : isAdmin() && x.ports.includes(22) ? `<button class="btn s p" data-act="inst1" data-ip="${esc(x.ip)}" title="установить агента по SSH">Агент…</button>` : ''}
            ${isAdmin() ? `<button class="btn s" data-act="sdel" data-ip="${esc(x.ip)}" title="убрать из списка">✕</button>` : ''}</td></tr>`).join('')
          || `<tr><td colspan="8" class="empty">${d.discovered.length ? 'по фильтру ничего нет' : 'список пуст — запустите сканирование'}</td></tr>`}</tbody></table></div>
      </section>
      ${inst.length ? `<section class="box"><h2 class="t">Установка агентов</h2>${inst.map((j) => `<div class="job"><div class="jh"><b>${esc(j.title)}</b><span class="nowrap">${jobStatus(j)}</span></div>
        <div class="note">${esc(j.created_by || '')} · ${fmtDT(j.created)}${j.message ? ' · ' + esc(j.message) : ''}</div>
        ${j.log ? `<details><summary class="note">журнал</summary><pre class="out">${esc(j.log)}</pre></details>` : ''}</div>`).join('')}</section>` : ''}`;
  }
  function installDlg(ips) {
    const m = modal(`<h2 class="t">Установка агента по SSH</h2>
      <div class="note">Устройства: <b>${ips.map(esc).join(', ')}</b>. Панель подключится по SSH и выполнит команду подключения (ключ, .repo, агент). Нужен пользователь root или пользователь с sudo.</div>
      <div class="row" style="align-items:flex-end"><label class="lbl grow" style="display:flex;flex-direction:column;gap:6px">Пользователь<input class="inp" id="su" value="root" autocomplete="off"></label>
        <label class="lbl" style="display:flex;flex-direction:column;gap:6px">Порт<input class="inp" id="sp" value="22" style="width:90px"></label></div>
      <div class="row"><label class="chk"><input type="radio" name="sa" value="password" checked>по паролю</label>
        <label class="chk"><input type="radio" name="sa" value="key" ${st.d.pubkey ? '' : 'disabled'}>ключом панели${st.d.pubkey ? '' : ' (не создан)'}</label></div>
      <label class="lbl" id="pwl">Пароль SSH<input class="inp" type="password" id="spw" style="width:100%;margin-top:6px" autocomplete="new-password"></label>
      <label class="lbl">Пароль sudo <span class="muted" style="text-transform:none;letter-spacing:0">(если отличается или вход по ключу; для root не нужен)</span><input class="inp" type="password" id="ssu" style="width:100%;margin-top:6px" autocomplete="new-password"></label>
      <div class="alert ok" style="font-size:12px">Пароли используются один раз для этой установки: не записываются ни в базу, ни в журнал, ни на диск.</div>
      <div class="row" style="justify-content:flex-end"><button class="btn" data-close>Отмена</button><button class="btn p" id="sgo">Установить</button></div>`);
    m.el.querySelectorAll('input[name=sa]').forEach((r) => { r.onchange = () => { $('#pwl', m.el).style.display = r.value === 'key' && r.checked ? 'none' : ''; }; });
    $('#sgo', m.el).onclick = async () => {
      const auth = m.el.querySelector('input[name=sa]:checked').value;
      const body = { ips, user: $('#su', m.el).value.trim(), port: +$('#sp', m.el).value || 22, auth, password: $('#spw', m.el).value, sudo_password: $('#ssu', m.el).value };
      try { const r = await POST('/api/scanner/install', body); m.close(); toast(`установка запущена: ${r.jobs.length}`); st.sel.clear(); await load(); draw(); }
      catch (e) { fail(e); }
    };
  }
  S.acts.osedit = (el) => {
    const x = st.d.discovered.find((d) => d.ip === el.dataset.ip);
    if (!x) return;
    let fam = x.family || 'linux';
    const m = modal(`<h2 class="t">Система устройства ${esc(x.ip)}</h2>
      <div class="note">${esc(x.hostname || '')}${x.vendor ? ' · ' + esc(x.vendor) : ''}${x.banner ? ' · ' + esc(x.banner) : ''}</div>
      <div class="lbl">Тип</div>
      <div class="famgrid">${Object.keys(FAM_RU).map((f) => `<button type="button" class="fam ${f === fam ? 'on' : ''}" data-f="${f}">${osIcon(f)}${FAM_RU[f]}</button>`).join('')}</div>
      <label class="lbl" style="display:flex;flex-direction:column;gap:6px">Система, дистрибутив или модель
        <input class="inp" id="osn" list="ossug" value="${esc(x.os_src === 'manual' ? x.osname : (x.family === fam ? x.osname : ''))}" placeholder="например, РЕД ОС 8 или MikroTik RouterOS 7" autocomplete="off"></label>
      <datalist id="ossug"></datalist>
      <div class="row" style="justify-content:space-between"><button class="btn" id="osreset" ${x.os_src === 'manual' ? '' : 'disabled'}>Вернуть автоопределение</button>
        <span class="row"><button class="btn" data-close>Отмена</button><button class="btn p" id="ossave">Сохранить</button></span></div>`);
    const sug = () => { $('#ossug', m.el).innerHTML = (OS_SUGGEST[fam] || []).map((o) => `<option value="${esc(o)}">`).join(''); };
    sug();
    m.el.querySelectorAll('.fam').forEach((b) => { b.onclick = () => { fam = b.dataset.f; m.el.querySelectorAll('.fam').forEach((x2) => x2.classList.toggle('on', x2 === b)); sug(); }; });
    $('#ossave', m.el).onclick = async () => { try { await api('PUT', `/api/scanner/${x.ip}/os`, { family: fam, name: $('#osn', m.el).value }); m.close(); await load(); draw(); } catch (e) { fail(e); } };
    $('#osreset', m.el).onclick = async () => { try { await api('PUT', `/api/scanner/${x.ip}/os`, { reset: true }); m.close(); await load(); draw(); } catch (e) { fail(e); } };
  };
  S.acts.copy = (el) => copyText(el.dataset.t);
  S.acts.scan = async () => {
    st.cidr = $('#cidr').value.trim(); st.mode = $('#smode').value;
    try { await POST('/api/scanner/scan', { cidr: st.cidr, ports: st.mode }); await load(); draw(); } catch (e) { fail(e); }
  };
  S.acts.mkkey = async () => { try { await POST('/api/scanner/key'); await load(); draw(); } catch (e) { fail(e); } };
  S.acts.sf = (el) => { st.filter = el.dataset.v; draw(); };
  S.on.ssel = (el) => { el.checked ? st.sel.add(el.dataset.ip) : st.sel.delete(el.dataset.ip); draw(); };
  S.acts.snone = () => { st.sel.clear(); draw(); };
  S.acts.inst = () => installDlg([...st.sel]);
  S.acts.inst1 = (el) => installDlg([el.dataset.ip]);
  S.acts.sdel = async (el) => { try { await api('DELETE', '/api/scanner/' + el.dataset.ip); await load(); draw(); } catch (e) { fail(e); } };
  S.acts.sclear = async () => { if (await confirmDlg('Очистить список', 'Убрать все найденные устройства из списка? Подключённые хосты и агенты не затрагиваются.', 'Очистить', true)) { try { await api('DELETE', '/api/scanner'); await load(); draw(); } catch (e) { fail(e); } } };
  draw();
  let tick = 0;
  every(2000, async () => {
    tick++;
    if ($('.modal') || $('details[open]', main) || (document.activeElement && document.activeElement.id === 'cidr')) return;
    if (!running() && tick % 5) return;
    try { await load(); draw(); } catch (e) { }
  });
}

/* ------------------------------------------------------------------ назначения */
async function vAssign(main) {
  const st = { pk: [], hosts: [], tasks: [], selP: new Set(S.assignPre || []), selH: new Set(), action: 'install', sched: 'now', at: '', pq: '', hf: '', tf: 'active', evr: {} };
  S.assignPre = null;
  async function load() {
    const [p, h, t] = await Promise.all([GET('/api/packages'), GET('/api/hosts'), GET('/api/tasks' + (st.tf ? '?status=' + st.tf : ''))]);
    st.pk = p.packages; st.hosts = h.hosts; st.tasks = t.tasks;
  }
  await load();
  const names = () => { const m = new Map(); st.pk.forEach((p) => { if (!m.has(p.name)) m.set(p.name, []); m.get(p.name).push(p); }); return m; };
  const harch = (h) => ({ amd64: 'x86_64', arm64: 'aarch64' }[h.arch] || h.arch);
  function compat(name, h) { const l = names().get(name) || []; return l.find((p) => p.latest && p.arches.split(' ').includes(harch(h))); }
  function summary() {
    let ops = 0, skip = [], off = 0;
    for (const hid of st.selH) {
      const h = st.hosts.find((x) => x.id === hid); if (!h) continue;
      if (h.status !== 'online') off++;
      for (const n of st.selP) {
        if ((st.action === 'install' || st.action === 'update') && !compat(n, h)) { skip.push(`${h.name}: ${n}`); continue; }
        ops++;
      }
    }
    return { ops, skip, off };
  }
  function plistHtml() {
    const nm = names();
    const pq = st.pq.toLowerCase();
    const plist = [...nm.keys()].filter((n) => !pq || n.toLowerCase().includes(pq)).sort();
    return plist.map((n) => { const l = nm.get(n); const lt = l.filter((p) => p.latest); const ar = [...new Set(lt.flatMap((p) => p.arches.split(' ')))];
      const evs = [...new Set(lt.map((p) => p.evr))];
      const info = evs.length === 1 ? `${evs[0]} · ${ar.join(' · ')}` : lt.map((p) => `${p.arch} ${p.evr}`).join(' · ');
      return `<div class="it ${st.selP.has(n) ? 'on' : ''}" data-act="tp" data-n="${esc(n)}"><label><input type="checkbox" class="cb" ${st.selP.has(n) ? 'checked' : ''} tabindex="-1">${esc(n)}</label><span class="sm">${esc(info)}</span></div>`; }).join('') || '<div class="empty">нет пакетов</div>';
  }
  function draw() {
    const hl = st.hosts.filter((h) => !st.hf || harch(h) === st.hf);
    const sm = summary();
    const archs = [...new Set(st.hosts.map(harch))].filter(Boolean);
    main.innerHTML = `<div class="cols3">
      <section class="box"><h2 class="t"><i>1</i>Пакеты</h2>
        <input class="inp" placeholder="Найти пакет в HomeRepo" value="${esc(st.pq)}" data-in="pq" id="pq">
        <div class="list" id="plist">${plistHtml()}</div>
        <div class="note">Ставится последняя версия из HomeRepo; зависимости из репозиториев ОС подтянет dnf.</div>
      </section>
      <section class="box"><h2 class="t"><i>2</i>Устройства</h2>
        <div class="row"><button class="btn s ${!st.hf ? 'p' : ''}" data-act="hf" data-v="">все</button>${archs.map((a) => `<button class="btn s ${st.hf === a ? 'p' : ''}" data-act="hf" data-v="${esc(a)}">${esc(a)}</button>`).join('')}
          <div class="grow"></div><button class="btn link" data-act="hall">выбрать все</button></div>
        <div class="list">${hl.map((h) => { const inst = [...st.selP].map((n) => compat(n, h) ? '' : n).filter(Boolean);
          return `<div class="it ${st.selH.has(h.id) ? 'on' : ''}" data-act="th" data-id="${h.id}"><label><input type="checkbox" class="cb" ${st.selH.has(h.id) ? 'checked' : ''} tabindex="-1"><span class="dot ${h.status}"></span>${esc(h.name)}</label>
            <span class="sm ${h.status !== 'online' ? 'c-ex' : ''}">${esc(h.arch || '?')}${h.status !== 'online' ? ' · ' + ago(h.last_poll) : ''}${inst.length && (st.action === 'install' || st.action === 'update') ? ' · нет сборки: ' + esc(inst.join(', ')) : ''}</span></div>`; }).join('') || '<div class="empty">нет устройств</div>'}</div>
        ${sm.off ? `<div class="note c-ex">${sm.off} из выбранных устройств сейчас не на связи — назначение выполнится, когда агент выйдет на связь.</div>` : ''}
      </section>
      <section class="box"><h2 class="t"><i>3</i>Действие</h2>
        <div class="lbl">Что сделать</div>
        ${[['install', 'Установить или обновить до последней версии'], ['update', 'Только обновить, если уже стоит'], ['remove', 'Удалить с устройств'], ['hold', 'Держать текущую версию (запретить обновление)'], ['unhold', 'Снять фиксацию версии']].map(([v, t]) => `<label class="chk"><input type="radio" name="act" value="${v}" data-ch="act" ${st.action === v ? 'checked' : ''}>${t}</label>`).join('')}
        <div class="lbl" style="margin-top:6px">Когда</div>
        <select class="sel" data-ch="sched"><option value="now" ${st.sched === 'now' ? 'selected' : ''}>При следующем опросе агента (≤ 2 мин)</option><option value="night" ${st.sched === 'night' ? 'selected' : ''}>Ночью, 03:00–05:00</option><option value="at" ${st.sched === 'at' ? 'selected' : ''}>В выбранное время…</option></select>
        ${st.sched === 'at' ? `<input class="inp" type="datetime-local" data-ch="at" value="${esc(st.at)}">` : ''}
        <div class="sum">${st.selP.size} пакет(ов) × ${st.selH.size} устройств = ${sm.ops} операций${sm.skip.length ? `<br>пропущено (нет сборки под архитектуру): ${sm.skip.length}` : ''}${sm.off ? `<br>отложено (устройство не на связи): ${sm.off}` : ''}</div>
        ${canOp() ? `<button class="btn p" data-act="go" ${sm.ops ? '' : 'disabled'} style="justify-content:center">Назначить</button>` : '<div class="note">Назначать может оператор или администратор.</div>'}
      </section></div>
      <section class="panel">
        <div class="bar"><b style="font-size:14px;letter-spacing:.1em;color:var(--cyan)">НАЗНАЧЕНИЯ</b><div class="grow"></div>
          <select class="sel" data-ch="tf">${[['active', 'Активные'], ['', 'Все'], ['done', 'Выполнено'], ['error', 'С ошибкой']].map(([v, t]) => `<option value="${v}" ${st.tf === v ? 'selected' : ''}>${t}</option>`).join('')}</select></div>
        <div class="tblwrap"><table class="t"><thead><tr><th>Пакет</th><th>Действие</th><th>Устройство</th><th>Создал</th><th>Статус</th><th>Результат</th><th></th></tr></thead>
        <tbody>${st.tasks.map((t) => taskRow(t, true)).join('') || '<tr><td colspan="7" class="empty">нет назначений</td></tr>'}</tbody></table></div>
      </section>`;
  }
  S.acts.tp = (el, e) => { e.preventDefault(); const n = el.dataset.n; st.selP.has(n) ? st.selP.delete(n) : st.selP.add(n); draw(); };
  S.acts.th = (el, e) => { e.preventDefault(); const id = +el.dataset.id; st.selH.has(id) ? st.selH.delete(id) : st.selH.add(id); draw(); };
  S.acts.hf = (el) => { st.hf = el.dataset.v; draw(); };
  S.acts.hall = () => { st.hosts.filter((h) => !st.hf || harch(h) === st.hf).forEach((h) => st.selH.add(h.id)); draw(); };
  S.on.pq = (el) => { st.pq = el.value; const l = $('#plist', main); if (l) l.innerHTML = plistHtml(); };
  S.on.act = (el) => { st.action = el.value; draw(); };
  S.on.sched = (el) => { st.sched = el.value; draw(); };
  S.on.at = (el) => { st.at = el.value; };
  S.on.tf = async (el) => { st.tf = el.value; await load(); draw(); };
  S.acts.go = async () => {
    const body = { action: st.action, items: [...st.selP].map((n) => ({ name: n })), hosts: [...st.selH], schedule: st.sched };
    if (st.sched === 'at') { const t = Date.parse(st.at); if (!t) return toast('укажите время', 'err'); body.at = t / 1000; }
    try {
      const r = await POST('/api/tasks', body);
      toast(`создано назначений: ${r.created}${r.skipped.length ? '; пропущено: ' + r.skipped.length : ''}`, r.created ? 'ok' : 'err', 7000);
      st.selP.clear(); st.selH.clear(); st.tf = 'active'; await load(); draw();
    } catch (e) { fail(e); }
  };
  taskActs(async () => { await load(); draw(); });
  draw();
  every(5000, async () => { if (document.activeElement && document.activeElement.id === 'pq') return; if ($('details[open]', main)) return; try { const t = await GET('/api/tasks' + (st.tf ? '?status=' + st.tf : '')); st.tasks = t.tasks; draw(); } catch (e) { } });
}

/* ------------------------------------------------------------------ подключение */
async function vConnect(main) {
  const c = await GET('/api/connect');
  let tokens = [];
  async function loadTokens() { if (canOp()) tokens = (await GET('/api/tokens')).tokens; }
  await loadTokens();
  function draw(newTok) {
    main.innerHTML = `<div class="cols2"><div class="colstack">
      ${canOp() ? `<section class="box"><h2 class="t">Подключить устройство с агентом</h2>
        <div class="note">Одна команда на хосте: ставит ключ и .repo (старые файлы HomeRepo отключаются), затем агент <b>homerepo-agent</b> — таймер раз в 2 минуты, свой секрет для каждого хоста. Агент только отправляет отчёт и забирает назначения — входящих подключений к хосту нет.</div>
        ${newTok ? `<div class="alert ok" style="flex-direction:column;align-items:stretch"><div>Токен <b>${esc(newTok.token)}</b> действует до ${fmtDT(newTok.expires)}. Выполните на хосте:</div>
          <code class="cmd">${esc(newTok.command)}</code><div class="row"><button class="btn s" data-act="copy" data-t="${esc(newTok.command)}">скопировать команду</button></div></div>` : ''}
        <div class="row"><input class="inp grow" id="tnote" placeholder="Заметка (например, имя хоста)"><select class="sel" id="thours"><option value="1">1 час</option><option value="24" selected>24 часа</option><option value="168">7 дней</option></select>
          <label class="chk"><input type="checkbox" id="tmulti">для нескольких хостов</label><button class="btn p" data-act="newtok">Создать токен</button></div>
        ${tokens.length ? `<div class="tblwrap"><table class="t"><thead><tr><th>Токен</th><th>Создал</th><th>Действует до</th><th>Использован</th><th></th></tr></thead><tbody>${tokens.map((t) => `<tr><td class="wrap">#${t.id} ${esc(t.note || '')}${t.multi ? '<span class="sub"><span class="tag info">многоразовый</span></span>' : ''}</td><td class="muted">${esc(t.created_by || '')}<span class="sub">${fmtDT(t.created)}</span></td>
          <td class="${t.expires < Date.now() / 1000 ? 'c-bad' : ''}">${fmtDT(t.expires)}</td><td>${t.used ? `${esc(t.host || '?')}${t.uses > 1 ? ' и др. (' + t.uses + ')' : ''}` : '<span class="muted">нет</span>'}</td>
          <td><button class="btn s" data-act="deltok" data-id="${t.id}">удалить</button></td></tr>`).join('')}</tbody></table></div>` : ''}
      </section>` : ''}
      <section class="box"><h2 class="t">Только репозиторий, без агента</h2>
        <code class="cmd">${esc(c.repo_only_command)}</code><div class="row"><button class="btn s" data-act="copy" data-t="${esc(c.repo_only_command)}">скопировать</button></div>
        <div class="note">Или вручную: положите файл ниже в <b>/etc/yum.repos.d/homerepo.repo</b> и импортируйте ключ: <code>rpm --import ${esc(c.pub_url)}</code></div>
      </section></div>
      <div class="colstack">
        <section class="box"><h2 class="t">/etc/yum.repos.d/homerepo.repo</h2><pre class="code">${esc(c.repo_file)}</pre>
          <div class="row"><button class="btn s" data-act="copy" data-t="${esc(c.repo_file)}">скопировать</button><a class="btn s" href="/agent/homerepo.repo" download="homerepo.repo">скачать</a></div>
          <div class="note">Один файл для всех архитектур: dnf подставит $basearch (x86_64 или aarch64). Пакеты проверяются по подписи (gpgcheck=1), новые пакеты клиенты видят через 5 минут (metadata_expire=300).</div></section>
        <section class="box"><h2 class="t">Ключ</h2><dl class="kv"><dt>Отпечаток</dt><dd>${esc((c.key_fpr || '—').replace(/(.{4})/g, '$1 '))}</dd><dt>Файл</dt><dd><a href="${esc(c.pub_url)}">${esc(c.pub_url)}</a></dd><dt>Агенты</dt><dd>${esc(c.api_url)}</dd></dl>
          <div class="note">Удалить агента с хоста: <code>curl -fsS ${esc(c.setup_url)} | sudo bash -s -- --uninstall</code></div></section>
      </div></div>`;
  }
  S.acts.copy = (el) => copyText(el.dataset.t);
  S.acts.newtok = async () => {
    try { const r = await POST('/api/tokens', { note: $('#tnote').value, hours: +$('#thours').value, multi: $('#tmulti').checked }); await loadTokens(); draw(r); } catch (e) { fail(e); }
  };
  S.acts.deltok = async (el) => { try { await api('DELETE', '/api/tokens/' + el.dataset.id); await loadTokens(); draw(); } catch (e) { fail(e); } };
  draw();
}

/* ------------------------------------------------------------------ журнал */
const AUD_RU = {
  login: 'вход', login_failed: 'неудачный вход', logout: 'выход', password_changed: 'смена пароля', upload: 'загрузка пакета', fetch: 'скачивание по ссылке',
  package_resign: 'переподпись', resign_foreign: 'переподпись чужих', package_delete: 'удаление пакетов', package_move: 'перемещение пакетов',
  repo_metadata: 'пересборка метаданных', repo_rescan: 'сканирование', repo_cleanup: 'очистка мусора', repo_key: 'проверка ключа', job_cancel: 'отмена задания',
  assign: 'назначение', task_cancel: 'отмена назначения', task_retry: 'повтор назначения', collect: 'забрать в HomeRepo', collect_upload: 'пакет от агента',
  host_enroll: 'подключение устройства', host_delete: 'удаление устройства', token_create: 'токен создан', token_delete: 'токен удалён',
  report: 'просмотр отчёта', errors_ack: 'ошибки скрыты', job_retry: 'повтор задания', device_os: 'указана система устройства', scan: 'сканирование сети', scan_clear: 'очистка найденных', ssh_install_start: 'установка агента по SSH', ssh_install: 'результат установки по SSH', ssh_key_create: 'создан SSH-ключ панели',
  user_create: 'пользователь создан', user_delete: 'пользователь удалён', user_role: 'смена роли', user_disable: 'блокировка', user_enable: 'разблокировка', user_reset_password: 'сброс пароля',
};
async function vLog(main) {
  let q = '';
  async function draw() {
    const r = await GET('/api/audit?limit=300&q=' + encodeURIComponent(q));
    const body = r.audit.map((a) => `<tr><td class="muted">${fmtDT(a.ts)}</td><td>${esc(a.user || '—')}</td><td class="muted">${esc(a.ip || '')}</td>
      <td class="${a.action === 'login_failed' ? 'c-bad' : ''}">${esc(AUD_RU[a.action] || a.action)}</td><td class="wrap small">${esc(a.detail || '')}</td></tr>`).join('');
    $('#lb', main).innerHTML = body || '<tr><td colspan="5" class="empty">записей нет</td></tr>';
  }
  main.innerHTML = `<section class="panel"><div class="bar"><b style="font-size:14px;letter-spacing:.1em;color:var(--cyan)">ЖУРНАЛ ДЕЙСТВИЙ</b><div class="grow"></div>
    <input class="inp" style="flex:0 1 320px" placeholder="Поиск: пользователь, действие, адрес" data-in="lq"></div>
    <div class="tblwrap"><table class="t"><thead><tr><th>Когда</th><th>Кто</th><th>Откуда</th><th>Действие</th><th>Подробности</th></tr></thead><tbody id="lb"></tbody></table></div></section>`;
  let t;
  S.on.lq = (el) => { clearTimeout(t); t = setTimeout(() => { q = el.value; draw().catch(fail); }, 300); };
  await draw();
}

/* ------------------------------------------------------------------ пользователи */
async function vUsers(main) {
  if (!isAdmin()) { main.innerHTML = '<div class="alert bad">Раздел доступен только администратору.</div>'; return; }
  async function draw(flash) {
    const r = await GET('/api/users');
    main.innerHTML = `${flash || ''}<div class="cols2"><section class="panel"><div class="bar"><b style="font-size:14px;letter-spacing:.1em;color:var(--cyan)">ПОЛЬЗОВАТЕЛИ ПАНЕЛИ</b></div>
      <div class="tblwrap"><table class="t"><thead><tr><th>Имя</th><th>Роль</th><th>Состояние</th><th>Последний вход</th><th></th></tr></thead><tbody>
      ${r.users.map((u) => `<tr><td class="nm">${esc(u.name)}${u.name === S.me.user ? ' <span class="muted">(вы)</span>' : ''}</td>
        <td><select class="sel" data-ch="role" data-id="${u.id}">${['viewer', 'operator', 'admin'].map((x) => `<option value="${x}" ${u.role === x ? 'selected' : ''}>${ROLE_RU[x]}</option>`).join('')}</select></td>
        <td>${u.disabled ? '<span class="tag bad">заблокирован</span>' : u.must_change ? '<span class="tag warn">ждёт смены пароля</span>' : '<span class="tag ok">активен</span>'}</td>
        <td class="muted">${fmtDT(u.last_login)}</td>
        <td class="nowrap"><button class="btn s" data-act="ureset" data-id="${u.id}" data-n="${esc(u.name)}">сбросить пароль</button>
          <button class="btn s" data-act="udis" data-id="${u.id}" data-v="${u.disabled ? 0 : 1}">${u.disabled ? 'разблокировать' : 'заблокировать'}</button>
          <button class="btn s d" data-act="udel" data-id="${u.id}" data-n="${esc(u.name)}">удалить</button></td></tr>`).join('')}</tbody></table></div></section>
      <section class="box"><h2 class="t">Новый пользователь</h2>
        <label class="lbl">Имя<input class="inp" id="un" style="width:100%;margin-top:6px"></label>
        <label class="lbl">Роль<select class="sel" id="ur" style="width:100%;margin-top:6px"><option value="viewer">наблюдатель — только просмотр</option><option value="operator">оператор — пакеты, подпись, назначения</option><option value="admin">администратор — всё, включая пользователей</option></select></label>
        <button class="btn p" data-act="unew">Создать</button>
        <div class="note">Панель выдаст временный пароль — при первом входе пользователь сменит его. Из консоли сервера: <code>homerepo user add ИМЯ --role operator</code>.</div></section></div>`;
  }
  const pwFlash = (name, pw) => `<div class="alert ok"><div>Временный пароль для <b>${esc(name)}</b>: <b style="font-size:15px;letter-spacing:.04em">${esc(pw)}</b> — передайте его пользователю, больше он показан не будет.</div><button class="btn s" data-act="copy" data-t="${esc(pw)}">скопировать</button></div>`;
  S.acts.copy = (el) => copyText(el.dataset.t);
  S.acts.unew = async () => { try { const r = await POST('/api/users', { name: $('#un').value.trim(), role: $('#ur').value }); await draw(pwFlash(r.name, r.password)); } catch (e) { fail(e); } };
  S.on.role = async (el) => { try { await api('PUT', '/api/users/' + el.dataset.id, { role: el.value }); toast('роль изменена'); } catch (e) { fail(e); await draw(); } };
  S.acts.udis = async (el) => { try { await api('PUT', '/api/users/' + el.dataset.id, { disabled: el.dataset.v === '1' }); await draw(); } catch (e) { fail(e); } };
  S.acts.ureset = async (el) => {
    if (!await confirmDlg('Сбросить пароль', `Пользователь <b>${esc(el.dataset.n)}</b> получит временный пароль, его сессии будут завершены.`, 'Сбросить')) return;
    try { const r = await api('PUT', '/api/users/' + el.dataset.id, { reset_password: true }); await draw(pwFlash(el.dataset.n, r.password)); } catch (e) { fail(e); }
  };
  S.acts.udel = async (el) => {
    if (!await confirmDlg('Удалить пользователя', `Удалить <b>${esc(el.dataset.n)}</b>? Записи журнала сохранятся.`, 'Удалить', true)) return;
    try { await api('DELETE', '/api/users/' + el.dataset.id); await draw(); } catch (e) { fail(e); }
  };
  await draw();
}

async function vAccount(main) {
  main.innerHTML = `<section class="box" style="max-width:520px"><h2 class="t">Смена пароля</h2>
    <form id="pf" class="colstack" style="gap:12px">
      <label class="lbl">Текущий пароль<input class="inp" type="password" name="old" style="width:100%;margin-top:6px" autocomplete="current-password" required></label>
      <label class="lbl">Новый пароль<input class="inp" type="password" name="new" style="width:100%;margin-top:6px" autocomplete="new-password" required minlength="10"></label>
      <label class="lbl">Повторите<input class="inp" type="password" name="new2" style="width:100%;margin-top:6px" autocomplete="new-password" required></label>
      <button class="btn p">Сменить</button></form>
    <div class="note">Не короче 10 символов, без имени пользователя. После смены остальные ваши сессии завершатся.</div></section>`;
  $('#pf').onsubmit = async (e) => {
    e.preventDefault(); const f = e.target;
    if (f.new.value !== f.new2.value) return toast('пароли не совпадают', 'err');
    try { await POST('/api/password', { old: f.old.value, new: f.new.value }); toast('пароль изменён'); f.reset(); } catch (er) { fail(er); }
  };
}

const VIEWS = { packages: vPackages, signing: vSigning, devices: vDevices, scanner: vScanner, device: vDevice, assign: vAssign, connect: vConnect, log: vLog, users: vUsers, account: vAccount };
boot();
