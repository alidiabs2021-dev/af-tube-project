from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends
from fastapi.responses import StreamingResponse, FileResponse
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
import uuid
from datetime import datetime, timezone, timedelta
import bcrypt
import jwt
import secrets
import asyncio
import json
import re
import tempfile
import shutil

# MongoDB connection
mongo_url = os.environ.get('MONGO_URL', 'mongodb://localhost:27017')
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ.get('DB_NAME', 'downloadhub')]

# JWT Config
JWT_ALGORITHM = "HS256"
def get_jwt_secret() -> str:
    return os.environ.get("JWT_SECRET", "super-secret-key-123")

# Create the main app
app = FastAPI(title="DownloadHub API")
api_router = APIRouter(prefix="/api")

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ============== Models ==============
class UserCreate(BaseModel):
    email: str
    password: str
    name: str

class UserLogin(BaseModel):
    email: str
    password: str

class VideoURLInput(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str
    is_audio: bool = False

class FavoriteCreate(BaseModel):
    video_url: str
    video_title: str
    thumbnail: Optional[str] = None
    platform: str

# ============== Password & Auth Helpers ==============
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))

def create_access_token(user_id: str, email: str) -> str:
    payload = {"sub": user_id, "email": email, "exp": datetime.now(timezone.utc) + timedelta(minutes=60), "type": "access"}
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)

def create_refresh_token(user_id: str) -> str:
    payload = {"sub": user_id, "exp": datetime.now(timezone.utc) + timedelta(days=7), "type": "refresh"}
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)

async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token: raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        from bson import ObjectId
        user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
        if not user: raise HTTPException(status_code=401, detail="User not found")
        user["id"] = str(user["_id"])
        return user
    except: raise HTTPException(status_code=401, detail="Invalid token")

def detect_platform(url: str) -> str:
    url_l = url.lower()
    if "youtube" in url_l or "youtu.be" in url_l: return "youtube"
    if "facebook" in url_l or "fb.watch" in url_l: return "facebook"
    if "instagram" in url_l: return "instagram"
    if "tiktok" in url_l: return "tiktok"
    return "other"

# ============== Auth Routes ==============
@api_router.post("/auth/register")
async def register(user_data: UserCreate, response: Response):
    email = user_data.email.lower().strip()
    if await db.users.find_one({"email": email}): raise HTTPException(status_code=400, detail="Email exists")
    user_doc = {"email": email, "password_hash": hash_password(user_data.password), "name": user_data.name, "role": "user", "created_at": datetime.now(timezone.utc)}
    res = await db.users.insert_one(user_doc)
    token = create_access_token(str(res.inserted_id), email)
    response.set_cookie(key="access_token", value=token, httponly=True, samesite="lax")
    return {"id": str(res.inserted_id), "name": user_data.name}

@api_router.post("/auth/login")
async def login(credentials: UserLogin, response: Response):
    user = await db.users.find_one({"email": credentials.email.lower().strip()})
    if not user or not verify_password(credentials.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(str(user["_id"]), user["email"])
    response.set_cookie(key="access_token", value=token, httponly=True, samesite="lax")
    return {"id": str(user["_id"]), "name": user["name"], "role": user.get("role", "user")}
# ============== Video Analysis & Download ==============
@api_router.post("/video/analyze")
async def analyze_video(video_input: VideoURLInput):
    url = video_input.url.strip()
    if not url: raise HTTPException(status_code=400, detail="URL is required")
    try:
        # استخدام yt-dlp مباشرة بدون مسارات محددة ليتناسب مع Render
        cmd = ["yt-dlp", "--dump-json", "--no-download", "--no-warnings", url]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        if process.returncode != 0: raise HTTPException(status_code=400, detail="Analysis failed")
        
        info = json.loads(stdout.decode())
        formats = []
        for f in info.get("formats", []):
            if f.get("height"):
                formats.append({
                    "format_id": f.get("format_id"),
                    "quality": f"{f.get('height')}p",
                    "ext": f.get("ext"),
                    "type": "video",
                    "filesize": f.get("filesize")
                })
        
        return {
            "id": info.get("id", str(uuid.uuid4())),
            "title": info.get("title", "Unknown"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "platform": detect_platform(url),
            "formats": formats[:15],
            "url": url
        }
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/video/download")
async def download_video(download_req: DownloadRequest, request: Request):
    temp_dir = tempfile.mkdtemp()
    output_template = os.path.join(temp_dir, "%(title)s.%(ext)s")
    try:
        cmd = ["yt-dlp", "-f", download_req.format_id if not download_req.is_audio else "bestaudio", "-o", output_template]
        if download_req.is_audio: cmd.extend(["-x", "--audio-format", "mp3"])
        cmd.append(download_req.url)
        
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await asyncio.wait_for(process.communicate(), timeout=300)
        
        files = os.listdir(temp_dir)
        if not files: raise HTTPException(status_code=500, detail="File not found")
        filepath = os.path.join(temp_dir, files[0])

        # حفظ في السجل إذا كان المستخدم مسجلاً
        try:
            user = await get_current_user(request)
            if user:
                await db.download_history.insert_one({
                    "id": str(uuid.uuid4()), "user_id": user["id"], "video_title": files[0],
                    "video_url": download_req.url, "downloaded_at": datetime.now(timezone.utc)
                })
        except: pass

        def iterfile():
            with open(filepath, 'rb') as f: yield from f
            shutil.rmtree(temp_dir, ignore_errors=True)

        return StreamingResponse(iterfile(), media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{files[0]}"'})
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))

# ============== History & Favorites ==============
@api_router.get("/history")
async def get_history(request: Request):
    user = await get_current_user(request)
    cursor = db.download_history.find({"user_id": user["id"]}).sort("downloaded_at", -1).limit(50)
    history = await cursor.to_list(length=50)
    for h in history: h["_id"] = str(h["_id"])
    return history

@api_router.post("/favorites")
async def add_favorite(favorite: FavoriteCreate, request: Request):
    user = await get_current_user(request)
    fav_doc = favorite.dict()
    fav_doc.update({"id": str(uuid.uuid4()), "user_id": user["id"], "created_at": datetime.now(timezone.utc)})
    await db.favorites.insert_one(fav_doc)
    return {"message": "Added to favorites"}

@api_router.get("/favorites")
async def get_favorites(request: Request):
    user = await get_current_user(request)
    cursor = db.favorites.find({"user_id": user["id"]}).sort("created_at", -1)
    favs = await cursor.to_list(length=100)
    for f in favs: f["_id"] = str(f["_id"])
    return favs

# ============== Admin & Stats ==============
@api_router.get("/stats")
async def get_stats(request: Request):
    user = await get_current_user(request)
    if user.get("role") != "admin": raise HTTPException(status_code=403, detail="Admin only")
    users_count = await db.users.count_documents({})
    downloads_count = await db.download_history.count_documents({})
    return {"total_users": users_count, "total_downloads": downloads_count}

@api_router.get("/health")
async def health(): return {"status": "healthy"}

app.include_router(api_router)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    # التأكد من وجود حساب أدمن افتراضي
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@aftube.com")
    if not await db.users.find_one({"email": admin_email}):
        await db.users.insert_one({
            "email": admin_email, "password_hash": hash_password("admin123"),
            "name": "Admin", "role": "admin", "created_at": datetime.now(timezone.utc)
        })

@app.on_event("shutdown")
async def shutdown():
    client.close()
