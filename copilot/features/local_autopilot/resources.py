"""Explicit local-only resource check before optional model inference."""
import subprocess
import httpx


def resources_available():
    try:
        result = subprocess.run(["nvidia-smi", "--query-compute-apps=used_memory", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, check=True)
        if any(int(row.strip()) > 50 for row in result.stdout.splitlines() if row.strip()):
            return False
        response = httpx.get("http://127.0.0.1:11434/api/ps", timeout=5)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            return False
        return all(isinstance(model, dict) and model.get("name") == "qwen3.5:9b" for model in payload["models"])
    except Exception:
        return False
