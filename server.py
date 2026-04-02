import os
from fastapi import FastAPI
from pydantic import BaseModel
from motor.motor_asyncio import AsyncIOMotorClient

app = FastAPI()

# جلب رابط قاعدة البيانات من إعدادات Render (اللي حطيناه بقسم Environment)
MONGO_DETAILS = os.getenv("MONGO_URI")
client = AsyncIOMotorClient(MONGO_DETAILS)
database = client.af_tube_db
collection = database.get_collection("videos")

# نموذج البيانات اللي رح يستقبلها السيرفر
class Video(BaseModel):
    title: str
    url: str

@app.get("/")
async def home():
    return {"message": "AF Tube Server is Active & Connected to MongoDB!"}

# ميزة إضافة فيديو جديد لقاعدة البيانات
@app.post("/add-video")
async def add_video(video: Video):
    new_video = {"title": video.title, "url": video.url}
    result = await collection.insert_one(new_video)
    return {"status": "Success", "id": str(result.inserted_id)}

# ميزة عرض كل الفيديوهات المخزنة
@app.get("/get-videos")
async def get_videos():
    videos = []
    cursor = collection.find()
    async for document in cursor:
        videos.append({"title": document["title"], "url": document["url"]})
    return videos
