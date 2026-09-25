/* deployctl control panel — all of the panel's behaviour.
 *
 * No framework and no CDN. The panel replaced htmx with the ~40 lines under
 * "fetch and swap" below for three reasons, in order of importance:
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
 * a button means adding a data-act, not a <script> block.
 */
'use strict';

(function () {
  const TOKEN = document.body.dataset.token || '';
  const ENV = document.body.dataset.env || '';

  // ---- request helpers -----------------------------------------------------

  /** Append the session token to a same-origin path. */
  function withToken(url) {
    return url + (url.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(TOKEN);
  }

  /** Fetch a fragment of HTML and put it into a target element. */
  async function swap(url, targetSel, { method = 'GET', body = null, indicator = null } = {}) {
    const target = document.querySelector(targetSel);
    const spinner = indicator ? document.querySelector(indicator) : null;
    if (spinner) spinner.style.display = 'inline';
    try {
      const res = await fetch(withToken(url), {
        method,
        body,
        headers: { 'X-Deployctl-Token': TOKEN },
      });
      const text = await res.text();
      if (target) {
        target.innerHTML = res.ok
          ? text
          : '<span class="hint err">' + escapeHtml(text.slice(0, 400)) + '</span>';
      }
    } catch (err) {
      if (target) target.innerHTML = '<span class="hint err">' + escapeHtml(String(err)) + '</span>';
    } finally {
      if (spinner) spinner.style.display = '';
    }
  }

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  // ---- output pane ---------------------------------------------------------
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
    document.querySelectorAll('.acard.running, .tut-run.running, .fstep.running')
      .forEach((c) => c.classList.remove('running'));
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
      document.getElementById('termState').textContent = '(' + e.data + ')';
      if (card) card.classList.remove('running');
      es.close();
      es = null;
      job = null;
      setCancel(false);
      refreshJobs();
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

  /**
   * The question a host-changing click asks. It names the environment, its hosts
   * and the image tag that will ship: with staging and production in one picker,
   * "Update — Continue?" is how the wrong one gets deployed, and a tag picked but
   * never saved is how the wrong version does.
   */
  function confirmText(label) {
    const d = document.body.dataset;
    return label + ' — ' + ENV.toUpperCase() + '\n\n'
      + 'Environment:  ' + ENV + '\n'
      + 'Hosts:        ' + (d.hosts || '—') + '\n'
      + 'Image:        ' + (d.image || '—') + '   (the saved tag)\n\n'
      + 'This changes a running deployment. Continue?';
  }

  /** Run a whitelisted action and stream its output into the pane. */
  function runAction(url, danger, label, card) {
    if (danger && !confirm(confirmText(label))) return;
    follow(url, label, card);
  }

  function attach(id, label) {
    follow('/jobs/' + encodeURIComponent(id) + '/stream', label, null);
  }

  function cancelJob() {
    if (!job) return;
    const message = 'Cancel "' + job.label + '" part-way through?\n\n'
      + 'A host can be left half-rolled. Afterwards run Status, then Update again to finish.';
    if (!confirm(message)) return;
    document.getElementById('termState').textContent = '(cancelling…)';
    swap('/jobs/' + encodeURIComponent(job.id) + '/cancel', '#jobBar', { method: 'POST' });
  }

  // ---- tabs ----------------------------------------------------------------

  function switchTab(name) {
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
    document.querySelectorAll('.pane').forEach((p) => p.classList.toggle('active', p.id === 'pane-' + name));
  }

  /** Jump from a flow's edit step to the section of the form it refers to. */
  function gotoSection(sectionId) {
    switchTab('configure');
    const card = document.getElementById('sec-' + sectionId);
    if (!card) return;
    card.open = true;
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.classList.add('flash');
    setTimeout(() => card.classList.remove('flash'), 1400);
  }

  // ---- hosts widget --------------------------------------------------------

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

    const pri = document.createElement('span');
    pri.className = 'pri';
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'primary_index';
    radio.value = String(index);
    pri.appendChild(radio);
    pri.appendChild(document.createTextNode(' primary'));

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'iconbtn';
    del.title = 'Remove';
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

  // ---- image tags ----------------------------------------------------------

  function pickTag(tag) {
    const input = document.getElementById('imageTagInput');
    if (input) {
      input.value = tag;
      input.style.outline = '2px solid var(--ok)';
      setTimeout(() => (input.style.outline = ''), 1200);
    }
    document.querySelectorAll('.tag-card').forEach((c) =>
      c.classList.toggle('tag-active', c.dataset.tag === tag));
  }

  // ---- config form ---------------------------------------------------------

  async function saveConfig(form) {
    const result = document.getElementById('saveResult');
    result.innerHTML = '<span class="hint">saving…</span>';
    await swap('/config?env=' + encodeURIComponent(ENV), '#saveResult', {
      method: 'POST',
      body: new FormData(form),
    });
    // Keep what confirmText names in step with what was just saved.
    const saved = document.querySelector('#saveResult [data-image]');
    if (saved) {
      document.body.dataset.image = saved.dataset.image;
      document.body.dataset.hosts = saved.dataset.hosts;
      const chip = document.getElementById('chipImage');
      if (chip) chip.textContent = saved.dataset.image;
    }
  }

  // ---- resizable split -----------------------------------------------------
  // Drag the gutter to set the output pane's width; double-click resets it. The
  // width is stored as a percentage so it still makes sense after the window is
  // resized, and kept in localStorage so it survives a reload.

  function initGutter() {
    const DEFAULT = '38%';
    const KEY = 'deployctl.termWidth';
    const gutter = document.getElementById('gutter');
    const shell = document.getElementById('shell');
    const root = document.documentElement;
    if (!gutter || !shell) return;

    const saved = localStorage.getItem(KEY);
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
      // Without these the drag selects text across the page and the cursor
      // flickers whenever it leaves the 5px handle.
      document.body.style.userSelect = 'none';
      document.body.style.cursor = 'col-resize';
      e.preventDefault();
    });

    window.addEventListener('mousemove', (e) => { if (dragging) apply(e.clientX); });

    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      gutter.classList.remove('dragging');
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
      localStorage.setItem(KEY, root.style.getPropertyValue('--term-w') || DEFAULT);
    });

    gutter.addEventListener('dblclick', () => {
      root.style.setProperty('--term-w', DEFAULT);
      localStorage.removeItem(KEY);
    });
  }

  // ---- tutorial ------------------------------------------------------------

  /** Which deployment shape the walkthrough shows; remembered across reloads. */
  function tutShape(shape) {
    const wrap = document.getElementById('tutWrap');
    if (!wrap) return;
    wrap.dataset.shape = shape;
    localStorage.setItem('deployctl.tutShape', shape);
  }

  function initTutorial() {
    const wrap = document.getElementById('tutWrap');
    if (!wrap) return;

    const savedShape = localStorage.getItem('deployctl.tutShape');
    if (savedShape === 'single' || savedShape === 'cluster') wrap.dataset.shape = savedShape;

    // A copy button on every command block. The text is captured before the
    // button is inserted, so the button's own label can never end up in the
    // clipboard.
    document.querySelectorAll('.tut-code').forEach((block) => {
      const text = block.textContent;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'copy-btn';
      btn.textContent = 'copy';
      btn.addEventListener('click', () => {
        navigator.clipboard.writeText(text).then(
          () => {
            btn.textContent = 'copied';
            btn.classList.add('copied');
            setTimeout(() => { btn.textContent = 'copy'; btn.classList.remove('copied'); }, 1400);
          },
          () => { btn.textContent = 'select it'; },
        );
      });
      block.appendChild(btn);
    });

    // Highlight the section currently in view. The scroll container is .panes,
    // not the window, so the listener has to go there.
    const panes = document.querySelector('.panes');
    const links = Array.from(document.querySelectorAll('.tut-nav .tn'));
    const sections = links.map((a) => document.querySelector(a.getAttribute('href'))).filter(Boolean);
    if (!panes || !sections.length) return;

    panes.addEventListener('scroll', () => {
      const pane = document.getElementById('pane-tutorial');
      if (!pane || !pane.classList.contains('active')) return;
      // The last section whose top has passed the sticky nav is the one being read.
      let index = 0;
      sections.forEach((section, i) => {
        if (section.getBoundingClientRect().top <= 120) index = i;
      });
      links.forEach((a, i) => a.classList.toggle('active', i === index));
    }, { passive: true });
  }

  // ---- wiring --------------------------------------------------------------
  // One delegated listener. Every interactive element declares data-act plus
  // whatever it needs; nothing carries an inline handler.

  function onClick(event) {
    // A click whose target IS a <dialog> landed on the backdrop: the content
    // lives in children, so the dialog itself is only hit when the pointer
    // missed it. Closing there is what a modal is expected to do, and handling
    // it here keeps the promise of one listener for the whole page.
    if (event.target.tagName === 'DIALOG') {
      event.target.close();
      return;
    }

    const el = event.target.closest('[data-act]');
    if (!el) return;
    const act = el.dataset.act;

    switch (act) {
      case 'tab':
        switchTab(el.dataset.tab);
        break;
      case 'goto-section':
        gotoSection(el.dataset.section);
        break;
      case 'run':
        runAction(el.dataset.url, el.dataset.danger === '1', el.dataset.label, el);
        break;
      case 'swap':
        event.preventDefault();
        swap(el.dataset.url, el.dataset.target, {
          method: el.dataset.method || 'GET',
          indicator: el.dataset.indicator || null,
        });
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
      case 'tut-shape':
        tutShape(el.dataset.shape);
        break;
      // Tag cards arrive later, as a server-rendered fragment. They are reached
      // by the same delegation as everything else — declaring data-act is what
      // makes that work, so the fragment must set it too (see routes.image_tags).
      case 'tag':
        pickTag(el.dataset.tag);
        break;
      default:
        break;
    }
  }

  function init() {
    document.addEventListener('click', onClick);

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
    initTutorial();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
