/* ═══════════════════════════════════════════════════════════
   AGENT TOWN — pixel office client
   ═══════════════════════════════════════════════════════════ */
(function () {
'use strict';

/* ── Tunables ───────────────────────────────────────────── */
const TILE = 32;          // world tile size
const COLS = 20;
const ROWS = 13;
const WALK_SPEED = 3.1;   // px per frame
const STREAM_MS = 42;     // chat typing speed (paced to roughly track speech)
const MIC_SILENCE_MS = 4000;  // how long to wait on silence before auto-sending
const SPEECH_RATE = 0.88;     // < 1 is slower and clearer than the default
const SENTENCE_PAUSE_MS = 340;  // breath between sentences
const CLAUSE_PAUSE_MS = 150;    // shorter breath after a comma break
const CONVO_SILENCE_MS = 2800;  // hands-free: silence before the turn ends
const MIN_UTTERANCE_MS = 700;   // ignore a stray click or cough as a full turn
const VAD_LEVEL = 14;           // mic level that counts as "still talking"
const BARGE_LEVEL = 38;         // louder bar to interrupt the agent mid-sentence
const BARGE_FRAMES = 6;         // consecutive loud frames before cutting off

/* ── Desk layout (tile coords) ──────────────────────────── */
const DESKS = [
    { key: 'manager',            tx: 2,  ty: 2  },
    { key: 'computer_scientist', tx: 6,  ty: 2  },
    { key: 'trader_bot_1',       tx: 11, ty: 2  },
    { key: 'trader_bot_2',       tx: 16, ty: 2  },
    { key: 'trader_bot_3',       tx: 2,  ty: 7  },
    { key: 'super_trader',       tx: 11, ty: 7  },
    { key: 'trader_bot_4',       tx: 6,  ty: 10 },
    { key: 'risk_manager',       tx: 16, ty: 10 },
];

const PLANTS = [{ tx: 0, ty: 5 }, { tx: 19, ty: 5 }];
const RUG    = { tx: 0, ty: 11, w: 4, h: 2 };
const CARPET = { tx: 7, ty: 5, w: 7, h: 3 };
const SERVERS= [{ tx: 19, ty: 10 }, { tx: 19, ty: 11 }, { tx: 0, ty: 12 }];

/* ── Character palettes ─────────────────────────────────── */
const SKINS = ['#c98f68', '#a9724d', '#e0b18a', '#8a5a3b'];
const SHIRTS = {
    manager:            '#3f6fa8',
    super_trader:       '#c0562f',
    risk_manager:       '#7a4ea8',
    computer_scientist: '#2f8a72',
    trader_bot:         '#4a6fa0',
};

/* ── State ──────────────────────────────────────────────── */
const S = {
    agents: [],
    byKey: {},
    active: null,
    lang: 'en',
    voice: true,
    kill: false,
    streaming: false,
    streamTimer: null,
    speaking: null,   // agent_key currently being voiced
    converse: false,  // hands-free conversation mode
    ws: null,
    wsTimer: null,
    player: { x: 9.5 * TILE, y: 6 * TILE, dir: 'down', moving: false, frame: 0, tick: 0 },
    keys: {},
    near: null,
    t: 0,
};

/* ── DOM ────────────────────────────────────────────────── */
const D = {};
['modeBadge','wsStatus','langToggle','busyMeta','officeCanvas','interactTip','interactName',
 'apName','apRole','apBadge','apAvatar','apTabBody','apTabText','chatLog','chatSys','chatInput',
 'sendBtn','micBtn','voiceBtn','terminateBtn','killBtn','netCanvas','orbCanvas','orbSub',
 'sysProfileTitle','sysProfileDesc','rosterBody','tickers','acctStats','btSymbol','btTf',
 'runBt','btResult','positions','auditList','gestureGrid','gestureTarget','refreshHub',
 'blotterMode','pendCount','pendingList','openCount','blotterOpen','execLog','refreshBlotter',
 'clearBtn','convoBtn',
 'apilotState','apilotToggle','apilotRefresh','apilotDest','brokerPanel','apilotStats',
 'apilotGuards','apilotFeed',
 'bkMode','bkToken','bkAccount','bkRegion','bkEnabled','bkSave','bkHint','bkStyle'
].forEach(id => D[id] = document.getElementById(id));

const ctx  = D.officeCanvas.getContext('2d');
const nctx = D.netCanvas.getContext('2d');
const octx = D.orbCanvas.getContext('2d');
const actx = D.apAvatar.getContext('2d');

/* ═══════════════════════════════════════════════════════════
   BOOT
   ═══════════════════════════════════════════════════════════ */
function init() {
    resize();
    window.addEventListener('resize', resize);
    bindUI();
    initTTS();
    loadAgents();
    connectWS();
    // Header self-heals: re-read status every 20s so the mode badge stays
    // right even if the broker config changed in another tab or via .env.
    setInterval(refreshStatus, 20000);
    requestAnimationFrame(loop);
}

function resize() {
    const stage = D.officeCanvas.parentElement;
    const dpr = window.devicePixelRatio || 1;
    const w = stage.clientWidth, h = stage.clientHeight;
    D.officeCanvas.width  = w * dpr;
    D.officeCanvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.imageSmoothingEnabled = false;
}

function bindUI() {
    D.sendBtn.onclick = send;
    D.chatInput.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); send(); } };
    D.micBtn.onclick = toggleMic;
    D.voiceBtn.onclick = () => {
        S.voice = !S.voice;
        D.voiceBtn.textContent = S.voice ? 'VOICE ON' : 'VOICE OFF';
        D.voiceBtn.classList.toggle('on', S.voice);
        if (!S.voice) stopSpeech();
    };
    D.voiceBtn.classList.add('on');
    D.terminateBtn.onclick = terminate;
    D.clearBtn.onclick = clearHistory;
    D.convoBtn.onclick = toggleConverse;
    D.killBtn.onclick = toggleKill;
    D.langToggle.onchange = e => { S.lang = e.target.value; };

    document.querySelectorAll('.sys-menu-item').forEach(b => b.onclick = () => {
        document.querySelectorAll('.sys-menu-item').forEach(x => x.classList.remove('active'));
        b.classList.add('active');
        showAspect(b.dataset.panel);
    });
    document.querySelectorAll('.ap-tab').forEach(b => b.onclick = () => {
        document.querySelectorAll('.ap-tab').forEach(x => x.classList.remove('active'));
        b.classList.add('active');
        showAspect(b.dataset.aptab);
    });
    document.querySelectorAll('.tab').forEach(b => b.onclick = () => switchTab(b.dataset.tab));

    D.refreshHub.onclick = loadHub;
    D.runBt.onclick = runBacktest;
    D.refreshBlotter.onclick = loadBlotter;
    D.apilotRefresh.onclick = loadAutopilot;
    D.apilotToggle.onclick = toggleAutopilot;
    D.bkSave.onclick = saveBrokerConfig;
    ['bkMode','bkToken','bkAccount','bkRegion','bkEnabled','bkStyle'].forEach(id =>
        D[id].addEventListener('input', () => { window.brokerFormDirty = true; }));

    addEventListener('keydown', e => {
        const k = e.key.toLowerCase();
        if (document.activeElement === D.chatInput) return;
        if (['w','a','s','d','arrowup','arrowdown','arrowleft','arrowright'].includes(k)) {
            S.keys[k] = true; e.preventDefault();
        }
        if (k === 'e' && S.near) { activate(S.near); e.preventDefault(); }
    });
    addEventListener('keyup', e => { S.keys[e.key.toLowerCase()] = false; });
}

/* ═══════════════════════════════════════════════════════════
   AGENTS
   ═══════════════════════════════════════════════════════════ */
async function loadAgents() {
    try {
        const r = await fetch('/agents');
        const j = await r.json();
        if (!j.ok) return;
        S.agents = j.agents;
        S.byKey = {};
        j.agents.forEach(a => {
            S.byKey[a.agent_key] = a;
            a.seed = hash(a.agent_key);
        });
        updateBusy();
        if (curTab === 'agents') renderRoster();
    } catch (e) { console.error('[agents]', e); }
    refreshStatus();
}

/* Pull /status and repaint the header badge + orb.
   Called on load, after a broker/mode change, and on a slow poll, so the
   header reflects the real execution mode without a hard refresh. */
async function refreshStatus() {
    try {
        const j = await (await fetch('/status')).json();
        if (!j.ok) return;
        S.mode = j.trading_mode || 'paper';
        S.exec = j.execution || {};
        // The badge answers one question: can this send a real order?
        const real = !!S.exec.uses_real_money;
        D.modeBadge.textContent = real
            ? (S.mode === 'broker' ? 'BROKER — REAL ORDERS' : 'LIVE MODE')
            : 'LOCAL DEMO';
        D.modeBadge.classList.toggle('live', real);
        S.style = j.style || 'day';
        D.orbSub.textContent = real ? 'LIVE' : 'SIMULATED';
        if (j.kill_switch_active && !S.kill) applyKill(true);
    } catch (_) {}
}

function updateBusy() {
    const busy = S.agents.filter(a => a.status === 'thinking' || a.status === 'online').length;
    D.busyMeta.textContent = `${busy}/${S.agents.length || 8} busy`;
}

function setStatus(key, status) {
    const a = S.byKey[key];
    if (!a) return;
    a.status = status;
    updateBusy();
    if (S.active === key) paintBadge(status);
    if (curTab === 'agents') renderRoster();
}

function paintBadge(status) {
    const cls = status === 'thinking' ? 'busy' : status === 'offline' ? 'off' : 'idle';
    const txt = status === 'thinking' ? 'BUSY' : status === 'offline' ? 'OFFLINE' : 'IDLE';
    D.apBadge.className = 'ap-badge ' + cls;
    D.apBadge.textContent = txt;
}

/* Badge flips to SPEAKING while the active agent's reply is being voiced. */
function syncSpeakingBadge() {
    if (!S.active) return;
    const speaking = S.speaking === S.active;
    if (speaking) {
        if (D.apBadge.textContent !== 'SPEAKING') {
            D.apBadge.className = 'ap-badge busy';
            D.apBadge.textContent = 'SPEAKING';
        }
    } else if (D.apBadge.textContent === 'SPEAKING') {
        paintBadge((S.byKey[S.active] || {}).status || 'idle');
    }
}

