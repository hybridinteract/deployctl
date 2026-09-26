/* deployctl control panel — all of the panel's behaviour.
 *
 * No framework and no CDN, for three reasons, in order of importance:
 *
 *   1. A page that can start a production deploy must not load script from a
 *      third-party host. A compromised CDN would have had the same reach as the
 *      operator's ssh key.
 *   2. It has to work offline. The panel is most valuable mid-incident, which is
 *      exactly when the network is least trustworthy.
 *   3. Every request has to carry the session token (see panel/security.py).
 *      Doing that centrally in one send() is simpler than hooking a framework's
 *      request pipeline, and it cannot be forgotten at a call site.
 *
 * There are no inline handlers anywhere: the Content-Security-Policy forbids
 * them, so behaviour is attached by delegation from data-act attributes. Adding
 * a button means adding a data-act and a case in onClick, not a <script> block.
 *
 * Sections, top to bottom: requests · output pane · actions · tabs · live
 * facts · config form · hosts widget · tags · copy · toasts · resizable split ·
 * wiring.
 */
'use strict';

(function () {
  const TOKEN = document.body.dataset.token || '';
  const ENV = document.body.dataset.env || '';

  // ---- requests --------------------------------------------------------------

  /** Append the session token to a same-origin path. */
  function withToken(url) {
    return url + (url.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(TOKEN);
  }

  /** Fetch a fragment of HTML and put it into a target (an element or a selector). */
  async function swap(url, target, { method = 'GET', body = null } = {}) {
    const el = typeof target === 'string' ? document.querySelector(target) : target;
    if (el) el.classList.add('loading');
    try {
      const res = await fetch(withToken(url), { method, body, headers: { 'X-Deployctl-Token': TOKEN } });
      const text = await res.text();
      if (el) {
        el.innerHTML = res.ok ? text : '<span class="hint err">' + escapeHtml(text.slice(0, 400)) + '</span>';
      }
    } catch (err) {
      if (el) el.innerHTML = '<span class="hint err">' + escapeHtml(String(err)) + '</span>';
    } finally {
      if (el) el.classList.remove('loading');
    }
  }

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  // ---- output pane -------------------------------------------------------------
  // What the pane shows is a VIEW of a job, never its owner. A command started
  // from here runs as a server-side job (panel/jobs.py) and keeps going when
  // this stream closes — which every other card, a reload and closing the tab
  // all do. Stopping the command itself is the separate, confirmed Cancel.

  let es = null;
  let job = null; // { id, label } of the job the pane is following, while it runs

  function showTerm() {
    document.getElementById('shell').classList.remove('no-term');
  }

  function clearRunning() {
    document.querySelectorAll('.running').forEach((c) => c.classList.remove('running'));
  }

  function setCancel(visible) {
    const button = document.getElementById('termCancel');
    if (button) button.hidden = !visible;
  }

  function refreshJobs() {
    swap('/jobs', '#jobBar');
  }

  /** Stop following. The command, if one is running, carries on. */
  function detach() {
    if (es) { es.close(); es = null; }
    const state = document.getElementById('termState');
    if (state) state.textContent = job ? '(detached — still running; re-attach below)' : '(stopped)';
    job = null;
    setCancel(false);
    clearRunning();
  }

  /** Follow an SSE stream in the pane. */
  function follow(url, label, card) {
    detach();
    showTerm();
    const out = document.getElementById('termOut');
    document.getElementById('termTitle').textContent = label;
    document.getElementById('termState').textContent = '(running…)';
    out.textContent = '';
    if (card) card.classList.add('running');

    es = new EventSource(withToken(url));
    es.onmessage = (e) => {
      out.textContent += e.data + '\n';
      out.scrollTop = out.scrollHeight;
    };
    // Only job streams send these; a log follow is a plain viewer.
    es.addEventListener('job', (e) => {
      job = { id: e.data, label };
      setCancel(true);
      refreshJobs();
    });
    es.addEventListener('busy', () => refreshJobs());
    es.addEventListener('done', (e) => {
      const wasJob = job !== null;
      document.getElementById('termState').textContent = '(' + e.data + ')';
      if (card) card.classList.remove('running');
      es.close();
      es = null;
      job = null;
      setCancel(false);
      refreshJobs();
      if (wasJob) {
        toast(label + ' — ' + (e.data === 'exit 0' ? 'done' : e.data), e.data === 'exit 0');
        // A job may have changed what runs, the history or GitHub: read them again.
        refreshLive(true);
      } else if (e.data === 'refused') {
        toast(label + ' — refused, see the output', false);
      }
    });
    es.onerror = () => {
      document.getElementById('termState').textContent = job
        ? '(connection lost — the command is still running; re-attach below)'
        : '(connection closed)';
      if (card) card.classList.remove('running');
      if (es) { es.close(); es = null; }
      job = null;
      setCancel(false);
    };
  }

  function attach(id, label) {
    follow('/jobs/' + encodeURIComponent(id) + '/stream', label, null);
  }

  function cancelJob() {
    if (!job) return;
    const message = 'Cancel "' + job.label + '" part-way through?\n\n'
      + 'A host can be left half-rolled. Afterwards run Status, then deploy again to finish.';
    if (!confirm(message)) return;
    document.getElementById('termState').textContent = '(cancelling…)';
    swap('/jobs/' + encodeURIComponent(job.id) + '/cancel', '#jobBar', { method: 'POST' });
  }

  // ---- actions -----------------------------------------------------------------
  // A button runs one whitelisted action. If it takes a value (data-params), the
  // value comes from the input of that name in the nearest [data-param-scope] —
  // a group's tag field, or a history row's hidden input. The server checks it
  // again; this only saves a round trip for an empty field.

  /** The values a button needs: { values } — or { missing: input } when one is empty. */
  function collectParams(el) {
    const names = (el.dataset.params || '').split(' ').filter(Boolean);
    const scope = el.closest('[data-param-scope]');
    const values = {};
    for (const name of names) {
      const input = scope ? scope.querySelector('[name="' + name + '"]') : null;
      const value = input ? input.value.trim() : '';
      if (!value) return { missing: input, name };
      values[name] = value;
    }
    return { values };
  }

  /**
   * The question a host-changing click asks. It names the environment, where the
   * action acts, the hosts and the tag: with staging and production in one
   * picker, "Deploy — Continue?" is how the wrong one gets deployed.
   */
  function confirmText(label, where, values) {
    const d = document.body.dataset;
    let text = label + ' — ' + ENV.toUpperCase() + '\n\n'
      + 'Environment:  ' + ENV + '\n'
      + 'Acts on:      ' + (where || '—') + '\n'
      + 'Hosts:        ' + (d.hosts || '—') + '\n';
    if (values.tag) text += 'Tag:          ' + values.tag + '\n';
    return text + '\nThis changes a running deployment. Continue?';
  }

  function runAction(el) {
    const got = collectParams(el);
    if (got.missing !== undefined) {
      if (got.missing) {
        got.missing.classList.add('invalid');
        got.missing.focus();
      }
      toast('Enter a ' + got.name + ' first — or pick one from Recent tags', false);
      return;
    }
    const label = [el.dataset.label, ...Object.values(got.values)].join(' ');
    if (el.dataset.danger === '1' && !confirm(confirmText(el.dataset.label, el.dataset.where, got.values))) return;
    let url = el.dataset.url;
    for (const [name, value] of Object.entries(got.values)) {
      url += '&' + encodeURIComponent(name) + '=' + encodeURIComponent(value);
    }
    follow(url, label, el);
  }

  // ---- tabs --------------------------------------------------------------------
  // Real tabs (role="tab"): arrow keys move between them, and the open one is in
  // the URL hash, so a reload stays where you were. Without a hash the page opens
  // where the server's landing() put it.

  function tabs() {
    return Array.from(document.querySelectorAll('[role="tab"]'));
  }

  function switchTab(name, { focus = false } = {}) {
    const tab = document.getElementById('tab-' + name);
    if (!tab) return;
    tabs().forEach((t) => {
      const on = t === tab;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.tabIndex = on ? 0 : -1;
    });
    document.querySelectorAll('[role="tabpanel"]').forEach((p) => p.classList.toggle('active', p.id === 'pane-' + name));
    if (focus) tab.focus();
    history.replaceState(null, '', '#' + name);
    loadLive(document.getElementById('pane-' + name));
  }

  function onTabKey(event) {
    const list = tabs();
    const index = list.indexOf(event.target);
    if (index < 0) return;
    const moves = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: list.length - 1 };
    if (!(event.key in moves)) return;
    event.preventDefault();
    const next = list[(moves[event.key] + list.length) % list.length];
    switchTab(next.dataset.tab, { focus: true });
  }

  /** Open a tab and bring one element into view (a Configure section, a flow). */
  function goto(tab, anchor) {
    switchTab(tab);
    const el = anchor ? document.getElementById(anchor) : null;
    if (!el) return;
    if (el.tagName === 'DETAILS') el.open = true;
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 1400);
  }

  // ---- live facts ----------------------------------------------------------------
  // Elements marked data-live="<part>" are filled from /live/<part>: what the hosts
  // run, the history, the CI checklist. The server caches each fact briefly, so a
  // part is fetched when it first becomes visible and again after every job.

  function liveUrl(part, fresh) {
    return '/live/' + part + '?env=' + encodeURIComponent(ENV) + (fresh ? '&fresh=1' : '');
  }

  /** Fill the live parts in root (root included) that have not been loaded yet. */
  function loadLive(root) {
    if (!root) return;
    const parts = Array.from(root.querySelectorAll('[data-live]'));
    if (root.matches('[data-live]')) parts.push(root);
    parts.forEach((el) => {
      if (el.dataset.loaded) return;
      el.dataset.loaded = '1';
      swap(liveUrl(el.dataset.live, false), el);
    });
  }

  /** Re-read every part already on screen; fresh = only a read made after now will do. */
  function refreshLive(fresh) {
    document.querySelectorAll('[data-live][data-loaded]').forEach((el) => {
      swap(liveUrl(el.dataset.live, fresh), el);
    });
  }

  // ---- config form -----------------------------------------------------------------

  async function saveConfig(form) {
    const result = document.getElementById('saveResult');
    result.innerHTML = '<span class="hint">saving…</span>';
    await swap('/config?env=' + encodeURIComponent(ENV), result, { method: 'POST', body: new FormData(form) });
    // Keep what confirmText names in step with what was just saved.
    const saved = result.querySelector('[data-hosts]');
    if (saved) document.body.dataset.hosts = saved.dataset.hosts;
    swap('/config/problems?env=' + encodeURIComponent(ENV), '#problems');
  }

  // ---- hosts widget ----------------------------------------------------------------

  function addHost() {
    const rows = document.getElementById('hostRows');
    const index = rows.querySelectorAll('.host-row').length;
    const div = document.createElement('div');
    div.className = 'host-row';

    const input = document.createElement('input');
    input.type = 'text';
    input.name = 'host_ip';
    input.placeholder = '10.0.0.10';
    input.className = 'mono';
    input.setAttribute('aria-label', 'Host address');

    const pri = document.createElement('label');
    pri.className = 'pri';
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'primary_index';
    radio.value = String(index);
    pri.append(radio, document.createTextNode(' primary'));

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'iconbtn';
    del.title = 'Remove';
    del.setAttribute('aria-label', 'Remove host');
    del.textContent = '✕';
    del.dataset.act = 'remove-host';

    div.append(input, pri, del);
    rows.appendChild(div);
  }

  /** Renumber primary_index radios so they still match row order after a removal. */
  function renumberHosts() {
    document.querySelectorAll('#hostRows .host-row').forEach((row, i) => {
      const radio = row.querySelector('input[type=radio]');
      if (radio) radio.value = String(i);
    });
  }

  // ---- tags ------------------------------------------------------------------------

  /** A tag card fills the tag input of its own group — never another group's. */
  function pickTag(card) {
    const scope = card.closest('[data-param-scope]');
    const input = scope ? scope.querySelector('input[name="tag"]') : null;
    if (input) {
      input.value = card.dataset.tag;
      input.classList.remove('invalid');
      input.classList.add('picked');
      setTimeout(() => input.classList.remove('picked'), 1200);
    }
    if (scope) {
      scope.querySelectorAll('.tag-card').forEach((c) => c.classList.toggle('tag-active', c === card));
    }
  }

  // ---- copy ------------------------------------------------------------------------

  function copyText(button) {
    const source = document.querySelector(button.dataset.copy);
    if (!source) return;
    navigator.clipboard.writeText(source.textContent).then(
      () => {
        button.textContent = 'copied';
        button.classList.add('copied');
        setTimeout(() => { button.textContent = 'copy'; button.classList.remove('copied'); }, 1400);
      },
      () => { button.textContent = 'select it'; },
    );
  }

  // ---- toasts ----------------------------------------------------------------------
  // A job can finish while you are on another tab; the toast says so, and how.

  function toast(text, ok) {
    const box = document.getElementById('toasts');
    if (!box) return;
    const item = document.createElement('div');
    item.className = 'toast ' + (ok ? 'toast-ok' : 'toast-bad');
    item.textContent = text;
    box.appendChild(item);
    setTimeout(() => item.remove(), 6000);
  }

  // ---- resizable split -------------------------------------------------------------
  // Drag the gutter to set the output pane's width; double-click resets it. The
  // width is stored as a percentage so it still makes sense after the window is
  // resized, and kept in localStorage so it survives a reload.

  function storage(action, key, value) {
    // Private windows and locked-down browsers throw on localStorage; the split
    // then simply is not remembered.
    try {
      if (action === 'get') return localStorage.getItem(key);
      if (action === 'set') localStorage.setItem(key, value);
      if (action === 'remove') localStorage.removeItem(key);
    } catch (err) { /* not remembered */ }
    return null;
  }

  function initGutter() {
    const DEFAULT = '38%';
    const KEY = 'deployctl.termWidth';
    const gutter = document.getElementById('gutter');
    const shell = document.getElementById('shell');
    const root = document.documentElement;
    if (!gutter || !shell) return;

    const saved = storage('get', KEY);
    if (saved) root.style.setProperty('--term-w', saved);

    let dragging = false;

    function apply(clientX) {
      const rect = shell.getBoundingClientRect();
      // The pane is anchored to the right edge, so its width is the distance
      // from the pointer to that edge.
      const width = Math.max(260, Math.min(rect.width * 0.85, rect.right - clientX));
      root.style.setProperty('--term-w', ((width / rect.width) * 100).toFixed(1) + '%');
    }

    gutter.addEventListener('mousedown', (e) => {
      dragging = true;
      gutter.classList.add('dragging');
      // Without this the drag selects text across the page and the cursor
      // flickers whenever it leaves the 5px handle.
      document.body.classList.add('resizing');
      e.preventDefault();
    });

    window.addEventListener('mousemove', (e) => { if (dragging) apply(e.clientX); });

    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      gutter.classList.remove('dragging');
      document.body.classList.remove('resizing');
      storage('set', KEY, root.style.getPropertyValue('--term-w') || DEFAULT);
    });

    gutter.addEventListener('dblclick', () => {
      root.style.setProperty('--term-w', DEFAULT);
      storage('remove', KEY);
    });
  }

  // ---- wiring ----------------------------------------------------------------------
  // One delegated listener. Every interactive element declares data-act plus
  // whatever it needs; nothing carries an inline handler.

  function onClick(event) {
    // A click whose target IS a <dialog> landed on the backdrop: the content
    // lives in children, so the dialog itself is only hit when the pointer
    // missed it. Closing there is what a modal is expected to do.
    if (event.target.tagName === 'DIALOG') {
      event.target.close();
      return;
    }

    const el = event.target.closest('[data-act]');
    if (!el) return;

    switch (el.dataset.act) {
      case 'tab':
        switchTab(el.dataset.tab);
        break;
      case 'goto':
        goto(el.dataset.tab, el.dataset.anchor || '');
        break;
      case 'run':
        runAction(el);
        break;
      case 'swap':
        event.preventDefault();
        swap(el.dataset.url, el.dataset.target, { method: el.dataset.method || 'GET' });
        break;
      case 'refresh':
        refreshLive(true);
        break;
      case 'preview':
        window.open(withToken('/config/preview?env=' + encodeURIComponent(ENV)), '_blank');
        break;
      case 'term-toggle':
        document.getElementById('shell').classList.toggle('no-term');
        break;
      case 'term-stop':
        detach();
        break;
      case 'attach':
        attach(el.dataset.job, el.dataset.label);
        break;
      case 'cancel':
        cancelJob();
        break;
      case 'term-clear':
        document.getElementById('termOut').textContent = '';
        break;
      // "Where do I get this value?" — a per-field modal, rendered by the
      // server next to its input. Esc closes it natively; the backdrop is
      // handled at the top of this function.
      case 'guide': {
        const dialog = document.getElementById(el.dataset.guide);
        if (dialog && typeof dialog.showModal === 'function') dialog.showModal();
        break;
      }
      case 'guide-close': {
        const open = el.closest('dialog');
        if (open) open.close();
        break;
      }
      case 'add-host':
        addHost();
        break;
      case 'remove-host':
        el.parentElement.remove();
        renumberHosts();
        break;
      // Tag cards arrive later, as a server-rendered fragment. They are reached
      // by the same delegation as everything else — declaring data-act is what
      // makes that work, so the fragment must set it too (see routes.image_tags).
      case 'tag':
        pickTag(el);
        break;
      case 'copy':
        copyText(el);
        break;
      default:
        break;
    }
  }

  function init() {
    document.addEventListener('click', onClick);
    document.addEventListener('input', (e) => e.target.classList.remove('invalid'));
    const tablist = document.querySelector('[role="tablist"]');
    if (tablist) tablist.addEventListener('keydown', onTabKey);

    const form = document.getElementById('cfgForm');
    if (form) {
      form.addEventListener('submit', (e) => {
        e.preventDefault();
        saveConfig(form);
      });
    }

    const picker = document.querySelector('.envpick');
    if (picker) {
      picker.addEventListener('change', () => {
        location.href = '/?env=' + encodeURIComponent(picker.value);
      });
    }

    initGutter();

    // Back/forward, or a hash typed into the address bar, opens that tab.
    window.addEventListener('hashchange', () => {
      if (document.getElementById('tab-' + location.hash.slice(1))) switchTab(location.hash.slice(1));
    });

    // The top bar and the stepper, then the open tab — from the hash if there is one.
    loadLive(document.querySelector('.topbar'));
    loadLive(document.querySelector('.journey'));
    const wanted = location.hash.slice(1);
    const open = document.querySelector('[role="tab"].active');
    switchTab(document.getElementById('tab-' + wanted) ? wanted : (open ? open.dataset.tab : 'operate'));
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
