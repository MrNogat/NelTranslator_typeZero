import os
import json
import asyncio
import socket
import platform
import traceback
from typing import Dict, Any
from pathlib import Path

import aiohttp
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# --- Absolute Path Configuration ---
SCRIPT_DIR = Path(__file__).parent.resolve()
STATIC_DIR = SCRIPT_DIR / "static"

# --- Main Configuration ---
load_dotenv()
app = FastAPI()
SPEECHMATICS_API_KEY = os.getenv("SPEECHMATICS_API_KEY")
CAMB_API_KEY = os.getenv("CAMB_API_KEY")
CAMB_VOICE_ID = int(os.getenv("CAMB_VOICE_ID", "20299"))

# ==================== THE CRITICAL URL FIX ====================
# This is the correct WebSocket URL for the Real-Time API, as used in your original script.
SPEECHMATICS_URL = "wss://eu2.rt.speechmatics.com/v2"
# =============================================================

CAMB_TTS_BASE_URL = "https://client.camb.ai/apis"
LANGUAGE_ID_MAP = { "en": 1, "pt": 111, "es": 58, "fr": 76, "de": 31, "it": 87, "ja": 88, "ko": 94, "ru": 114, "zh": 139 }

# --- Connection Management ---
class UserConnection:
    def __init__(self, websocket: WebSocket, lang: str): self.ws = websocket; self.lang = lang
class ConnectionManager:
    def __init__(self): self.sessions: Dict[str, Dict[str, UserConnection]] = {}
    async def connect(self, ws: WebSocket, session_id: str, user_id: str, lang: str):
        await ws.accept()
        if session_id not in self.sessions: self.sessions[session_id] = {}
        self.sessions[session_id][user_id] = UserConnection(ws, lang)
        print(f"User '{user_id}' ({lang}) connected to session '{session_id}'.")
    def disconnect(self, session_id: str, user_id: str):
        if session_id in self.sessions and user_id in self.sessions[session_id]:
            del self.sessions[session_id][user_id]
            if not self.sessions[session_id]: del self.sessions[session_id]
        print(f"User '{user_id}' disconnected from session '{session_id}'.")
    async def broadcast_json(self, session_id: str, payload: Dict, sender_id: str):
        if session_id in self.sessions:
            for user_id, conn in self.sessions[session_id].items():
                if user_id != sender_id:
                    try: await conn.ws.send_json(payload)
                    except Exception: print(f"Could not send broadcast to {user_id}; connection closed.")
manager = ConnectionManager()

# --- Core Logic: TTS and STT Services ---
async def camb_ai_tts_generator(text: str, lang_code: str, target_ws: WebSocket):
    language_id = LANGUAGE_ID_MAP.get(lang_code); headers = {"x-api-key": CAMB_API_KEY, "Content-Type": "application/json"}
    if not language_id: return
    async with aiohttp.ClientSession() as session:
        try:
            payload = {"text": text, "voice_id": CAMB_VOICE_ID, "language": language_id}
            async with session.post(f"{CAMB_TTS_BASE_URL}/tts", json=payload, headers=headers) as r: r.raise_for_status(); d = await r.json(); task_id = d.get("task_id")
            if not task_id: return
            run_id = None
            for _ in range(45):
                async with session.get(f"{CAMB_TTS_BASE_URL}/tts/{task_id}", headers={"x-api-key": CAMB_API_KEY}) as r: r.raise_for_status(); s = await r.json()
                if s.get("status") == "SUCCESS": run_id = s.get("run_id"); break
                if s.get("status") == "FAILED": return
                await asyncio.sleep(0.5)
            if not run_id: print("TTS Job timed out."); return
            async with session.get(f"{CAMB_TTS_BASE_URL}/tts-result/{run_id}", headers={"x-api-key": CAMB_API_KEY}) as r: r.raise_for_status(); await target_ws.send_bytes(await r.read())
        except Exception as e: print(f"TTS generation failed: {e}")