/* ═══════════════════════════════════════════════════════════
   ACTIVATION / PANEL
   ═══════════════════════════════════════════════════════════ */
const ASPECTS = {
    manager: {
        memory:  'Tracks every approval and rejection with its reasoning.',
        skills:  'Arbitration, final authorisation, conflict resolution.',
        soul:    'Cautious. Weighs risk above upside on every call.',
        settings:'Model: nemotron-3-nano · approval required for live mode.',
    },
    super_trader: {
        memory:  'Combines demo proposals from multiple trading roles.',
        skills:  'Multi-timeframe reads, liquidity mapping, macro context.',
        soul:    'Opportunistic but never enters without a stop.',
        settings:'Watches EUR/USD · XAU/USD · GBP/USD · NAS100.',
    },
    risk_manager: {
        memory:  'Every drawdown check and veto is written to the audit trail.',
        skills:  'Drawdown caps, position sizing, correlation, news blackout.',
        soul:    'Immovable. Rules outrank conviction, always.',
        settings:'Daily cap $1000 · max lot 5.0 · max 5 concurrent.',
    },
    computer_scientist: {
        memory:  'Session results, win rates and drawdown history.',
        skills:  'Backtests, log analysis, strategy and code review.',
        soul:    'Empirical. Trusts measured outcomes over narrative.',
        settings:'Reads the audit trail · proposes, never executes.',
    },
    trader_bot: {
        memory:  'Recent setups and outcomes for its assigned market.',
        skills:  'Single-market technicals: structure, momentum, levels.',
        soul:    'Narrow focus. One instrument, studied closely.',
        settings:'Reports ideas upward to the Super Trader.',
    },
};

function aspectFor(agent, panel) {
    if (!agent) return 'Select an agent in the town to inspect its memory.';
    const set = ASPECTS[agent.agent_key] || ASPECTS[agent.role] || ASPECTS.trader_bot;
    return set[panel] || '—';
}

let curPanel = 'memory';
function showAspect(panel) {
    curPanel = panel;
    const a = S.active ? S.byKey[S.active] : null;
    D.apTabBody.innerHTML =
        `<span class="ap-label">${panel[0].toUpperCase() + panel.slice(1)}:</span> ` +
        `<span id="apTabText">${esc(aspectFor(a, panel))}</span>`;
    document.querySelectorAll('.ap-tab').forEach(x =>
        x.classList.toggle('active', x.dataset.aptab === panel));
    document.querySelectorAll('.sys-menu-item').forEach(x =>
        x.classList.toggle('active', x.dataset.panel === panel));
}

function activate(key) {
    const a = S.byKey[key];
    if (!a) return;
    S.active = key;

    D.apName.textContent = a.name;
    D.apRole.textContent = a.agent_key;
    paintBadge(a.status || 'idle');
    drawAvatar(a);
    showAspect(curPanel);

    D.sysProfileTitle.textContent = a.name;
    D.sysProfileDesc.textContent = aspectFor(a, 'soul');

    stopSpeech();
    D.chatLog.innerHTML = '';
    D.chatInput.disabled = false;
    D.sendBtn.disabled = false;
    if (curTab === 'town') D.chatInput.focus();
    if (curTab === 'gesture') renderGestures();

    loadHistory(key, a);
}

/* Replay stored conversation so the thread survives reload and restart. */
async function loadHistory(key, a) {
    try {
        const j = await (await fetch(`/agent/history/${encodeURIComponent(key)}?limit=40`)).json();
        if (S.active !== key) return;               // user moved on while fetching
        const msgs = j.messages || [];
        D.chatLog.innerHTML = '';
        if (msgs.length) {
            sysLine(`${a.name} reconnected — ${msgs.length} earlier message(s) restored.`);
            msgs.forEach(m => bubble(m.content, m.role === 'user' ? 'me' : 'agent', false));
        } else {
            sysLine(`${a.name} connected in local demo mode.`);
        }
        scroll();
    } catch (_) {
        D.chatLog.innerHTML = '';
        sysLine(`${a.name} connected in local demo mode.`);
    }
}

/* ═══════════════════════════════════════════════════════════
   CHAT
   ═══════════════════════════════════════════════════════════ */
function sysLine(t) {
    const d = document.createElement('div');
    d.className = 'chat-sys';
    d.textContent = t;
    D.chatLog.appendChild(d);
    scroll();
}

/* Split into sentence-ish chunks so speech can start on the first one
   instead of waiting for the whole reply to finish typing. */
function chunkText(t) {
    const raw = t.match(/[^.!?\n]+[.!?]*\s*/g) || [t];
    const out = [];
    for (const c of raw) {
        // Fold tiny fragments (abbreviations, stray numerals) into the previous
        // chunk so the voice doesn't stutter on them.
        if (out.length && c.trim().length < 14) out[out.length - 1] += c;
        else out.push(c);
    }
    return out;
}

function bubble(text, who, stream) {
    const d = document.createElement('div');
    d.className = 'bubble ' + who;
    D.chatLog.appendChild(d);
    if (!stream) { d.textContent = text; scroll(); return; }

    S.streaming = true;
    d.classList.add('typing');

    // Pre-compute voice chunks and the char offset each one starts at.
    const voiceOn = S.voice && !S.kill;
    const chunks = voiceOn ? chunkText(text) : null;
    const starts = [];
    if (chunks) {
        let acc = 0;
        for (const c of chunks) { starts.push(acc); acc += c.length; }
    }
    let ci = 0;
    let i = 0;

    S.streamTimer = setInterval(() => {
        // Speak each chunk the moment typing reaches it — chunk 0 fires at i=0,
        // so the voice starts with the first character rather than at the end.
        while (chunks && ci < chunks.length && i >= starts[ci]) {
            speak(chunks[ci], who === 'agent' ? S.active : null);
            ci++;
        }

        if (i < text.length) { d.textContent += text[i++]; scroll(); }
        else {
            clearInterval(S.streamTimer);
            S.streamTimer = null;
            S.streaming = false;
            d.classList.remove('typing');
            checkProposal(text);
        }
    }, STREAM_MS);
}

/* ── proposal detection + card ── */
async function checkProposal(text) {
    if (!S.active) return;
    try {
        const r = await fetch('/trade/propose', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agent_key: S.active, text }),
        });
        const j = await r.json();
        if (j.ok && j.is_proposal) proposalCard(j);
    } catch (_) { /* proposal parsing is best-effort */ }
}

function proposalCard(p) {
    const o = p.order;
    const ok = p.risk_approved;
    const card = document.createElement('div');
    card.className = 'prop-card' + (ok ? '' : ' vetoed');

    const rr = o.rr ? `1:${o.rr}` : '—';
    const tp = o.take_profit ? o.take_profit : '—';

    card.innerHTML =
        `<div class="prop-head"><span>TRADE PROPOSAL · ${esc(p.trading_mode.toUpperCase())}</span>` +
        `<span class="prop-verdict">${ok ? 'RISK PASSED' : 'RISK VETO'}</span></div>` +
        `<div class="prop-line"><span class="side-${o.side}">${o.side.toUpperCase()}</span> ` +
        `${esc(o.symbol)} <span style="color:var(--text-dim)">@</span> ${o.entry_price}</div>` +
        `<div class="prop-grid">` +
        cell('Size', o.size + ' lot') +
        cell('Stop', o.stop_loss) +
        cell('Target', tp) +
        cell('Risk', '$' + o.risk_usd) +
        `</div>` +
        `<div class="prop-checks">` +
        p.checks.map(c => {
            const fail = c.startsWith('✗');
            return `<div class="prop-check ${fail ? 'fail' : 'pass'}">${esc(c)}</div>`;
        }).join('') +
        `</div>` +
        (ok && p.execution && p.execution.uses_real_money
            ? `<div class="apilot-warn">Approving sends a real market order to
               ${esc(p.execution.destination)}. It fills at the live price, which
               may differ from ${o.entry_price}.</div>`
            : '') +
        (ok && p.execution && p.execution.warning
            ? `<div class="apilot-warn">${esc(p.execution.warning)}</div>`
            : '') +
        (ok
            ? `<div class="prop-actions">
                 <button class="prop-btn approve">APPROVE &amp; EXECUTE</button>
                 <button class="prop-btn reject">REJECT</button>
               </div>`
            : `<div class="prop-result bad">Blocked by the risk gate — cannot be executed.</div>`) +
        `<div class="prop-result" hidden></div>`;

    D.chatLog.appendChild(card);
    scroll();

    if (!ok) return;

    const [approveBtn, rejectBtn] = card.querySelectorAll('.prop-btn');
    const results = card.querySelectorAll('.prop-result');
    const out = results[results.length - 1];

    const finish = (cls, msg) => {
        approveBtn.disabled = rejectBtn.disabled = true;
        out.hidden = false;
        out.className = 'prop-result ' + cls;
        out.textContent = msg;
    };

    approveBtn.onclick = () => decide(true);
    rejectBtn.onclick  = () => decide(false);

    async function decide(approve) {
        approveBtn.disabled = rejectBtn.disabled = true;
        try {
            const r = await fetch('/trade/decide', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ proposal_id: p.proposal_id, approve }),
            });
            const j = await r.json();
            if (!approve) return finish('bad', 'Rejected by manager. Nothing sent.');
            if (r.status === 403 || r.status === 503) return finish('bad', j.detail || 'Execution blocked.');
            if (j.status === 'stale_risk') return finish('bad', 'Risk re-check failed at execution — order dropped.');
            if (j.executed) {
                const id = j.result?.order_id || j.result?.position_id || '';
                const fill = j.result?.entry_price;
                const at = (fill && fill !== o.entry_price) ? ` Filled at ${fill}.` : '';
                finish('ok', `Executed in ${String(j.mode).toUpperCase()} mode.${at} ` +
                             `Ref ${String(id).slice(0, 8)}`);
                if (curTab === 'blotter') loadBlotter();
            } else {
                finish('bad', j.error || j.detail || 'Execution failed.');
            }
        } catch (e) {
            finish('bad', 'Execution request failed: ' + e.message);
        }
    }
}

function cell(k, v) { return `<div class="prop-cell"><div class="k">${k}</div><div class="v">${v}</div></div>`; }

