// app.js — all JS for Agentic Scrum Office

// ── Sprites ────────────────────────────────────────────────────
document.getElementById('spr-pm').src  = '/characters/michael.png';
document.getElementById('spr-dev').src = '/characters/jim.png';
document.getElementById('spr-qa').src  = '/characters/dwight.png';

// ── State ──────────────────────────────────────────────────────
let lastTs = 0, prevAttempt = 1;
const AGENTS = ['pm','dev','qa'];
const screenColors = { pm:'#a6e3a1', dev:'#89b4fa', qa:'#fab387' };
const bubColors    = { pm:'#0F6E56', dev:'#185FA5', qa:'#854F0B' };

// ── Tab switching ───────────────────────────────────────────────
function switchTab(name) {
  ['ticket','diff','stats'].forEach(t => {
    document.getElementById('tab-'+t).classList.toggle('active', t === name);
    document.getElementById('ptab-'+t).classList.toggle('active', t === name);
  });
}

// ── Office helpers ──────────────────────────────────────────────
function showBubble(agent, text) {
  AGENTS.forEach(a => document.getElementById('bub-'+a).classList.remove('show'));
  const b = document.getElementById('bub-'+agent);
  // Capitalise first letter, lowercase the rest — more natural than ALL CAPS
  b.textContent = text.charAt(0).toUpperCase() + text.slice(1).toLowerCase();
  b.style.borderColor = bubColors[agent];
  b.classList.add('show');
}

function bounce(agent) {
  const el = document.getElementById('spr-'+agent);
  el.classList.remove('bounce'); void el.offsetWidth; el.classList.add('bounce');
  el.addEventListener('animationend', () => el.classList.remove('bounce'), { once:true });
}

function activateScreen(agent) {
  AGENTS.forEach(a => [1,2,3].forEach(n => {
    const e = document.getElementById(a+'-l'+n);
    if (e) e.style.background = '#313244';
  }));
  [1,2,3].forEach((n,i) => {
    const e = document.getElementById(agent+'-l'+n);
    if (e) setTimeout(() => e.style.background = screenColors[agent], i*100);
  });
}

function flashPod(agent, type) {
  const el = document.getElementById('pod-'+agent);
  el.classList.remove('flash-pass','flash-fail'); void el.offsetWidth; el.classList.add('flash-'+type);
  el.addEventListener('animationend', () => el.classList.remove('flash-pass','flash-fail'), { once:true });
}

function qaWalksToDev() {
  const el = document.getElementById('spr-qa');
  el.classList.remove('walking'); void el.offsetWidth; el.classList.add('walking');
  el.addEventListener('animationend', () => el.classList.remove('walking'), { once:true });
}

// ── Jira board ──────────────────────────────────────────────────
const COLS = ['analysis','dev','qa','failed','done'];

function moveTicket(colId, title) {
  COLS.forEach(c => {
    document.getElementById('ticket-'+c).classList.remove('visible','active-card','arriving');
    document.getElementById('jcol-'+c).classList.remove('col-active');
    document.getElementById('cnt-'+c).textContent = '0';
  });
  const ticket  = document.getElementById('ticket-'+colId);
  const titleEl = document.getElementById('t-'+colId+'-title');
  if (titleEl && title) titleEl.textContent = title;
  ticket.classList.add('visible','active-card','arriving');
  document.getElementById('jcol-'+colId).classList.add('col-active');
  document.getElementById('cnt-'+colId).textContent = '1';
}

function boardFromState(agent, status, verdict) {
  if (agent === 'pm')  return moveTicket('analysis', status);
  if (agent === 'dev') return moveTicket('dev', status);
  if (agent === 'qa' && verdict === 'fail') return moveTicket('failed', 'Needs rework');
  if (agent === 'qa')  return moveTicket('qa', status);
  if (agent === 'done' && verdict === 'pass') return moveTicket('done', 'Deployed to prod ✓');
  if (agent === 'done' && verdict === 'fail') return moveTicket('failed', 'Gave up');
}

