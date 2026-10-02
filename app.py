import os
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
import ai_brochure_server as ai

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=None)

@app.get("/")
def index():
    return send_from_directory(ROOT, "supplier_profile_database.html")

@app.get("/health")
def health():
    return jsonify({"ok": True})

@app.post("/api/ai/review-brochure")
def review_brochure():
    try:
        payload = request.get_json(force=True)
        result = ai.ai_review(payload)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.get("/product_photos/<path:filename>")
def product_photos(filename):
    return send_from_directory(ROOT / "product_photos", filename)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