function dots() {
    const d = document.createElement('div');
    d.className = 'dots';
    d.id = 'dots';
    d.innerHTML = '<i></i><i></i><i></i>';
    D.chatLog.appendChild(d);
    scroll();
}
function undots() { const d = document.getElementById('dots'); if (d) d.remove(); }
function scroll() { D.chatLog.scrollTop = D.chatLog.scrollHeight; }

async function send() {
    const text = D.chatInput.value.trim();
    if (!text || !S.active || S.streaming) return;
    const key = S.active;

    stopSpeech();               // drop any reply still being read out
    bubble(text, 'me', false);
    D.chatInput.value = '';
    setStatus(key, 'thinking');
    dots();

    try {
        const r = await fetch('/agent/message', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agent_key: key, message: text, lang: S.lang }),
        });
        const j = await r.json();
        undots();
        bubble(j.reply || 'No response.', 'agent', true);
    } catch (e) {
        undots();
        bubble('Connection error: ' + e.message, 'agent', false);
    }
    setStatus(key, 'idle');
    if (curTab === 'gesture') renderGestures();
}

/* ── SPEECH ENGINE ────────────────────────────────────────
   The browser's native queue plays utterances back-to-back with no gap,
   which is what made it sound like fast reading. This queue drives one
   utterance at a time and inserts a real pause between them, so sentences
   land with a breath in between.
   ─────────────────────────────────────────────────────── */

// Voices ranked by how natural they sound. First match wins.
const VOICE_PREFS = {
    en: [
        'Google UK English Female', 'Google US English',
        'Microsoft Aria', 'Microsoft Jenny', 'Microsoft Michelle',
        'Samantha', 'Karen', 'Moira', 'Daniel',
    ],
    bn: ['Google বাংলা', 'Bangla', 'Bengali'],
};

let voicesReady = false;
let pickedVoice = { en: null, bn: null };

function loadVoices() {
    if (!('speechSynthesis' in window)) return;
    const all = speechSynthesis.getVoices();
    if (!all.length) return;             // fires again via onvoiceschanged

    for (const lang of ['en', 'bn']) {
        const tag = lang === 'bn' ? 'bn' : 'en';
        const pool = all.filter(v => (v.lang || '').toLowerCase().startsWith(tag));
        let found = null;
        for (const want of VOICE_PREFS[lang]) {
            found = pool.find(v => (v.name || '').toLowerCase().includes(want.toLowerCase()));
            if (found) break;
        }
        // Prefer a non-local (cloud) voice as fallback — they sound better.
        pickedVoice[lang] = found || pool.find(v => !v.localService) || pool[0] || null;
    }
    voicesReady = true;
}

if ('speechSynthesis' in window) {
    loadVoices();
    speechSynthesis.onvoiceschanged = loadVoices;
}

/* Per-agent voice colour so they don't all sound like the same person. */
const VOICE_TONE = {
    manager:            { rate: 0.84, pitch: 0.88 },
    risk_manager:       { rate: 0.82, pitch: 0.82 },
    computer_scientist: { rate: 0.92, pitch: 1.00 },
    super_trader:       { rate: 0.90, pitch: 0.95 },
    trader_bot_1:       { rate: 0.93, pitch: 1.06 },
    trader_bot_2:       { rate: 0.93, pitch: 0.98 },
    trader_bot_3:       { rate: 0.94, pitch: 1.10 },
    trader_bot_4:       { rate: 0.92, pitch: 1.02 },
};

/* Strip markdown and symbols the agents emit so they aren't read aloud. */
function speechClean(t) {
    return String(t)
        .replace(/```[\s\S]*?```/g, ' code block ')
        .replace(/`([^`]+)`/g, '$1')
        .replace(/\*\*([^*]+)\*\*/g, '$1')
        .replace(/[*_#>|]/g, ' ')
        .replace(/^\s*[-–•]\s*/gm, ', ')
        .replace(/\bR:R\b/gi, 'risk to reward')
        .replace(/\bSL\b/g, 'stop loss')
        .replace(/\bTP\b/g, 'take profit')
        .replace(/\bEURUSD\b/gi, 'euro dollar')
        .replace(/\bGBPUSD\b/gi, 'pound dollar')
        .replace(/\bXAUUSD\b/gi, 'gold')
        .replace(/\bNAS100\b/gi, 'nasdaq 100')
        .replace(/\s{2,}/g, ' ')
        .trim();
}

const speechQ = [];
let speechBusy = false;
let speechGen = 0;          // bumped on stop, so stale callbacks are ignored
let neuralTTS = false;      // NVIDIA Magpie available
let curAudio = null;        // <audio> element currently playing

/* Ask the backend once whether neural TTS is configured. */
async function initTTS() {
    try {
        const j = await (await fetch('/tts/status')).json();
        neuralTTS = !!j.neural;
    } catch (_) { neuralTTS = false; }
}

function speak(t, agentKey) {
    const clean = speechClean(t);
    if (!clean) return;

    // Break long sentences at commas so there is somewhere to breathe.
    const parts = clean.length > 150 ? clean.split(/,\s+/) : [clean];
    parts.forEach((p, idx) => {
        if (!p.trim()) return;
        speechQ.push({
            text: p.trim(),
            agent: agentKey || S.active,
            pause: idx < parts.length - 1 ? CLAUSE_PAUSE_MS : SENTENCE_PAUSE_MS,
            gen: speechGen,
        });
    });
    pumpSpeech();
}

function pumpSpeech() {
    if (speechBusy || !speechQ.length) return;
    const item = speechQ.shift();
    if (item.gen !== speechGen) return pumpSpeech();   // cancelled mid-flight

    speechBusy = true;
    S.speaking = item.agent;

    const done = () => {
        speechBusy = false;
        if (item.gen !== speechGen) return;
        if (speechQ.length) setTimeout(pumpSpeech, item.pause);
        else { S.speaking = null; onSpeechIdle(); }
    };

    if (neuralTTS) playNeural(item, done);
    else playBrowser(item, done);
}

/* NVIDIA Magpie — neural voice. Falls back to the browser on any failure. */
async function playNeural(item, done) {
    try {
        const r = await fetch('/tts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: item.text, agent_key: item.agent, lang: S.lang }),
        });
        if (!r.ok) throw new Error('tts ' + r.status);
        if (item.gen !== speechGen) return done();

        const url = URL.createObjectURL(await r.blob());
        const a = new Audio(url);
        curAudio = a;
        a.onended = () => { URL.revokeObjectURL(url); curAudio = null; done(); };
        a.onerror = () => { URL.revokeObjectURL(url); curAudio = null; playBrowser(item, done); };
        await a.play();
    } catch (_) {
        // One failure is enough to stop paying the round-trip cost this session.
        neuralTTS = false;
        playBrowser(item, done);
    }
}

/* Browser Web Speech API — always available, lower quality. */
function playBrowser(item, done) {
    if (!('speechSynthesis' in window)) return done();

    const u = new SpeechSynthesisUtterance(item.text);
    const lang = S.lang === 'bn' ? 'bn' : 'en';

    if (!voicesReady) loadVoices();
    if (pickedVoice[lang]) u.voice = pickedVoice[lang];
    u.lang = pickedVoice[lang]?.lang || (lang === 'bn' ? 'bn-BD' : 'en-US');

    const tone = VOICE_TONE[item.agent] || { rate: SPEECH_RATE, pitch: 0.95 };
    u.rate = tone.rate;
    u.pitch = tone.pitch;
    u.volume = 0.9;

    u.onend = done;
    u.onerror = () => { speechBusy = false; S.speaking = null; setTimeout(pumpSpeech, 60); };
    speechSynthesis.speak(u);
}

function stopSpeech() {
    speechGen++;
    speechQ.length = 0;
    speechBusy = false;
    S.speaking = null;
    if (curAudio) { try { curAudio.pause(); } catch (_) {} curAudio = null; }
    if ('speechSynthesis' in window) speechSynthesis.cancel();
}

/* ── mic ──────────────────────────────────────────────────
   Continuous dictation. Short pauses do not end the turn: the transcript
   accumulates until you click the mic again, or MIC_SILENCE_MS of true
   silence elapses. Chrome ends recognition on its own periodically, so we
   restart it while the user still intends to be listening.
   ─────────────────────────────────────────────────────── */
let rec = null;
let micOn = false;          // user intent, survives Chrome's auto-stops
let micCommitted = '';      // text from earlier recogniser sessions this turn
let micSilenceTimer = null;
let micStartedAt = 0;

function toggleMic() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) return alert('Speech recognition unavailable in this browser. Try Chrome.');
    if (micOn) { stopMic(true); return; }
    startMic(SR);
}

function startMic(SR) {
    micOn = true;
    micCommitted = '';
    micStartedAt = Date.now();
    D.chatInput.value = '';
    D.micBtn.classList.add('rec');
    D.chatInput.placeholder = 'Listening… take your time';
    stopSpeech();               // don't transcribe our own TTS
    ensureVAD();                // level meter keeps the turn open while you talk
    spawnRecognizer(SR);
    armSilence();
}

function spawnRecognizer(SR) {
    rec = new SR();
    rec.lang = S.lang === 'bn' ? 'bn-BD' : 'en-US';
    rec.continuous = true;      // keep going through pauses
    rec.interimResults = true;  // show words as they land
    rec.maxAlternatives = 1;

    rec.onresult = e => {
        // Rebuild this session's transcript from scratch each time. Appending
        // per-event double-counts finals, which produced repeated words.
        let cur = '';
        for (let k = 0; k < e.results.length; k++) cur += e.results[k][0].transcript;
        D.chatInput.value = (micCommitted + cur).replace(/\s+/g, ' ').trimStart();
        armSilence();
    };

    rec.onerror = e => {
        // 'no-speech' and 'aborted' are routine; keep the session alive.
        if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
            stopMic(false);
            alert('Microphone permission was denied. Allow it in the browser site settings.');
        }
    };

    rec.onend = () => {
        // Chrome ends recognition on its own every so often. Commit what this
        // session heard, then restart while the user still holds the turn.
        if (!micOn) return;
        const sofar = D.chatInput.value.trim();
        micCommitted = sofar ? sofar + ' ' : '';
        try { rec.start(); } catch (_) { /* already starting */ }
    };

    try { rec.start(); } catch (_) { /* already started */ }
}

function armSilence() {
    clearTimeout(micSilenceTimer);
    const wait = S.converse ? CONVO_SILENCE_MS : MIC_SILENCE_MS;
    micSilenceTimer = setTimeout(() => {
        if (!micOn) return;
        // Guard against a cough or click ending the turn instantly.
        if (Date.now() - micStartedAt < MIN_UTTERANCE_MS) return armSilence();
        stopMic(true);
    }, wait);
}

function stopMic(autoSend) {
    micOn = false;
    clearTimeout(micSilenceTimer);
    micSilenceTimer = null;
    if (rec) { try { rec.onend = null; rec.stop(); } catch (_) {} rec = null; }
    D.micBtn.classList.remove('rec');
    D.chatInput.placeholder = 'Ask the active agent…';

    const said = D.chatInput.value.trim();
    if (autoSend && said) send();
    else if (S.converse && !said) scheduleConvoListen();   // heard nothing, keep the loop alive
}

/* ── HANDS-FREE CONVERSATION ──────────────────────────────
   Agent finishes speaking -> mic opens by itself. You start talking while it
   is still speaking -> speech cuts out immediately (barge-in) and your words
   are captured instead. No buttons in the loop.
   ─────────────────────────────────────────────────────── */
let convoTimer = null;

function toggleConverse() {
    if (!S.active && !S.converse) {
        alert('Activate an agent first — walk to a desk and press E.');
        return;
    }
    S.converse = !S.converse;
    D.convoBtn.classList.toggle('on', S.converse);
    D.convoBtn.textContent = S.converse ? 'CONVERSING' : 'CONVERSE';

    if (S.converse) {
        if (!S.voice) {                     // conversation needs the voice on
            S.voice = true;
            D.voiceBtn.textContent = 'VOICE ON';
            D.voiceBtn.classList.add('on');
        }
        sysLine('Conversation mode on. Just talk — interrupt any time.');
        startBargeWatch();
        scheduleConvoListen(400);
    } else {
        clearTimeout(convoTimer);
        stopBargeWatch();
        if (micOn) stopMic(false);
        sysLine('Conversation mode off.');
    }
}

/* Open the mic once the agent has stopped talking. */
function scheduleConvoListen(delay) {
    clearTimeout(convoTimer);
    if (!S.converse) return;
    convoTimer = setTimeout(() => {
        if (!S.converse || micOn || S.streaming || S.speaking) return;
        const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (SR) startMic(SR);
    }, delay || 550);
}

/* Called when the speech queue drains. */
function onSpeechIdle() {
    if (S.converse) scheduleConvoListen();
}

/* ── VOICE ACTIVITY DETECTION ─────────────────────────────
   One analyser serves two jobs:

     while listening  -> any voice keeps your turn open, so a pause between
                         words cannot end the sentence early. The recogniser's
                         own events are too sparse to rely on for this.
     while speaking   -> sustained loud input means you are interrupting, so
                         the agent is cut off.

   echoCancellation is essential: without it the mic picks the agent's own
   voice out of the speakers and the system interrupts itself.
   ─────────────────────────────────────────────────────── */
let vadCtx = null, vadStream = null, vadRAF = null;

async function ensureVAD() {
    if (vadCtx) return true;
    try {
        vadStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                echoCancellation: true,   // don't hear our own TTS
                noiseSuppression: true,
                autoGainControl: true,
            },
        });
        vadCtx = new (window.AudioContext || window.webkitAudioContext)();
        const src = vadCtx.createMediaStreamSource(vadStream);
        const an = vadCtx.createAnalyser();
        an.fftSize = 512;
        an.smoothingTimeConstant = 0.75;
        src.connect(an);

        const buf = new Uint8Array(an.frequencyBinCount);
        let loud = 0;

        const tick = () => {
            an.getByteFrequencyData(buf);
            let sum = 0;
            for (let k = 0; k < buf.length; k++) sum += buf[k];
            const level = sum / buf.length;

            if (S.speaking) {
                // Interrupting: needs to be clearly louder and sustained, so
                // residual speaker bleed cannot trigger it.
                if (S.converse && level > BARGE_LEVEL) {
                    if (++loud >= BARGE_FRAMES) {
                        loud = 0;
                        stopSpeech();
                        sysLine('— interrupted —');
                        scheduleConvoListen(120);
                    }
                } else loud = 0;
            } else if (micOn) {
                // Listening: any voice at all means keep waiting for more.
                if (level > VAD_LEVEL) armSilence();
                loud = 0;
            } else loud = 0;

            vadRAF = requestAnimationFrame(tick);
        };
        tick();
        return true;
    } catch (_) {
        sysLine('Microphone access denied — voice features need it enabled.');
        return false;
    }
}

function startBargeWatch() { ensureVAD(); }

function stopBargeWatch() {
    // Only tear down when nothing needs the mic any more.
    if (micOn || S.converse) return;
    if (vadRAF) cancelAnimationFrame(vadRAF);
    vadRAF = null;
    if (vadStream) { vadStream.getTracks().forEach(t => t.stop()); vadStream = null; }
    if (vadCtx) { try { vadCtx.close(); } catch (_) {} vadCtx = null; }
}

async function clearHistory() {
    if (!S.active) return;
    const a = S.byKey[S.active];
    if (!confirm(`Wipe ${a.name}'s memory and chat history? This cannot be undone.`)) return;
    try {
        await fetch(`/agent/history/${encodeURIComponent(S.active)}`, { method: 'DELETE' });
        stopSpeech();
        D.chatLog.innerHTML = '';
        sysLine(`${a.name} memory cleared. Starting fresh.`);
    } catch (e) { alert('Clear failed: ' + e.message); }
}