// ── Ticket tab ──────────────────────────────────────────────────
function updateTicketTab(ticketText) {
  if (!ticketText || ticketText.length < 20) return;
  document.getElementById('ticket-empty').style.display = 'none';
  document.getElementById('ticket-content').style.display = 'block';

  const summaryMatch = ticketText.match(/Summary:\s*(.+)/);
  if (summaryMatch) document.getElementById('tp-summary').textContent = summaryMatch[1].trim();

  const bugsEl = document.getElementById('tp-bugs');
  bugsEl.innerHTML = '';
  const bugMatches = [...ticketText.matchAll(/\d+\.\s*Function:\s*(.+)\n\s*Problem:\s*(.+)\n\s*Expected:\s*(.+)/g)];
  bugMatches.forEach((m, i) => {
    const div = document.createElement('div');
    div.style.cssText = 'margin-bottom:10px;padding:10px;background:#1a1a2e;border-left:3px solid #f38ba8;border-radius:4px;';
    div.innerHTML = `
      <div style="font-size:11px;font-weight:600;color:#f38ba8;margin-bottom:4px;">Bug ${i+1} — ${m[1].trim()}</div>
      <div style="font-size:11px;color:#cdd6f4;margin-bottom:3px;line-height:1.5;">${m[2].trim()}</div>
      <div style="font-size:10px;color:#a6e3a1;line-height:1.5;">Expected: ${m[3].trim()}</div>`;
    bugsEl.appendChild(div);
  });
  document.getElementById('st-bugs').textContent = bugMatches.length || '—';
}

// ── Diff tab ────────────────────────────────────────────────────
function updateDiffTab(original, patched) {
  if (!original || !patched) return;
  const origLines  = original.split('\n');
  const patchLines = patched.split('\n');
  const container  = document.getElementById('diff-lines');
  container.innerHTML = '';
  let adds = 0, rems = 0, lineNum = 1;

  const maxLen = Math.max(origLines.length, patchLines.length);
  for (let i = 0; i < maxLen; i++) {
    const o = origLines[i];
    const p = patchLines[i];
    if (o === p) {
      if (o && o.trim()) addDiffLine(container, 'neu', lineNum, o);
      lineNum++;
    } else {
      if (o !== undefined) { addDiffLine(container, 'rem', lineNum, '− ' + o); rems++; }
      if (p !== undefined) { addDiffLine(container, 'add', lineNum, '+ ' + p); adds++; }
      lineNum++;
    }
  }
  document.getElementById('diff-badge').textContent = 'Patch ready';
  document.getElementById('diff-badge').style.color = '#a6e3a1';
  document.getElementById('diff-stats').style.display = 'flex';
  document.getElementById('diff-adds').textContent = `+${adds} added`;
  document.getElementById('diff-rems').textContent = `−${rems} removed`;
}

function addDiffLine(container, type, num, text) {
  const div  = document.createElement('div'); div.className = 'diff-line';
  const bar  = document.createElement('div'); bar.className = 'dl-bar ' + type;
  const ln   = document.createElement('div'); ln.className  = 'dl-num'; ln.textContent = num;
  const code = document.createElement('div'); code.className = 'dl-code ' + type; code.textContent = text || '';
  div.appendChild(bar); div.appendChild(ln); div.appendChild(code);
  container.appendChild(div);
}

// ── Stats tab ────────────────────────────────────────────────────
let agentStartTimes = {}, agentDurations = {};
let cycleCount = parseInt(localStorage.getItem('cycleCount') || '0');
document.getElementById('st-cycles').textContent = cycleCount;

