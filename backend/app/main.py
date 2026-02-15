from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="Vector API",
    description="Tech news aggregation & AI analysis platform",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/v1/health")
async def health_check() -> dict:
    return {
        "status": "ok",
        "version": "0.1.0",
        "dbConnected": False,
        "lastFetchAt": None,
    }
