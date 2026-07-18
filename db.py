# db.py
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi

# MongoDB Atlas URI (yours)
uri = "mongodb+srv://nagonu:0500868021Yaw@cluster0.yp3zg2d.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

client = MongoClient(
    uri,
    server_api=ServerApi("1"),
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=12000,
    connect=False,
)

# Main DB
db = client["nagobu"]