/* ── terminate / kill ── */
async function terminate() {
    if (!S.active) return;
    const a = S.byKey[S.active];
    if (!confirm(`Terminate session with ${a.name}?`)) return;
    await fetch('/agent/terminate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_key: S.active }),
    }).catch(() => {});
    sysLine(`${a.name} session terminated.`);
    setStatus(S.active, 'offline');
}

async function toggleKill() {
    const on = !S.kill;
    if (!confirm(on ? 'Activate KILL SWITCH? All trading halts immediately.'
                    : 'Deactivate kill switch and resume?')) return;
    try {
        const r = await fetch('/kill-switch', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ activate: on }),
        });
        const j = await r.json();
        applyKill(on);
        if (S.active) sysLine(on
            ? `KILL SWITCH ACTIVE — ${j.positions_closed ?? 0} position(s) closed.`
            : 'Kill switch released. Trading resumed.');
    } catch (e) { console.error('[kill]', e); }
}

function applyKill(on) {
    S.kill = on;
    D.killBtn.classList.toggle('on', on);
    D.killBtn.textContent = on ? '⛔ KILL SWITCH ACTIVE — CLICK TO RELEASE'
                               : '⛔ KILL SWITCH — HALT ALL TRADING';
    if (on) {
        stopSpeech();
        if (micOn) stopMic(false);
        if (S.converse) {          // stop the hands-free loop too
            S.converse = false;
            clearTimeout(convoTimer);
            stopBargeWatch();
            D.convoBtn.classList.remove('on');
            D.convoBtn.textContent = 'CONVERSE';
        }
    }
    if (curTab === 'blotter') loadBlotter();
}

/* ═══════════════════════════════════════════════════════════
   WEBSOCKET
   ═══════════════════════════════════════════════════════════ */
function connectWS() {
    const p = location.protocol === 'https:' ? 'wss:' : 'ws:';
    wsState('wait', 'Connecting locally.');
    try {
        S.ws = new WebSocket(`${p}//${location.host}/ws`);
        S.ws.onopen = () => { wsState('', 'Connected locally.'); clearTimeout(S.wsTimer); };
        S.ws.onmessage = e => {
            try {
                const m = JSON.parse(e.data);
                if (m.type === 'init' && m.agents) {
                    S.agents = m.agents;
                    S.byKey = {};
                    m.agents.forEach(a => { S.byKey[a.agent_key] = a; a.seed = hash(a.agent_key); });
                    updateBusy();
                } else if (m.type === 'agent_status') {
                    setStatus(m.agent_key, m.status);
                } else if (m.type === 'autopilot' && m.entry) {
                    onAutopilotEvent(m.entry);
                }
            } catch (_) {}
        };
        S.ws.onclose = () => { wsState('down', 'Reconnecting locally.'); retryWS(); };
        S.ws.onerror = () => wsState('down', 'Reconnecting locally.');
    } catch (_) { wsState('down', 'Reconnecting locally.'); retryWS(); }
}
function retryWS() { clearTimeout(S.wsTimer); S.wsTimer = setTimeout(connectWS, 3000); }
function wsState(cls, txt) {
    D.wsStatus.className = 'conn-status ' + cls;
    D.wsStatus.innerHTML = `<i class="dot"></i>${txt}`;
}

/* ═══════════════════════════════════════════════════════════
   GAME LOOP
   ═══════════════════════════════════════════════════════════ */
function loop() {
    S.t++;
    if (S.t % 6 === 0) syncSpeakingBadge();
    if (curTab === 'town') { step(); render(); }
    else { drawNet(); drawOrb(); }
    requestAnimationFrame(loop);
}

function step() {
    const p = S.player, k = S.keys;
    let dx = 0, dy = 0;
    if (k['a'] || k['arrowleft'])  { dx -= 1; p.dir = 'left'; }
    if (k['d'] || k['arrowright']) { dx += 1; p.dir = 'right'; }
    if (k['w'] || k['arrowup'])    { dy -= 1; p.dir = 'up'; }
    if (k['s'] || k['arrowdown'])  { dy += 1; p.dir = 'down'; }

    p.moving = !!(dx || dy);
    if (p.moving) {
        const l = Math.hypot(dx, dy) || 1;
        p.x = clamp(p.x + (dx / l) * WALK_SPEED, TILE * .6, (COLS - .6) * TILE);
        p.y = clamp(p.y + (dy / l) * WALK_SPEED, TILE * .6, (ROWS - .6) * TILE);
        if (++p.tick % 8 === 0) p.frame = (p.frame + 1) % 4;
    } else p.frame = 0;

    // nearest desk within range
    let best = null, bd = 1e9;
    for (const d of DESKS) {
        const cx = (d.tx + .5) * TILE, cy = (d.ty + 1.35) * TILE;
        const dist = Math.hypot(p.x - cx, p.y - cy);
        if (dist < bd) { bd = dist; best = d; }
    }
    S.near = bd < TILE * 1.5 ? best.key : null;

    const tip = D.interactTip;
    if (S.near) {
        const a = S.byKey[S.near];
        const d = DESKS.find(x => x.key === S.near);
        const sc = scaleInfo();
        D.interactName.textContent = a ? a.name : S.near;
        tip.hidden = false;
        tip.style.left = (sc.ox + (d.tx + .5) * TILE * sc.s) + 'px';
        tip.style.top  = (sc.oy + (d.ty - .1) * TILE * sc.s) + 'px';
    } else tip.hidden = true;
}

