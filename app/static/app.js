// === DOM References ===
const agentGrid = document.getElementById('agentGrid');
const chatPanel = document.getElementById('chatPanel');
const chatAgentName = document.getElementById('chatAgentName');
const chatMessages = document.getElementById('chatMessages');
const chatInput = document.getElementById('chatInput');
const sendBtn = document.getElementById('sendBtn');
const micBtn = document.getElementById('micBtn');
const terminateBtn = document.getElementById('terminateBtn');
const killSwitchBtn = document.getElementById('killSwitchBtn');
const langToggle = document.getElementById('langToggle');
const modeBadge = document.getElementById('modeBadge');
const pageTitle = document.getElementById('pageTitle');

// === State ===
let selectedAgent = null;
let lang = 'en';
let killSwitchActive = false;
let ws = null;
let reconnectTimer = null;

// === I18N ===
const I18N = {
    en: {
        title: 'Agent Town — Trading System',
        killSwitch: '⛔ KILL SWITCH',
        killSwitchActive: '✅ RESUME',
        send: 'Send',
        placeholder: 'Type a message...',
        terminate: '🛑 Terminate',
        paper: 'PAPER',
        live: 'LIVE',
        noAgents: 'No agents available',
        micListening: '🎤 Listening...',
        micReady: '🎤',
        terminated: 'Agent terminated.',
        killActivated: 'Kill switch activated! All trading halted.',
        killDeactivated: 'Kill switch deactivated. Trading resumed.',
    },
    bn: {
        title: 'এজেন্ট টাউন — ট্রেডিং সিস্টেম',
        killSwitch: '⛔ কিল সুইচ',
        killSwitchActive: '✅ পুনরায় শুরু',
        send: 'পাঠান',
        placeholder: 'একটি বার্তা লিখুন...',
        terminate: '🛑 বন্ধ করুন',
        paper: 'পেপার',
        live: 'লাইভ',
        noAgents: 'কোনো এজেন্ট নেই',
        micListening: '🎤 শুনছি...',
        micReady: '🎤',
        terminated: 'এজেন্ট বন্ধ করা হয়েছে।',
        killActivated: 'কিল সুইচ সক্রিয়! সমস্ত ট্রেডিং বন্ধ।',
        killDeactivated: 'কিল সুইচ নিষ্ক্রিয়। ট্রেডিং পুনরায় চালু।',
    }
};

function t(key) {
    return I18N[lang][key] || I18N['en'][key] || key;
}

// === Agent Loading ===
// Emoji map for agent roles
const AGENT_EMOJIS = {
    manager: '👔',
    super_trader: '📈',
    risk_manager: '🛡️',
    computer_scientist: '💻',
    trader_bot: '🤖',
};

async function loadAgents() {
    try {
        const res = await fetch('/agents');
        if (!res.ok) throw new Error('Failed to fetch agents');
        const data = await res.json();
        // Backend returns {ok: true, agents: [...]} with {agent_key, name, role, status}
        const agents = (data.agents || []).map(a => ({
            key: a.agent_key,
            name: a.name,
            status: a.status || 'idle',
            emoji: AGENT_EMOJIS[a.role] || '🤖',
        }));
        renderAgentGrid(agents);
    } catch (err) {
        console.error('loadAgents error:', err);
        agentGrid.innerHTML = '<p>Loading agents...</p>';
    }
}

function renderAgentGrid(agents) {
    agentGrid.innerHTML = '';
    if (!agents || agents.length === 0) {
        agentGrid.innerHTML = `<p>${t('noAgents')}</p>`;
        return;
    }
    agents.forEach(agent => {
        const card = createAgentCard(agent);
        agentGrid.appendChild(card);
    });
}

function createAgentCard(agent) {
    const card = document.createElement('div');
    card.className = 'agent-card';
    card.dataset.key = agent.key;

    if (selectedAgent === agent.key) {
        card.classList.add('selected');
    }

    const status = agent.status || 'offline';
    const emoji = agent.emoji || '🤖';
    const name = agent.name || agent.key;

    card.innerHTML = `
        <div class="status-dot ${status}"></div>
        <div class="agent-avatar">${emoji}</div>
        <div class="agent-name">${name}</div>
    `;

    card.addEventListener('click', () => openChat(agent.key, name));
    return card;
}

// === Chat ===
function openChat(agentKey, agentName) {
    selectedAgent = agentKey;
    chatAgentName.textContent = agentName || agentKey;
    chatMessages.innerHTML = '';
    chatPanel.classList.remove('hidden');

    // Highlight selected card
    document.querySelectorAll('.agent-card').forEach(card => {
        card.classList.toggle('selected', card.dataset.key === agentKey);
    });

    chatInput.focus();
}

function closeChat() {
    selectedAgent = null;
    chatPanel.classList.add('hidden');
    document.querySelectorAll('.agent-card').forEach(card => {
        card.classList.remove('selected');
    });
}

async function sendMessage() {
    const message = chatInput.value.trim();
    if (!message || !selectedAgent) return;

    appendMessage(message, 'user');
    chatInput.value = '';

    try {
        const res = await fetch('/agent/message', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                agent_key: selectedAgent,
                message: message,
                lang: lang
            })
        });

        if (!res.ok) throw new Error('Message send failed');
        const data = await res.json();
        const reply = data.reply || data.response || data.message || 'No response.';
        appendMessage(reply, 'agent');
        speakText(reply, lang);
    } catch (err) {
        console.error('sendMessage error:', err);
        appendMessage('⚠️ Error communicating with agent.', 'agent');
    }
}

