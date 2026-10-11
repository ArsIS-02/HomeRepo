'use strict';
/* HomeRepo — раздел «Плейбуки»: список и рецепты, конструктор с живым YAML, выполнение в реальном времени.
   Использует помощники из app.js (GET, POST, modal, esc, S.acts/S.on …). */

const RUN_MODE_RU = { check: 'проверка', run: 'выполнение', syntax: 'синтаксис' };
const RUN_ST = {
  queued: '<span class="tag info">в очереди</span>', running: '<span class="tag run">выполняется</span>',
  cancel: '<span class="tag run">останавливается</span>', done: '<span class="tag ok">готово</span>',
  failed: '<span class="tag bad">ошибка</span>', cancelled: '<span class="tag dim">остановлено</span>',
};
const ACC_RU = {
  ok: '<span class="tag ok">доступ есть</span>', none: '<span class="tag dim">нет доступа</span>',
  pending: '<span class="tag info">ждёт агента</span>', error: '<span class="tag bad">ошибка</span>',
  stale: '<span class="tag warn">ключ сменился</span>',
};
const RES_ICON = { ok: '✓', changed: '●', failed: '✕', unreachable: '⚠', skipped: '–', ignored: '!' };
const RES_RU = { ok: 'без изменений', changed: 'изменено', failed: 'ошибка', unreachable: 'хост недоступен', skipped: 'пропущено', ignored: 'ошибка (пропущена)' };
const runSt = (s) => RUN_ST[s] || esc(s);
function dur(a, b) { if (!a) return '—'; const s = Math.max(0, Math.round((b || Date.now() / 1000) - a)); return s < 60 ? s + ' с' : Math.floor(s / 60) + ' мин ' + (s % 60) + ' с'; }
function groupRu(g) {
  if (g === 'homerepo') return 'все с агентом';
  if (g.startsWith('os_')) return g.slice(3).replace(/_/g, ' ');
  if (g.startsWith('arch_')) return g.slice(5);
  return g;
}

/* подсветка YAML без библиотек: комментарии, ключи, строки, шаблоны Jinja */
function hlYaml(text) {
  return text.split('\n').map((ln) => {
    if (/^\s*#/.test(ln)) return `<span class="yc">${esc(ln)}</span>`;
    const m = ln.match(/^(\s*-?\s*)([A-Za-z_][\w.\-]*)(:)(\s.*|$)/);
    let out;
    if (m) out = `${esc(m[1])}<span class="yk">${esc(m[2])}</span>${m[3]}${esc(m[4])}`;
    else out = esc(ln);
    return out.replace(/\{\{.*?\}\}/g, (x) => `<span class="yj">${x}</span>`);
  }).join('\n');
}

function downloadText(name, text) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: 'text/yaml;charset=utf-8' }));
  a.download = name; document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
}

async function askSecrets(list) {
  if (!list || !list.length) return {};
  return new Promise((res) => {
    let result = null;
    const m = modal(`<h2 class="t">Секретные переменные</h2>
      <div class="note">Значения передаются только в этот запуск: не сохраняются ни в базе, ни в журнале, ни в выводе Ansible.</div>
      ${list.map((s) => `<label class="lbl" style="display:flex;flex-direction:column;gap:6px">${esc(s.label || s.name)} <span class="muted" style="text-transform:none;letter-spacing:0">{{ ${esc(s.name)} }}</span>
        <input class="inp" type="password" data-sec="${esc(s.name)}" autocomplete="new-password"></label>`).join('')}
      <div class="row" style="justify-content:flex-end"><button class="btn" data-close>Отмена</button><button class="btn p" id="secok">Запустить</button></div>`, true, () => res(result));
    m.el.querySelector('#secok').onclick = () => {
      const r = {};
      for (const i of m.el.querySelectorAll('[data-sec]')) { if (!i.value) { i.focus(); return toast('заполните все поля', 'err'); } r[i.dataset.sec] = i.value; }
      result = r; m.close();
    };
  });
}

