"""
Run this once to install all dependencies into the correct Python environment.
Usage: python install.py
"""
import sys
import subprocess

PACKAGES = [
    "flask",
    "waitress",
    "requests",
    "beautifulsoup4",
    "nba_api",
]

print(f"Installing packages into: {sys.executable}")
subprocess.run([sys.executable, "-m", "pip", "install"] + PACKAGES, check=True)
print("\nAll dependencies installed. Run 'python serve.py' to start the dashboard.")
