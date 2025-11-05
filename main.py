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
                # Criamos um "lock" para garantir que não tentamos modificar
                # current_sentence a partir de duas tarefas ao mesmo tempo
                text_lock = asyncio.Lock()

                async def finalize_and_queue():
                    """
                    Pega no texto atual, envia-o para processamento e limpa o buffer.
                    """
                    nonlocal current_sentence, last_partial_text
                    final_text = ""
                    async with text_lock:
                        # Prioriza a frase completa. Se vazia, usa o último parcial.
                        final_text = current_sentence.strip() or last_partial_text.strip()
                        if final_text:
                            print(f"\n[QUEUED from user (chunk)]: {final_text}")
                            await processing_queue.put(final_text)
                        
                        # CRÍTICO: Limpa a frase para o próximo chunk
                        current_sentence = ""
                        last_partial_text = "" # Limpa também para evitar dados velhos

                async def audio_sender():
                    """Envia áudio do cliente para a Speechmatics. (Sem alterações)"""
                    while True:
                        audio_chunk = await audio_stream.get()
                        if audio_chunk is None: break
                        if sm_ws.closed: break
                        await sm_ws.send_bytes(audio_chunk)
                    if not sm_ws.closed: await sm_ws.send_str(json.dumps({"message": "EndOfStream"}))

                async def transcript_receiver():
                    """Recebe transcrições da Speechmatics. (Modificado para usar lock)"""
                    nonlocal current_sentence, last_partial_text
                    async for msg in sm_ws:
                        if msg.type != aiohttp.WSMsgType.TEXT: continue
                        data = json.loads(msg.data); msg_type = data.get("message")
                        
                        async with text_lock: # Protege o acesso ao texto
                            if msg_type == "AddPartialTranscript":
                                results = data.get("results", [])
                                if results and results[0].get("alternatives"):
                                    partial = results[0]["alternatives"][0].get("content", "")
                                    # O display é a frase deste chunk + o parcial novo
                                    display_text = f"{current_sentence} {partial}".strip()
                                    last_partial_text = display_text # Guarda para o caso de finalização
                                    await speaker_ws.send_json({"type": "partial_transcript", "text": display_text})
                            
                            elif msg_type == "AddTranscript":
                                results = data.get("results", [])
                                for res in results:
                                    if res.get("alternatives"): 
                                        current_sentence += res["alternatives"][0].get("content", "") + " "
                
                async def event_watcher():
                    """Espera pelo 'mouseup' (end_speech_event) para finalizar o último pedaço."""
                    await end_speech_event.wait()
                    print("\n[EVENT]: MouseUp detectado, finalizando último chunk...")
                    await finalize_and_queue() # Envia o que sobrou
                    # Limpa o texto "Speaking: ..." do ecrã do utilizador
                    try:
                        await speaker_ws.send_json({"type": "partial_transcript", "text": ""})
                    except Exception:
                        pass # O user pode ter desconectado
                    end_speech_event.clear() # CRÍTICO: Reseta o evento para a próxima fala

                # --- AQUI ESTÁ A NOVA LÓGICA DE CHUNKING ---
                async def periodic_finalizer(interval_seconds: int):
                    """
                    Nova tarefa: finaliza o texto a cada N segundos, 
                    simulando os "chunks" que você pediu.
                    """
                    while True:
                        await asyncio.sleep(interval_seconds)
                        # Se o utilizador soltou o botão, a event_watcher tratará disso.
                        if end_speech_event.is_set():
                            await asyncio.sleep(0.1) # Dá tempo à event_watcher para limpar
                            continue # Volta ao início do loop e espera
                        
                        # Se o utilizador ainda está a falar, finaliza o chunk atual
                        print(f"\n[TIMER]: {interval_seconds}s atingido, finalizando chunk...")
                        await finalize_and_queue()


                # Inicia todas as tarefas, incluindo o novo finalizador periódico
                await asyncio.gather(
                    audio_sender(), 
                    transcript_receiver(), 
                    event_watcher(),
                    periodic_finalizer(interval_seconds=5) # <-- A LÓGICA DE 3 SEGUNDOS
                )
                
                # Limpeza final caso algo tenha sobrado (ex: desconexão abrupta)
                await finalize_and_queue()

    except Exception:
        print(f"--- !!! CRITICAL ERROR IN STT TASK !!! ---"); traceback.print_exc()
        try: await speaker_ws.send_json({"type": "system_error", "message": "Connection to speech-to-text service failed."})
        except Exception: pass