function scaleInfo() {
    const cw = D.officeCanvas.clientWidth, ch = D.officeCanvas.clientHeight;
    const s = Math.min(cw / (COLS * TILE), ch / (ROWS * TILE));
    return { s, ox: (cw - COLS * TILE * s) / 2, oy: (ch - ROWS * TILE * s) / 2 };
}

/* ═══════════════════════════════════════════════════════════
   RENDER — OFFICE
   ═══════════════════════════════════════════════════════════ */
function render() {
    const cw = D.officeCanvas.clientWidth, ch = D.officeCanvas.clientHeight;
    ctx.fillStyle = '#070b10';
    ctx.fillRect(0, 0, cw, ch);

    const { s, ox, oy } = scaleInfo();
    ctx.save();
    ctx.translate(ox, oy);
    ctx.scale(s, s);

    drawFloor();
    drawCarpet(CARPET, '#12222c');
    drawCarpet(RUG, '#3a1f2c');
    PLANTS.forEach(drawPlant);
    SERVERS.forEach(drawServer);

    // z-sorted entities
    const ents = DESKS.map(d => ({ z: d.ty, kind: 'desk', d }));
    ents.push({ z: S.player.y / TILE - .2, kind: 'player' });
    ents.sort((a, b) => a.z - b.z);
    ents.forEach(e => e.kind === 'desk' ? drawDesk(e.d) : drawPlayer());

    ctx.restore();
    drawNet();
    drawOrb();
}

function drawFloor() {
    const W = COLS * TILE, H = ROWS * TILE;
    ctx.fillStyle = '#0d1b24';
    ctx.fillRect(0, 0, W, H);
    // diagonal stripes
    ctx.strokeStyle = 'rgba(255,255,255,.022)';
    ctx.lineWidth = 6;
    for (let i = -H; i < W; i += 26) {
        ctx.beginPath(); ctx.moveTo(i, H); ctx.lineTo(i + H, 0); ctx.stroke();
    }
    // grid
    ctx.strokeStyle = 'rgba(255,255,255,.028)';
    ctx.lineWidth = 1;
    for (let x = 0; x <= COLS; x++) { ctx.beginPath(); ctx.moveTo(x*TILE, 0); ctx.lineTo(x*TILE, H); ctx.stroke(); }
    for (let y = 0; y <= ROWS; y++) { ctx.beginPath(); ctx.moveTo(0, y*TILE); ctx.lineTo(W, y*TILE); ctx.stroke(); }
    // border
    ctx.strokeStyle = '#1b2d38';
    ctx.lineWidth = 2;
    ctx.strokeRect(1, 1, W - 2, H - 2);
}

function drawCarpet(r, col) {
    ctx.fillStyle = col;
    ctx.fillRect(r.tx*TILE, r.ty*TILE, r.w*TILE, r.h*TILE);
    ctx.strokeStyle = 'rgba(255,255,255,.05)';
    ctx.lineWidth = 1;
    ctx.strokeRect(r.tx*TILE+.5, r.ty*TILE+.5, r.w*TILE-1, r.h*TILE-1);
}

function drawPlant(p) {
    const x = p.tx*TILE + 8, y = p.ty*TILE + 6;
    ctx.fillStyle = '#7a4a2c'; ctx.fillRect(x+4, y+16, 10, 9);
    ctx.fillStyle = '#5b3520'; ctx.fillRect(x+4, y+16, 10, 2);
    ctx.fillStyle = '#2f7d4a';
    ctx.fillRect(x+7, y+4, 4, 13);
    ctx.fillRect(x+1, y+8, 5, 3);
    ctx.fillRect(x+12, y+6, 5, 3);
    ctx.fillRect(x+3, y+2, 4, 4);
    ctx.fillRect(x+11, y+11, 4, 3);
    ctx.fillStyle = '#3d9c5d';
    ctx.fillRect(x+8, y+2, 3, 4);
}

function drawServer(p) {
    const x = p.tx*TILE + 6, y = p.ty*TILE + 8;
    ctx.fillStyle = '#131c24'; ctx.fillRect(x, y, 20, 18);
    ctx.strokeStyle = '#26333d'; ctx.lineWidth = 1; ctx.strokeRect(x+.5, y+.5, 19, 17);
    const on = (Math.floor(S.t / 30) % 2) === 0;
    ctx.fillStyle = on ? '#43c96b' : '#1d4a2c'; ctx.fillRect(x+3, y+4, 3, 3);
    ctx.fillStyle = '#d98b2b'; ctx.fillRect(x+3, y+10, 3, 3);
    ctx.fillStyle = '#1f2b34';
    for (let i = 0; i < 3; i++) ctx.fillRect(x+9, y+4+i*5, 8, 3);
}

function drawDesk(d) {
    const a = S.byKey[d.key];
    const x = d.tx*TILE, y = d.ty*TILE;
    const sel = S.active === d.key;
    const near = S.near === d.key;
    const busy = a && a.status === 'thinking';
    const off  = a && a.status === 'offline';

    // selection glow
    if (sel || near) {
        ctx.strokeStyle = sel ? '#35d6e3' : '#d98b2b';
        ctx.lineWidth = 1.5;
        ctx.strokeRect(x-3.5, y-3.5, TILE*2+7, TILE*2+7);
    }

    // desk top
    ctx.fillStyle = '#182430'; ctx.fillRect(x+1, y+20, TILE*2-2, 20);
    ctx.fillStyle = '#101a23'; ctx.fillRect(x+1, y+36, TILE*2-2, 4);
    ctx.fillStyle = '#0c141b'; ctx.fillRect(x+4, y+40, 4, 8);
    ctx.fillRect(x+TILE*2-8, y+40, 4, 8);

    // monitor
    const mx = x + 14, my = y + 2;
    ctx.fillStyle = '#0a1015'; ctx.fillRect(mx-2, my-2, 36, 24);
    const scrOn = !off;
    ctx.fillStyle = scrOn ? (busy ? '#1d4d63' : '#17394a') : '#0e1a20';
    ctx.fillRect(mx, my, 32, 20);
    if (scrOn) {
        // code lines flicker
        const seed = (a ? a.seed : 3) + Math.floor(S.t / (busy ? 8 : 26));
        ctx.fillStyle = busy ? '#7fe3f0' : '#3d8fa5';
        for (let i = 0; i < 4; i++) {
            const w = 6 + ((seed * (i + 3) * 7) % 20);
            ctx.fillRect(mx + 3, my + 3 + i * 4, w, 2);
        }
    }
    ctx.fillStyle = '#1b2732'; ctx.fillRect(mx+13, my+20, 6, 4);
    ctx.fillRect(mx+8, my+24, 16, 2);

    // status LED
    ctx.fillStyle = off ? '#e2544a' : busy ? '#d98b2b' : '#43c96b';
    if (!off && busy && (Math.floor(S.t / 14) % 2)) ctx.fillStyle = '#5b3d16';
    ctx.fillRect(x + TILE*2 - 8, y + 24, 4, 4);

    // seated character
    drawSeated(x + TILE*0.5, y + TILE + 4, d.key, a, busy, S.speaking === d.key);

    // nameplate
    const label = a ? a.name : d.key;
    ctx.font = '9px "Share Tech Mono", monospace';
    ctx.textAlign = 'center';
    const w = ctx.measureText(label).width + 8;
    ctx.fillStyle = 'rgba(5,9,13,.85)';
    ctx.fillRect(x + TILE - w/2, y + TILE*2 - 2, w, 11);
    ctx.fillStyle = sel ? '#35d6e3' : off ? '#6c7f8c' : '#9fb3c0';
    ctx.fillText(label, x + TILE, y + TILE*2 + 6);
    ctx.textAlign = 'left';
}

function drawSeated(cx, cy, key, a, busy, talking) {
    const skin = SKINS[(a ? a.seed : 1) % SKINS.length];
    const shirt = SHIRTS[key] || SHIRTS[a && a.role] || SHIRTS.trader_bot;
    const off = a && a.status === 'offline';
    const bob = busy ? Math.sin(S.t / 7) * 1 : (talking ? Math.sin(S.t / 5) * .6 : 0);

    ctx.globalAlpha = off ? .45 : 1;
    // chair back
    ctx.fillStyle = '#141d26'; ctx.fillRect(cx-8, cy-2, 16, 12);
    // body
    ctx.fillStyle = shirt; ctx.fillRect(cx-6, cy-8 + bob, 12, 11);
    // arms on desk
    ctx.fillStyle = skin;
    ctx.fillRect(cx-8, cy-4 + bob, 3, 6);
    ctx.fillRect(cx+5, cy-4 + bob, 3, 6);
    // head
    ctx.fillStyle = skin; ctx.fillRect(cx-5, cy-17 + bob, 10, 10);
    // hair
    ctx.fillStyle = '#231a15'; ctx.fillRect(cx-5, cy-18 + bob, 10, 4);
    // eyes
    if (!off) {
        ctx.fillStyle = '#0a0a0a';
        ctx.fillRect(cx-3, cy-13 + bob, 2, 2);
        ctx.fillRect(cx+1, cy-13 + bob, 2, 2);
    }

    // mouth — animates open/closed while the reply is being voiced
    if (talking && !off) {
        const open = (Math.floor(S.t / 5) % 2) === 0;
        ctx.fillStyle = '#5a2b26';
        if (open) ctx.fillRect(cx-2, cy-10 + bob, 4, 3);
        else      ctx.fillRect(cx-2, cy-9 + bob, 4, 1);
    }
    ctx.globalAlpha = 1;

    // speech waves while talking
    if (talking && !off) {
        const ph = Math.floor(S.t / 6) % 3;
        ctx.strokeStyle = '#35d6e3';
        ctx.lineWidth = 1;
        for (let k = 0; k < 3; k++) {
            ctx.globalAlpha = k === ph ? .9 : .25;
            const r = 5 + k * 3;
            ctx.beginPath();
            ctx.arc(cx + 9, cy - 12 + bob, r, -0.7, 0.7);
            ctx.stroke();
        }
        ctx.globalAlpha = 1;
    }

    // thinking bubble
    if (busy) {
        const b = Math.floor(S.t / 16) % 4;
        ctx.fillStyle = '#d98b2b';
        for (let i = 0; i < 3; i++) {
            ctx.globalAlpha = b === i ? 1 : .3;
            ctx.fillRect(cx - 4 + i * 4, cy - 25, 3, 3);
        }
        ctx.globalAlpha = 1;
    }
}

