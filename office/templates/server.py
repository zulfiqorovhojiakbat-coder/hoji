"""Run this project: python3 server.py  (then open http://127.0.0.1:8000)."""
import miniapi
import api  # noqa: F401  registers the routes

if __name__ == "__main__":
    miniapi.run()