/* ------------------------------------------------------------------ список плейбуков */
async function vPlaybooks(main) {
  const st = { sel: new Set() };
  async function load() {
    const [p, s, r, t, rc] = await Promise.all([GET('/api/playbooks'), GET('/api/ansible/status'), GET('/api/runs?limit=15'),
      GET('/api/ansible/targets'), GET('/api/ansible/recipes')]);
    Object.assign(st, { pbs: p.playbooks, status: s, runs: r.runs, targets: t.targets, recipes: rc.recipes });
  }
  await load();
  function draw() {
    const s = st.status, ri = s.runner;
    const okN = st.targets.filter((t) => t.access.state === 'ok' || t.local).length;
    main.innerHTML = `
      <div class="tiles">
        <div class="tile"><b>${st.pbs.length}</b><span>плейбуков</span></div>
        <div class="tile"><b style="color:${okN ? 'var(--ok)' : 'var(--warn)'}">${okN} / ${st.targets.length}</b><span>хостов с доступом Ansible</span></div>
        <div class="tile"><b>${s.running}</b><span>выполняется сейчас</span></div>
        <div class="tile"><b style="font-size:20px;line-height:1.7;color:${ri.alive && ri.ansible ? 'var(--cyan)' : 'var(--bad)'}">${ri.alive ? (ri.ansible ? 'Ansible ' + esc(ri.ansible) : 'нет Ansible') : 'не отвечает'}</b><span>исполнитель</span></div>
      </div>
      ${!ri.alive ? '<div class="alert bad"><div>Служба-исполнитель не отвечает: <code>systemctl status homerepo-runner</code>. Плейбуки встанут в очередь и выполнятся, когда она запустится.</div></div>' : ''}
      ${ri.alive && !ri.ansible ? '<div class="alert warn"><div>На сервере HomeRepo не установлен Ansible: <code>sudo dnf install ansible</code> (или <code>ansible-core</code>). Конструктор работает и без него, но запускать плейбуки будет нечем.</div></div>' : ''}
      <div class="cols2" style="grid-template-columns:minmax(0,1.7fr) minmax(0,1fr)">
        <div class="colstack">
          <section class="panel"><div class="bar"><b class="ttl">МОИ ПЛЕЙБУКИ · ${st.pbs.length}</b><div class="grow"></div>
            ${canOp() ? '<button class="btn" data-act="pbimport">Импорт YAML</button><button class="btn p" data-act="pbnew">Новый плейбук</button>' : ''}</div>
            <div class="tblwrap"><table class="t"><thead><tr><th>Плейбук</th><th>Шагов</th><th>Последний запуск</th><th>Изменён</th><th></th></tr></thead><tbody>
            ${st.pbs.map((p) => `<tr class="clk" data-act="pbopen" data-id="${p.id}"><td class="nm">${esc(p.name)}<span class="sub">${esc(p.description || (p.raw ? 'YAML' : ''))}</span></td>
              <td>${p.raw ? '<span class="tag dim">YAML</span>' : p.steps}</td>
              <td>${p.last_run ? `${runSt(p.last_run.status)}<span class="sub">${esc(RUN_MODE_RU[p.last_run.mode] || '')} · ${ago(p.last_run.finished || p.last_run.created)}</span>` : '<span class="muted">не запускался</span>'}</td>
              <td class="muted">${ago(p.updated)}<span class="sub">${esc(p.updated_by || '')}</span></td>
              <td class="nowrap">${canOp() ? `<button class="btn s" data-act="pbcopy" data-id="${p.id}" title="копия">⧉</button>` : ''}</td></tr>`).join('')
              || '<tr><td colspan="5" class="empty">плейбуков пока нет — начните с рецепта ниже или создайте новый</td></tr>'}
            </tbody></table></div></section>
          <section class="box"><h2 class="t">Начать с рецепта</h2>
            <div class="recipes">${st.recipes.map((r) => `<button class="recipe" data-act="pbrecipe" data-r="${esc(r.id)}" ${canOp() ? '' : 'disabled'}>
              <b>${esc(r.name)}</b><span>${esc(r.description)}</span><i>${r.steps.map(esc).join(' → ')}</i></button>`).join('')}</div>
            <div class="note">Рецепт создаёт обычный плейбук — его можно поправить в конструкторе. Значения вроде домена или адреса сервера журналов замените на свои.</div>
          </section>
          <section class="panel"><div class="bar"><b class="ttl">ПОСЛЕДНИЕ ЗАПУСКИ</b></div>
            <div class="tblwrap"><table class="t"><thead><tr><th>#</th><th>Плейбук</th><th>Режим</th><th>Хосты</th><th>Статус</th><th>Кто, когда</th><th>Длилось</th></tr></thead><tbody>
            ${st.runs.map((r) => `<tr class="clk" data-act="runopen" data-id="${r.id}"><td class="muted">${r.id}</td><td>${esc(r.name)}</td><td>${esc(RUN_MODE_RU[r.mode] || r.mode)}</td>
              <td class="wrap small" style="max-width:220px">${esc(r.hosts.join(', '))}</td><td>${runSt(r.status)}${r.message ? `<span class="sub">${esc(r.message)}</span>` : ''}</td>
              <td class="muted">${esc(r.created_by || '')}<span class="sub">${fmtDT(r.created)}</span></td><td class="muted">${dur(r.started, r.finished)}</td></tr>`).join('')
              || '<tr><td colspan="7" class="empty">запусков ещё не было</td></tr>'}</tbody></table></div></section>
        </div>
        <div class="colstack">${accessBox(st)}</div>
      </div>`;
  }
  S.acts.pbopen = (el, e) => { if (e.target.closest('button')) return; location.hash = '#/playbook/' + el.dataset.id; };
  S.acts.runopen = (el) => { location.hash = '#/run/' + el.dataset.id; };
  S.acts.pbnew = async () => {
    try { const r = await POST('/api/playbooks', { name: 'Новый плейбук', model: { become: true, gather_facts: true, steps: [] } }); location.hash = '#/playbook/' + r.id; } catch (e) { fail(e); }
  };
  S.acts.pbrecipe = async (el) => { try { const r = await POST('/api/playbooks', { recipe: el.dataset.r }); location.hash = '#/playbook/' + r.id; } catch (e) { fail(e); } };
  S.acts.pbcopy = async (el) => { try { await POST(`/api/playbooks/${el.dataset.id}/copy`); toast('копия создана'); await load(); draw(); } catch (e) { fail(e); } };
  S.acts.pbimport = () => {
    const m = modal(`<h2 class="t">Импорт плейбука YAML</h2>
      <label class="lbl" style="display:flex;flex-direction:column;gap:6px">Название<input class="inp" id="imn" placeholder="например, Настройка Graylog"></label>
      <label class="lbl" style="display:flex;flex-direction:column;gap:6px">YAML<textarea class="inp code" id="imy" rows="14" placeholder="- name: ...\n  hosts: all\n  tasks: ..."></textarea></label>
      <div class="note">Плейбук сохранится как есть и откроется в редакторе YAML. Группы из hosts: будут указывать на хосты, выбранные при запуске.</div>
      <div class="row" style="justify-content:flex-end"><button class="btn" data-close>Отмена</button><button class="btn p" id="imok">Импортировать</button></div>`);
    m.el.querySelector('.dlg').classList.add('wide');
    m.el.querySelector('#imok').onclick = async () => {
      try { const r = await POST('/api/playbooks', { name: $('#imn', m.el).value || 'Импортированный плейбук', raw: true, yaml: $('#imy', m.el).value }); m.close(); location.hash = '#/playbook/' + r.id; } catch (e) { fail(e); }
    };
  };
  accessActs(st, async () => { await load(); draw(); });
  draw();
  every(5000, async () => { if ($('.modal')) return; try { await load(); draw(); } catch (e) { } });
}

function accessBox(st) {
  const ri = st.status.runner;
  return `<section class="box"><h2 class="t">Доступ Ansible к хостам</h2>
    <div class="note">Агент HomeRepo создаёт на хосте пользователя <b>${esc(ri.remote_user)}</b>, кладёт ему открытый ключ исполнителя и даёт sudo без пароля. Ключи вручную раскладывать не нужно; отзыв убирает ключ, sudo и самого пользователя.</div>
    <div class="list" style="max-height:360px">${st.targets.map((t) => `<div class="it ${st.sel.has(t.id) ? 'on' : ''}" data-act="acsel" data-id="${t.id}">
      <label><input type="checkbox" class="cb" ${st.sel.has(t.id) ? 'checked' : ''} tabindex="-1"><span class="dot ${t.status}"></span>${esc(t.name)}</label>
      <span class="sm">${t.local ? '<span class="tag info">этот сервер, локально</span>' : (ACC_RU[t.access.state] || esc(t.access.state))}</span></div>`).join('') || '<div class="empty">устройств с агентом нет</div>'}</div>
    ${isAdmin() ? `<div class="row"><button class="btn p" data-act="acgrant" ${st.sel.size ? '' : 'disabled'}>Разрешить Ansible</button><button class="btn" data-act="acrevoke" ${st.sel.size ? '' : 'disabled'}>Отозвать</button>
      <div class="grow"></div><button class="btn link" data-act="acall">выбрать без доступа</button></div>` : ''}
    <dl class="kv"><dt>Ключ исполнителя</dt><dd>${esc(ri.fingerprint || 'ещё не создан')}</dd><dt>Ansible</dt><dd>${esc(ri.ansible || 'не установлен')}</dd></dl>
    <div class="note">Агент выполнит назначение при следующем опросе (до 2 минут). Доступ к самому серверу HomeRepo даёт исполнителю права root на сервере — выдавайте его осознанно.</div>
  </section>`;
}
function accessActs(st, reload) {
  S.acts.acsel = (el) => { const id = +el.dataset.id; st.sel.has(id) ? st.sel.delete(id) : st.sel.add(id); reload(); };
  S.acts.acall = () => { st.targets.forEach((t) => { if (t.access.state !== 'ok' && !t.local) st.sel.add(t.id); }); reload(); };
  const go = async (op) => {
    const names = st.targets.filter((t) => st.sel.has(t.id)).map((t) => t.name);
    const txt = op === 'grant'
      ? `Агент на <b>${names.map(esc).join(', ')}</b> создаст пользователя для Ansible с ключом исполнителя HomeRepo и sudo без пароля.`
      : `Агент на <b>${names.map(esc).join(', ')}</b> уберёт ключ исполнителя и sudo, а пользователя, созданного для Ansible, удалит.`;
    if (!await confirmDlg(op === 'grant' ? 'Разрешить Ansible' : 'Отозвать доступ Ansible', txt, op === 'grant' ? 'Разрешить' : 'Отозвать', op !== 'grant')) return;
    try { await POST('/api/ansible/access', { hosts: [...st.sel], op }); st.sel.clear(); toast('назначено: агенты выполнят при следующем опросе'); reload(); } catch (e) { fail(e); }
  };
  S.acts.acgrant = () => go('grant');
  S.acts.acrevoke = () => go('revoke');
}

