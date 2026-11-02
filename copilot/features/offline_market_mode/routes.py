from fastapi import APIRouter

router = APIRouter()


@router.get("/offline-market-mode/status", include_in_schema=False)
def status():
    return {"mode": "browser-connectivity-only", "automatic_retry": False}
