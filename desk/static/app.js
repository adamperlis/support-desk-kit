// Support Desk: what needs you first, everything else a click away. Data comes from the local
// server every few seconds; actions go back to it. Data values are untrusted text: everything goes in
// through textContent, never innerHTML.
(() => {
  let token = document.querySelector('meta[name="desk-token"]').content;
  const DESK_NAME = document.querySelector('meta[name="desk-name"]')?.content || 'Support Desk';
  const POLL_MS = 4000;
  const QUIET_HOURS = 6;
  class SignInRequired extends Error {}
  async function request(url, options) {
    const response = await fetch(url, options);
    if (response.status === 401) {
      document.querySelector('.app').hidden = true;
      location.replace('/login');
      throw new SignInRequired();
    }
    return response;
  }
  // A release built from main but not yet in front of the store's review.
  const NOT_SUBMITTED = /not in App Store Connect|not yet submitted|Built, not yet|not on the production track/i;

  const S = { data: null, version: '', view: 'foryou', q: '', sel: null, closing: null, online: true, busy: new Set(), edits: {}, tab: {}, hiding: new Set(),
    msgs: {}, asked: new Set(), expanded: new Set(), product: 'all' };
  try { S.view = localStorage.getItem('desk.view2') || S.view; } catch { /* private window: defaults are fine */ }

  const $ = (sel) => document.querySelector(sel);
  const el = (tag, attrs, ...kids) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else if (k === 'class') node.className = v;
      else node.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false && kid !== '') node.append(kid instanceof Node ? kid : String(kid));
    return node;
  };

  const isSlack = (c) => c.source === 'slack';
  const sourceLink = (c, cls = 'btn btn-sm') => el('a', { class: cls, href: isSlack(c) ? c.slack : c.gmail, target: '_blank', rel: 'noopener' },
    isSlack(c) ? 'Open in Slack' : 'Open in Gmail');
  // replaceChildren would print a null slot as the text "null"
  const fill = (node, ...kids) => node.replaceChildren(...kids.flat().filter((k) => k != null && k !== false));
  const responsiveTable = (...kids) => el('div', { class: 'table-scroll', tabindex: '0', role: 'region', 'aria-label': 'Scrollable table' },
    el('table', { class: 'table' }, ...kids));
  // A small empty state: the bot over one line. Named by its text, so each keeps its own blinks.
  const emptyNote = (text) => el('div', { class: 'empty empty-sm' }, el('desk-bot', { size: 56, name: `empty:${text}`, 'aria-hidden': 'true' }), el('p', {}, text));

  // ---------- time ----------
  const day = (iso) => new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  const clock = (d) => d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  const stamp = (iso) => `${day(iso)}, ${clock(new Date(iso))}`;
  const ago = (iso) => {
    const min = Math.round((Date.now() - new Date(iso)) / 60000);
    if (min < 1) return 'just now';
    if (min < 60) return `${min} min ago`;
    if (min < 1440) return `${Math.round(min / 60)} h ago`;
    return day(iso);
  };

  // ---------- data ----------
  const list = (name) => (S.data && Array.isArray(S.data[name]) ? S.data[name] : []);
  // A conversation closed in the last few seconds (Undo still showing) already counts as closed.
  const convs = () => list('conversations').map((c) => (S.hiding.has(c.id) ? { ...c, status: 'resolved', owed: '' } : c));
  const conv = (id) => convs().find((c) => c.id === id);
  const workFor = (id) => list('work').find((w) => w.conversation === id);
  const fixesFor = (id) => list('prs').filter((p) => p.conversation === id);
  const desk = () => list('desk_sessions');
  const deskFor = (c) => { const keys = new Set(fixesFor(c.id).map((p) => p.pr)); return desk().filter((s) => s.target === c.id || keys.has(s.target)); };
  const draftsPending = () => list('drafts').filter((d) => d.status === 'pending');
  // The few settings from config.json the server shares (see PAGE_CONFIG in server.py).
  const cfg = () => (S.data && S.data.desk) || {};
  // The products a fix or build session can work on: the ones with a repo in config.json.
  const fixable = (c) => (cfg().products || []).includes(c.product);
  const slackOn = () => Boolean(cfg().slack?.enabled);
  const slackChannel = () => cfg().slack?.channel_name || 'the Slack channel';
  // What we sign our side of a thread as: "Support", or "Sales" on a sales desk.
  const usLabel = () => { const u = String(cfg().use_case || 'support'); return u.charAt(0).toUpperCase() + u.slice(1); };
  // A drafted reply's gap for the owner to fill in: [Owner: …], or with the owner's own name.
  const hasGap = (text) => {
    const name = String(cfg().owner || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return new RegExp(`\\[(?:Owner${name ? `|${name}` : ''}):`).test(text);
  };
  // Your own tickets (desk/tickets.py): beside the conversations, never in them.
  const RANK = { now: 0, soon: 1, later: 2 };
  const ticketsOpen = () => list('tickets').filter((t) => t.status === 'open')
    .sort((a, b) => (RANK[a.priority] ?? 1) - (RANK[b.priority] ?? 1) || (a.created < b.created ? -1 : 1));
  // The newest session doing `action` for this conversation; a finished one only while it's recent.
  const sessionFor = (c, action) => deskFor(c).find((s) => s.action === action && s.status !== 'ended'
    && (!['done', 'app'].includes(s.status) || Date.now() - new Date(s.started) < 6 * 3600000));
  // Sessions that stopped (to ask something, or on a dropped connection) and that nothing newer replaced.
  const stopped = () => desk().filter((s, _, all) => (s.status === 'blocked' || s.status === 'failed')
    && !all.some((n) => n !== s && n.action === s.action && n.target === s.target && n.started > s.started));
  const busy = (w) => Boolean(w) && (w.state === 'working' || w.state === 'pr-open');
  // A resolved thread with a reply still waiting to go out isn't done yet.
  const isOpen = (c) => c.status !== 'resolved' || list('drafts').some((d) => d.conversation === c.id && d.status === 'pending');
  const canFix = (c) => Boolean(c.needs_fix) && fixable(c) && !busy(workFor(c.id))
    && !deskFor(c).some((s) => s.action === 'fix' && s.status === 'working');
  // A feature idea: acknowledged ("on the list"), nothing owed, no bug to fix and nothing built or under way.
  // Anything labeled a bug isn't an idea, even when there's nothing left to fix.
  const canBuild = (c) => isOpen(c) && c.status === 'triaged' && !c.needs_fix && !c.owed && !c.prs && fixable(c)
    && !String(c.labels || '').split(' · ').includes('bug')
    && !['working', 'pr-open', 'blocked'].includes(workFor(c.id)?.state)
    && !deskFor(c).some((s) => (s.action === 'build' || s.action === 'fix') && s.status === 'working');

  // ---------- what needs you ----------
  // Every item is one decision with one main button. The same list drives the For you view, its count
  // and the coral dot on conversation rows.
  function forYou() {
    const items = [];
    const drafted = new Set();
    for (const d of draftsPending()) { drafted.add(d.conversation); items.push({ kind: 'draft', conv: d.conversation, d }); }
    const seenPr = new Set();
    const seenRel = new Set();
    for (const p of list('prs')) {
      const c = conv(p.conversation);
      if (!c) continue;
      if (p.pr_state === 'open' && !seenPr.has(p.pr)) {
        seenPr.add(p.pr);
        const who = list('prs').filter((x) => x.pr === p.pr).map((x) => x.customer);
        items.push({ kind: 'merge', conv: c.id, p, who: [...new Set(who)] });
      } else if (p.release_state === 'waiting' && (p.release === 'next release' || NOT_SUBMITTED.test(p.release_note || ''))) {
        const k = `${p.app} ${p.release}`;
        if (seenRel.has(k)) continue;
        seenRel.add(k);
        const prs = list('prs').filter((x) => x.app === p.app && x.release === p.release && x.release_state === 'waiting');
        items.push({ kind: 'release', conv: c.id, p, prs: [...new Set(prs.map((x) => x.pr))], who: [...new Set(prs.map((x) => x.customer))] });
      }
    }
    for (const c of convs()) {
      if (!isOpen(c)) continue;
      if (c.status === 'needs-owner' && !drafted.has(c.id)) items.push({ kind: 'answer', conv: c.id, c });
      else if (c.owed && !drafted.has(c.id) && c.status !== 'needs-owner') items.push({ kind: 'reply', conv: c.id, c });
      if (canFix(c)) items.push({ kind: 'fix', conv: c.id, c });
      else if (canBuild(c)) items.push({ kind: 'idea', conv: c.id, c });
      const w = workFor(c.id);
      if (w && w.state === 'blocked') items.push({ kind: 'blocked', conv: c.id, c, w });
    }
    // A reply or fix that stopped shows on its own card; anything else that stopped gets one here.
    const onCard = (s) => items.some((i) => i.conv === s.target
      && ((s.action === 'reply' && (i.kind === 'answer' || i.kind === 'reply')) || (s.action === 'fix' && i.kind === 'fix')
        || (s.action === 'build' && i.kind === 'idea')));
    for (const s of stopped()) if (!onCard(s)) items.push({ kind: 'session', conv: conv(s.target) ? s.target : null, s });
    return items;
  }
  const needsYou = () => new Set(forYou().map((i) => i.conv));

  // One status per conversation, in priority order.
  function statusOf(c, you) {
    const w = workFor(c.id);
    if (!isOpen(c)) return ['Done', 'ok'];
    if (you.has(c.id)) return ['Needs you', 'coral'];
    if (busy(w) || deskFor(c).some((s) => s.status === 'working')) return ['In progress', 'info'];
    if (c.release_state === 'open') return ['PR open', 'violet'];
    if (c.release_state === 'waiting') return ['Fixed, not live', 'warn'];
    if (c.status === 'waiting-on-customer') return ['Waiting on customer', ''];
    if (c.release_state === 'live') return ['Live', 'ok'];
    return ['Open', ''];
  }
  const VIEWS = [
    { id: 'open', label: 'Open', test: (c) => isOpen(c) },
    { id: 'progress', label: 'In progress', test: (c, you) => isOpen(c) && ['In progress', 'PR open', 'Fixed, not live'].includes(statusOf(c, you)[0]) },
    { id: 'customer', label: 'Waiting on customer', test: (c) => isOpen(c) && c.status === 'waiting-on-customer' },
    { id: 'done', label: 'Done', test: (c) => !isOpen(c) },
  ];

  // ---------- actions ----------
  async function act(action, payload = {}, button = null, quiet = false) {
    const key = action + JSON.stringify(payload);
    if (S.busy.has(key)) return false;
    S.busy.add(key);
    if (button) button.disabled = true;
    try {
      const send = () => request('/api/action', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Desk-Token': token },
        body: JSON.stringify({ action, ...payload }),
      });
      let r = await send();
      if (r.status === 403) {  // the desk was set up again since this page loaded: take its current key and retry once
        const page = new DOMParser().parseFromString(await (await request('/', { cache: 'no-store' })).text(), 'text/html');
        token = page.querySelector('meta[name="desk-token"]')?.content || token;
        r = await send();
      }
      const body = await r.json().catch(() => ({}));
      if (!r.ok || !quiet) toast(body.message || (r.ok ? 'Done.' : 'That didn’t work.'), !r.ok);
      if (r.ok) poll(true);
      return r.ok;
    } catch (error) {
      if (error instanceof SignInRequired) return false;
      toast(`The ${DESK_NAME} server isn’t answering. Is it still running?`, true);
      return false;
    } finally {
      S.busy.delete(key);
      if (button) button.disabled = false;
    }
  }
  function toast(message, bad = false, ...extra) {
    // Good news comes with a small hop from the bot; a problem doesn't get the mascot.
    const t = el('div', { class: `toast${bad ? ' bad' : ''}`, role: 'status' },
      bad ? null : el('desk-bot', { size: 32, hop: true, 'aria-hidden': 'true' }), el('span', {}, message), ...extra);
    $('#toasts').append(t);
    setTimeout(() => t.remove(), bad ? 9000 : 6000);
    return t;
  }

  // Close as not applicable, in one click. The card goes at once; the desk is told after five seconds,
  // so Undo in the toast costs nothing.
  const UNDO_MS = 5000;
  function quickClose(c) {
    if (S.hiding.has(c.id)) return;
    const pr = fixesFor(c.id).find((p) => p.pr_state === 'open');
    if (pr || busy(workFor(c.id))) {  // the close runbook stops to ask about these, so ask here instead
      toast(`${c.customer} has ${pr ? `${pr.pr} open` : 'a fix in progress'}. Close it from here if you're sure.`);
      goTo(c.id); S.closing = c.id; renderDetail();
      return;
    }
    S.hiding.add(c.id);
    render();
    let undone = false;
    const undo = el('button', { class: 'toast-act', type: 'button', onclick: () => { undone = true; S.hiding.delete(c.id); t.remove(); render(); } }, 'Undo');
    const t = toast(`Closed ${c.customer}'s conversation.`, false, undo);
    setTimeout(async () => {
      t.remove();
      if (undone) return;
      if (!(await act('close', { id: c.id }, null, true))) S.hiding.delete(c.id);
      else await poll(true);  // the desk now reports it closed itself
      S.hiding.delete(c.id);
      render();
    }, UNDO_MS);
  }
  const button = (label, cls, action, payload, title) => el('button', { class: `btn btn-sm ${cls}`, type: 'button', title,
    onclick: (e) => act(action, payload, e.currentTarget) }, label);
  // Every session opens in the Claude app. A working one pauses on the way, so its tooltip says so.
  const openBtn = (s) => button('Open in Claude', '', 'open-session', { short: s.short }, {
    app: 'Brings it forward in the Claude app',
    working: 'Moves it into the Claude app. It pauses on the way, so tell it to keep going there',
  }[s.status] || 'Moves it into the Claude app, where you can read it and answer it. It carries on there with the same conversation');
  // An ended session can still be opened in the app from its transcript.
  const canOpen = (s) => s.status !== 'ended' || Boolean(s.session_id);
  const buildBtn = (c) => button('Build it', 'btn-strong', 'build', { id: c.id },
    'Starts a session in the app’s repo that builds the smallest version and opens a PR. If the idea is bigger than a small change, it asks you first');
  // A session's last words, plain and short enough for one line of a card or table.
  const brief = (s, n = 160) => { const x = String(s || '').replace(/[*`#]+/g, '').trim(); return x.length > n ? `${x.slice(0, n).replace(/\s+\S*$/, '')}…` : x; };
  const VERB = { reply: 'Drafting a reply', fix: 'Fixing it', build: 'Building it', close: 'Closing it', send: 'Sending', 'merge-ship': 'Merging and shipping', ship: 'Shipping', pass: 'Checking', 'refresh-full': 'Refreshing', ticket: 'Working on it' };
  // What a session is doing, in one line, with the buttons that fit its state.
  const runRow = (s, ...extra) => {
    const line = {
      working: [el('span', { class: 'spin', 'aria-hidden': 'true' }), el('b', {}, `${VERB[s.action] || 'Working'}…`), s.step ? ` ${s.step}` : ''],
      failed: [el('b', {}, 'Stopped: the connection dropped.'), ' Retry picks up where it left off.'],
      blocked: [el('b', {}, 'Asking you: '), brief(s.said, 280) || 'open it to see the question.'],
      done: [el('b', {}, 'Finished. '), brief(s.said)],
      app: [el('b', {}, 'In the Claude app. '), brief(s.said)],
    }[s.status];
    if (!line) return null;
    return el('div', { class: `run run--${s.status}` }, el('p', { title: s.status === 'working' ? null : s.said }, line),
      el('div', { class: 'actions' }, s.status === 'failed' ? button('Retry', 'btn-strong', 'retry-session', { short: s.short }, 'Continues the same session') : null,
        openBtn(s), ...extra));
  };
  const pill = (spec) => (spec ? el('span', { class: `pill ${spec[1] || ''}` }, spec[0]) : null);
  const goTo = (id) => { const c = conv(id); S.view = c && !isOpen(c) ? 'done' : 'open'; save(); select(id); };

  // ---------- header ----------
  function renderStrip() {
    const ap = (S.data && S.data.autopilot) || {};
    const last = (S.data && S.data.passes && S.data.passes[0]) || null;
    const parts = [];
    if (!S.online) parts.push(el('span', { class: 'live down' }), el('span', { class: 'txt' }, 'Not connected to the desk server'));
    else {
      // The switch beside this says whether autopilot is on; the text says what it last did and when it runs next.
      const bits = [];
      if (ap.on === false) bits.push('Paused');
      if (last) {
        const did = [last.acked.length && `acknowledged ${last.acked.length}`, last.notes.length && `told ${last.notes.length}`,
          last.closed.length && `closed ${last.closed.length}`].filter(Boolean).join(', ');
        bits.push(`last check ${ago(last.at)}${did ? `: ${did}` : ''}`);
      }
      if (ap.on !== false && ap.next_pass_at) bits.push(`next ${clock(new Date(ap.next_pass_at * 1000))}`);
      if (ap.error) bits.push(ap.error);
      parts.push(el('span', { class: 'txt' }, bits.join(' · ')));
    }
    $('#strip').replaceChildren(...parts);
    const sw = $('#autopilot');
    sw.disabled = !S.online || !S.data;
    if (!S.switching) sw.setAttribute('aria-checked', String(Boolean(S.data) && ap.on !== false));
    if (S.data && !sw.dataset.ready) setTimeout(() => { sw.dataset.ready = ''; }, 50);  // it slides only once it has shown the desk's real state
    sw.title = ap.on === false ? `Autopilot is paused. Turn it on to let the desk run ${cfg().use_case || 'support'} by itself again.`
      : `Autopilot reads Gmail${slackOn() ? ' and the Slack bug channel' : ''}, acknowledges emails, sends release notes and closes what’s done. Turn it off to pause.`;
    const running = desk().some((s) => s.action === 'pass' && s.status === 'working');
    const check = $('#check-now');
    check.disabled = running;
    check.textContent = running ? 'Checking…' : 'Check now';
    // The mascot mirrors the desk: it dozes while nothing can run and looks alert while working.
    window.deskBot?.mood(!S.online || ap.on === false ? 'sleeping'
      : running || desk().some((s) => s.status === 'working') ? 'working' : 'idle');
    $('#banner').hidden = !S.data || (S.data.cli || {}).signed_in !== false;
  }

  function renderNav() {
    const you = needsYou();
    const n = forYou().length;
    const item = (id, label, count, hot, icon) => el('li', {}, el('button', {
      class: `view${hot && count ? ' hot' : ''}`, type: 'button', 'aria-current': String(S.view === id),
      onclick: () => {
        S.view = id; S.sel = null; save(); render();
        document.querySelectorAll('.board, .list').forEach(node => { node.scrollTop = 0; });
      },
    }, icon ? el('i', { 'data-lucide': icon }) : null, label, count == null ? null : el('span', { class: 'n' }, String(count))));
    $('#views-top').replaceChildren(item('foryou', 'For you', n, true, 'inbox'), item('tickets', 'Tickets', ticketsOpen().length, true, 'ticket'));
    const ICON = { open: 'message-square', progress: 'loader', customer: 'clock-3', done: 'circle-check' };
    $('#views-conv').replaceChildren(...VIEWS.map((v) => item(v.id, v.label, convs().filter((c) => v.test(c, you)).length, false, ICON[v.id])));
    const working = desk().filter((s) => s.status === 'working').length;
    $('#views-more').replaceChildren(item('releases', 'Releases', null, false, 'package'), item('activity', 'Activity', working || null, false, 'activity'));
    $('#account-settings').setAttribute('aria-current', S.view === 'settings' ? 'page' : 'false');
    window.lucide?.createIcons({ attrs: { width: 16, height: 16, 'stroke-width': 1.75 } });
    const nav = $('.side');
    if (nav.dataset.view !== S.view) {
      nav.dataset.view = S.view;
      if (window.innerWidth <= 980) nav.querySelector('[aria-current="true"]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  }
  const save = () => { try { localStorage.setItem('desk.view2', S.view); } catch { /* fine */ } };

  // ---------- For you ----------
  function renderForYou() {
    const box = $('#foryou');
    if (document.activeElement && box.contains(document.activeElement) && document.activeElement.tagName === 'TEXTAREA') return;
    const items = forYou();
    const q = S.q.toLowerCase();
    const shown = q ? items.filter((i) => JSON.stringify([conv(i.conv)?.customer, conv(i.conv)?.issue, i.p?.pr, i.d?.body]).toLowerCase().includes(q)) : items;
    const group = (kind) => shown.filter((i) => i.kind === kind);
    const head = (c, extra) => el('div', { class: 'fy-head' },
      el('span', { class: 'who' }, c ? c.customer : ''), el('span', { class: 'prod' }, c ? c.product : ''), extra || null,
      c ? el('button', { class: 'link', type: 'button', onclick: () => goTo(c.id) }, 'Open conversation') : null);
    const sec = (title, items_, card) => (items_.length
      ? el('section', { class: 'fy-sec' }, el('h3', {}, title, el('span', { class: 'n' }, String(items_.length))), items_.map(card)) : null);

    const draftCard = ({ d }) => {
      const c = conv(d.conversation);
      // A reply in the customer's language has an English tab. You edit in English; Translate turns your
      // edits back into their language, and Send always sends that tab.
      const foreign = Boolean(d.lang && d.lang !== 'English' && d.body_en);
      const tab = foreign && S.tab[d.id] === 'en' ? 'en' : 'native';
      const enKey = `${d.id}:en`;
      const native = S.edits[d.id] ?? d.body;
      const dirty = () => foreign && S.edits[enKey] != null && S.edits[enKey].trim() !== d.body_en.trim();
      const enDirty = dirty();
      const key = tab === 'en' ? enKey : d.id;
      const ta = el('textarea', { 'aria-label': tab === 'en' ? `Reply to ${d.customer}, in English` : `Reply to ${d.customer}` });
      ta.value = S.edits[key] ?? (tab === 'en' ? d.body_en : d.body);
      const fit = () => { ta.style.height = 'auto'; ta.style.height = `${ta.scrollHeight + 2}px`; };  // the whole reply shows, no inner scroll
      ta.addEventListener('input', () => { S.edits[key] = ta.value; fit(); sync(); });
      requestAnimationFrame(fit);
      const tabBtn = (id, label) => el('button', { class: 'tab', type: 'button', role: 'tab', 'aria-selected': String(tab === id),
        onclick: () => { S.tab[d.id] = id; renderForYou(); } }, label, id === 'en' && enDirty ? el('span', { class: 'dot', title: 'Edited' }) : null);
      const gap = hasGap(native);
      const translate = el('button', { class: 'btn btn-strong', type: 'button', onclick: async (e) => {
        const b = e.currentTarget;
        b.textContent = 'Translating…';
        if (await act('translate-draft', { id: d.id, english: S.edits[enKey] }, b)) {
          delete S.edits[enKey]; delete S.edits[d.id]; S.tab[d.id] = 'native';
        } else b.textContent = `Translate to ${d.lang}`;
      } }, `Translate to ${d.lang}`);
      const noteFor = (en) => (!foreign ? (d.lang ? null : 'Checking its language…')
        : tab === 'en' ? (en ? `Your edits are in English. Translate them to ${d.lang} before you send.` : `Edit here in English, then translate. Send always sends the ${d.lang} version.`)
          : (S.edits[d.id] != null ? 'The English tab shows the version from before your edits here.' : `This is what ${d.customer} receives.`));
      const send = el('button', { class: 'btn btn-coral', type: 'button', onclick: async (e) => {
        if (await act('send-draft', { id: d.id, body: S.edits[d.id] ?? d.body }, e.currentTarget)) { delete S.edits[d.id]; delete S.edits[enKey]; }
      } }, 'Send');
      const info = el('span', { class: 'why' });
      // Typing doesn't re-render the card (it would steal the cursor), so the buttons follow the text here.
      const sync = () => {
        const en = dirty();
        translate.hidden = !en;
        send.disabled = en;
        send.title = en ? `Translate your English edits to ${d.lang} first` : '';
        info.textContent = noteFor(en) || `Drafted ${ago(d.created)}. Edit it here; Send goes out from ${usLabel()} in the same thread.`;
      };
      sync();
      return el('article', { class: 'fy fy--lead' }, head(c || { customer: d.customer, product: d.product }),
        d.why ? el('p', { class: 'why' }, d.why) : null,
        el('p', { class: 'subj' }, d.subject.startsWith('Re:') ? d.subject : `Re: ${d.subject}`),
        foreign ? el('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Language' }, tabBtn('native', d.lang), tabBtn('en', 'English')) : null,
        ta,
        gap && !enDirty ? el('p', { class: 'gap' }, 'Fill in the [Owner: …] part, then send.') : null,
        el('div', { class: 'actions' }, translate, send,
          el('button', { class: 'btn btn-quiet', type: 'button', onclick: (e) => act('discard-draft', { id: d.id }, e.currentTarget) }, 'Discard'),
          info));
    };
    // The session that made the PR, opened in the Claude app: a cloud one by its link, one the desk started
    // by handing it over. GitHub is the fallback, and the PR number above always links there too.
    const madeBy = (p) => {
      const m = (S.data && S.data.made_by && S.data.made_by[p.url]) || {};
      const tip = 'Opens the session that made this PR in the Claude app, to see what it changed and ask it about it';
      if (/^claude:\/\/claude\.ai\/epitaxy\/session_[A-Za-z0-9]+$/.test(m.app || '')) return el('a', { class: 'btn btn-sm', href: m.app, title: tip }, 'Open in Claude');
      if (/^[0-9a-f]{6,12}$/.test(m.short || '')) return button('Open in Claude', '', 'open-session', { short: m.short }, tip);
      return el('a', { class: 'btn btn-sm', href: p.url, target: '_blank', rel: 'noopener' }, 'Review on GitHub');
    };
    const mergeCard = ({ p, who }) => el('article', { class: 'fy' },
      head(conv(p.conversation), el('a', { class: 'mono', href: p.url, target: '_blank', rel: 'noopener' }, p.pr)),
      el('p', {}, p.title), el('p', { class: 'why' }, `Fixes ${who.join(' and ')}'s report. Merging also ships it, in one release with anything else for ${p.app.split(' ')[0]} today.`),
      el('div', { class: 'actions' }, button('Merge & ship', 'btn-coral', 'merge-ship', { pr: p.pr }, 'Starts a session that checks, merges and ships it'),
        madeBy(p)));
    const releaseCard = ({ p, prs, who }) => el('article', { class: 'fy' },
      el('div', { class: 'fy-head' }, el('span', { class: 'who' }, `${p.app} ${p.release === 'next release' ? '' : p.release}`.trim())),
      el('p', {}, p.release === 'next release' ? `${prs.join(', ')} ${prs.length > 1 ? 'are' : 'is'} merged, but no release has started.`
        : `Built with ${prs.join(', ')}, but not in review yet.`),
      el('p', { class: 'why' }, `Gets the fix to ${who.join(', ')}. They're told automatically when it's in review and when it's live.`),
      el('div', { class: 'actions' }, button(p.release === 'next release' ? 'Start the release' : 'Submit for review', 'btn-coral', 'ship', { pr: p.pr },
        'Starts a session that ships it')));
    // Conversations from the Slack bug channel are answered in Slack; the desk never posts there.
    const answerCard = ({ c }) => {
      if (isSlack(c)) {
        return el('article', { class: 'fy' }, head(c), el('p', {}, c.owed || c.issue),
          el('p', { class: 'why' }, 'Reported in Slack. Reply there; the desk only reads the channel.'),
          el('div', { class: 'actions' }, sourceLink(c, 'btn btn-strong'),
            el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onclick: () => quickClose(c) }, 'Close as not applicable')));
      }
      const s = sessionFor(c, 'reply');
      const idle = !s || s.status === 'done';
      return el('article', { class: 'fy' }, head(c),
        el('p', {}, c.owed || c.issue),
        s ? runRow(s) : null,
        el('div', { class: 'actions' }, idle ? button(s ? 'Draft again' : 'Draft a reply', 'btn-strong', 'reply', { id: c.id }, 'A session drafts it in your voice and puts it here for you to approve') : null,
          sourceLink(c),
          el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onclick: () => quickClose(c) }, 'Close as not applicable')));
    };
    const fixCard = ({ c }) => {
      const s = sessionFor(c, 'fix');
      return el('article', { class: 'fy' }, head(c), el('p', {}, c.issue),
        s ? runRow(s) : el('p', { class: 'why' }, 'A reported bug nothing is fixing yet. Autopilot starts a fix itself within the hour, or start it now.'),
        !s || s.status === 'done' ? el('div', { class: 'actions' }, button('Fix it', 'btn-strong', 'fix', { id: c.id }, 'Starts a session that fixes it and opens a PR')) : null);
    };
    const ideaCard = ({ c }) => {
      const s = sessionFor(c, 'build');
      return el('article', { class: 'fy' }, head(c), el('p', {}, c.issue),
        s ? runRow(s) : el('p', { class: 'why' }, `A feature idea. ${c.customer} was told it's on the list. Build it, or close it if it isn't going to happen.`),
        !s || s.status === 'done' ? el('div', { class: 'actions' }, buildBtn(c),
          el('button', { class: 'btn btn-sm btn-quiet', type: 'button', onclick: () => quickClose(c) }, 'Close as not applicable')) : null);
    };
    const sessionCard = ({ s }) => {
      const c = conv(s.target);
      return el('article', { class: 'fy' }, c ? head(c) : el('div', { class: 'fy-head' }, el('span', { class: 'who' }, s.title)),
        c ? el('p', {}, s.title) : null,
        runRow(s, button('Dismiss', 'btn-quiet', 'stop-session', { short: s.short }, 'Stops it. Its conversation is kept')));
    };
    const blockedCard = ({ c, w }) => {
      const s = deskFor(c).find((x) => x.status !== 'ended');
      return el('article', { class: 'fy' }, head(c), el('p', {}, `${w.session || 'A session'} stopped: ${w.note || 'no reason given'}.`),
        el('div', { class: 'actions' }, s ? openBtn(s) : null));
    };

    const sections = [
      sec('Replies to approve', group('draft'), draftCard),
      sec('Pull requests to merge', group('merge'), mergeCard),
      sec('Releases to send out', group('release'), releaseCard),
      sec('Only you can answer', group('answer'), answerCard),
      sec('Replies owed', group('reply'), answerCard),
      sec('Bugs with no fix yet', group('fix'), fixCard),
      sec('Feature ideas', group('idea'), ideaCard),
      sec('Stuck', group('blocked'), blockedCard),
      sec('Sessions that stopped', group('session'), sessionCard),
    ].filter(Boolean);
    const open = ticketsOpen();
    const products = [...new Set(open.map((t) => t.product))];
    fill(box,
      el('h2', {}, 'For you'),
      el('p', { class: 'sub' }, 'Only the things that need you. Autopilot handles acknowledgments, release notes and closing on its own.'),
      // Tickets live in their own view; this card says how many there are and takes you there.
      open.length ? el('article', { class: 'fy' },
        el('div', { class: 'fy-head' }, el('span', { class: 'who' }, `${open.length} ${open.length === 1 ? 'ticket needs' : 'tickets need'} you outside customer support`)),
        el('p', { class: 'why' }, `Across ${products.length > 2 ? `${products.slice(0, -1).join(', ')} and ${products.at(-1)}` : products.join(' and ')}${open.some((t) => t.priority === 'now') ? `; ${open.filter((t) => t.priority === 'now').length} marked now` : ''}.`),
        el('div', { class: 'actions' }, el('button', { class: 'btn btn-sm btn-strong', type: 'button', onclick: () => { S.view = 'tickets'; save(); render(); } }, 'Open tickets'))) : null,
      ...(sections.length ? sections : [el('div', { class: 'allclear' }, el('desk-bot', { size: 104, name: 'allclear', 'aria-hidden': 'true' }),
        el('b', {}, open.length ? 'No customer conversations need you.' : 'Nothing needs you.'),
        (S.data?.autopilot || {}).on === false ? 'Autopilot is paused, so nothing runs on its own.' : 'Autopilot is handling the rest.')]));
  }

  // ---------- tickets ----------
  // Things only you can do across your products. A session adds one only when you ask (desk/tickets.py), and marks it
  // done when the work is finished; one about a PR closes by itself once the PR is merged or closed.
  function renderTickets(board) {
    const open = ticketsOpen();
    const done = list('tickets').filter((t) => t.status === 'done').sort((a, b) => (a.done_at < b.done_at ? 1 : -1)).slice(0, 30);
    const products = [...new Set(list('tickets').map((t) => t.product))].sort();
    if (S.product !== 'all' && !products.includes(S.product)) S.product = 'all';
    const q = S.q.toLowerCase();
    const match = (t) => (S.product === 'all' || t.product === S.product)
      && (!q || [t.title, t.needed, t.context, t.product, t.key].some((x) => String(x || '').toLowerCase().includes(q)));
    const shown = open.filter(match);
    const chip = (id, label, count) => el('button', { class: 'tab', type: 'button', role: 'tab', 'aria-selected': String(S.product === id),
      onclick: () => { S.product = id; render(); } }, label, el('span', { class: 'n' }, String(count)));
    // A web page opens in a tab; a claude:// link opens that session in the Claude app; a path is shown to copy.
    const linkOf = (t) => (/^https:\/\//.test(t.link || '') ? el('a', { class: 'btn btn-sm', href: t.link, target: '_blank', rel: 'noopener' }, 'Open')
      : /^claude:\/\/claude\.ai\/epitaxy\/local_[0-9a-f-]{36}$/.test(t.link || '') ? el('a', { class: 'btn btn-sm', href: t.link }, 'Open in Claude')
        : t.link && t.link.startsWith('/') ? el('code', { class: 'mono', title: 'A file on this Mac' }, t.link) : null);
    // Work on it starts a local Claude session on the ticket (desk/ticket-runbook.md); its latest one shows on the card.
    // Past the desk's cap of working ticket sessions, a press waits its turn in line and starts by itself later.
    const Q = (S.data && S.data.ticket_queue) || { waiting: [], failed: {}, cap: 4, working: 0 };
    const card = (t) => {
      const s = desk().find((x) => x.action === 'ticket' && x.target === t.id);
      const busy = s && ['working', 'blocked'].includes(s.status);
      const pos = (Q.waiting || []).findIndex((w) => w.id === t.id);
      const failed = pos < 0 && (Q.failed || {})[t.id];
      const queued = pos >= 0 ? el('div', { class: 'run run--queued' },
        el('p', {}, el('b', {}, 'Waiting its turn. '), pos ? `${pos} ahead of it. ` : 'Next in line. ',
          `It starts by itself once fewer than ${Q.cap} ticket sessions are working.`),
        el('div', { class: 'actions' }, button('Leave the line', 'btn-quiet', 'ticket-unqueue', { id: t.id }, 'Takes it out of the line. Nothing starts'))) : null;
      return el('article', { class: 'fy' },
        el('div', { class: 'fy-head' }, el('span', { class: 'who' }, t.title), el('span', { class: 'prod' }, t.product),
          t.priority === 'now' ? pill(['Now', 'coral']) : null),
        el('p', {}, t.needed),
        t.context ? el('p', { class: 'why' }, t.context) : null,
        queued || (s ? runRow(s) : null),
        failed ? el('p', { class: 'why' }, `${failed} Work on it tries again.`) : null,
        el('div', { class: 'actions' },
          busy || queued ? null : button('Work on it', 'btn-coral', 'ticket-work', { id: t.id },
            'Starts a Claude session on this Mac that does what it can on this ticket, then asks you before anything only you can do'),
          button('Done', '', 'ticket-done', { id: t.id }, 'Marks it done. You can reopen it below'), linkOf(t),
          el('span', { class: 'why' }, `Added ${ago(t.created)}${t.source ? ` by ${t.source}` : ''}`)));
    };
    const sec = (title, rows) => (rows.length ? el('section', { class: 'fy-sec' }, el('h3', {}, title, el('span', { class: 'n' }, String(rows.length))), rows.map(card)) : null);
    fill(board,
      el('h2', {}, 'Tickets'),
      el('p', { class: 'sub' }, 'What only you can do across your products: merges, decisions, sign-ins and keys. Customer conversations stay in For you, and the autopilot never acts on these.'),
      (Q.waiting || []).length ? el('p', { class: 'why' }, `${Q.working} of ${Q.cap} ticket sessions working, ${Q.waiting.length} waiting their turn.`) : null,
      products.length > 1 ? el('div', { class: 'tabs', role: 'tablist', 'aria-label': 'Product' },
        chip('all', 'All', open.length), ...products.map((p) => chip(p, p, open.filter((t) => t.product === p).length))) : null,
      shown.length ? [sec('Now', shown.filter((t) => t.priority === 'now')), sec('Soon', shown.filter((t) => (t.priority || 'soon') === 'soon')),
        sec('Later', shown.filter((t) => t.priority === 'later'))]
        : emptyNote(open.length ? 'No tickets match.' : 'No tickets need you.'),
      done.length ? el('details', { class: 'fy-sec' }, el('summary', {}, `Done (${done.length})`),
        done.map((t) => el('article', { class: 'fy' },
          el('div', { class: 'fy-head' }, el('span', { class: 'who' }, t.title), el('span', { class: 'prod' }, t.product)),
          el('div', { class: 'actions' }, button('Reopen', 'btn-quiet', 'ticket-reopen', { id: t.id }, 'Puts it back on the list'),
            el('span', { class: 'why' }, `Done ${ago(t.done_at)}${t.done_note ? ` · ${t.done_note}` : ''}`))))) : null);
  }

  // ---------- conversations ----------
  function filtered() {
    const you = needsYou();
    const v = VIEWS.find((x) => x.id === S.view) || VIEWS[0];
    const q = S.q.toLowerCase();
    return convs().filter((c) => v.test(c, you))
      .filter((c) => !q || [c.customer, c.product, c.issue, c.prs, c.owed].some((x) => String(x || '').toLowerCase().includes(q)))
      .sort((a, b) => Number(you.has(b.id)) - Number(you.has(a.id)) || (a.last_activity < b.last_activity ? 1 : -1));
  }
  function renderList() {
    const you = needsYou();
    const rows = filtered();
    if (window.innerWidth > 980 && rows.length && !rows.some((c) => c.id === S.sel)) S.sel = rows[0].id;
    $('#list-empty').hidden = rows.length > 0;
    $('#list').replaceChildren(...rows.map((c) => el('li', {
      class: 'item', role: 'option', 'aria-selected': String(S.sel === c.id), tabindex: '0', 'data-id': c.id,
      onclick: () => select(c.id), onkeydown: (e) => { if (e.key === 'Enter') select(c.id); },
    },
    el('div', { class: 'who' }, you.has(c.id) ? el('span', { class: 'youdot', title: 'Needs you' }) : null, c.customer, el('span', { class: 'prod' }, c.product)),
    el('div', { class: 'when', title: stamp(c.last_activity) }, day(c.last_activity)),
    el('div', { class: 'issue' }, c.issue),
    el('div', { class: 'tags' }, pill(statusOf(c, you))))));
  }

  // ---------- what the customer wrote ----------
  // Opening a conversation reads its emails from Gmail (about 20 seconds); the desk keeps them on this Mac
  // and reads them again once the conversation has newer activity than the copy.
  const msgState = (c) => ((S.data && S.data.messages) || {})[c.id] || {};
  async function loadMessages(id, t) {
    S.msgs[id] = { ...(S.msgs[id] || {}), loading: true };
    try {
      const r = await request(`/api/messages?id=${id}`, { cache: 'no-store' });
      S.msgs[id] = { t, doc: r.ok ? await r.json() : null };
    } catch {
      delete S.msgs[id];
      return;
    }
    if (S.sel === id) renderDetail();
  }
  function wantMessages(c) {
    if (isSlack(c)) return;  // nothing to read from Gmail
    const st = msgState(c);
    const have = S.msgs[c.id];
    if (st.t && have?.t !== st.t && !have?.loading) loadMessages(c.id, st.t);
    const ask = `${c.id}|${c.last_activity}`;  // once per page load, and again only when something new came in
    if ((!st.t || Date.parse(c.last_activity) / 1000 > st.t) && !st.started && !st.error && !S.asked.has(ask)) {
      S.asked.add(ask);
      act('read-messages', { id: c.id }, null, true);
    }
  }
  // Mail apps hard-wrap plain text, so a line that carries on in lowercase is one sentence: join it back up.
  // Paragraph breaks, sign-offs ("Thanks,\nAlex") and list items keep their lines.
  const unwrap = (t) => t.replace(/([^\n])\n(?=[a-z(])/g, '$1 ');
  function msgItem(c, m, i, all) {
    const key = `${c.id}:${m.at}:${i}`;
    const text = unwrap(m.text);
    const long = text.length > 700 || text.split('\n').length > 10;
    const open = S.expanded.has(key);
    return el('li', { class: `msg${m.us ? ' msg--us' : ''}${m.auto ? ' msg--auto' : ''}` },
      el('div', { class: 'msg-h' }, el('b', {}, m.us ? usLabel() : m.from), m.auto ? el('span', { class: 'pill' }, 'Automatic') : null,
        m.at.startsWith('1970') ? null : el('span', { class: 'when' }, stamp(m.at))),
      (i === 0 || all[i - 1].thread !== m.thread) && m.subject ? el('p', { class: 'msg-subj' }, m.subject) : null,
      el('p', { class: `msg-body${long && !open ? ' clamp' : ''}` }, text || 'No text in this message.'),
      long ? el('button', { class: 'linkish', type: 'button', onclick: () => { if (open) S.expanded.delete(key); else S.expanded.add(key); renderDetail(); } },
        open ? 'Show less' : 'Show more') : null,
      m.atts.length ? el('p', { class: 'msg-att' }, `Attached: ${m.atts.join(', ')}`) : null);
  }
  function slackCard(c) {
    return el('section', { class: 'card' },
      el('div', { class: 'msgs-h' }, el('h3', {}, 'Slack')),
      el('p', {}, `${c.customer} reported this in ${slackChannel()}.`),
      el('p', { class: 'msgs-note' }, 'Each autopilot pass reads the channel. The desk never posts, replies or emails about it.'),
      el('div', { class: 'actions' }, sourceLink(c)));
  }
  function messagesCard(c) {
    const st = msgState(c);
    const doc = S.msgs[c.id]?.doc;
    const status = st.started ? [el('span', { class: 'spin', 'aria-hidden': 'true' }), 'Reading from Gmail…'] : st.at && !st.error ? `Read ${ago(st.at)}` : '';
    return el('section', { class: 'card' },
      el('div', { class: 'msgs-h' }, el('h3', {}, 'Emails'),
        el('span', { class: 'msgs-st' }, status, st.started ? null
          : el('button', { class: 'linkish', type: 'button', onclick: () => act('read-messages', { id: c.id }, null, true) }, st.error ? 'Try again' : 'Refresh'))),
      st.error ? el('p', { class: 'msgs-err' }, st.error) : null,
      doc && doc.trimmed ? el('p', { class: 'msgs-note' }, `${doc.trimmed} earlier message${doc.trimmed > 1 ? 's are' : ' is'} too long to show here. Gmail has the whole thread.`) : null,
      doc ? (doc.messages.length ? el('ol', { class: 'msgs' }, doc.messages.map((m, i, all) => msgItem(c, m, i, all))) : el('p', { class: 'msgs-note' }, 'No messages in this thread.'))
        : st.started ? el('p', { class: 'msgs-note' }, 'The first read takes about 20 seconds.') : null,
      doc && doc.errors.length ? el('p', { class: 'msgs-err' }, `${doc.errors.length === 1 ? 'One thread' : 'Some threads'} couldn't be read. Gmail has them.`) : null,
      doc ? el('p', { class: 'msgs-note' }, 'Read from the mailbox through Composio. Email addresses are blanked, and the copy stays on this Mac.') : null);
  }

  function timeline(c) {
    const ev = [];
    ev.push({ at: c.last_activity, kind: c.last_from === 'us' ? 'us' : 'them', what: c.last_from === 'us' ? 'We wrote last' : `${c.customer} wrote last` });
    for (const p of fixesFor(c.id)) {
      const link = el('a', { href: p.url, target: '_blank', rel: 'noopener' }, p.pr);
      if (p.pr_state === 'open') ev.push({ at: null, kind: 'you', what: [link, ` is open: ${p.title}`] });
      else if (p.pr_state === 'merged' && p.merged_at) ev.push({ at: p.merged_at, kind: 'ship', what: [link, ` merged: ${p.title}`] });
      if (p.release_state === 'live') ev.push({ at: null, kind: 'ship', what: `Live in ${p.app} ${p.release}` });
      else if (p.release_state === 'waiting') ev.push({ at: null, kind: '', what: `Waiting on ${p.app} ${p.release}${p.release_note ? `: ${p.release_note}` : ''}` });
      else if (p.release_state === 'unverified') ev.push({ at: null, kind: '', what: `On ${p.app}'s production track; the store can't confirm it's public yet` });
    }
    for (const n of (S.data && S.data.notes_sent) || []) {
      const [cid, app, release, m] = n.key.split('|');
      if (cid === c.id) ev.push({ at: n.at, kind: 'us', what: `Told ${c.customer} it's ${m === 'live' ? 'live' : 'in review'}: ${app} ${release}` });
    }
    const SAYS = { working: (s) => `working${s.step ? `: ${s.step}` : ''}`, blocked: () => 'asking you something', failed: () => 'stopped, the connection dropped', done: () => 'finished', app: () => 'moved to the Claude app', ended: () => 'ended' };
    for (const s of deskFor(c)) ev.push({ at: s.started, kind: ['working', 'blocked', 'failed'].includes(s.status) ? 'you' : '', what: [`${s.title}: ${(SAYS[s.status] || SAYS.ended)(s)} `, canOpen(s) ? openBtn(s) : null] });
    for (const d of list('drafts').filter((x) => x.conversation === c.id)) {
      if (d.status === 'pending') ev.push({ at: d.created, kind: 'you', what: 'A drafted reply is waiting for you in For you' });
      if (d.status === 'sent') ev.push({ at: d.updated, kind: 'us', what: 'Your approved reply was sent' });
      if (d.status === 'failed') ev.push({ at: d.updated, kind: 'you', what: `A reply didn't send: ${d.error || ''}` });
    }
    const w = workFor(c.id);
    if (w) ev.push({ at: w.updated, kind: w.state === 'blocked' ? 'you' : '', what: `${w.session || 'A session'}: ${w.state}${w.note ? `, ${w.note}` : ''}` });
    ev.sort((a, b) => (a.at ? 1 : 0) - (b.at ? 1 : 0) || (a.at && b.at ? (a.at < b.at ? 1 : -1) : 0));
    const seen = new Set();  // two fixes in one release would say "Live in …" twice
    const once = ev.filter((e) => typeof e.what !== 'string' || (!seen.has(e.what) && seen.add(e.what)));
    return el('ol', { class: 'tl' }, once.map((e) => el('li', { class: e.kind }, el('span', { class: 'when' }, e.at ? day(e.at) : 'now'), el('span', { class: 'pip' }), el('span', { class: 'what' }, e.what))));
  }

  function renderDetail() {
    const box = $('#detail');
    const c = conv(S.sel);
    if (!c) {
      box.hidden = window.innerWidth <= 980;
      // It looks over at the list, where the conversations are.
      box.replaceChildren(el('div', { class: 'd-empty' }, el('desk-bot', { size: 72, name: 'detail', gaze: 'left', 'aria-hidden': 'true' }), el('p', {}, 'Pick a conversation.')));
      return;
    }
    box.hidden = false;
    wantMessages(c);
    const you = needsYou();
    const mine = forYou().filter((i) => i.conv === c.id);
    const build = sessionFor(c, 'build');
    // A bug or an idea is one button, so it's right here; anything with more to it (a draft, a merge) is handled in For you.
    const main = mine.every((i) => i.kind === 'idea') ? (!build || build.status === 'done' ? buildBtn(c) : null)
      : mine.every((i) => i.kind === 'fix') ? button('Fix it', 'btn-strong', 'fix', { id: c.id }, 'Starts a session that fixes it and opens a PR')
        : el('button', { class: 'btn btn-coral btn-sm', type: 'button', onclick: () => { S.view = 'foryou'; save(); render(); } }, 'Handle it in For you');
    fill(box,
      window.innerWidth <= 980 ? el('button', { class: 'btn btn-quiet btn-sm detail-back', type: 'button', onclick: () => select(null) }, '← Back to conversations') : null,
      el('header', { class: 'd-head' },
        el('h2', { class: 'd-who' }, c.customer, el('span', { class: 'prod' }, c.product), pill(statusOf(c, you))),
        el('p', { class: 'd-issue' }, c.issue),
        el('div', { class: 'actions' }, sourceLink(c))),
      mine.length ? el('section', { class: 'card next' }, el('h3', {}, 'Needs you'),
        el('p', {}, mine.map((i) => ({ draft: 'A drafted reply is ready to approve.', merge: `${i.p?.pr} is ready to merge.`, release: `${i.p?.app} ${i.p?.release} needs sending out.`,
          answer: c.owed || 'Needs your answer.', reply: `Reply owed: ${c.owed}`, fix: 'A bug nothing is fixing yet.', blocked: 'A session is stuck.',
          idea: 'A feature idea nobody is building yet.', session: 'A session stopped.' }[i.kind])).join(' ')),
        main ? el('div', { class: 'actions' }, main) : null) : null,
      build ? runRow(build) : null,
      c.owed && !mine.length ? el('section', { class: 'card callout' }, el('h3', {}, 'Still owed'), el('p', {}, c.owed)) : null,
      isSlack(c) ? slackCard(c) : messagesCard(c),
      el('section', { class: 'card' }, el('h3', {}, 'What happened'), timeline(c)),
      isOpen(c) ? el('div', { class: 'actions' },
        el('button', { class: 'linkish', type: 'button', onclick: () => { S.closing = S.closing === c.id ? null : c.id; renderDetail(); } }, 'Close as not applicable')) : null,
      S.closing === c.id ? closeForm(c) : null);
  }

  function closeForm(c) {
    const input = el('input', { type: 'text', maxlength: '200', placeholder: 'Reason (optional): spam, duplicate, handled elsewhere…', 'aria-label': 'Reason' });
    const go = el('button', { class: 'btn btn-ink btn-sm', type: 'button', onclick: async (e) => {
      if (await act('close', { id: c.id, reason: input.value, force: true }, e.currentTarget)) { S.closing = null; renderDetail(); }
    } }, 'Close it');
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go.click(); if (e.key === 'Escape') { S.closing = null; renderDetail(); } });
    setTimeout(() => input.focus(), 0);
    return el('div', { class: 'close-form' }, input, go, el('button', { class: 'btn btn-quiet btn-sm', type: 'button', onclick: () => { S.closing = null; renderDetail(); } }, 'Cancel'));
  }

  // ---------- releases and activity ----------
  function renderReleases(board) {
    const ship = (S.data && S.data.ship) || {};
    const r = (S.data && S.data.refresh) || {};
    fill(board,
      el('div', { class: 'row' }, el('h2', {}, 'Releases'),
        el('button', { class: 'btn btn-sm', type: 'button', disabled: Boolean(r.running), onclick: (e) => act('refresh-quick', {}, e.currentTarget) },
          r.running ? 'Rechecking…' : 'Recheck releases')),
      el('p', { class: 'sub' }, `What customers can install today and what ships next. Autopilot rechecks every two hours${list('meta')[0]?.github_checked_at ? `; last ${ago(list('meta')[0].github_checked_at)}` : ''}.`),
      el('div', { class: 'rel-grid' }, list('releases').map((x) => {
        const train = ship[x.repo];
        return el('article', { class: 'rel' },
          el('div', {}, el('span', { class: 'app-name' }, x.app), ' ', el('span', { class: 'plat' }, x.platform)),
          el('div', { class: 'row' }, el('span', { class: 'live-v' }, x.live_version), pill(x.live_state === 'live' ? ['Live', 'ok'] : ['Unconfirmed', 'warn'])),
          el('div', { class: 'row' }, el('span', { class: 'k' }, 'Next'), x.next_version ? el('span', { class: /\d/.test(x.next_version) ? 'mono' : '' }, x.next_version) : null, x.next_stage),
          el('div', { class: 'row' }, el('span', { class: 'k' }, 'Merged'), `${x.unreleased} since ${x.live_version}`),
          train ? el('div', { class: 'row' }, el('span', { class: 'k' }, 'Today'), `${train.version ? `${train.version} ` : ''}${train.state}`) : null);
      })),
      el('h3', { class: 'sec-h' }, 'Merged, not released yet'),
      !list('unreleased').length ? emptyNote('Everything merged has shipped.') : responsiveTable(
        el('thead', {}, el('tr', {}, ['App', 'PR', 'Title', 'Merged', 'Ships in'].map((h) => el('th', {}, h)))),
        el('tbody', {}, list('unreleased').map((u) => el('tr', {},
          el('td', {}, u.app), el('td', { class: 'mono' }, el('a', { href: u.url, target: '_blank', rel: 'noopener' }, u.pr)),
          el('td', {}, u.title, u.support ? [' ', pill(['support', 'coral'])] : null),
          el('td', { class: 'dim' }, u.merged_at ? day(u.merged_at) : ''), el('td', {}, u.release, el('div', { class: 'dim' }, u.release_note)))))));
  }

  function renderActivity(board) {
    const ap = (S.data && S.data.autopilot) || {};
    const passes = (S.data && S.data.passes) || [];
    const notes = (S.data && S.data.notes_sent) || [];
    const names = Object.fromEntries(convs().map((c) => [c.id, c.customer]));
    const table = (heads, rows, empty) => {
      if (!rows.length) return emptyNote(empty);
      return responsiveTable(el('thead', {}, el('tr', {}, heads.map((h) => el('th', {}, h)))), el('tbody', {}, rows));
    };
    const passRows = passes.map((x) => el('tr', {},
      el('td', { class: 'dim' }, ago(x.at)), el('td', {}, x.summary), el('td', {}, x.acked.join(', ')),
      el('td', {}, x.notes.join(', ')), el('td', {}, x.closed.join(', ')), el('td', {}, (x.needs_owner || []).join(', '))));
    const noteRows = notes.map((n) => {
      const [cid, app, release, m] = n.key.split('|');
      return el('tr', {}, el('td', { class: 'dim' }, ago(n.at)), el('td', {}, names[cid] || '—'),
        el('td', {}, `${app} ${release}: ${m === 'live' ? 'live' : 'in review'}`));
    });
    const STATE = { working: ['Working', 'info'], blocked: ['Asking you', 'coral'], failed: ['Connection dropped', 'warn'], done: ['Done', 'ok'], app: ['In Claude', 'violet'], ended: ['Ended', ''] };
    const sessionRows = desk().map((s) => el('tr', {},
      el('td', {}, s.title, s.auto ? el('div', { class: 'dim' }, 'started by autopilot') : null),
      el('td', {}, pill(STATE[s.status] || [s.status, ''])),
      el('td', { class: 'dim', title: s.status === 'working' ? null : s.said }, brief(s.status === 'working' ? s.step : s.said)),
      el('td', { class: 'dim' }, ago(s.started)),
      el('td', {}, canOpen(s) ? openBtn(s) : null)));
    const workRows = list('work').map((w) => {
      const quiet = w.state === 'working' && Date.now() - new Date(w.updated) > QUIET_HOURS * 3600000;
      return el('tr', {},
        el('td', {}, el('button', { class: 'linkish', type: 'button', onclick: () => goTo(w.conversation) }, w.customer)),
        el('td', {}, w.session), el('td', {}, w.state), el('td', { class: 'dim' }, w.note),
        el('td', { class: quiet ? '' : 'dim', style: quiet ? 'color: var(--warn)' : null }, ago(w.updated)));
    });
    fill(board,
      el('h2', {}, 'Activity'),
      el('p', { class: 'sub' }, `Autopilot checks about hourly (${ap.hours || '08:00–23:00'}): it reads Gmail${slackOn() ? ' and the Slack bug channel' : ''}, acknowledges new emails, sends release notes and closes what's done. Releases are rechecked every two hours. Bugs nobody is fixing get a fix session, up to ${ap.fix_cap || 3} a day.`),
      el('h3', { class: 'sec-h' }, 'Checks'),
      table(['When', 'What it did', 'Acknowledged', 'Told', 'Closed', 'Left for you'], passRows,
        'No checks yet. The first one runs within about ten minutes of the desk starting.'),
      el('h3', { class: 'sec-h' }, 'Release notes sent'),
      table(['When', 'Customer', 'News'], noteRows, 'None yet. Customers get one note when their fix reaches review and one when it goes live.'),
      el('h3', { class: 'sec-h' }, 'Sessions'),
      table(['Session', 'State', 'Latest', 'Started', ''], sessionRows, 'No sessions yet.'),
      el('h3', { class: 'sec-h' }, 'What sessions report'),
      table(['Customer', 'Session', 'Status', 'Note', 'Updated'], workRows, 'Nothing reported.'));
  }

  // ---------- frame ----------
  function render() {
    renderStrip();
    renderNav();
    const isConv = VIEWS.some((v) => v.id === S.view);
    $('#foryou').hidden = S.view !== 'foryou';
    $('#list-pane').hidden = !isConv;
    $('#board').hidden = !['releases', 'activity', 'tickets'].includes(S.view);
    $('#settings').hidden = S.view !== 'settings';
    $('#search').disabled = S.view === 'settings';
    $('#main').classList.toggle('board-on', !isConv);
    if (S.view === 'settings') { $('#detail').hidden = true; dispatchEvent(new Event('desksettingsopen')); return; }
    if (!S.data) return;
    if (S.view === 'foryou') { $('#detail').hidden = true; renderForYou(); return; }
    if (S.view === 'releases') { $('#detail').hidden = true; renderReleases($('#board')); return; }
    if (S.view === 'activity') { $('#detail').hidden = true; renderActivity($('#board')); return; }
    if (S.view === 'tickets') { $('#detail').hidden = true; renderTickets($('#board')); return; }
    renderList();
    renderDetail();
  }
  function select(id) {
    const previous = S.sel;
    S.sel = id;
    S.closing = null;
    render();
    if (id) document.querySelector(`.item[data-id="${id}"]`)?.scrollIntoView({ block: 'nearest' });
    if (id && id !== previous) $('#detail').scrollTop = 0;
    if (window.innerWidth <= 980) {
      const target = id ? $('#detail .detail-back') : [...document.querySelectorAll('.item')].find(node => node.dataset.id === previous);
      target?.focus({ preventScroll: true });
    }
  }

  async function poll(force = false) {
    try {
      const response = await request('/api/state', { cache: 'no-store' });
      if (!response.ok) throw new Error('The desk did not return its state.');
      const d = await response.json();
      const wasOffline = !S.online;
      S.online = true;
      if (force || wasOffline || d.version !== S.version) {
        S.data = d;
        S.version = d.version;
        const typing = document.activeElement && document.activeElement.closest('.close-form');
        const sel = getSelection();  // don't pull an email out from under text being selected or copied
        const selecting = sel && !sel.isCollapsed && $('#detail').contains(sel.anchorNode);
        if (!typing && !selecting) render(); else renderStrip();
      } else renderStrip();
    } catch (error) {
      if (error instanceof SignInRequired) return;
      S.online = false;
      renderStrip();
    }
  }

  $('#check-now').addEventListener('click', (e) => act('check-now', {}, e.currentTarget));
  function closeAccountMenu(restoreFocus = false) {
    $('#account-panel').hidden = true;
    $('#account-trigger').setAttribute('aria-expanded', 'false');
    if (restoreFocus) $('#account-trigger').focus();
  }
  $('#account-trigger').addEventListener('click', () => {
    const panel = $('#account-panel');
    panel.hidden = !panel.hidden;
    $('#account-trigger').setAttribute('aria-expanded', String(!panel.hidden));
  });
  $('#account-settings').addEventListener('click', () => {
    closeAccountMenu();
    S.view = 'settings'; S.sel = null; save(); render();
    $('#settings .settings-body').scrollTop = 0;
    $('#settings-heading').focus({ preventScroll: true });
  });
  document.addEventListener('pointerdown', event => {
    if (!event.target.closest('.account')) closeAccountMenu();
  });
  $('.account').addEventListener('focusout', event => {
    if (!event.currentTarget.contains(event.relatedTarget)) closeAccountMenu();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !$('#account-panel').hidden) {
      event.preventDefault();
      closeAccountMenu(true);
    }
  });
  $('#sign-out').addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const response = await request('/auth/logout', { method: 'POST' });
      if (!response.ok) throw new Error('Sign-out failed');
      document.querySelector('.app').hidden = true;
      location.replace('/login');
    } catch (error) {
      if (error instanceof SignInRequired) return;
      button.disabled = false;
      toast('Could not sign out. Check your connection and try again.', true);
    }
  });
  // The switch moves at once; the desk confirms on the next poll, and it flips back if the desk said no.
  $('#autopilot').addEventListener('click', async (e) => {
    const sw = e.currentTarget;
    const on = sw.getAttribute('aria-checked') !== 'true';
    sw.setAttribute('aria-checked', String(on));
    S.switching = true;
    const ok = await act('autopilot', { on }, null, true);
    S.switching = false;
    if (!ok) sw.setAttribute('aria-checked', String(!on));
    renderStrip();
  });
  $('#search').addEventListener('input', (e) => { S.q = e.target.value.trim(); render(); });
  $('#banner-copy').addEventListener('click', async (e) => {
    try { await navigator.clipboard.writeText($('#banner-cmd').textContent); e.currentTarget.textContent = 'Copied'; }
    catch { getSelection().selectAllChildren($('#banner-cmd')); e.currentTarget.textContent = 'Press ⌘C'; }
  });
  document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented) return;
    if (e.target.closest('input, textarea, .dialkit-root')) return;
    if (e.key === '/') { e.preventDefault(); $('#search').focus(); return; }
    if (e.key === 'Escape') { select(null); return; }
    if ((e.key === 'j' || e.key === 'k') && VIEWS.some((v) => v.id === S.view)) {
      const rows = filtered();
      if (!rows.length) return;
      const i = rows.findIndex((c) => c.id === S.sel);
      select(rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === 'j' ? 1 : -1)))].id);
    }
  });
  window.addEventListener('resize', () => { if (VIEWS.some((v) => v.id === S.view)) renderDetail(); });

  render();
  poll(true);
  setInterval(() => poll(), POLL_MS);
})();
