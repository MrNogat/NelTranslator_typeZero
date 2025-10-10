// static/app.js
const connectBtn = document.getElementById('connect-button');
const pttBtn = document.getElementById('ptt-button');
const statusDiv = document.getElementById('status');
const messagesDiv = document.getElementById('messages');
const languageSelect = document.getElementById('language');

let ws = null;
let audioContext = null;
let audioProcessor = null;
let mediaStreamSource = null;
let currentAudioSource = null;
const playbackQueue = [];
let isPlayingAudio = false;
const SAMPLE_RATE = 16000;

connectBtn.onclick = () => {
    if (ws) { ws.close(); return; }
    const sessionId = document.getElementById('session_id').value;
    const userId = document.getElementById('user_id').value;
    const lang = languageSelect.value;
    if (!sessionId || !userId) { alert("Please enter Session ID and User Name."); return; }
    
    const wsUrl = `wss://${window.location.host}/ws/${sessionId}/${userId}/${lang}`;
    ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';
    setupWebSocketHandlers();
};

function setupWebSocketHandlers() {
    ws.onopen = () => {
        statusDiv.textContent = `Status: Connected`;
        connectBtn.textContent = "Disconnect";
        pttBtn.disabled = false;
        initializeMicrophone();
    };
    ws.onmessage = async (event) => {
        if (event.data instanceof ArrayBuffer) {
            playbackQueue.push(event.data);
            playbackWorker();
            return;
        }
        const data = JSON.parse(event.data);
        switch (data.type) {
            case 'user_joined':
                addMessage(`System: User '${data.user_id}' has joined.`, 'system-message');
                break;
            case 'partial_transcript':
                document.getElementById('live-transcript').textContent = `Speaking: ${data.text}`;
                break;
            case 'my_final_transcription':
                document.getElementById('live-transcript').textContent = '';
                addMessage(`You: ${data.text}`, 'my-message');
                break;
            case 'translated_message':
                addMessage(`[${data.speaker}]: ${data.text}`, 'other-message');
                break;
            case 'user_left':
                addMessage(`System: User '${data.user_id}' has left.`, 'system-message');
                break;
            case 'system_error':
                addMessage(`SYSTEM ERROR: ${data.message}`, 'system-message');
                break;
        }
    };
    ws.onclose = () => {
        statusDiv.textContent = "Status: Disconnected";
        connectBtn.textContent = "Join Session";
        pttBtn.disabled = true;
        pttBtn.classList.remove('active');
        if (audioContext && audioContext.state !== 'closed') {
            audioContext.close();
            audioContext = null;
        }
        ws = null;
    };
    ws.onerror = (error) => {
        console.error("WebSocket Error:", error);
        statusDiv.textContent = "Status: Error. Check console.";
    };
}

// --- PTT Button Logic (Live Streaming with End Signal) ---
pttBtn.addEventListener('mousedown', () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
        pttBtn.classList.add('active');
        pttBtn.textContent = 'Speaking...';
        if (playbackQueue.length > 0) { playbackQueue.length = 0; }
        if (currentAudioSource) { currentAudioSource.stop(); }
    }
});

function stopSpeaking() {
    if (ws && ws.readyState === WebSocket.OPEN && pttBtn.classList.contains('active')) {
        pttBtn.classList.remove('active');
        pttBtn.textContent = 'Hold to Speak';
        // This is the non-negotiable part: immediately tell the server you are done.
        ws.send(JSON.stringify({ type: 'end_of_speech' }));
    }
}
pttBtn.addEventListener('mouseup', stopSpeaking);
pttBtn.addEventListener('mouseleave', stopSpeaking);

// --- Microphone Logic (Live Streaming) ---
async function initializeMicrophone() {
    try {
        const context = await getAudioContext();
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        mediaStreamSource = context.createMediaStreamSource(stream);
        audioProcessor = context.createScriptProcessor(4096, 1, 1);
        audioProcessor.onaudioprocess = (e) => {
            if (!pttBtn.classList.contains('active')) return;
            const inputData = e.inputBuffer.getChannelData(0);
            const resampledData = resample(inputData, context.sampleRate, SAMPLE_RATE);
            const pcm16 = toPCM16(resampledData);
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(pcm16);
            }
        };
        mediaStreamSource.connect(audioProcessor);
        audioProcessor.connect(context.destination);
    } catch (err) {
        console.error("Error accessing microphone:", err);
        alert(`Error accessing microphone: ${err.message}`);
    }
}

// --- Playback and Utility Functions ---
async function playbackWorker() {
    if (isPlayingAudio || playbackQueue.length === 0) { return; }
    isPlayingAudio = true;
    while (playbackQueue.length > 0) {
        const audioBlob = playbackQueue.shift();
        await playAudio(audioBlob);
    }
    isPlayingAudio = false;
}
async function getAudioContext() {
    if (!audioContext || audioContext.state === 'closed') {
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (audioContext.state === 'suspended') { await audioContext.resume(); }
    return audioContext;
}
function playAudio(arrayBuffer) {
    return new Promise(async (resolve) => {
        try {
            const context = await getAudioContext();
            if (pttBtn.classList.contains('active')) { return resolve(); }
            const audioBuffer = await context.decodeAudioData(arrayBuffer.slice(0));
            currentAudioSource = context.createBufferSource();
            currentAudioSource.buffer = audioBuffer;
            currentAudioSource.connect(context.destination);
            currentAudioSource.onended = () => { currentAudioSource = null; resolve(); };
            currentAudioSource.start(0);
        } catch (err) {
            console.error("Error during audio playback:", err);
            resolve();
        }
    });
}
function addMessage(text, className) {
    const p = document.createElement('p');
    p.textContent = text;
    p.className = className;
    messagesDiv.appendChild(p);
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
}
function resample(audioData, fromSampleRate, toSampleRate) {
    if (fromSampleRate === toSampleRate) { return audioData; }
    const ratio = fromSampleRate / toSampleRate;
    const outputLength = Math.floor(audioData.length / ratio);
    const outputData = new Float32Array(outputLength);
    for (let i = 0; i < outputLength; i++) {
        outputData[i] = audioData[Math.round(i * ratio)];
    }
    return outputData;
}
function toPCM16(input) {
    const buffer = new ArrayBuffer(input.length * 2);
    const view = new DataView(buffer);
    for (let i = 0; i < input.length; i++) {
        const s = Math.max(-1, Math.min(1, input[i]));
        view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    }
    return buffer;
}