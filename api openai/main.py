from fastapi import FastAPI, WebSocket, WebSocketDisconnect,Depends
import asyncio
import json
import base64
import logging
import ssl
import websockets
from typing import Dict, Optional
from dotenv import load_dotenv
import os
import models
from database import Base , declarative_base , Sensionalocal, Engine , Client
from sqlalchemy.orm import Session
from datetime import datetime
from schemas import Conversation
import uuid



app = FastAPI()


from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Sesuaikan dengan frontend
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

Base.metadata.create_all(Engine)



def get_db():

    db= Sensionalocal() 

    try :
        yield db
    finally : 
        db.close()




load_dotenv()

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
 
    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket

    def disconnect(self, client_id: str):
        self.active_connections.pop(client_id, None)

    async def send_message(self, message: str, client_id: str):
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_text(message)

    async def send_base64(self, base64_data: str, client_id: str):
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_json({
                "type": "audio",
                "data": base64_data
            })

manager = ConnectionManager()

class OpenAIRealtimeClient:
    
    def __init__(self, instructions: str, client_id: str, voice: str = "alloy"):
        self.url = "wss://gpt4o-realtime.openai.azure.com/openai/realtime?api-version=2024-10-01-preview&deployment=gpt-4o-realtime-preview"
        self.model = "gpt-4o-realtime-preview-2024-10-01"
        self.api_key = os.getenv("model_key")
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.instructions = instructions
        self.voice = voice
        self.client_id = client_id
        self.audio_buffer = b''
        
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

        self.session_config = {
            "modalities": ["audio", "text"],
            "instructions": self.instructions,
            "voice": self.voice,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "turn_detection": None,
            "input_audio_transcription": {
                "model": "whisper-1"
            },
            "temperature": 0.6
        }

    async def connect(self):
        """Connect to OpenAI WebSocket"""
        logger.info(f"Connecting to OpenAI WebSocket: {self.url}")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "realtime=v1"
        }
        
        self.ws = await websockets.connect(
            f"{self.url}?model={self.model}",
            extra_headers=headers,
            ssl=self.ssl_context
        )
        logger.info("Connected to OpenAI Realtime API")

        await self.send_event({
            "type": "session.update",
            "session": self.session_config
        })
        await self.send_event({"type": "response.create"})

    async def send_event(self, event):
        """Send event to OpenAI WebSocket"""
        if self.ws:
            await self.ws.send(json.dumps(event))
            logger.debug(f"Event sent - type: {event['type']}")

    async def handle_openai_messages(self):
        """Handle messages from OpenAI WebSocket"""
        try:
            async for message in self.ws:
                event = json.loads(message)
                await self.handle_event(event)
        except websockets.ConnectionClosed as e:
            logger.error(f"OpenAI WebSocket connection closed: {e}")
        except Exception as e:
            logger.error(f"Error handling OpenAI messages: {e}")

    async def handle_event(self, event):
        """Handle different event types from OpenAI"""
        event_type = event.get("type")
        
        if event_type == "error":
            await manager.send_message(f"Error: {event['error']['message']}", self.client_id)
        
        elif event_type == "response.audio.delta":
            # Send audio chunks to client
            await manager.send_base64(event["delta"], self.client_id)
        
        elif event_type == "response.text.delta":
            await manager.send_message(event["delta"], self.client_id)
        
        elif event_type == "response.done":
            await manager.send_message("Response completed", self.client_id)

    async def process_audio(self, base64_audio: str):
        """Process received base64 audio"""
        try:
            await self.send_event({
                "type": "input_audio_buffer.append",
                "audio": base64_audio
            })
            await self.send_event({"type": "input_audio_buffer.commit"})
            await self.send_event({"type": "response.create"})
        except Exception as e:
            logger.error(f"Error processing audio: {e}")
            await manager.send_message(f"Error processing audio: {str(e)}", self.client_id)

    async def cleanup(self):
        """Clean up WebSocket connection"""
        if self.ws:
            await self.ws.close()

@app.websocket("/ws/{client_id}/{voice}")
async def websocket_endpoint(websocket: WebSocket, client_id: str , voice:str):
    openai_client = OpenAIRealtimeClient(
        instructions="kamu adalah planner perjalanan tolong pakai bahasa indonesia ",
        client_id=client_id,
        voice=voice
    )
    
    try:
        await manager.connect(websocket, client_id)
        await openai_client.connect()
        
        # Start OpenAI message handler
        openai_handler = asyncio.create_task(openai_client.handle_openai_messages())
        
        while True:
            # Receive message from client
            data = await websocket.receive_json()
            
            if data["type"] == "audio":
                await openai_client.process_audio(data["data"])
            elif data["type"] == "close":
                break
                
    except WebSocketDisconnect:
        logger.info(f"Client {client_id} disconnected")
    except Exception as e:
        logger.error(f"Error in websocket endpoint: {e}")
    finally:
        manager.disconnect(client_id)
        await openai_client.cleanup()
        if 'openai_handler' in locals():
            openai_handler.cancel()



@app.post("/conversation")
def post_feature_request(request : Conversation , db : Session = Depends(get_db)) : 
    Conversation = models.Conversation(id_conversation = request.id_conversation , user_message=request.user_message ,agent_message=request.agent_message , timestamp=datetime.now())
    db.add(Conversation)
    db.commit()
    db.refresh(Conversation)
    return Conversation


@app.post("/create-conversation-id")
def create_id():
    new_id  = uuid.uuid4()
    return {"conversation_id": new_id}





















if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