function tickStats(agent, verdict, attempt) {
  document.getElementById('st-attempts').textContent = attempt + ' / 3';

  if (verdict === 'pass') {
    document.getElementById('st-verdict').textContent = 'Pass';
    document.getElementById('st-verdict').className = 'stat-val pass';
    document.getElementById('st-status').textContent = 'Done';
    cycleCount++;
    localStorage.setItem('cycleCount', cycleCount);
    document.getElementById('st-cycles').textContent = cycleCount;
  } else if (verdict === 'fail') {
    document.getElementById('st-verdict').textContent = 'Fail';
    document.getElementById('st-verdict').className = 'stat-val fail';
  }

  if (AGENTS.includes(agent)) {
    document.getElementById('st-status').textContent = agent.toUpperCase() + ' working';
    const now = Date.now();
    if (!agentStartTimes[agent]) agentStartTimes[agent] = now;
    const prev = { pm: null, dev: 'pm', qa: 'dev' }[agent];
    if (prev && agentStartTimes[prev] && !agentDurations[prev]) {
      agentDurations[prev] = Math.round((now - agentStartTimes[prev]) / 1000);
      updateTimingBars();
    }
  }
  if (agent === 'done' && agentStartTimes['qa'] && !agentDurations['qa']) {
    agentDurations['qa'] = Math.round((Date.now() - agentStartTimes['qa']) / 1000);
    updateTimingBars();
  }
}

function updateTimingBars() {
  const times = { pm: agentDurations.pm||0, dev: agentDurations.dev||0, qa: agentDurations.qa||0 };
  const maxT  = Math.max(...Object.values(times), 1);
  ['pm','dev','qa'].forEach(a => {
    const t = times[a];
    document.getElementById('bar-'+a).style.width = (t/maxT*100)+'%';
    document.getElementById('time-'+a).textContent = t ? t+'s' : '—';
  });
}

// ── File fetch helper ────────────────────────────────────────────
async function fetchFile(path) {
  try {
    const r = await fetch(path + '?t=' + Date.now());
    return r.ok ? r.text() : null;
  } catch { return null; }
}

let ticketLoaded = false, diffLoaded = false;

// ── Main state handler ───────────────────────────────────────────
function applyState(s) {
  if (s.ts === lastTs) return;
  lastTs = s.ts;
  const { agent, status, message, attempt, verdict } = s;

  // Status bar
  document.getElementById('status-txt').textContent = `[${agent.toUpperCase()}] ${status}`;
  document.getElementById('apill').textContent = attempt > 1 ? `Attempt ${attempt}` : '';

  // LED
  const led = document.getElementById('led');
  led.className = 'led';
  if (agent === 'done') led.classList.add(verdict === 'pass' ? 'pass' : 'fail');
  else if (AGENTS.includes(agent)) led.classList.add('on');

  // Pods
  AGENTS.forEach(a => document.getElementById('pod-'+a).classList.remove('active'));
  if (AGENTS.includes(agent)) {
    document.getElementById('pod-'+agent).classList.add('active');
    showBubble(agent, status);
    bounce(agent);
    activateScreen(agent);
    if (agent === 'qa' && verdict) flashPod('qa', verdict);
    if (agent === 'qa' && attempt > prevAttempt) setTimeout(qaWalksToDev, 500);
    prevAttempt = attempt;

    // Auto-switch tabs
    if (agent === 'pm')  switchTab('ticket');
    if (agent === 'dev') switchTab('diff');
    if (agent === 'qa')  switchTab('ticket');
  } else {
    AGENTS.forEach(a => document.getElementById('bub-'+a).classList.remove('show'));
    if (agent === 'done') switchTab('stats');
  }

  // Load files into panel
  if (agent === 'dev' && !ticketLoaded) {
    ticketLoaded = true;
    fetchFile('/workspace/ticket.txt').then(t => { if (t) updateTicketTab(t); });
  }
  if ((agent === 'qa' || agent === 'done') && !diffLoaded) {
    diffLoaded = true;
    Promise.all([
      fetchFile('/workspace/buggy_script.py'),
      fetchFile('/workspace/patched_script.py')
    ]).then(([orig, patch]) => { if (orig && patch) updateDiffTab(orig, patch); });
  }
  if (agent === 'qa' || agent === 'done') {
    fetchFile('/workspace/qa_review.txt').then(r => {
      if (!r) return;
      document.getElementById('qa-section').style.display = 'block';
      const passed = r.includes('Verdict: PASS');
      document.getElementById('tp-verdict').textContent = passed ? '✓ Pass' : '✗ Fail';
      document.getElementById('tp-verdict').style.color  = passed ? '#a6e3a1' : '#f38ba8';
    });
  }

  boardFromState(agent, status, verdict);
  tickStats(agent, verdict, attempt);
}

