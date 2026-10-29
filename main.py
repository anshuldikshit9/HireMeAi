import uvicorn
import os

def main():
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"Starting HireMeAI server on http://{host}:{port}...")
    uvicorn.run("backend.main:app", host=host, port=port, reload=True)

if __name__ == "__main__":
    main()