async def stream_translation_and_tts(text_to_translate: str, source_lang: str, target_conn: UserConnection, speaker_id: str):
    """
    1. Faz streaming da tradução de TEXTO para o cliente de destino (para legendas ao vivo).
    2. Acumula o texto completo.
    3. Envia o texto completo para o gerador de ÁUDIO TTS (camb_ai_tts_generator).
    """
    headers = {"x-api-key": CAMB_API_KEY, "Content-Type": "application/json"}
    
    # --- A CORREÇÃO ESTÁ AQUI ---
    # Convertemos os códigos de idioma (ex: "pt") para os IDs numéricos (ex: 111)
    source_language_id = LANGUAGE_ID_MAP.get(source_lang)
    target_language_id = LANGUAGE_ID_MAP.get(target_conn.lang)

    # Se algum dos idiomas não for encontrado no mapa, aborta para evitar erros.
    if not source_language_id or not target_language_id:
        print(f"Erro: Códigos de idioma inválidos. Fonte: {source_lang}, Destino: {target_conn.lang}")
        return
    
    payload = {
        "text": text_to_translate,
        "source_language": source_language_id, # <- Corrigido
        "target_language": target_language_id  # <- Corrigido
    }
    # --- FIM DA CORREÇÃO ---
    
    full_translated_text = ""
    
    try:
        async with aiohttp.ClientSession() as session:
            # Usamos o novo endpoint de streaming
            async with session.post(f"{CAMB_TTS_BASE_URL}/translation/stream", json=payload, headers=headers) as r:
                r.raise_for_status() # Isto irá agora falhar se o payload estiver errado
                
                # Fazemos o streaming da resposta
                async for chunk in r.content.iter_any():
                    if chunk:
                        decoded_chunk = chunk.decode('utf-8')
                        full_translated_text += decoded_chunk
                        
                        # Envia a tradução parcial para legendas ao vivo
                        await target_conn.ws.send_json({
                            "type": "partial_translation",
                            "speaker": speaker_id,
                            "text": full_translated_text
                        })

        # O Streaming terminou. Agora temos o texto completo.
        # Envia a mensagem final (para o histórico de chat)
        await target_conn.ws.send_json({
            "type": "translated_message",
            "speaker": speaker_id,
            "text": full_translated_text
        })

        # E agora, gera o ÁUDIO com o texto completo
        if full_translated_text:
            print(f"Sending full text '{full_translated_text}' to TTS for lang '{target_conn.lang}'")
            await camb_ai_tts_generator(full_translated_text, target_conn.lang, target_conn.ws)
            
    except Exception as e:
        # A excepção original (que causa o seu erro) será impressa aqui no terminal
        print(f"Error during streaming translation or TTS: {e}")
        traceback.print_exc()
        try:
            await target_conn.ws.send_json({
                "type": "system_error", 
                "message": f"Failed to get translation/audio for speaker {speaker_id}."
            })
        except Exception:
            pass # A ligação pode já estar fechada


async def tts_worker_task(processing_queue: asyncio.Queue, session_id: str, user_id: str, lang: str):
    """
    Esta é a nova versão da tts_worker_task.
    Ela chama a nova função stream_translation_and_tts.
    """
    while True:
        try:
            final_text = await processing_queue.get()
            if final_text is None: break
            
            # 1. Envia a transcrição final para o PRÓPRIO utilizador
            if session_id in manager.sessions and user_id in manager.sessions[session_id]:
                await manager.sessions[session_id][user_id].ws.send_json({
                    "type": "my_final_transcription", 
                    "text": final_text
                })
            else:
                continue # O utilizador desconectou-se entretanto

            # 2. Faz o streaming da tradução E do áudio para os OUTROS utilizadores
            for other_user_id, conn in manager.sessions.get(session_id, {}).items():
                if other_user_id != user_id:
                    # Inicia a nova tarefa de streaming para cada ouvinte
                    asyncio.create_task(stream_translation_and_tts(
                        text_to_translate=final_text,
                        source_lang=lang,
                        target_conn=conn,
                        speaker_id=user_id
                    ))
                    
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
                    print(f"Received end_of_speech from {user_id}")
                    
                    # 1. Dispara o evento para finalizar o TEXTO
                    end_speech_event.set()
                    
                    # 2. [A CORREÇÃO] Drena a fila de ÁUDIO.
                    print(f"Draining audio queue for {user_id} to prevent accumulation...")
                    drained_count = 0
                    while not audio_queue.empty():
                        audio_queue.get_nowait() # Retira item sem esperar
                        drained_count += 1
                    if drained_count > 0:
                        print(f"Drained {drained_count} late audio packets.")

            elif "bytes" in data:
                # 3. Só adiciona áudio à fila se NÃO estivermos no
                #    processo de "parar de falar".
                if not end_speech_event.is_set():
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