// ── Poll state.json ──────────────────────────────────────────────
async function poll() {
  try {
    const r = await fetch('/workspace/state.json?t=' + Date.now());
    if (r.ok) applyState(await r.json());
  } catch(_) {
    // Server not ready yet — retry silently
  }
  setTimeout(poll, 1000);
}

// ── Drop zone ────────────────────────────────────────────────────
function onDragOver(e) {
  e.preventDefault();
  document.getElementById('dropzone').classList.add('dragover');
}
function onDragLeave() {
  document.getElementById('dropzone').classList.remove('dragover');
}
function onDrop(e) {
  e.preventDefault();
  document.getElementById('dropzone').classList.remove('dragover');
  const file = e.dataTransfer.files[0];
  if (file) handleFile(file);
}

document.getElementById('file-input').addEventListener('change', function() {
  if (this.files[0]) handleFile(this.files[0]);
});

function handleFile(file) {
  if (!file.name.endsWith('.py')) {
    document.getElementById('drop-label').textContent = 'Python files only (.py)';
    return;
  }
  const formData = new FormData();
  formData.append('file', file);
  document.getElementById('drop-label').textContent = 'Uploading...';

  fetch('/upload', { method: 'POST', body: formData })
    .then(r => r.json())
    .then(data => {
      if (data.ok) {
        document.getElementById('dropzone').classList.add('has-file');
        document.getElementById('drop-icon').textContent = '✓';
        document.getElementById('drop-label').textContent = file.name;
        document.getElementById('drop-sub').textContent = `${data.lines} lines — ready to run`;
        const btn = document.getElementById('run-btn');
        btn.style.opacity = '1';
        btn.style.pointerEvents = 'auto';
      } else {
        document.getElementById('drop-label').textContent = 'Error: ' + data.error;
      }
    })
    .catch(() => {
      document.getElementById('drop-label').textContent = 'Upload failed — is server.py running?';
    });
}

function runPipeline() {
  const btn = document.getElementById('run-btn');
  btn.textContent = 'Running...';
  btn.style.background = '#003d99';
  btn.style.pointerEvents = 'none';

  // Reset panel for new run
  ticketLoaded = false; diffLoaded = false;
  agentStartTimes = {}; agentDurations = {};
  document.getElementById('ticket-empty').style.display = 'block';
  document.getElementById('ticket-content').style.display = 'none';
  document.getElementById('diff-lines').innerHTML = '';
  document.getElementById('diff-badge').textContent = 'Waiting for Dev...';
  document.getElementById('diff-badge').style.color = '#45475a';
  document.getElementById('diff-stats').style.display = 'none';
  document.getElementById('qa-section').style.display = 'none';
  switchTab('ticket');

  fetch('/run', { method: 'POST' })
    .then(r => r.json())
    .then(data => {
      if (!data.ok) {
        btn.textContent = 'Run ▸';
        btn.style.background = '#0052CC';
        btn.style.pointerEvents = 'auto';
        alert(data.error);
      }
      pollRunStatus();
    })
    .catch(() => {
      btn.textContent = 'Run ▸';
      btn.style.pointerEvents = 'auto';
    });
}

function pollRunStatus() {
  fetch('/status')
    .then(r => r.json())
    .then(data => {
      if (data.running) {
        setTimeout(pollRunStatus, 2000);
      } else {
        const btn = document.getElementById('run-btn');
        btn.textContent = 'Run again ▸';
        btn.style.background = '#0052CC';
        btn.style.pointerEvents = 'auto';
      }
    });
}

// Start polling
poll();