function appendMessage(text, type) {
    const msg = document.createElement('div');
    msg.className = `message ${type}`;
    msg.textContent = text;
    chatMessages.appendChild(msg);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

// === Speech Recognition (Mic) ===
let recognition = null;

function initSpeechRecognition() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
        console.warn('SpeechRecognition not supported');
        micBtn.disabled = true;
        return;
    }

    recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = false;

    recognition.onresult = (event) => {
        const transcript = event.results[0][0].transcript;
        chatInput.value = transcript;
        sendMessage();
    };

    recognition.onstart = () => {
        micBtn.classList.add('listening');
        micBtn.textContent = t('micListening');
    };

    recognition.onend = () => {
        micBtn.classList.remove('listening');
        micBtn.textContent = t('micReady');
    };

    recognition.onerror = (event) => {
        console.error('Speech recognition error:', event.error);
        micBtn.classList.remove('listening');
        micBtn.textContent = t('micReady');
    };
}

function toggleMic() {
    if (!recognition) return;
    if (micBtn.classList.contains('listening')) {
        recognition.stop();
    } else {
        recognition.lang = lang === 'bn' ? 'bn-BD' : 'en-US';
        recognition.start();
    }
}

// === Text-to-Speech ===
function speakText(text, language) {
    if (!window.speechSynthesis) return;
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = language === 'bn' ? 'bn-BD' : 'en-US';
    utterance.rate = 1;
    utterance.pitch = 1;
    window.speechSynthesis.speak(utterance);
}

// === Kill Switch ===
async function toggleKillSwitch() {
    killSwitchActive = !killSwitchActive;

    try {
        await fetch('/kill-switch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ activate: killSwitchActive })
        });
    } catch (err) {
        console.error('Kill switch error:', err);
    }

    updateKillSwitchUI();

    if (killSwitchActive) {
        appendMessage(t('killActivated'), 'agent');
    } else {
        appendMessage(t('killDeactivated'), 'agent');
    }
}

function updateKillSwitchUI() {
    if (killSwitchActive) {
        killSwitchBtn.textContent = t('killSwitchActive');
        killSwitchBtn.classList.add('active');
    } else {
        killSwitchBtn.textContent = t('killSwitch');
        killSwitchBtn.classList.remove('active');
    }
}

// === Terminate Agent ===
async function terminateAgent() {
    if (!selectedAgent) return;

    try {
        await fetch('/agent/terminate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agent_key: selectedAgent })
        });
        appendMessage(t('terminated'), 'agent');
    } catch (err) {
        console.error('Terminate error:', err);
    }
}

// === Language Toggle ===
function updateLanguage(newLang) {
    lang = newLang;
    pageTitle.textContent = t('title');
    sendBtn.textContent = t('send');
    chatInput.placeholder = t('placeholder');
    terminateBtn.textContent = t('terminate');
    modeBadge.textContent = t('paper');
    updateKillSwitchUI();
}

// === WebSocket ===
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;

    try {
        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            console.log('WebSocket connected');
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
            }
        };

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                handleWSMessage(data);
            } catch (err) {
                console.error('WS message parse error:', err);
            }
        };

        ws.onclose = () => {
            console.log('WebSocket disconnected, reconnecting in 3s...');
            reconnectTimer = setTimeout(connectWebSocket, 3000);
        };

        ws.onerror = (err) => {
            console.error('WebSocket error:', err);
            ws.close();
        };
    } catch (err) {
        console.error('WebSocket connection error:', err);
        reconnectTimer = setTimeout(connectWebSocket, 3000);
    }
}

function handleWSMessage(data) {
    switch (data.type) {
        case 'init':
            if (data.agents) {
                const agents = data.agents.map(a => ({
                    key: a.agent_key,
                    name: a.name,
                    status: a.status || 'idle',
                    emoji: AGENT_EMOJIS[a.role] || '🤖',
                }));
                renderAgentGrid(agents);
            }
            break;

        case 'agent_status':
            updateAgentStatus(data.agent_key, data.status);
            break;

        default:
            console.log('Unknown WS message type:', data.type);
    }
}

function updateAgentStatus(agentKey, status) {
    const card = document.querySelector(`.agent-card[data-key="${agentKey}"]`);
    if (!card) return;

    const dot = card.querySelector('.status-dot');
    if (dot) {
        dot.className = `status-dot ${status}`;
    }
}

// === Event Listeners ===
sendBtn.addEventListener('click', sendMessage);

chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
        e.preventDefault();
        sendMessage();
    }
});

micBtn.addEventListener('click', toggleMic);
killSwitchBtn.addEventListener('click', toggleKillSwitch);
terminateBtn.addEventListener('click', terminateAgent);

langToggle.addEventListener('change', (e) => {
    updateLanguage(e.target.value);
});

// === Init ===
document.addEventListener('DOMContentLoaded', () => {
    initSpeechRecognition();
    loadAgents();
    connectWebSocket();
    updateLanguage(lang);
});