async def speechmatics_processor(audio_stream: asyncio.Queue, processing_queue: asyncio.Queue, speaker_ws: WebSocket, lang: str, end_speech_event: asyncio.Event):
    config = {"message": "StartRecognition", "audio_format": {"type": "raw", "encoding": "pcm_s16le", "sample_rate": 16000}, "transcription_config": {"language": lang, "enable_partials": True}}
    headers = {"Authorization": f"Bearer {SPEECHMATICS_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"{SPEECHMATICS_URL}/{lang}", headers=headers) as sm_ws:
                await sm_ws.send_str(json.dumps(config))
                
                current_sentence = ""; last_partial_text = ""

                async def finalize_and_queue():
                    nonlocal current_sentence, last_partial_text
                    final_text = current_sentence.strip() or last_partial_text.strip()
                    if final_text:
                        print(f"\n[QUEUED from user]: {final_text}")
                        await processing_queue.put(final_text)
                    current_sentence = ""; last_partial_text = ""

                async def audio_sender():
                    while True:
                        audio_chunk = await audio_stream.get()
                        if audio_chunk is None: break
                        if sm_ws.closed: break
                        await sm_ws.send_bytes(audio_chunk)
                    if not sm_ws.closed: await sm_ws.send_str(json.dumps({"message": "EndOfStream"}))

                async def transcript_receiver():
                    nonlocal current_sentence, last_partial_text
                    async for msg in sm_ws:
                        if msg.type != aiohttp.WSMsgType.TEXT: continue
                        data = json.loads(msg.data); msg_type = data.get("message")
                        if msg_type == "AddPartialTranscript":
                            results = data.get("results", [])
                            if results and results[0].get("alternatives"):
                                partial = results[0]["alternatives"][0].get("content", "")
                                display_text = f"{current_sentence} {partial}".strip()
                                last_partial_text = display_text
                                await speaker_ws.send_json({"type": "partial_transcript", "text": display_text})
                        elif msg_type == "AddTranscript":
                            results = data.get("results", [])
                            for res in results:
                                if res.get("alternatives"): current_sentence += res["alternatives"][0].get("content", "") + " "
                
                async def event_watcher():
                    while True:
                        await end_speech_event.wait()
                        end_speech_event.clear()
                        await finalize_and_queue()

                await asyncio.gather(audio_sender(), transcript_receiver(), event_watcher())
                await finalize_and_queue()
    except Exception:
        print(f"--- !!! CRITICAL ERROR IN STT TASK !!! ---"); traceback.print_exc()
        try: await speaker_ws.send_json({"type": "system_error", "message": "Connection to speech-to-text service failed."})
        except Exception: pass

async def tts_worker_task(processing_queue: asyncio.Queue, session_id: str, user_id: str, lang: str):
    while True:
        try:
            final_text = await processing_queue.get()
            if final_text is None: break
            await manager.sessions[session_id][user_id].ws.send_json({"type": "my_final_transcription", "text": final_text})
            for other_user_id, conn in manager.sessions.get(session_id, {}).items():
                if other_user_id != user_id:
                    translated_text = f"(Translated from {lang}) {final_text}"
                    await conn.ws.send_json({"type": "translated_message", "speaker": user_id, "text": translated_text})
                    await camb_ai_tts_generator(translated_text, conn.lang, conn.ws)
        except Exception:
             print(f"--- ERROR IN TTS WORKER for user {user_id} ---"); traceback.print_exc()

# --- API Endpoints & Main Loop ---
@app.get("/")
async def get(): return FileResponse(STATIC_DIR / "index.html")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.websocket("/ws/{session_id}/{user_id}/{lang}")
async def websocket_endpoint(websocket: WebSocket, session_id: str, user_id: str, lang: str):
    await manager.connect(websocket, session_id, user_id, lang)
    await manager.broadcast_json(session_id, {"type": "user_joined", "user_id": user_id}, sender_id=user_id)
    
    audio_queue = asyncio.Queue()
    processing_queue = asyncio.Queue()
    end_speech_event = asyncio.Event()
    
    stt_task = asyncio.create_task(speechmatics_processor(audio_queue, processing_queue, websocket, lang, end_speech_event))
    tts_task = asyncio.create_task(tts_worker_task(processing_queue, session_id, user_id, lang))

    try:
        while True:
            data = await websocket.receive()
            if "text" in data:
                message = json.loads(data["text"])
                if message.get("type") == "end_of_speech":
                    end_speech_event.set()
            elif "bytes" in data:
                await audio_queue.put(data["bytes"])
    except WebSocketDisconnect:
        print(f"Client {user_id} disconnected gracefully.")
    except Exception:
        print(f"--- !!! UNEXPECTED ERROR IN MAIN LOOP FOR USER {user_id} !!! ---"); traceback.print_exc()
    finally:
        print(f"Cleaning up for user {user_id}...")
        await audio_queue.put(None)
        await processing_queue.put(None)
        if not stt_task.done(): stt_task.cancel()
        if not tts_task.done(): tts_task.cancel()
        manager.disconnect(session_id, user_id)
        await manager.broadcast_json(session_id, {"type": "user_left", "user_id": user_id}, sender_id=user_id)
        print(f"Cleanup complete for user {user_id}.")

# --- Server Startup ---
def get_network_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(('10.255.255.255', 1)); IP = s.getsockname()[0]
    except Exception: IP = '127.0.0.1'
    finally: s.close()
    return IP
if __name__ == "__main__":
    PORT = 5005; network_ip = get_network_ip()
    print("=================================================================")
    print(f"    Starting Secure Server (HTTPS) on a {platform.system()} machine...")
    print(f"-> Local:    https://localhost:{PORT}")
    print(f"-> Network:  https://{network_ip}:{PORT}")
    print("!!! BROWSER WARNING: You must accept the 'Not Secure' warning to proceed. !!!")
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True, ssl_keyfile="./key.pem", ssl_certfile="./cert.pem")