function drawPlayer() {
    const p = S.player;
    const x = Math.round(p.x), y = Math.round(p.y);
    const step = p.moving ? [0,1,0,-1][p.frame] : 0;

    // shadow
    ctx.fillStyle = 'rgba(0,0,0,.35)';
    ctx.beginPath(); ctx.ellipse(x, y + 11, 8, 3, 0, 0, 7); ctx.fill();

    // legs
    ctx.fillStyle = '#233242';
    ctx.fillRect(x-5, y+2 + (step>0?1:0), 4, 9);
    ctx.fillRect(x+1, y+2 + (step<0?1:0), 4, 9);
    // body
    ctx.fillStyle = '#2f9fb0'; ctx.fillRect(x-6, y-8, 12, 11);
    ctx.fillStyle = '#247f8d'; ctx.fillRect(x-6, y-8, 12, 3);
    // arms
    ctx.fillStyle = '#d9a173';
    ctx.fillRect(x-8, y-6 + step, 2, 8);
    ctx.fillRect(x+6, y-6 - step, 2, 8);
    // head
    ctx.fillStyle = '#e8b98d'; ctx.fillRect(x-5, y-18, 10, 10);
    ctx.fillStyle = '#3a2a1e'; ctx.fillRect(x-5, y-19, 10, 4);
    // eyes by direction
    ctx.fillStyle = '#111';
    if (p.dir === 'left')       { ctx.fillRect(x-4, y-14, 2, 2); }
    else if (p.dir === 'right') { ctx.fillRect(x+2, y-14, 2, 2); }
    else if (p.dir === 'down')  { ctx.fillRect(x-3, y-14, 2, 2); ctx.fillRect(x+1, y-14, 2, 2); }

    // "YOU" tag
    ctx.font = '8px "Share Tech Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillStyle = '#35d6e3';
    ctx.fillText('YOU', x, y - 22);
    ctx.textAlign = 'left';
}

/* ═══════════════════════════════════════════════════════════
   RENDER — HEADER VISUALS
   ═══════════════════════════════════════════════════════════ */
function drawNet() {
    const w = 260, h = 120;
    nctx.clearRect(0, 0, w, h);
    const hubX = w - 14, hubY = h / 2;
    const cols = ['#35d6e3','#d98b2b','#43c96b','#8f7bd8','#e2544a','#3f8fd8'];
    const busy = S.agents.filter(a => a.status === 'thinking').length;

    for (let i = 0; i < 6; i++) {
        const sy = 12 + i * 19;
        nctx.strokeStyle = cols[i];
        nctx.globalAlpha = .22 + (i < busy ? .55 : 0) + Math.sin(S.t/40 + i)*.06;
        nctx.lineWidth = 1;
        nctx.beginPath();
        nctx.moveTo(6, sy);
        nctx.bezierCurveTo(w*.45, sy, w*.55, hubY, hubX, hubY);
        nctx.stroke();
    }
    nctx.globalAlpha = 1;
    nctx.fillStyle = '#43c96b';
    nctx.beginPath(); nctx.arc(hubX, hubY, 3, 0, 7); nctx.fill();
    nctx.globalAlpha = .3;
    nctx.beginPath(); nctx.arc(hubX, hubY, 6 + Math.sin(S.t/22)*2, 0, 7); nctx.fill();
    nctx.globalAlpha = 1;
}

function drawOrb() {
    const c = 55;
    octx.clearRect(0, 0, 110, 110);
    // rings
    for (let i = 3; i >= 1; i--) {
        octx.strokeStyle = '#1d5a63';
        octx.globalAlpha = .12 * i;
        octx.lineWidth = 1;
        octx.beginPath();
        octx.arc(c, c, 16 + i * 9 + Math.sin(S.t/38 + i)*1.4, 0, 7);
        octx.stroke();
    }
    // glow
    const g = octx.createRadialGradient(c, c, 2, c, c, 22);
    g.addColorStop(0, 'rgba(160,240,250,.95)');
    g.addColorStop(.45, 'rgba(53,214,227,.35)');
    g.addColorStop(1, 'rgba(53,214,227,0)');
    octx.globalAlpha = 1;
    octx.fillStyle = g;
    octx.beginPath(); octx.arc(c, c, 22, 0, 7); octx.fill();
    // core
    octx.fillStyle = '#dffaff';
    octx.beginPath(); octx.arc(c, c, 4.5 + Math.sin(S.t/16)*.7, 0, 7); octx.fill();
}

function drawAvatar(a) {
    actx.clearRect(0, 0, 34, 34);
    const skin = SKINS[a.seed % SKINS.length];
    const shirt = SHIRTS[a.agent_key] || SHIRTS[a.role] || SHIRTS.trader_bot;
    actx.fillStyle = '#0a1218'; actx.fillRect(0, 0, 34, 34);
    actx.fillStyle = shirt; actx.fillRect(9, 20, 16, 12);
    actx.fillStyle = skin;   actx.fillRect(10, 8, 14, 13);
    actx.fillStyle = '#231a15'; actx.fillRect(10, 6, 14, 5);
    actx.fillStyle = '#0a0a0a';
    actx.fillRect(13, 14, 3, 3);
    actx.fillRect(19, 14, 3, 3);
}

/* ═══════════════════════════════════════════════════════════
   TABS
   ═══════════════════════════════════════════════════════════ */
let curTab = 'town';

function switchTab(tab) {
    curTab = tab;
    document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x.dataset.tab === tab));
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + tab));
    if (tab === 'agents')  renderRoster();
    if (tab === 'visual')  loadHub();
    if (tab === 'blotter') loadBlotter();
    if (tab === 'autopilot') loadAutopilot();
    if (tab === 'gesture') renderGestures();
    if (tab === 'town')    resize();
}

/* ══ AUTOPILOT ══════════════════════════════════════════════
   Shows where orders go, whether the loop is running, what the
   guardrails are set to, and everything it has done. The broker
   card is deliberately blunt about real vs simulated money.
   ═══════════════════════════════════════════════════════════ */
let apilotRunning = false;

function row(label, value, cls) {
    return `<div class="stat-row"><span>${label}</span>` +
           `<b class="${cls || ''}">${value}</b></div>`;
}

async function loadAutopilot() {
    let st = null;
    try {
        const r = await fetch('/autopilot/status');
        st = await r.json();
    } catch (_) {
        D.apilotStats.innerHTML = '<div class="hub-empty">Autopilot unreachable.</div>';
        return;
    }

    apilotRunning = !!st.running;
    D.apilotState.textContent = st.halted ? `HALTED — ${st.halt_reason}`
                              : st.running ? 'running' : 'stopped';
    D.apilotState.className = st.halted ? 'bad' : (st.running ? 'good' : '');
    D.apilotToggle.textContent = st.running ? 'STOP' : 'START';
    D.apilotToggle.classList.toggle('on', st.running);

    const ex = st.execution || {};
    D.apilotDest.textContent = (ex.mode || 'paper').toUpperCase();

    const cfg = st.config || {};
    D.apilotStats.innerHTML =
        row('Cycles run', st.cycles ?? 0) +
        row('Last scan', st.last_cycle_at ? shortTime(st.last_cycle_at) : '—') +
        row('Next scan', st.next_cycle_at ? shortTime(st.next_cycle_at) : '—') +
        row('Scan interval', `${cfg.AUTOPILOT_INTERVAL_SEC}s`) +
        row('Market', st.market_open ? 'open' : 'closed', st.market_open ? 'good' : '') +
        row('Session', st.session || '—') +
        row('Fills this hour', `${st.fills_last_hour}/${cfg.AUTOPILOT_MAX_TRADES_PER_HOUR}`) +
        row('Fills today', `${st.fills_today}/${cfg.AUTOPILOT_MAX_TRADES_PER_DAY}`) +
        (st.last_error ? row('Last error', st.last_error, 'bad') : '');

    D.apilotGuards.innerHTML =
        row('On its own?', st.will_place_orders ? 'places orders' : 'asks first',
            st.will_place_orders ? 'bad' : 'good') +
        row('Max size', `${cfg.AUTOPILOT_MAX_SIZE} lots`) +
        row('Min reward:risk', `${cfg.AUTOPILOT_MIN_RR}x`) +
        row('Halt at loss', `$${cfg.AUTOPILOT_HALT_DRAWDOWN}`) +
        row('Watching', (cfg.AUTOPILOT_SYMBOLS || []).join(', ')) +
        row('Real-account unlock', cfg.AUTOPILOT_ALLOW_LIVE ? 'granted' : 'withheld',
            cfg.AUTOPILOT_ALLOW_LIVE ? 'bad' : 'good') +
        row('Overnight alerts', st.notifications ? 'Telegram on' : 'off',
            st.notifications ? 'good' : '');

    renderBrokerCard(ex);
    renderApilotFeed();
    loadBrokerForm();
}

/* Pre-fill the credential form from the saved (redacted) config. */
async function loadBrokerForm() {
    if (window.brokerFormDirty) return;   // don't clobber what the user is typing
    try {
        const c = await (await fetch('/broker/config')).json();
        D.bkMode.value = c.mode || 'paper';
        D.bkStyle.value = (S.exec && S.style) || 'day';
        D.bkAccount.value = c.account_id || '';
        D.bkRegion.value = c.region || 'london';
        D.bkEnabled.checked = !!c.trading_enabled;
        D.bkToken.placeholder = c.token_set
            ? `saved (${c.token_hint}) — leave blank to keep it`
            : 'paste MetaApi token';
    } catch (_) {}
}

