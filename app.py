import json
import psycopg2
from flask import Flask, request, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename
import os
import uuid
import tempfile
import zipfile
from urllib.parse import urlparse
try:
    import redis
except ImportError:
    redis = None

app = Flask(__name__)

# Store uploads on shared volume to work across replicas
UPLOAD_DIR = os.path.join(app.root_path, "data", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
FILE_UPLOAD_DIR = os.path.join(UPLOAD_DIR, "files")
os.makedirs(FILE_UPLOAD_DIR, exist_ok=True)
ARTWORK_DIR = os.path.join(UPLOAD_DIR, "artwork")
os.makedirs(ARTWORK_DIR, exist_ok=True)

def find_mp4_atom(f, target_path, start_offset=0, max_size=None):
    if not target_path:
        return start_offset, max_size

    f.seek(start_offset)
    current_target = target_path[0]
    end_offset = (start_offset + max_size) if max_size is not None else None
    
    while True:
        if end_offset is not None and f.tell() >= end_offset:
            break
        
        header = f.read(8)
        if len(header) < 8:
            break
            
        atom_size = int.from_bytes(header[0:4], 'big')
        atom_type = header[4:8]
        
        header_len = 8
        if atom_size == 1:
            large_size_bytes = f.read(8)
            if len(large_size_bytes) < 8:
                break
            atom_size = int.from_bytes(large_size_bytes, 'big')
            header_len = 16
            
        if atom_size <= 0:
            break
            
        if atom_type == current_target:
            sub_start = f.tell()
            sub_size = atom_size - header_len
            
            if atom_type == b'meta':
                sub_start += 4
                sub_size -= 4
                
            return find_mp4_atom(f, target_path[1:], sub_start, sub_size)
            
        f.seek(f.tell() + atom_size - header_len)
        
    return None

def extract_m4a_artwork(file_path):
    try:
        with open(file_path, 'rb') as f:
            res = find_mp4_atom(f, [b'moov', b'udta', b'meta', b'ilst', b'covr'])
            if res:
                covr_start, covr_size = res
                res_data = find_mp4_atom(f, [b'data'], covr_start, covr_size)
                if res_data:
                    data_start, data_size = res_data
                    f.seek(data_start + 8)
                    pic_data = f.read(data_size - 8)
                    
                    mime_type = 'image/jpeg'
                    if pic_data.startswith(b'\x89PNG\r\n\x1a\n'):
                        mime_type = 'image/png'
                    elif pic_data.startswith(b'GIF89a') or pic_data.startswith(b'GIF87a'):
                        mime_type = 'image/gif'
                    return pic_data, mime_type
    except Exception as e:
        print(f"Error extracting M4A artwork: {e}")
    return None

def extract_mp3_artwork(file_path):
    try:
        with open(file_path, 'rb') as f:
            header = f.read(10)
            if len(header) < 10 or header[0:3] != b'ID3':
                return None
            
            version_major = header[3]
            tag_size = (header[6] << 21) | (header[7] << 14) | (header[8] << 7) | header[9]
            tag_size = min(tag_size, 10 * 1024 * 1024)
            
            tag_data = f.read(tag_size)
            
            offset = 0
            while offset + 10 < len(tag_data):
                frame_id = tag_data[offset:offset+4]
                if not all(65 <= b <= 90 or 48 <= b <= 57 for b in frame_id):
                    break
                
                size_bytes = tag_data[offset+4:offset+8]
                if version_major == 4:
                    frame_size = (size_bytes[0] << 21) | (size_bytes[1] << 14) | (size_bytes[2] << 7) | size_bytes[3]
                else:
                    frame_size = int.from_bytes(size_bytes, 'big')
                
                if frame_size <= 0 or offset + 10 + frame_size > len(tag_data):
                    break
                
                frame_body = tag_data[offset+10:offset+10+frame_size]
                if frame_id == b'APIC':
                    if len(frame_body) > 4:
                        encoding = frame_body[0]
                        mime_end = frame_body.find(b'\x00', 1)
                        if mime_end != -1:
                            mime_type = frame_body[1:mime_end].decode('ascii', errors='ignore')
                            pic_type = frame_body[mime_end+1]
                            desc_start = mime_end + 2
                            if encoding in [0, 3]:
                                desc_end = frame_body.find(b'\x00', desc_start)
                                pic_data_start = desc_end + 1 if desc_end != -1 else desc_start
                            else:
                                desc_end = frame_body.find(b'\x00\x00', desc_start)
                                pic_data_start = desc_end + 2 if desc_end != -1 else desc_start
                            
                            picture_data = frame_body[pic_data_start:]
                            return picture_data, mime_type
                
                offset += 10 + frame_size
    except Exception as e:
        print(f"Error extracting MP3 artwork: {e}")
    return None

def extract_m4a_artist(file_path):
    try:
        with open(file_path, 'rb') as f:
            res = find_mp4_atom(f, [b'moov', b'udta', b'meta', b'ilst', b'\xa9ART'])
            if not res:
                res = find_mp4_atom(f, [b'moov', b'udta', b'meta', b'ilst', b'aART'])
            if res:
                start, size = res
                res_data = find_mp4_atom(f, [b'data'], start, size)
                if res_data:
                    data_start, data_size = res_data
                    f.seek(data_start + 16)
                    artist_data = f.read(data_size - 16)
                    return artist_data.decode('utf-8', errors='ignore').strip()
    except Exception as e:
        print(f"Error extracting M4A artist: {e}")
    return None

def extract_mp3_artist(file_path):
    try:
        with open(file_path, 'rb') as f:
            header = f.read(10)
            if len(header) < 10 or header[0:3] != b'ID3':
                return None
            
            version_major = header[3]
            tag_size = (header[6] << 21) | (header[7] << 14) | (header[8] << 7) | header[9]
            tag_size = min(tag_size, 10 * 1024 * 1024)
            
            tag_data = f.read(tag_size)
            
            offset = 0
            while offset + 10 < len(tag_data):
                frame_id = tag_data[offset:offset+4]
                if not all((65 <= b <= 90) or (48 <= b <= 57) for b in frame_id):
                    break
                
                size_bytes = tag_data[offset+4:offset+8]
                if version_major == 4:
                    frame_size = (size_bytes[0] << 21) | (size_bytes[1] << 14) | (size_bytes[2] << 7) | size_bytes[3]
                else:
                    frame_size = int.from_bytes(size_bytes, 'big')
                
                if frame_size <= 0 or offset + 10 + frame_size > len(tag_data):
                    break
                
                frame_body = tag_data[offset+10:offset+10+frame_size]
                if frame_id == b'TPE1':
                    if len(frame_body) > 1:
                        encoding = frame_body[0]
                        text_bytes = frame_body[1:]
                        if encoding == 0:
                            return text_bytes.decode('latin1', errors='ignore').strip()
                        elif encoding == 1:
                            return text_bytes.decode('utf-16', errors='ignore').strip()
                        elif encoding == 2:
                            return text_bytes.decode('utf-16-be', errors='ignore').strip()
                        elif encoding == 3:
                            return text_bytes.decode('utf-8', errors='ignore').strip()
                        else:
                            return text_bytes.decode('utf-8', errors='ignore').strip()
                
                offset += 10 + frame_size
    except Exception as e:
        print(f"Error extracting MP3 artist: {e}")
    return None

UPLOAD_ORIGIN = os.getenv("UPLOAD_ORIGIN", "https://noteslook.shop")

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_NAME = os.getenv("DB_NAME", "notepad")
DB_USER = os.getenv("DB_USER", "notepad")
DB_PASSWORD = os.getenv("DB_PASSWORD", "notepad")

def parse_host_port(raw_value, default_host, default_port):
    if not raw_value:
        return default_host, default_port
    parsed = None
    try:
        if "://" in raw_value:
            parsed = urlparse(raw_value)
            host = parsed.hostname or default_host
            port = parsed.port or default_port
        elif ":" in raw_value:
            host, port = raw_value.split(":", 1)
            port = int(port)
        else:
            host = raw_value
            port = default_port
        return host or default_host, int(port)
    except (ValueError, AttributeError):
        return default_host, default_port

REDIS_HOST, REDIS_PORT = parse_host_port(os.getenv("REDIS_HOST"), "redis", 6379)
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_TTL = int(os.getenv("REDIS_TTL", "60"))

def get_db():
    return psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    content TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_mod TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS music_tracks (
                    id TEXT PRIMARY KEY,
                    storage_name TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    download_url TEXT NOT NULL,
                    size BIGINT NOT NULL,
                    mime TEXT,
                    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pdf_highlights (
                    id SERIAL PRIMARY KEY,
                    doc_name TEXT NOT NULL,
                    pdf_name TEXT NOT NULL,
                    highlighted_text TEXT NOT NULL,
                    color TEXT NOT NULL DEFAULT '#fff59d',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_pdf_highlights_doc_pdf
                ON pdf_highlights (doc_name, pdf_name, created_at)
            """)
            cur.execute("""
                ALTER TABLE music_tracks
                ADD COLUMN IF NOT EXISTS doc_name TEXT NOT NULL DEFAULT 'global'
            """)
            cur.execute("""
                ALTER TABLE music_tracks
                ADD COLUMN IF NOT EXISTS artwork_url TEXT
            """)
            cur.execute("""
                ALTER TABLE music_tracks
                ADD COLUMN IF NOT EXISTS artist TEXT
            """)
            cur.execute("""
                INSERT INTO sync_state (id, last_mod)
                VALUES (1, CURRENT_TIMESTAMP)
                ON CONFLICT (id) DO NOTHING
            """)
            conn.commit()

redis_client = None
if redis:
    try:
        redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, socket_connect_timeout=2)
        redis_client.ping()
    except redis.RedisError:
        redis_client = None

def cache_set(key, value, ttl=REDIS_TTL):
    if not redis_client:
        return
    try:
        redis_client.set(key, value, ex=ttl)
    except redis.RedisError:
        pass

def cache_get(key):
    if not redis_client:
        return None
    try:
        raw = redis_client.get(key)
        return raw.decode() if raw else None
    except redis.RedisError:
        return None

def invalidate_cache(key):
    if not redis_client:
        return
    try:
        redis_client.delete(key)
    except redis.RedisError:
        pass

def persist_doc_cache(name, content):
    if not redis_client or not name:
        return
    cache_set(f"doc:{name}", content)

def get_cached_doc(name):
    if not redis_client or not name:
        return None
    return cache_get(f"doc:{name}")

def invalidate_doc_cache(name):
    if not redis_client or not name:
        return
    invalidate_cache(f"doc:{name}")

def invalidate_list_cache():
    invalidate_cache("doc_list")

@app.route("/")
def index():
    response = send_from_directory("templates", "index.html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)

@app.route("/manifest.webmanifest")
@app.route("/manifest.json")
def serve_manifest():
    return send_from_directory("static", "manifest.webmanifest", mimetype="application/manifest+json")


@app.route("/static/uploads/<path:filename>")
def uploaded_files(filename):
    return send_from_directory(UPLOAD_DIR, filename)

@app.after_request
def add_cors_headers(resp):
    if (
        request.path.startswith("/upload_video")
        or request.path.startswith("/upload_image")
        or request.path.startswith("/upload_file")
        or request.path.startswith("/upload_pdf")
        or request.path.startswith("/upload_epub")
    ):
        resp.headers["Access-Control-Allow-Origin"] = UPLOAD_ORIGIN
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/list")
def list_files():
    cached = cache_get("doc_list")
    if cached:
        try:
            return jsonify(json.loads(cached))
        except json.JSONDecodeError:
            pass

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM documents ORDER BY name")
            result = [r[0] for r in cur.fetchall()]
            cache_set("doc_list", json.dumps(result))
            return jsonify(result)

@app.route("/open", methods=["POST"])
def open_file():
    name = request.json["name"]
    cached = get_cached_doc(name)
    if cached is not None:
        return jsonify({"content": cached})
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT content FROM documents WHERE name=%s", (name,))
            row = cur.fetchone()
            if row:
                persist_doc_cache(name, row[0])
                return jsonify({"content": row[0]})
    return jsonify({"error": "Not found"}), 404


@app.route("/cache-status")
def cache_status():
    if not redis_client:
        return jsonify({"error": "redis-unavailable"}), 503

    try:
        keys = redis_client.keys("doc:*")
    except redis.RedisError:
        return jsonify({"error": "redis-error"}), 503

    result = []
    for key in keys:
        try:
            ttl = redis_client.ttl(key)
            value = redis_client.get(key)
            result.append({
                "key": key.decode(),
                "ttl": ttl,
                "length": len(value) if value else 0
            })
        except redis.RedisError:
            continue

    return jsonify(result)

@app.route("/save", methods=["POST"])
def save_file():
    data = request.json
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO documents (name, content)
                VALUES (%s, %s)
                ON CONFLICT (name)
                DO UPDATE SET content=EXCLUDED.content,
                              updated_at=CURRENT_TIMESTAMP
            """, (data["name"], data["content"]))
            cur.execute("UPDATE sync_state SET last_mod=CURRENT_TIMESTAMP WHERE id=1")
            conn.commit()
    persist_doc_cache(data["name"], data["content"])
    invalidate_list_cache()
    return jsonify({"status": "saved"})

@app.route("/delete", methods=["POST"])
def delete_file():
    name = request.json["name"]
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM documents WHERE name=%s", (name,))
            deleted = cur.rowcount
            cur.execute("UPDATE sync_state SET last_mod=CURRENT_TIMESTAMP WHERE id=1")
            conn.commit()
    invalidate_list_cache()
    invalidate_doc_cache(name)
    return jsonify({"status": "deleted" if deleted else "not found"})

@app.route('/last_modified')
def last_modified():
    return 'ok', 200

@app.route("/upload_video", methods=["POST", "OPTIONS"])
def upload_video():
    if request.method == "OPTIONS":
        return ("", 204)

    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    file = request.files["video"]
    if not file or file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    if not (file.mimetype or "").startswith("video/"):
        return jsonify({"error": "Unsupported file type"}), 400

    safe_name = secure_filename(file.filename)
    ext = os.path.splitext(safe_name)[1].lower() or ".mp4"
    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest_path = os.path.join(UPLOAD_DIR, unique_name)
    file.save(dest_path)

    host = request.host
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    # Force :8443 for local HTTPS host when no port is present
    if host == "noteslook.lan" and scheme == "https":
        host = "noteslook.lan:8443"
    return jsonify({"url": f"{scheme}://{host}/static/uploads/{unique_name}"})

@app.route("/upload_image", methods=["POST", "OPTIONS"])
def upload_image():
    if request.method == "OPTIONS":
        return ("", 204)

    if "image" not in request.files:
        return jsonify({"error": "No image file provided"}), 400

    file = request.files["image"]
    if not file or file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    if not (file.mimetype or "").startswith("image/"):
        return jsonify({"error": "Unsupported file type"}), 400

    safe_name = secure_filename(file.filename)
    ext = os.path.splitext(safe_name)[1].lower() or ".jpg"
    unique_name = f"{uuid.uuid4().hex}{ext}"
    dest_path = os.path.join(UPLOAD_DIR, unique_name)
    file.save(dest_path)

    base_url = get_public_base_url()
    return jsonify({"url": f"{base_url}/static/uploads/{unique_name}"})


def get_public_base_url():
    host = request.host
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    if host == "noteslook.lan" and scheme == "https":
        host = "noteslook.lan:8443"
    return f"{scheme}://{host}"

@app.route("/upload_pdf", methods=["POST", "OPTIONS"])
def upload_pdf():
    if request.method == "OPTIONS":
        return ("", 204)

    upload = request.files.get("pdf") or request.files.get("file")
    if not upload:
        return jsonify({"error": "No PDF file provided"}), 400
    if not upload.filename:
        return jsonify({"error": "Empty filename"}), 400

    safe_name = secure_filename(upload.filename)
    ext = os.path.splitext(safe_name)[1].lower()
    mimetype = (upload.mimetype or "").lower()

    if ext != ".pdf" and mimetype != "application/pdf":
        return jsonify({"error": "Unsupported file type"}), 400

    pdf_name = f"{uuid.uuid4().hex}.pdf"
    dest_path = os.path.join(FILE_UPLOAD_DIR, pdf_name)
    upload.save(dest_path)

    base_url = get_public_base_url()
    return jsonify({
        "pdf_name": pdf_name,
        "url": f"{base_url}/static/uploads/files/{pdf_name}",
        "download_url": f"{base_url}/download_file/{pdf_name}?name={secure_filename(safe_name) or 'document.pdf'}",
        "name": safe_name or "document.pdf"
    })

@app.route("/upload_epub", methods=["POST", "OPTIONS"])
def upload_epub():
    if request.method == "OPTIONS":
        return ("", 204)

    upload = request.files.get("epub") or request.files.get("file")
    if not upload:
        return jsonify({"error": "No EPUB file provided"}), 400
    if not upload.filename:
        return jsonify({"error": "Empty filename"}), 400

    safe_name = secure_filename(upload.filename)
    ext = os.path.splitext(safe_name)[1].lower()
    mimetype = (upload.mimetype or "").lower()
    if ext != ".epub" and "epub" not in mimetype:
        return jsonify({"error": "Unsupported file type"}), 400

    epub_name = f"{uuid.uuid4().hex}.epub"
    dest_path = os.path.join(FILE_UPLOAD_DIR, epub_name)
    upload.save(dest_path)

    base_url = get_public_base_url()
    return jsonify({
        "epub_name": epub_name,
        "url": f"{base_url}/static/uploads/files/{epub_name}",
        "download_url": f"{base_url}/download_file/{epub_name}?name={secure_filename(safe_name) or 'book.epub'}",
        "name": safe_name or "book.epub"
    })


@app.route("/upload_file", methods=["POST", "OPTIONS"])
def upload_file():
    if request.method == "OPTIONS":
        return ("", 204)

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file or file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    mimetype = (file.mimetype or "").lower()
    if mimetype.startswith("image/") or mimetype.startswith("video/"):
        return jsonify({"error": "Use the dedicated image/video uploader"}), 400

    safe_name = secure_filename(file.filename)
    ext = os.path.splitext(safe_name)[1].lower() or ""
    track_id = uuid.uuid4().hex
    storage_name = f"{track_id}{ext}"
    dest_path = os.path.join(FILE_UPLOAD_DIR, storage_name)
    file.save(dest_path)

    size = os.path.getsize(dest_path)
    base_url = get_public_base_url()
    download_endpoint = f"{base_url}/download_file/{storage_name}?name={secure_filename(safe_name)}"
    public_url = f"{base_url}/static/uploads/files/{storage_name}"

    # Extract artwork and artist if possible
    artwork_url = None
    artist = None
    try:
        art_res = None
        if mimetype == "audio/mpeg" or ext == ".mp3":
            art_res = extract_mp3_artwork(dest_path)
            artist = extract_mp3_artist(dest_path)
        elif mimetype in ["audio/mp4", "audio/x-m4a"] or ext in [".m4a", ".mp4"]:
            art_res = extract_m4a_artwork(dest_path)
            artist = extract_m4a_artist(dest_path)
            
        if art_res:
            pic_data, mime_type = art_res
            ext_map = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif'}
            art_ext = ext_map.get(mime_type, '.jpg')
            art_name = f"{track_id}_art{art_ext}"
            art_path = os.path.join(ARTWORK_DIR, art_name)
            with open(art_path, 'wb') as art_f:
                art_f.write(pic_data)
            artwork_url = f"{base_url}/static/uploads/artwork/{art_name}"
    except Exception as e:
        print(f"Error extracting artwork/artist for {storage_name}: {e}")

    doc_name = request.args.get("doc") or request.form.get("doc") or "global"

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO music_tracks (
                    id, storage_name, original_name, url, download_url, size, mime, doc_name, artwork_url, artist
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                track_id, storage_name, safe_name, public_url, download_endpoint, size, mimetype, doc_name, artwork_url, artist
            ))
            conn.commit()

    return jsonify({
        "id": track_id,
        "url": public_url,
        "download_url": download_endpoint,
        "name": safe_name,
        "size": size,
        "mime": mimetype,
        "artwork_url": artwork_url,
        "artist": artist
    })


@app.route("/download_file/<path:filename>")
def download_file(filename):
    safe_name = request.args.get("name") or filename
    safe_name = secure_filename(safe_name) or filename
    return send_from_directory(FILE_UPLOAD_DIR, filename, as_attachment=True, download_name=safe_name)


@app.route("/pdf_highlights", methods=["GET", "POST"])
def pdf_highlights():
    if request.method == "GET":
        doc_name = (request.args.get("doc") or "global").strip() or "global"
        pdf_name = (request.args.get("pdf") or "").strip()
        if not pdf_name:
            return jsonify({"error": "Missing pdf parameter"}), 400

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, highlighted_text, color, created_at
                    FROM pdf_highlights
                    WHERE doc_name = %s AND pdf_name = %s
                    ORDER BY created_at ASC, id ASC
                """, (doc_name, pdf_name))
                rows = cur.fetchall()

        return jsonify([
            {
                "id": row[0],
                "text": row[1],
                "color": row[2],
                "created_at": row[3].isoformat() if row[3] else None
            }
            for row in rows
        ])

    data = request.get_json(silent=True) or {}
    doc_name = (data.get("doc") or "global").strip() or "global"
    pdf_name = (data.get("pdf") or "").strip()
    text = (data.get("text") or "").strip()
    color = (data.get("color") or "#fff59d").strip() or "#fff59d"

    if not pdf_name:
        return jsonify({"error": "Missing pdf"}), 400
    if not text:
        return jsonify({"error": "Missing text"}), 400

    # Keep highlight notes bounded and predictable.
    text = text[:1000]
    color = color[:32]

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pdf_highlights (doc_name, pdf_name, highlighted_text, color)
                VALUES (%s, %s, %s, %s)
                RETURNING id, highlighted_text, color, created_at
            """, (doc_name, pdf_name, text, color))
            row = cur.fetchone()
            conn.commit()

    return jsonify({
        "id": row[0],
        "text": row[1],
        "color": row[2],
        "created_at": row[3].isoformat() if row[3] else None
    }), 201