/* ------------------------------------------------------------------ конструктор */
async function vPlaybook(main, id) {
  const st = { id: +id, open: new Set(), errors: {}, dirty: false, selH: new Set(), hf: '' };
  const [pb, cat, tg] = await Promise.all([GET('/api/playbooks/' + id), GET('/api/ansible/catalog'), GET('/api/ansible/targets')]);
  st.pb = pb; st.cat = cat; st.targets = tg.targets; st.runner = tg.runner;
  st.raw = !!pb.raw; st.yaml = pb.yaml; st.name = pb.name; st.desc = pb.description || '';
  st.model = pb.model || { become: true, gather_facts: true, steps: [] };
  st.model.steps = st.model.steps || []; st.model.vars = st.model.vars || []; st.model.secrets = st.model.secrets || [];
  st.secrets = pb.secrets || [];
  if (st.model.steps.length && st.model.steps.length <= 3) st.model.steps.forEach((_, i) => st.open.add(i));
  st.targets.filter((t) => t.access.state === 'ok' || t.local).forEach((t) => st.selH.add(t.id));
  const B = {}; cat.blocks.forEach((b) => { B[b.type] = b; });
  const ro = !canOp();

  function fieldHtml(i, f, v) {
    const base = `data-i="${i}" data-k="${esc(f.key)}"`;
    const lab = (inner, wide) => `<label class="fld${wide ? ' wide' : ''}"><span class="lbl">${esc(f.label)}${f.required ? ' *' : ''}</span>${inner}${f.help ? `<span class="hint">${esc(f.help)}</span>` : ''}</label>`;
    if (v === undefined) v = f.default !== undefined ? f.default : '';
    const dis = ro ? 'disabled' : '';
    switch (f.kind) {
      case 'bool': return `<label class="chk fld"><input type="checkbox" data-ch="spb" ${base} ${v ? 'checked' : ''} ${dis}>${esc(f.label)}</label>`;
      case 'select':
        if (f.editable) return lab(`<input class="inp" list="dl-${esc(f.key)}" data-in="sp" ${base} value="${esc(v)}" ${dis}><datalist id="dl-${esc(f.key)}">${f.options.map((o) => `<option value="${esc(o[0])}">`).join('')}</datalist>`);
        return lab(`<select class="sel" data-ch="sp" ${base} ${dis}>${f.options.map((o) => `<option value="${esc(o[0])}" ${String(v) === String(o[0]) ? 'selected' : ''}>${esc(o[1])}</option>`).join('')}</select>`);
      case 'number': return lab(`<input class="inp" type="number" data-in="sp" ${base} value="${esc(v)}" placeholder="${esc(f.placeholder || '')}" ${dis}>`);
      case 'code': return lab(`<textarea class="inp code" rows="5" data-in="sp" ${base} placeholder="${esc(f.placeholder || '')}" ${dis}>${esc(v)}</textarea>`, true);
      case 'lines': return lab(`<textarea class="inp code" rows="3" data-in="sp" ${base} placeholder="${esc(f.placeholder || '')}" ${dis}>${esc(Array.isArray(v) ? v.join('\n') : v)}</textarea>`, true);
      case 'repo_packages': {
        const sel = Array.isArray(v) ? v : String(v || '').split(/[\s,]+/).filter(Boolean);
        return lab(`<div class="chips">${sel.map((n) => `<span class="chip">${esc(n)}${ro ? '' : `<button type="button" data-act="pkrm" ${base} data-n="${esc(n)}" title="убрать">×</button>`}</span>`).join('')}
          ${ro ? '' : `<input class="inp" list="dl-repo" data-ch="pkadd" ${base} placeholder="добавить пакет HomeRepo…" style="flex:1 1 180px">`}</div>`, true);
      }
      case 'branches': {
        const sel = Array.isArray(v) ? v : String(v || '').split(/[\s,]+/).filter(Boolean);
        return lab(`<div class="row" style="gap:14px">${cat.branches.map((b) => `<label class="chk"><input type="checkbox" data-ch="brs" ${base} data-b="${esc(b)}" ${sel.includes(b) ? 'checked' : ''} ${dis}>${esc(b)}</label>`).join('')}</div>`);
      }
      default: return lab(`<input class="inp" data-in="sp" ${base} value="${esc(v)}" placeholder="${esc(f.placeholder || '')}" ${dis}${f.kind === 'secret' ? ' spellcheck="false"' : ''}>`);
    }
  }
  function stepHtml(s, i) {
    const b = B[s.type] || { title: s.type, fields: [], desc: '' };
    const open = st.open.has(i);
    const err = st.errors[i];
    return `<div class="step ${open ? 'open' : ''} ${err ? 'err' : ''}" id="step-${i}">
      <div class="stephead" data-act="stoggle" data-i="${i}">
        <span class="num">${i + 1}</span><span class="grow"><b>${esc(b.title)}</b><span class="stt" id="stt-${i}">${s.title ? ' · ' + esc(s.title) : ''}</span>
          ${s.only_os ? `<span class="tag dim">${esc((cat.os_options.find((o) => o.key === s.only_os) || {}).title || s.only_os)}</span>` : ''}</span>
        <span class="serr" id="serr-${i}">${err ? esc(err) : ''}</span>
        ${ro ? '' : `<span class="ctl"><button class="btn s" data-act="sup" data-i="${i}" title="выше" ${i ? '' : 'disabled'}>↑</button><button class="btn s" data-act="sdown" data-i="${i}" title="ниже" ${i < st.model.steps.length - 1 ? '' : 'disabled'}>↓</button>
          <button class="btn s" data-act="sdup" data-i="${i}" title="повторить шаг">⧉</button><button class="btn s d" data-act="sdel" data-i="${i}" title="удалить шаг">✕</button></span>`}
        <span class="chev">${open ? '▾' : '▸'}</span></div>
      ${open ? `<div class="stepbody"><div class="note">${esc(b.desc)}</div>
        <div class="fields">${b.fields.map((f) => fieldHtml(i, f, (s.params || {})[f.key])).join('')}</div>
        <details class="more" ${s.title || s.only_os || s.no_become || s.ignore_errors || s.notify ? 'open' : ''}><summary>Дополнительно: название, условие, sudo, перезапуск службы</summary>
          <div class="fields">
            <label class="fld"><span class="lbl">Название шага</span><input class="inp" data-in="stitle" data-i="${i}" value="${esc(s.title || '')}" placeholder="${esc(b.title)}" ${ro ? 'disabled' : ''}></label>
            <label class="fld"><span class="lbl">Выполнять</span><select class="sel" data-ch="sos" data-i="${i}" ${ro ? 'disabled' : ''}>${cat.os_options.map((o) => `<option value="${esc(o.key)}" ${(s.only_os || '') === o.key ? 'selected' : ''}>${esc(o.title)}</option>`).join('')}</select></label>
            <label class="fld"><span class="lbl">Если изменилось — перезапустить службу</span><input class="inp" data-in="snotify" data-i="${i}" value="${esc(s.notify || '')}" placeholder="например, nginx" ${ro ? 'disabled' : ''}></label>
            <label class="chk fld"><input type="checkbox" data-ch="sflag" data-k="no_become" data-i="${i}" ${s.no_become ? 'checked' : ''} ${ro ? 'disabled' : ''}>без sudo</label>
            <label class="chk fld"><input type="checkbox" data-ch="sflag" data-k="ignore_errors" data-i="${i}" ${s.ignore_errors ? 'checked' : ''} ${ro ? 'disabled' : ''}>продолжать при ошибке</label>
          </div></details></div>` : ''}
    </div>`;
  }
  function targetsHtml() {
    const groups = [...new Set(st.targets.flatMap((t) => t.groups))];
    const list = st.targets.filter((t) => !st.hf || t.groups.includes(st.hf));
    const noacc = [...st.selH].map((i) => st.targets.find((t) => t.id === i)).filter((t) => t && !t.local && t.access.state !== 'ok');
    return `<div class="row" style="gap:4px"><button class="btn s ${!st.hf ? 'p' : ''}" data-act="thf" data-v="">все</button>${groups.map((g) => `<button class="btn s ${st.hf === g ? 'p' : ''}" data-act="thf" data-v="${esc(g)}">${esc(groupRu(g))}</button>`).join('')}
        <div class="grow"></div><button class="btn link" data-act="tall">все</button><button class="btn link" data-act="tnone">никого</button></div>
      <div class="list" style="max-height:230px">${list.map((t) => `<div class="it ${st.selH.has(t.id) ? 'on' : ''}" data-act="tsel" data-id="${t.id}">
        <label><input type="checkbox" class="cb" ${st.selH.has(t.id) ? 'checked' : ''} tabindex="-1"><span class="dot ${t.status}"></span>${esc(t.name)}</label>
        <span class="sm">${esc((t.os_name || '') + ' ' + (t.os_version || ''))} · ${esc(t.arch || '')} ${t.local ? '<span class="tag info">локально</span>' : (t.access.state === 'ok' ? '' : ACC_RU[t.access.state] || '')}</span></div>`).join('') || '<div class="empty">нет устройств</div>'}</div>
      ${noacc.length ? `<div class="alert warn" style="font-size:12px"><div>У ${noacc.length} из выбранных хостов нет доступа Ansible — выдайте его в разделе «Плейбуки» (справа) или настройте SSH-ключ вручную.</div></div>` : ''}`;
  }
  function draw() {
    const m = st.model;
    main.innerHTML = `
      <div class="box" style="flex-direction:row;flex-wrap:wrap;align-items:flex-end;gap:12px 18px">
        <div class="grow" style="min-width:280px;display:flex;flex-direction:column;gap:8px">
          <div class="note"><a href="#/playbooks">Плейбуки</a> / ${st.raw ? 'YAML' : 'конструктор'} <span id="dirty" class="${st.dirty ? 'c-ex' : 'muted'}">${st.dirty ? '· есть несохранённые изменения' : '· сохранено'}</span></div>
          <input class="inp title-inp" data-in="pname" value="${esc(st.name)}" placeholder="Название плейбука" ${ro ? 'disabled' : ''}>
          <input class="inp" data-in="pdesc" value="${esc(st.desc)}" placeholder="Что делает этот плейбук (необязательно)" ${ro ? 'disabled' : ''}>
        </div>
        ${ro ? '' : `<div class="row"><button class="btn p" data-act="pbsave">Сохранить</button>${!st.raw ? '<button class="btn" data-act="pbraw" title="перевести плейбук в ручное редактирование YAML">YAML вручную</button>' : ''}
          <button class="btn d" data-act="pbdel">Удалить</button></div>`}
      </div>
      <div class="cols2 pbgrid">
        <div class="colstack">
          ${st.raw ? `<section class="box"><h2 class="t">YAML плейбука</h2>
              <textarea class="inp code yamledit" data-in="rawyaml" spellcheck="false" ${ro ? 'disabled' : ''}>${esc(st.yaml)}</textarea>
              <div class="note">Плейбук редактируется вручную, конструктор для него недоступен. Хосты выбираются при запуске; группы из <code>hosts:</code> указывают на выбранные хосты.</div></section>`
          : `<section class="box"><h2 class="t">Параметры</h2>
              <div class="fields">
                <label class="chk fld"><input type="checkbox" data-ch="pbecome" ${m.become !== false ? 'checked' : ''} ${ro ? 'disabled' : ''}>выполнять с sudo (become)</label>
                <label class="chk fld"><input type="checkbox" data-ch="pgather" ${m.gather_facts !== false ? 'checked' : ''} ${ro ? 'disabled' : ''}>собирать сведения о хостах (facts)</label>
                <label class="fld"><span class="lbl">Хостов за раз</span><input class="inp" type="number" min="0" data-in="pserial" value="${esc(m.serial || 0)}" ${ro ? 'disabled' : ''}><span class="hint">0 — все сразу; 1 — по одному (удобно для перезагрузок)</span></label>
              </div>
              <details class="more" ${m.vars.length || m.secrets.length ? 'open' : ''}><summary>Переменные и секреты · ${m.vars.length + m.secrets.length}</summary>
                <div class="lbl" style="margin-top:10px">Переменные (используйте в полях как {{ имя }})</div>
                ${m.vars.map((v, i) => `<div class="row"><input class="inp" style="flex:0 1 200px" data-in="vname" data-i="${i}" value="${esc(v.name)}" placeholder="имя" ${ro ? 'disabled' : ''}><input class="inp grow" data-in="vval" data-i="${i}" value="${esc(v.value)}" placeholder="значение" ${ro ? 'disabled' : ''}>${ro ? '' : `<button class="btn s d" data-act="vdel" data-i="${i}">✕</button>`}</div>`).join('')}
                ${ro ? '' : '<button class="btn s" data-act="vadd">+ переменная</button>'}
                <div class="lbl" style="margin-top:12px">Секреты — спрашиваются при запуске и нигде не сохраняются</div>
                ${m.secrets.map((v, i) => `<div class="row"><input class="inp" style="flex:0 1 200px" data-in="sname" data-i="${i}" value="${esc(v.name)}" placeholder="имя" ${ro ? 'disabled' : ''}><input class="inp grow" data-in="slabel" data-i="${i}" value="${esc(v.label || '')}" placeholder="подсказка, например «Пароль базы»" ${ro ? 'disabled' : ''}>${ro ? '' : `<button class="btn s d" data-act="secdel" data-i="${i}">✕</button>`}</div>`).join('')}
                ${ro ? '' : '<button class="btn s" data-act="secadd">+ секрет</button>'}
              </details>
            </section>
            <section class="box"><div class="row"><h2 class="t grow">Шаги · ${m.steps.length}</h2>
                <button class="btn link" data-act="sexpand">${st.open.size ? 'свернуть все' : 'развернуть все'}</button></div>
              <div class="steps">${m.steps.map(stepHtml).join('') || '<div class="empty">Шагов пока нет. Добавьте первый — например, «Пакеты» или «Подключить HomeRepo».</div>'}</div>
              ${ro ? '' : '<button class="btn p addstep" data-act="sadd">+ Добавить шаг</button>'}
              <datalist id="dl-repo">${cat.repo_packages.map((n) => `<option value="${esc(n)}">`).join('')}</datalist>
            </section>`}
        </div>
        <div class="colstack sticky">
          <section class="box"><div class="row"><h2 class="t grow">Запуск</h2><span class="muted small">${st.runner.ansible ? 'Ansible ' + esc(st.runner.ansible) : 'Ansible не найден'}</span></div>
            <div id="tg">${targetsHtml()}</div>
            ${isAdmin() ? `<div class="row"><button class="btn" data-act="run" data-m="syntax" title="ansible-playbook --syntax-check">Синтаксис</button>
              <button class="btn" data-act="run" data-m="check" title="--check --diff: показать, что изменится, ничего не меняя">Проверить</button>
              <button class="btn p grow" data-act="run" data-m="run">Выполнить</button></div>
              <div class="note">«Проверить» запускает Ansible в режиме проверки: видно, что изменится и где, но на хостах ничего не меняется.</div>`
            : '<div class="note">Запускать плейбуки может администратор.</div>'}
            ${st.pb.runs && st.pb.runs.length ? `<div class="lbl">Запуски этого плейбука</div><div class="runlist">${st.pb.runs.slice(0, 6).map((r) => `<a href="#/run/${r.id}" class="runrow"><span>#${r.id} ${esc(RUN_MODE_RU[r.mode] || r.mode)}</span>${runSt(r.status)}<span class="muted small">${ago(r.finished || r.created)}</span></a>`).join('')}</div>` : ''}
          </section>
          <section class="box"><div class="row"><h2 class="t grow">YAML</h2><button class="btn s" data-act="ycopy">скопировать</button><button class="btn s" data-act="ydl">скачать .yml</button></div>
            <pre class="yaml" id="yaml">${hlYaml(st.yaml || '')}</pre></section>
        </div>
      </div>`;
  }
  /* ---- живой предпросмотр: перерисовываются только YAML и метки ошибок, поля ввода не трогаются */
  let tm = null;
  function changed(now) {
    st.dirty = true;
    const d = $('#dirty', main); if (d) { d.textContent = '· есть несохранённые изменения'; d.className = 'c-ex'; }
    clearTimeout(tm);
    if (st.raw) { const y = $('#yaml', main); if (y) y.innerHTML = hlYaml(st.yaml); return; }
    tm = setTimeout(preview, now ? 0 : 350);
  }
  async function preview() {
    try {
      const r = await POST('/api/playbooks/render', { model: Object.assign({}, st.model, { name: st.name, description: st.desc }) });
      st.yaml = r.yaml; st.errors = r.errors || {}; st.secrets = r.secrets || [];
      const y = $('#yaml', main); if (y) y.innerHTML = hlYaml(r.yaml);
      st.model.steps.forEach((_, i) => {
        const e = $('#serr-' + i, main), c = $('#step-' + i, main);
        if (e) e.textContent = st.errors[i] || '';
        if (c) c.classList.toggle('err', !!st.errors[i]);
      });
    } catch (e) { fail(e); }
  }
  async function save(quiet) {
    try {
      const body = st.raw ? { name: st.name, description: st.desc, raw: true, yaml: st.yaml } : { name: st.name, description: st.desc, model: st.model };
      const r = await api('PUT', '/api/playbooks/' + st.id, body);
      st.yaml = r.yaml; st.errors = r.errors || {}; st.dirty = false;
      const fresh = await GET('/api/playbooks/' + st.id); st.pb = fresh; st.secrets = fresh.secrets || [];
      if (!quiet) toast(Object.keys(st.errors).length ? 'сохранено, но в шагах есть ошибки' : 'сохранено', Object.keys(st.errors).length ? 'err' : 'ok');
      draw();
      return true;
    } catch (e) { fail(e); return false; }
  }
  const step = (el) => st.model.steps[+el.dataset.i];
  // параметры шагов
  S.on.sp = (el) => { const s = step(el); s.params = s.params || {}; s.params[el.dataset.k] = el.value; changed(); };
  S.on.spb = (el) => { const s = step(el); s.params = s.params || {}; s.params[el.dataset.k] = el.checked; changed(); };
  S.on.brs = (el) => {
    const s = step(el); s.params = s.params || {};
    const cur = new Set(Array.isArray(s.params[el.dataset.k]) ? s.params[el.dataset.k] : []);
    el.checked ? cur.add(el.dataset.b) : cur.delete(el.dataset.b);
    s.params[el.dataset.k] = cat.branches.filter((b) => cur.has(b)); changed();
  };
  S.on.pkadd = (el) => {
    const v = el.value.trim(); if (!v) return;
    const s = step(el); s.params = s.params || {};
    const cur = Array.isArray(s.params[el.dataset.k]) ? s.params[el.dataset.k] : String(s.params[el.dataset.k] || '').split(/[\s,]+/).filter(Boolean);
    v.split(/[\s,]+/).forEach((x) => { if (x && !cur.includes(x)) cur.push(x); });
    s.params[el.dataset.k] = cur; draw(); changed(true);
    const inp = $(`#step-${el.dataset.i} [data-ch=pkadd]`, main); if (inp) inp.focus();
  };
  S.acts.pkrm = (el) => { const s = step(el); s.params[el.dataset.k] = (s.params[el.dataset.k] || []).filter((x) => x !== el.dataset.n); draw(); changed(true); };
  S.on.stitle = (el) => { const s = step(el); s.title = el.value; const t = $('#stt-' + el.dataset.i, main); if (t) t.textContent = el.value ? ' · ' + el.value : ''; changed(); };
  S.on.sos = (el) => { step(el).only_os = el.value; draw(); changed(true); };
  S.on.snotify = (el) => { step(el).notify = el.value.trim(); changed(); };
  S.on.sflag = (el) => { step(el)[el.dataset.k] = el.checked; changed(); };
  // параметры плейбука
  S.on.pname = (el) => { st.name = el.value; changed(); };
  S.on.pdesc = (el) => { st.desc = el.value; changed(); };
  S.on.pbecome = (el) => { st.model.become = el.checked; changed(); };
  S.on.pgather = (el) => { st.model.gather_facts = el.checked; changed(); };
  S.on.pserial = (el) => { st.model.serial = +el.value || 0; changed(); };
  S.on.vname = (el) => { st.model.vars[+el.dataset.i].name = el.value; changed(); };
  S.on.vval = (el) => { st.model.vars[+el.dataset.i].value = el.value; changed(); };
  S.on.sname = (el) => { st.model.secrets[+el.dataset.i].name = el.value; changed(); };
  S.on.slabel = (el) => { st.model.secrets[+el.dataset.i].label = el.value; changed(); };
  S.acts.vadd = () => { st.model.vars.push({ name: '', value: '' }); draw(); };
  S.acts.vdel = (el) => { st.model.vars.splice(+el.dataset.i, 1); draw(); changed(true); };
  S.acts.secadd = () => { st.model.secrets.push({ name: '', label: '' }); draw(); };
  S.acts.secdel = (el) => { st.model.secrets.splice(+el.dataset.i, 1); draw(); changed(true); };
  S.on.rawyaml = (el) => { st.yaml = el.value; changed(); };
  // шаги
  S.acts.stoggle = (el, e) => { if (e.target.closest('button')) return; const i = +el.dataset.i; st.open.has(i) ? st.open.delete(i) : st.open.add(i); draw(); };
  S.acts.sexpand = () => { if (st.open.size) st.open.clear(); else st.model.steps.forEach((_, i) => st.open.add(i)); draw(); };
  const moveOpen = (a, b) => { const oa = st.open.has(a), ob = st.open.has(b); st.open.delete(a); st.open.delete(b); if (oa) st.open.add(b); if (ob) st.open.add(a); };
  S.acts.sup = (el) => { const i = +el.dataset.i; if (!i) return; const s = st.model.steps; [s[i - 1], s[i]] = [s[i], s[i - 1]]; moveOpen(i, i - 1); draw(); changed(true); };
  S.acts.sdown = (el) => { const i = +el.dataset.i; const s = st.model.steps; if (i >= s.length - 1) return; [s[i + 1], s[i]] = [s[i], s[i + 1]]; moveOpen(i, i + 1); draw(); changed(true); };
  S.acts.sdup = (el) => { const i = +el.dataset.i; st.model.steps.splice(i + 1, 0, JSON.parse(JSON.stringify(st.model.steps[i]))); const o = new Set(); st.open.forEach((x) => o.add(x > i ? x + 1 : x)); o.add(i + 1); st.open = o; draw(); changed(true); };
  S.acts.sdel = async (el) => {
    const i = +el.dataset.i;
    if (!await confirmDlg('Удалить шаг', `Шаг ${i + 1} «${esc((B[st.model.steps[i].type] || {}).title || '')}» будет удалён из плейбука.`, 'Удалить', true)) return;
    st.model.steps.splice(i, 1); const o = new Set(); st.open.forEach((x) => { if (x < i) o.add(x); else if (x > i) o.add(x - 1); }); st.open = o; draw(); changed(true);
  };
  S.acts.sadd = () => {
    const m = modal(`<div class="row"><h2 class="t grow">Добавить шаг</h2><input class="inp" id="bq" placeholder="поиск блока" style="flex:0 1 240px"><button class="btn s" data-close>Закрыть</button></div>
      <div id="bgrid">${blocksHtml('')}</div>`);
    m.el.querySelector('.dlg').classList.add('wide');
    m.el.querySelector('#bq').addEventListener('input', (e) => { $('#bgrid', m.el).innerHTML = blocksHtml(e.target.value.toLowerCase()); });
    m.el.addEventListener('click', (e) => {
      const b = e.target.closest('[data-b]'); if (!b) return;
      const blk = B[b.dataset.b]; const params = {};
      blk.fields.forEach((f) => { if (f.default !== undefined) params[f.key] = f.default; });
      st.model.steps.push({ type: blk.type, params, title: '' });
      st.open.add(st.model.steps.length - 1); m.close(); draw(); changed(true);
      setTimeout(() => { const c = $('#step-' + (st.model.steps.length - 1), main); if (c) { c.scrollIntoView({ block: 'center' }); const f = c.querySelector('input:not([type=checkbox]),textarea,select'); if (f) f.focus(); } }, 50);
    });
  };
  function blocksHtml(q) {
    return cat.groups.map((g) => {
      const bl = cat.blocks.filter((b) => b.group === g.key && (!q || (b.title + ' ' + b.desc).toLowerCase().includes(q)));
      if (!bl.length) return '';
      return `<div class="lbl" style="margin:12px 0 8px">${esc(g.title)}</div><div class="blockgrid">${bl.map((b) => `<button class="blockcard" data-b="${esc(b.type)}"><b>${esc(b.title)}</b><span>${esc(b.desc)}</span></button>`).join('')}</div>`;
    }).join('') || '<div class="empty">ничего не найдено</div>';
  }
  // сохранение и прочее
  S.acts.pbsave = () => save();
  S.acts.pbraw = async () => {
    if (!await confirmDlg('Редактировать YAML вручную', 'Плейбук переходит в ручное редактирование: конструктор для него станет недоступен. Если хотите сохранить вариант для конструктора — сначала сделайте копию на странице «Плейбуки».', 'Перейти к YAML')) return;
    st.raw = true; draw(); changed();
  };
  S.acts.pbdel = async () => {
    if (!await confirmDlg('Удалить плейбук', `Плейбук <b>${esc(st.name)}</b> будет удалён. История его запусков останется.`, 'Удалить', true)) return;
    try { await api('DELETE', '/api/playbooks/' + st.id); location.hash = '#/playbooks'; } catch (e) { fail(e); }
  };
  S.acts.ycopy = () => copyText(st.yaml);
  S.acts.ydl = () => downloadText((st.name || 'playbook').replace(/[^\wа-яё.-]+/gi, '-').toLowerCase() + '.yml', st.yaml);
  // цели и запуск
  const redrawTg = () => { const t = $('#tg', main); if (t) t.innerHTML = targetsHtml(); };
  S.acts.tsel = (el) => { const i = +el.dataset.id; st.selH.has(i) ? st.selH.delete(i) : st.selH.add(i); redrawTg(); };
  S.acts.thf = (el) => { st.hf = el.dataset.v; redrawTg(); };
  S.acts.tall = () => { st.targets.filter((t) => !st.hf || t.groups.includes(st.hf)).forEach((t) => st.selH.add(t.id)); redrawTg(); };
  S.acts.tnone = () => { st.selH.clear(); redrawTg(); };
  S.acts.run = async (el) => {
    const mode = el.dataset.m;
    if (st.dirty && !await save(true)) return;
    if (Object.keys(st.errors).length && mode !== 'syntax') return toast('в шагах есть ошибки — исправьте их перед запуском', 'err');
    const hosts = [...st.selH];
    if (!hosts.length && mode !== 'syntax') return toast('выберите хосты для запуска', 'err');
    const names = st.targets.filter((t) => st.selH.has(t.id)).map((t) => t.name);
    if (mode === 'run' && !await confirmDlg('Выполнить плейбук', `Плейбук <b>${esc(st.name)}</b> внесёт изменения на ${names.length} хост(ах): ${names.map(esc).join(', ')}.<br>Сначала можно «Проверить» — это покажет изменения без их применения.`, 'Выполнить')) return;
    let secrets = {};
    if (mode !== 'syntax' && st.secrets.length) { secrets = await askSecrets(st.secrets); if (!secrets) return; }
    try { const r = await POST('/api/runs', { playbook_id: st.id, hosts, mode, secrets }); location.hash = '#/run/' + r.run; } catch (e) { fail(e); }
  };
  draw();
  if (!st.raw && !st.yaml) preview();
  window.onbeforeunload = () => (st.dirty ? 'есть несохранённые изменения' : undefined);
  S.timers.push(setInterval(() => { if (!document.body.contains(main) || location.hash.indexOf('#/playbook/') !== 0) window.onbeforeunload = null; }, 1000));
}