async function saveBrokerConfig() {
    window.brokerFormDirty = false;
    const body = {
        mode: D.bkMode.value,
        account_id: D.bkAccount.value.trim(),
        region: D.bkRegion.value.trim(),
        trading_enabled: D.bkEnabled.checked,
    };
    // Only send the token when the user actually typed one, so a blank field
    // keeps the saved token instead of wiping it.
    const tok = D.bkToken.value.trim();
    if (tok) body.token = tok;

    // Style is a separate endpoint: it changes candles, freshness limits,
    // entry tolerance and model routing together.
    try {
        await fetch('/style', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ style: D.bkStyle.value }),
        });
    } catch (_) {}

    if (body.trading_enabled && body.mode === 'broker' && !tok) {
        // fine — they may be reusing a saved token; the status check will tell.
    }
    D.bkSave.disabled = true;
    D.bkHint.textContent = 'Saving and testing…';
    try {
        const r = await fetch('/broker/config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const j = await r.json();
        D.bkToken.value = '';   // never keep the secret in the field
        const h = j.status || {};
        if (body.mode !== 'broker') {
            D.bkHint.textContent = 'Saved. Mode is ' + body.mode + ' — no broker needed.';
        } else if (h.reachable) {
            const acct = h.account || {};
            D.bkHint.textContent = `Connected ✓  ${acct.login ?? ''} ` +
                `${h.is_demo === false ? 'REAL' : 'demo'}  bal ${fmtMoney(acct.balance)}`;
        } else {
            D.bkHint.textContent = 'Saved, but not connected: ' + (h.error || 'check credentials');
        }
    } catch (e) {
        D.bkHint.textContent = 'Save failed: ' + e.message;
    } finally {
        D.bkSave.disabled = false;
        refreshStatus();   // repaint the header badge for the new mode
        loadAutopilot();
    }
}

async function renderBrokerCard(ex) {
    let html = row('Mode', (ex.mode || 'paper').toUpperCase()) +
               row('Orders go to', ex.destination || '—') +
               row('Real money path', ex.uses_real_money ? 'YES' : 'no',
                   ex.uses_real_money ? 'bad' : 'good');
    if (ex.warning) html += `<div class="apilot-warn">${ex.warning}</div>`;

    if (ex.mode === 'broker') {
        try {
            const r = await fetch('/broker/status');
            const b = await r.json();
            const acct = b.account || {};
            html += row('Bridge', b.reachable ? 'connected' : 'not connected',
                        b.reachable ? 'good' : 'bad');
            if (b.deployment) {
                html += row('Server', b.deployment.server || '—') +
                        row('Terminal', b.deployment.connection_status || b.deployment.state || '—');
            }
            if (b.reachable) {
                html += row('Account', `${acct.login ?? '—'} (${acct.currency ?? ''})`) +
                        row('Account type', b.is_demo === true ? 'DEMO'
                                          : b.is_demo === false ? 'REAL MONEY' : 'unknown',
                            b.is_demo === false ? 'bad' : 'good') +
                        row('Balance', fmtMoney(acct.balance)) +
                        row('Equity', fmtMoney(acct.equity)) +
                        row('Free margin', fmtMoney(acct.free_margin));
                const syms = Object.entries(b.symbols || {})
                    .map(([k, v]) => `${k}→${v || '?'}`).join('  ');
                if (syms) html += row('Symbol mapping', syms);
            }
            if (b.error) html += `<div class="apilot-warn">${b.error}</div>`;
        } catch (_) {
            html += `<div class="apilot-warn">Could not read broker status.</div>`;
        }
    } else if (ex.mode === 'paper') {
        html += `<div class="apilot-note">Nothing here reaches a broker. To trade a
                 Trading.com demo account from any OS, set TRADING_MODE=broker and add
                 your MetaApi token — see .env.example.</div>`;
    }
    D.brokerPanel.innerHTML = html;
}

function fmtMoney(v) {
    return (v === null || v === undefined) ? '—' : Number(v).toFixed(2);
}
function shortTime(iso) {
    try { return new Date(iso).toLocaleTimeString(); } catch (_) { return iso; }
}

async function toggleAutopilot() {
    const starting = !apilotRunning;
    if (starting) {
        // Anything that can reach a real account gets an explicit confirmation.
        let warn = 'Start the autopilot?\n\nIt will scan the markets on a timer.';
        try {
            const s = await (await fetch('/autopilot/status')).json();
            warn += s.will_place_orders
                ? `\n\nIt WILL PLACE ORDERS BY ITSELF via: ${s.execution?.destination}.`
                : '\n\nIt will queue proposals for your approval and place nothing on its own.';
        } catch (_) {}
        if (!confirm(warn)) return;
    }
    D.apilotToggle.disabled = true;
    try {
        const r = await fetch(starting ? '/autopilot/start' : '/autopilot/stop', { method: 'POST' });
        const j = await r.json();
        if (!r.ok) alert(j.detail || 'Autopilot could not change state.');
    } catch (e) {
        alert('Autopilot request failed: ' + e.message);
    } finally {
        D.apilotToggle.disabled = false;
        refreshStatus();
        loadAutopilot();
    }
}

let apilotFeed = [];

async function renderApilotFeed() {
    try {
        const r = await fetch('/autopilot/log?limit=60');
        const j = await r.json();
        apilotFeed = j.entries || [];
    } catch (_) {}
    paintApilotFeed();
}

function paintApilotFeed() {
    if (!apilotFeed.length) {
        D.apilotFeed.innerHTML =
            '<div class="hub-empty">Nothing yet. Start the autopilot to see it work.</div>';
        return;
    }
    D.apilotFeed.innerHTML = apilotFeed.map(e => `
        <div class="apilot-row ${e.level}">
            <span class="apilot-at">${shortTime(e.at)}</span>
            <span class="apilot-lvl">${e.level}</span>
            <span class="apilot-msg">${escapeHtml(e.message)}</span>
        </div>`).join('');
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function onAutopilotEvent(entry) {
    apilotFeed.unshift(entry);
    if (apilotFeed.length > 60) apilotFeed.pop();
    if (curTab === 'autopilot') { paintApilotFeed(); }
    // A fill or a halt changes the book and the kill switch, so refresh those.
    if (entry.level === 'trade' || entry.level === 'halt') {
        if (curTab === 'blotter') loadBlotter();
        if (curTab === 'autopilot') loadAutopilot();
        if (entry.level === 'halt') applyKill(true);
    }
}

/* ── BLOTTER ── */
async function loadBlotter() {
    D.blotterMode.textContent = S.kill
        ? 'KILL SWITCH ACTIVE'
        : `${S.mode || 'paper'} — ${(S.exec && S.exec.destination) || 'simulated locally'}`;

    // pending proposals
    try {
        const j = await (await fetch('/trade/pending')).json();
        const p = j.pending || [];
        D.pendCount.textContent = p.length;
        if (!p.length) {
            D.pendingList.innerHTML = '<div class="hub-empty">No proposals awaiting decision.</div>';
        } else {
            D.pendingList.innerHTML = '';
            p.forEach(item => {
                const o = item.order;
                const row = document.createElement('div');
                row.className = 'pend-row';
                row.innerHTML =
                    `<div><div class="pend-desc">${o.side.toUpperCase()} ${esc(o.symbol)} ${o.size} lot @ ${o.entry_price}</div>` +
                    `<div class="pend-meta">SL ${o.stop_loss} · TP ${o.take_profit || '—'} · risk $${o.risk_usd} · from ${esc(item.agent_key)}</div></div>` +
                    `<span class="pill ${item.risk_approved ? 'idle' : 'off'}">${item.risk_approved ? 'RISK OK' : 'VETO'}</span>` +
                    `<span class="pend-btns"></span>`;
                const btns = row.querySelector('.pend-btns');
                if (item.risk_approved) {
                    const a = mkBtn('APPROVE', async () => { await decideFromBlotter(item.proposal_id, true); });
                    btns.appendChild(a);
                }
                btns.appendChild(mkBtn('REJECT', async () => { await decideFromBlotter(item.proposal_id, false); }));
                D.pendingList.appendChild(row);
            });
        }
    } catch (_) { D.pendingList.innerHTML = '<div class="hub-empty">Pending queue unreachable.</div>'; }

    // open positions
    try {
        const j = await (await fetch('/trades')).json();
        const p = j.positions || [];
        D.openCount.textContent = p.length;
        if (!p.length) D.blotterOpen.innerHTML = '<div class="hub-empty">No open positions.</div>';
        else {
            const t = document.createElement('table');
            t.className = 'mini-table';
            t.innerHTML = '<thead><tr><th>SYMBOL</th><th>SIDE</th><th>SIZE</th><th>ENTRY</th><th>OPENED</th><th></th></tr></thead>';
            const tb = document.createElement('tbody');
            p.forEach(x => {
                const tr = document.createElement('tr');
                tr.innerHTML =
                    `<td>${esc(x.symbol)}</td><td>${esc(x.direction || '')}</td>` +
                    `<td>${x.size}</td><td>${x.entry_price}</td>` +
                    `<td>${esc(String(x.opened_at || '').slice(0, 19))}</td><td></td>`;
                tr.lastElementChild.appendChild(mkBtn('CLOSE', async () => {
                    if (!confirm(`Close ${x.symbol} ${x.direction} ${x.size} at market?`)) return;
                    try {
                        const r = await fetch('/trade/close', {
                            method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ position_id: x.id }),
                        });
                        const jj = await r.json();
                        if (jj.ok) loadBlotter();
                        else alert(jj.detail || 'Close failed.');
                    } catch (e) { alert('Close failed: ' + e.message); }
                }));
                tb.appendChild(tr);
            });
            t.appendChild(tb);
            D.blotterOpen.innerHTML = '';
            D.blotterOpen.appendChild(t);
        }
    } catch (_) { D.blotterOpen.innerHTML = '<div class="hub-empty">Book unreachable.</div>'; }

    // execution log
    try {
        const j = await (await fetch('/audit?limit=40')).json();
        const want = ['trade_proposed','risk_assessment','trade_approved','trade_rejected',
                      'trade_executed','trade_executed_LIVE','position_closed',
                      'kill_switch_activated','kill_switch'];
        const rec = (j.records || []).filter(x => want.includes(x.action_type)).slice(0, 18);
        if (!rec.length) D.execLog.innerHTML = '<div class="hub-empty">No execution events yet.</div>';
        else D.execLog.innerHTML = rec.map(x =>
            `<div class="audit-row"><span class="a-ts">${esc(String(x.timestamp||'').slice(11,19))}</span>` +
            `<span class="a-kind">${esc(x.action_type||'')}</span>` +
            `<span class="a-det">${esc(x.detail||'')}</span></div>`).join('');
    } catch (_) { D.execLog.innerHTML = '<div class="hub-empty">Audit unreachable.</div>'; }
}