@app.route("/pdf_highlights/<int:highlight_id>", methods=["DELETE"])
def delete_pdf_highlight(highlight_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pdf_highlights WHERE id=%s", (highlight_id,))
            deleted = cur.rowcount
            conn.commit()
    return jsonify({"status": "deleted" if deleted else "not found"})


@app.route("/music_tracks")
def list_music_tracks():
    doc_name = request.args.get("doc") or "global"
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, original_name, storage_name, size, mime, uploaded_at, artwork_url, artist
                FROM music_tracks
                WHERE doc_name = %s
                ORDER BY uploaded_at ASC
            """, (doc_name,))
            rows = cur.fetchall()
    base_url = get_public_base_url()
    return jsonify([
        {
            "id": row[0],
            "name": row[1],
            "url": f"{base_url}/static/uploads/files/{row[2]}",
            "download_url": f"{base_url}/download_file/{row[2]}?name={secure_filename(row[1] or '')}",
            "size": row[3],
            "mime": row[4],
            "uploaded_at": row[5].isoformat() if row[5] else None,
            "artwork_url": row[6],
            "artist": row[7]
        }
        for row in rows
    ])


@app.route("/music_tracks/<track_id>", methods=["DELETE"])
def delete_music_track(track_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT storage_name, artwork_url FROM music_tracks WHERE id=%s", (track_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({"status": "not found"}), 404
            storage_name = row[0]
            artwork_url = row[1]
            file_path = os.path.join(FILE_UPLOAD_DIR, storage_name)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass
            if artwork_url:
                try:
                    art_filename = os.path.basename(artwork_url)
                    art_path = os.path.join(ARTWORK_DIR, art_filename)
                    if os.path.exists(art_path):
                        os.remove(art_path)
                except Exception:
                    pass
            cur.execute("DELETE FROM music_tracks WHERE id=%s", (track_id,))
        conn.commit()
    return jsonify({"status": "deleted"})

@app.route("/download_music", methods=["POST"])
def download_music():
    data = request.get_json(silent=True) or {}
    track_ids = data.get("track_ids") or []
    if not isinstance(track_ids, list) or len(track_ids) == 0:
        return jsonify({"error": "No track IDs provided"}), 400

    safe_files = []
    with get_db() as conn:
        with conn.cursor() as cur:
            format_strings = ','.join(['%s'] * len(track_ids))
            cur.execute(f"""
                SELECT storage_name, original_name
                FROM music_tracks
                WHERE id IN ({format_strings})
            """, tuple(track_ids))
            rows = cur.fetchall()

    for storage_name, original_name in rows:
        storage_base = os.path.basename(storage_name)
        if not storage_base:
            continue
        path = os.path.join(FILE_UPLOAD_DIR, storage_base)
        if os.path.isfile(path):
            arcname = original_name or storage_base
            safe_files.append((arcname, path))

    if not safe_files:
        return jsonify({"error": "No valid files found"}), 404

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp_path = tmp.name
    tmp.close()

    used_names = set()
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, path in safe_files:
            name, ext = os.path.splitext(arcname)
            counter = 1
            unique_name = arcname
            while unique_name in used_names:
                unique_name = f"{name} ({counter}){ext}"
                counter += 1
            used_names.add(unique_name)
            zf.write(path, arcname=unique_name)

    return send_file(tmp_path, mimetype="application/zip", as_attachment=True, download_name="music.zip")

@app.route("/download_images", methods=["POST"])
def download_images():
    data = request.get_json(silent=True) or {}
    names = data.get("files") or []
    if not isinstance(names, list) or len(names) == 0:
        return jsonify({"error": "No files provided"}), 400

    safe_files = []
    for name in names:
        base = os.path.basename(name)
        if not base:
            continue
        path = os.path.join(UPLOAD_DIR, base)
        if os.path.isfile(path):
            safe_files.append((base, path))

    if not safe_files:
        return jsonify({"error": "No valid files found"}), 404

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp_path = tmp.name
    tmp.close()

    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for base, path in safe_files:
            zf.write(path, arcname=base)

    return send_file(tmp_path, mimetype="application/zip", as_attachment=True, download_name="images.zip")

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
