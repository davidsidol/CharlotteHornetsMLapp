"""
Charlotte Hornets Dashboard - Windows Launcher
Uses Waitress WSGI server (Windows-native, no gunicorn needed)
Run: python serve.py
Access at: http://localhost:8080
"""
import sys
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

from waitress import serve
from app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("HOST", "0.0.0.0")
    print("")
    print("  ==========================================")
    print("   Charlotte Hornets Dashboard")
    print("  ==========================================")
    print(f"   Local:   http://localhost:{port}")
    print(f"   Network: http://0.0.0.0:{port}")
    print("   Press Ctrl+C to stop")
    print("  ==========================================")
    print("")
    serve(app, host=host, port=port, threads=32)