function mkBtn(label, fn) {
    const b = document.createElement('button');
    b.className = 'inline-btn';
    b.textContent = label;
    b.onclick = fn;
    return b;
}

async function decideFromBlotter(pid, approve) {
    try {
        const r = await fetch('/trade/decide', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ proposal_id: pid, approve }),
        });
        const j = await r.json();
        if (approve && !j.executed) alert(j.detail || j.status || 'Execution did not complete.');
    } catch (e) { alert('Decision failed: ' + e.message); }
    loadBlotter();
}

/* ── AGENTS: roster ── */
function renderRoster() {
    if (!S.agents.length) {
        D.rosterBody.innerHTML = '<tr><td colspan="5" class="hub-empty">Loading agents…</td></tr>';
        return;
    }
    D.rosterBody.innerHTML = '';
    S.agents.forEach(a => {
        const st = a.status || 'idle';
        const cls = st === 'thinking' ? 'busy' : st === 'offline' ? 'off' : 'idle';
        const txt = st === 'thinking' ? 'BUSY' : st === 'offline' ? 'OFFLINE' : 'IDLE';
        const tr = document.createElement('tr');
        if (S.active === a.agent_key) tr.className = 'on';
        tr.innerHTML =
            `<td class="r-name">${esc(a.name)}</td>` +
            `<td>${esc(a.agent_key)}</td>` +
            `<td>${esc(a.role)}</td>` +
            `<td>nemotron-3-nano</td>` +
            `<td><span class="pill ${cls}">${txt}</span></td>`;
        tr.onclick = () => { activate(a.agent_key); renderRoster(); };
        D.rosterBody.appendChild(tr);
    });
}

/* ── VISUAL HUB ── */
async function loadHub() {
    // market feed
    D.tickers.innerHTML = '<div class="hub-empty">Fetching live prices…</div>';
    try {
        const r = await fetch('/market/summary');
        const j = await r.json();
        const m = j.markets || {};
        const keys = Object.keys(m);
        if (!keys.length) { D.tickers.innerHTML = '<div class="hub-empty">No market data.</div>'; }
        else {
            D.tickers.innerHTML = keys.map(k => {
                const d = m[k] || {};
                if (d.last == null) return `<div class="tick dead"><span class="tick-sym">${esc(k)}</span><span class="tick-px">—</span><span class="tick-rng">unavailable</span></div>`;
                // Age and source matter more than the number itself: a price
                // too old for the active style is not one you can trade on.
                const age = d.age_sec == null ? '' : (d.age_sec < 90
                    ? `${Math.round(d.age_sec)}s` : `${Math.round(d.age_sec / 60)}m`);
                const spread = d.spread ? ` · sp ${d.spread}` : '';
                const stale = d.tradeable === false;
                return `<div class="tick${stale ? ' stale' : ''}" title="${esc(d.freshness || '')}">` +
                       `<span class="tick-sym">${esc(k)}</span>` +
                       `<span class="tick-px">${d.last}</span>` +
                       `<span class="tick-rng">${esc(d.source || '')} ${age}${spread}` +
                       `${stale ? ' · STALE' : ''}</span></div>`;
            }).join('');
        }
    } catch (_) { D.tickers.innerHTML = '<div class="hub-empty">Market feed unreachable.</div>'; }

    // account
    try {
        const j = await (await fetch('/status')).json();
        const pnl = j.daily_pnl ?? 0;
        const cls = pnl > 0 ? 'up' : pnl < 0 ? 'down' : '';
        D.acctStats.innerHTML =
            row('Mode', (j.trading_mode || 'paper').toUpperCase()) +
            row('Style', `${(j.style || 'day').toUpperCase()} · ${j.style_profile?.timeframe || ''}`) +
            row('Price feed', (j.quote_source || 'public').toUpperCase(),
                j.quote_source === 'broker' ? 'good' : '') +
            row('Kill switch', j.kill_switch_active ? 'ACTIVE' : 'released') +
            row('Open positions', j.open_positions ?? 0) +
            row('Daily P&L', `<span class="${cls}">${pnl >= 0 ? '+' : ''}${Number(pnl).toFixed(2)}</span>`);
    } catch (_) { D.acctStats.innerHTML = '<div class="hub-empty">Status unreachable.</div>'; }

    // positions
    try {
        const j = await (await fetch('/trades')).json();
        const p = j.positions || [];
        if (!p.length) D.positions.innerHTML = '<div class="hub-empty">No open positions.</div>';
        else D.positions.innerHTML =
            '<table class="mini-table"><thead><tr><th>SYMBOL</th><th>SIDE</th><th>SIZE</th><th>ENTRY</th><th>OPENED</th></tr></thead><tbody>' +
            p.map(x => `<tr><td>${esc(x.symbol)}</td><td>${esc(x.direction||x.side||'')}</td><td>${x.size}</td><td>${x.entry_price}</td><td>${esc(String(x.opened_at||'').slice(0,19))}</td></tr>`).join('') +
            '</tbody></table>';
    } catch (_) { D.positions.innerHTML = '<div class="hub-empty">Book unreachable.</div>'; }

    // audit
    try {
        const j = await (await fetch('/audit?limit=12')).json();
        const rec = j.records || [];
        if (!rec.length) D.auditList.innerHTML = '<div class="hub-empty">No events yet.</div>';
        else D.auditList.innerHTML = rec.map(x =>
            `<div class="audit-row"><span class="a-ts">${esc(String(x.timestamp||'').slice(11,19))}</span>` +
            `<span class="a-kind">${esc(x.action_type||'')}</span>` +
            `<span class="a-det">${esc(x.detail||'')}</span></div>`).join('');
    } catch (_) { D.auditList.innerHTML = '<div class="hub-empty">Audit unreachable.</div>'; }
}

function row(k, v) { return `<div class="stat-row"><span>${k}</span><span>${v}</span></div>`; }

async function runBacktest() {
    const sym = D.btSymbol.value, tf = D.btTf.value;
    D.btResult.innerHTML = '<div class="hub-empty">Replaying candles…</div>';
    try {
        const r = await fetch(`/backtest?symbol=${encodeURIComponent(sym)}&timeframe=${encodeURIComponent(tf)}`, { method: 'POST' });
        const j = await r.json();
        if (!j.ok) { D.btResult.innerHTML = `<div class="hub-empty">${esc(j.error || 'Backtest failed.')}</div>`; return; }
        const x = j.result;
        const pf = x.profit_factor === null || !isFinite(x.profit_factor) ? '∞' : x.profit_factor;
        D.btResult.innerHTML =
            '<div class="bt-stats">' +
            stat('Trades', x.total_trades) +
            stat('Win rate', x.win_rate + '%', x.win_rate >= 50 ? 'up' : 'down') +
            stat('Net P&L', (x.total_pnl >= 0 ? '+' : '') + x.total_pnl, x.total_pnl >= 0 ? 'up' : 'down') +
            stat('Profit factor', pf) +
            stat('Max DD', x.max_drawdown, 'down') +
            stat('W / L', `${x.wins} / ${x.losses}`) +
            '</div>';
    } catch (e) { D.btResult.innerHTML = '<div class="hub-empty">Backtest unreachable.</div>'; }
}

function stat(k, v, cls) { return `<div class="bt-stat"><div class="k">${k}</div><div class="v ${cls||''}">${v}</div></div>`; }

/* ── GESTURE deck ── */
const GESTURES = [
    { ico:'📊', name:'Market read',      desc:'Ask for a full multi-timeframe read of its market.',
      msg:'Give me your current market read: structure, momentum and the key levels you are watching.' },
    { ico:'🎯', name:'Propose a trade',  desc:'Request a setup with entry, stop loss and take profit.',
      msg:'Propose one trade now. Include symbol, direction, entry, stop loss, take profit, lot size and your reasoning.' },
    { ico:'🛡️', name:'Risk check',       desc:'Validate a 1.0 lot idea against the current limits.',
      msg:'Run a risk check on a 1.0 lot position. Confirm drawdown headroom, size limit and concurrent trade count, then APPROVE or REJECT.' },
    { ico:'📉', name:'Session review',   desc:'Summarise performance and where it went wrong.',
      msg:'Review the session so far: win rate, average win and loss, drawdown, and the two changes you would make.' },
    { ico:'📰', name:'Macro brief',      desc:'What macro events matter in the next few hours.',
      msg:'What macro events or data releases matter for the next few hours, and how would you position around them?' },
    { ico:'⚖️', name:'Second opinion',   desc:'Challenge the last proposal on the desk.',
      msg:'Challenge the most recent trade proposal. What is the strongest argument against taking it?' },
    { ico:'🧾', name:'Status report',    desc:'Short standup-style status from this agent.',
      msg:'Give me a short status report: what you are tracking, your current bias, and anything blocking you.' },
    { ico:'🚨', name:'Stand down',       desc:'Tell it to stop opening new risk.',
      msg:'Stand down. Do not open new risk. Confirm what you are doing with anything already open.' },
];

function renderGestures() {
    const a = S.active ? S.byKey[S.active] : null;
    D.gestureTarget.textContent = a
        ? `sending to ${a.name}`
        : 'no active agent — press E on a desk first';

    D.gestureGrid.innerHTML = '';
    GESTURES.forEach(g => {
        const b = document.createElement('button');
        b.className = 'gesture-card';
        b.disabled = !a || S.streaming;
        b.innerHTML = `<div class="g-ico">${g.ico}</div><div class="g-name">${g.name}</div><div class="g-desc">${esc(g.desc)}</div>`;
        b.onclick = () => {
            if (!S.active) return;
            switchTab('town');
            D.chatInput.value = g.msg;
            send();
        };
        D.gestureGrid.appendChild(b);
    });
}

/* ═══════════════════════════════════════════════════════════
   UTILS
   ═══════════════════════════════════════════════════════════ */
function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
function hash(s) { let h = 0; for (let i = 0; i < s.length; i++) h = (h*31 + s.charCodeAt(i)) & 0xffff; return h; }
function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

document.addEventListener('DOMContentLoaded', init);
})();