/* ------------------------------------------------------------------ выполнение в реальном времени */
async function vRun(main, id) {
  const st = { lastEv: 0, lastLog: 0, tasks: [], byId: {}, recap: null, notes: [], log: '', sel: null, showLog: true, follow: true };
  function addEvents(evs) {
    for (const e of evs) {
      st.lastEv = Math.max(st.lastEv, e.id);
      if (e.kind === 'task_start') {
        if (!st.byId[e.task]) { const t = { id: e.task, name: e.task_name === 'Gathering Facts' ? 'Сбор сведений о хосте (facts)' : e.task_name, handler: e.data.handler, action: e.data.action, res: {} }; st.byId[e.task] = t; st.tasks.push(t); }
      } else if (e.kind === 'result') {
        let t = st.byId[e.task];
        if (!t) { t = { id: e.task, name: e.task_name, res: {} }; st.byId[e.task] = t; st.tasks.push(t); }
        t.res[e.host] = { status: e.status, data: (e.data && e.data.data) || {}, ts: e.ts };
      } else if (e.kind === 'stats') st.recap = e.data.recap;
      else if (e.kind === 'no_hosts' || e.kind === 'no_hosts_remaining') st.notes.push(e.data.msg);
      else if (e.kind === 'play_start') st.play = e.task_name;
    }
  }
  async function poll() {
    const r = await GET(`/api/runs/${id}?ev=${st.lastEv}&log=${st.lastLog}`);
    st.run = r.run;
    addEvents(r.events);
    for (const l of r.log) { st.lastLog = Math.max(st.lastLog, l.id); st.log += l.text; }
    return r.events.length || r.log.length;
  }
  await poll();
  // первый раз дочитываем всё, если событий больше лимита
  for (let i = 0; i < 20 && (await poll()); i++);
  const fin = () => ['done', 'failed', 'cancelled'].includes(st.run.status);
  function grid() {
    const hosts = st.run.hosts;
    if (!st.tasks.length) return `<div class="empty">${st.run.status === 'queued' ? 'запуск в очереди — исполнитель начнёт через пару секунд' : fin() ? 'задач не выполнялось' : 'Ansible собирает сведения о хостах…'}</div>`;
    const running = !fin();
    const last = st.tasks[st.tasks.length - 1];
    return `<table class="t rgrid"><thead><tr><th class="tn">Шаг</th>${hosts.map((h) => `<th class="hc" title="${esc(h)}">${esc(h)}</th>`).join('')}</tr></thead><tbody>
      ${st.tasks.map((t, i) => `<tr><td class="tn"><span class="muted">${i + 1}.</span> ${esc(t.name)}${t.handler ? ' <span class="tag dim">обработчик</span>' : ''}</td>
        ${hosts.map((h) => { const r = t.res[h]; const sel = st.sel && st.sel.t === t.id && st.sel.h === h;
          if (!r) return `<td class="rc ${running && t === last ? 'wait' : 'none'}">${running && t === last ? '<i class="spin"></i>' : ''}</td>`;
          return `<td class="rc ${r.status} ${sel ? 'sel' : ''}" data-act="cell" data-t="${esc(t.id)}" data-h="${esc(h)}" title="${esc(h)}: ${esc(RES_RU[r.status] || r.status)}"><span>${RES_ICON[r.status] || '?'}</span></td>`; }).join('')}</tr>`).join('')}
      ${st.recap ? `<tr class="recap"><td class="tn">Итог</td>${hosts.map((h) => { const x = st.recap[h] || {}; return `<td class="small"><span class="c-ok">${x.ok || 0}</span> · <span class="c-ex">${x.changed || 0}</span>${x.failures || x.unreachable ? ` · <span class="c-bad">${(x.failures || 0) + (x.unreachable || 0)}</span>` : ''}</td>`; }).join('')}</tr>` : ''}
      </tbody></table>
      <div class="legend">${Object.keys(RES_ICON).map((k) => `<span><i class="rc ${k}">${RES_ICON[k]}</i>${RES_RU[k]}</span>`).join('')}<span class="muted">итог: без изменений · изменено · ошибки</span></div>`;
  }
  function detail() {
    if (!st.sel) return '<div class="note">Нажмите на клетку, чтобы увидеть подробности: вывод команды, изменения в файле (diff), текст ошибки.</div>';
    const t = st.byId[st.sel.t]; const r = t && t.res[st.sel.h];
    if (!r) return '';
    const d = r.data || {};
    const pre = (lbl, v) => (v === undefined || v === null || v === '' ? '' : `<div class="lbl">${lbl}</div><pre class="out">${esc(typeof v === 'string' ? v : JSON.stringify(v, null, 1))}</pre>`);
    return `<div class="jh"><b>${esc(st.sel.h)} · ${esc(t.name)}</b><span class="rc ${r.status} inl">${RES_ICON[r.status] || ''} ${esc(RES_RU[r.status] || r.status)}</span></div>
      ${t.action ? `<div class="note">${esc(t.action)} · ${fmtDT(r.ts)}</div>` : ''}
      ${pre('Сообщение', d.msg)}${pre('Изменения (diff)', d.diff)}${pre('Вывод', d.stdout)}${pre('Ошибки', d.stderr)}
      ${d.rc !== undefined ? `<div class="note">код возврата: ${esc(d.rc)}</div>` : ''}
      ${d.items ? `<div class="lbl">Элементы</div><table class="t"><tbody>${d.items.map((it) => `<tr><td class="rc ${it.status} inl">${RES_ICON[it.status] || ''}</td><td>${esc(it.item)}</td><td class="muted small wrap">${esc(it.msg || '')}</td></tr>`).join('')}</tbody></table>` : ''}
      ${!d.msg && !d.diff && !d.stdout && !d.stderr && !d.items ? '<div class="note">Ansible не вернул подробностей для этого шага.</div>' : ''}`;
  }
  function draw() {
    const r = st.run;
    const tot = { ok: 0, changed: 0, bad: 0, skipped: 0 };
    Object.values(st.recap || {}).forEach((x) => { tot.ok += x.ok || 0; tot.changed += x.changed || 0; tot.bad += (x.failures || 0) + (x.unreachable || 0); tot.skipped += x.skipped || 0; });
    main.innerHTML = `
      <div class="box" style="flex-direction:row;flex-wrap:wrap;justify-content:space-between;align-items:center">
        <div><div class="note"><a href="#/playbooks">Плейбуки</a> / ${r.playbook_id ? `<a href="#/playbook/${r.playbook_id}">${esc(r.name)}</a>` : esc(r.name)} / запуск #${r.id}</div>
          <h1 class="rh">${esc(r.name)} <span class="tag ${r.mode === 'run' ? 'warn' : 'info'}">${esc(RUN_MODE_RU[r.mode] || r.mode)}</span> ${runSt(r.status)}</h1>
          <div class="row note" style="gap:6px 18px;font-size:13px"><span>Хосты <b class="tx">${esc(r.hosts.join(', '))}</b></span><span>Запустил <b class="tx">${esc(r.created_by || '')}</b> ${fmtDT(r.created)}</span>
            <span>Длится <b class="tx" id="rdur">${dur(r.started, r.finished)}</b></span>${r.message ? `<span>Итог <b class="tx">${esc(r.message)}</b></span>` : ''}</div></div>
        <div class="row">${isAdmin() && !fin() ? '<button class="btn d" data-act="rcancel">Остановить</button>' : ''}
          ${isAdmin() && fin() && r.playbook_id ? `<button class="btn" data-act="rerun" data-m="check">Проверить ещё раз</button><button class="btn p" data-act="rerun" data-m="run">Выполнить ещё раз</button>` : ''}</div>
      </div>
      ${st.notes.map((n) => `<div class="alert warn"><div>${esc(n)}</div></div>`).join('')}
      ${st.recap ? `<div class="tiles"><div class="tile"><b>${r.hosts.length}</b><span>хостов</span></div><div class="tile"><b style="color:var(--ok)">${tot.ok}</b><span>шагов без изменений</span></div>
        <div class="tile"><b style="color:var(--warn)">${tot.changed}</b><span>${r.mode === 'check' ? 'изменилось бы' : 'изменено'}</span></div>
        <div class="tile"><b style="color:${tot.bad ? 'var(--bad)' : 'var(--dim)'}">${tot.bad}</b><span>ошибок</span></div><div class="tile"><b style="color:var(--dim)">${tot.skipped}</b><span>пропущено</span></div></div>` : ''}
      <div class="cols2" style="grid-template-columns:minmax(0,1.6fr) minmax(0,1fr)">
        <section class="panel"><div class="bar"><b class="ttl">ХОД ВЫПОЛНЕНИЯ${st.play ? ' · ' + esc(st.play) : ''}</b></div><div class="tblwrap" id="grid">${grid()}</div></section>
        <section class="box" id="detail">${detail()}</section>
      </div>
      <section class="panel"><div class="bar"><b class="ttl">ВЫВОД ANSIBLE</b><div class="grow"></div><label class="chk small"><input type="checkbox" data-ch="follow" ${st.follow ? 'checked' : ''}>прокручивать за выводом</label></div>
        <pre class="out runlog" id="rlog">${esc(st.log)}</pre></section>`;
    const lg = $('#rlog', main); if (lg && st.follow) lg.scrollTop = lg.scrollHeight;
  }
  function update() {
    // частичная перерисовка, чтобы не сбивать прокрутку и выделение текста в выводе
    const g = $('#grid', main); if (!g) return draw();
    if (st._lastStatus !== st.run.status || (!!st.recap) !== st._recap || st.notes.length !== st._notes) {
      st._lastStatus = st.run.status; st._recap = !!st.recap; st._notes = st.notes.length; return draw();
    }
    g.innerHTML = grid();
    const rd = $('#rdur', main); if (rd) rd.textContent = dur(st.run.started, st.run.finished);
    const lg = $('#rlog', main);
    if (lg && lg.textContent.length !== st.log.length) { lg.textContent = st.log; if (st.follow) lg.scrollTop = lg.scrollHeight; }
  }
  S.acts.cell = (el) => { st.sel = { t: el.dataset.t, h: el.dataset.h }; $('#detail', main).innerHTML = detail(); $('#grid', main).innerHTML = grid(); };
  S.on.follow = (el) => { st.follow = el.checked; };
  S.acts.rcancel = async () => { try { await POST(`/api/runs/${id}/cancel`); toast('останавливаю…'); } catch (e) { fail(e); } };
  S.acts.rerun = async (el) => {
    try {
      const pb = await GET('/api/playbooks/' + st.run.playbook_id);
      const mode = el.dataset.m;
      if (mode === 'run' && !await confirmDlg('Выполнить ещё раз', `Плейбук <b>${esc(pb.name)}</b> (текущая версия) на ${st.run.hosts.map(esc).join(', ')}.`, 'Выполнить')) return;
      let secrets = {};
      if (pb.secrets && pb.secrets.length) { secrets = await askSecrets(pb.secrets); if (!secrets) return; }
      const r = await POST('/api/runs', { playbook_id: pb.id, hosts: (st.run.opts && st.run.opts.host_ids) || [], mode, secrets });
      location.hash = '#/run/' + r.run;
    } catch (e) { fail(e); }
  };
  st._lastStatus = st.run.status; st._recap = !!st.recap; st._notes = st.notes.length;
  draw();
  const t = setInterval(async () => {
    if (document.hidden) return;
    try { const ch = await poll(); if (ch || !fin()) update(); if (fin() && !ch) clearInterval(t); } catch (e) { }
  }, 1000);
  S.timers.push(t);
}

VIEWS.playbooks = vPlaybooks;
VIEWS.playbook = vPlaybook;
VIEWS.run = vRun;
