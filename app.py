import os
import time
import json
import threading
import re
import uuid
import subprocess
import yt_dlp
import imageio_ffmpeg
from flask import Flask, render_template, request, send_file, jsonify, Response, stream_with_context
from werkzeug.utils import secure_filename
from openai import OpenAI

app = Flask(__name__)

# Configuración
DOWNLOAD_FOLDER = 'descargas'
UPLOAD_FOLDER = 'uploads'
COOKIES_FILE = 'cookies.txt' 
STATS_FILE = 'stats.json' # Archivo para guardar las estadísticas
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 

ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'webm', 'flv', 'm4v', 'wmv'}
tasks = {}

# ==========================================
# SISTEMA DE ESTADÍSTICAS
# ==========================================
def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                return json.load(f)
        except: pass
    return {
        'total_links_descargados': 0,
        'herramientas_locales': {'mp4': 0, 'mp3': 0, 'wav': 0, 'txt': 0}
    }

def save_stats(stats):
    try:
        with open(STATS_FILE, 'w') as f:
            json.dump(stats, f)
    except: pass

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def clean_ansi(text):
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

def progress_hook(d, task_id):
    if d['status'] == 'downloading':
        p = d.get('_percent_str', '0%').strip('%')
        try: progress = float(p)
        except ValueError: progress = 0
        tasks[task_id]['progress'] = progress

# ==========================================
# MOTOR 1: DESCARGADOR DE ENLACES
# ==========================================
def download_video_task(url, task_id, quality):
    if quality == '1080':
        format_selector = 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]/best'
    elif quality == '720':
        format_selector = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best'
    elif quality == '480':
        format_selector = 'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]/best'
    else:
        format_selector = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'

    ydl_opts = {
        'format': format_selector,
        'outtmpl': os.path.join(DOWNLOAD_FOLDER, f'{task_id}_raw.%(ext)s'),
        'restrictfilenames': True,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'cookiefile': COOKIES_FILE, 
        'ffmpeg_location': FFMPEG_PATH,
        'progress_hooks': [lambda d: progress_hook(d, task_id)],
        # NUEVO: Camuflaje para evadir bloqueos
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
        },
        'extractor_retries': 3,
    }

    tasks[task_id] = {'progress': 0, 'status': 'running'}
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            expected_filename = ydl.prepare_filename(info)

        tasks[task_id]['file_path'] = expected_filename
        tasks[task_id]['final_name'] = f"{info.get('title', 'Video')}.mp4"
        tasks[task_id]['mime_type'] = 'video/mp4'
        tasks[task_id]['status'] = 'success'

    except Exception as e:
        tasks[task_id]['status'] = 'error'
        raw_error = str(e)
        clean_error = clean_ansi(raw_error)
        if "Unsupported URL" in clean_error: tasks[task_id]['error_message'] = "Enlace no válido o perfil privado."
        elif "Sign in" in clean_error or "login" in clean_error.lower(): tasks[task_id]['error_message'] = "El video es privado. Actualiza cookies.txt."
        else: tasks[task_id]['error_message'] = "Error: " + clean_error[:100]

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/download', methods=['POST'])
def start_download():
    video_url = request.form.get('url', '').strip()
    quality = request.form.get('quality', 'best')
    
    if not video_url: return jsonify({"error": "Por favor, ingresa un enlace."}), 400
    if not video_url.startswith(('http://', 'https://')): return jsonify({"error": "Enlace inválido."}), 400

    if "x.com" in video_url: video_url = video_url.replace("x.com", "twitter.com")
    if "threads.com" in video_url: video_url = video_url.replace("threads.com", "threads.net")
    if "threads.net" in video_url and "?" in video_url: video_url = video_url.split("?")[0]

    # Registrar estadística
    stats = load_stats()
    stats['total_links_descargados'] += 1
    save_stats(stats)

    task_id = str(int(time.time() * 1000))
    threading.Thread(target=download_video_task, args=(video_url, task_id, quality)).start()
    return jsonify({"task_id": task_id}), 202

@app.route('/download_progress')
def download_progress():
    task_id = request.args.get('task_id')
    @stream_with_context
    def generate():
        while task_id in tasks:
            task = tasks[task_id]
            yield f"data: {json.dumps(task)}\n\n"
            if task['status'] in ('success', 'error'): break
            time.sleep(0.5) 
    return Response(generate(), mimetype='text/event-stream')

@app.route('/download_file')
def download_file():
    task_id = request.args.get('task_id')
    if task_id in tasks and tasks[task_id]['status'] == 'success':
        task = tasks[task_id]
        return send_file(task['file_path'], mimetype=task.get('mime_type', 'video/mp4'), as_attachment=True, download_name=task.get('final_name', 'video.mp4'))
    return jsonify({"error": "Tarea no completada."}), 404

# ==========================================
# MOTOR 2: PROCESADOR IA Y FFMPEG PURO
# ==========================================
@app.route('/process_media', methods=['POST'])
def process_media():
    if 'video' not in request.files: return "No se encontró el archivo", 400
    file = request.files['video']
    action = request.form.get('action', 'txt')

    if file.filename == '': return "Ningún archivo seleccionado", 400
    if file and allowed_file(file.filename):
        # Registrar estadística de herramienta local
        stats = load_stats()
        if action in stats['herramientas_locales']:
            stats['herramientas_locales'][action] += 1
            save_stats(stats)

        filename = secure_filename(file.filename)
        unique_id = str(uuid.uuid4())
        input_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}_{filename}")
        temp_audio = os.path.join(app.config['UPLOAD_FOLDER'], f"{unique_id}_temp.mp3")
        file.save(input_path)

        if action == 'txt': output_filename, mimetype_str, download_name = f"{unique_id}_transcripcion.txt", 'text/plain', "transcripcion_exacta.txt"
        elif action == 'mp3': output_filename, mimetype_str, download_name = f"{unique_id}_audio.mp3", 'audio/mpeg', "audio_estandar.mp3"
        elif action == 'wav': output_filename, mimetype_str, download_name = f"{unique_id}_audio.wav", 'audio/wav', "audio_estudio.wav"
        else: output_filename, mimetype_str, download_name = f"{unique_id}_convertido.mp4", 'video/mp4', "video_convertido.mp4"

        output_path = os.path.join(app.config['UPLOAD_FOLDER'], output_filename)

        try:
            if action == 'txt':
                cmd = [FFMPEG_PATH, '-y', '-i', input_path, '-vn', '-acodec', 'libmp3lame', '-b:a', '64k', '-ar', '16000', temp_audio]
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                
                api_key = os.environ.get("OPENAI_API_KEY")
                if not api_key:
                    text = "[Error: Falta la clave API de OpenAI. Configúrala en Render]"
                else:
                    try:
                        client = OpenAI(api_key=api_key)
                        with open(temp_audio, "rb") as audio_file:
                            transcript = client.audio.transcriptions.create(
                                model="whisper-1",
                                file=audio_file,
                                language="es"
                            )
                        text = transcript.text
                    except Exception as e:
                        text = f"[Error en la IA de OpenAI: {str(e)}]"
                        
                with open(output_path, 'w', encoding='utf-8') as f: f.write(text)
            
            elif action == 'mp3':
                cmd = [FFMPEG_PATH, '-y', '-i', input_path, '-q:a', '0', '-map', 'a', output_path]
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                
            elif action == 'wav':
                cmd = [FFMPEG_PATH, '-y', '-i', input_path, '-vn', '-acodec', 'pcm_s16le', '-ar', '44100', output_path]
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                
            else: 
                cmd = [FFMPEG_PATH, '-y', '-i', input_path, '-c:v', 'libx264', '-preset', 'fast', '-crf', '28', '-c:a', 'aac', output_path]
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

            response_file = send_file(output_path, mimetype=mimetype_str, as_attachment=True, download_name=download_name)
            return response_file
            
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.decode('utf-8')[-200:] if e.stderr else 'Error desconocido procesando media'
            return f"Error interno de video: {error_msg}", 500
        except Exception as e:
            return f"Error general: {str(e)}", 500
        finally:
            for p in [input_path, temp_audio]:
                if os.path.exists(p):
                    try: os.remove(p)
                    except: pass
    else: return f"Formato no soportado. Extensiones permitidas: {', '.join(ALLOWED_EXTENSIONS)}", 400

# ==========================================
# PANEL DE ADMINISTRADOR
# ==========================================
@app.route('/panel-admin')
def admin_panel():
    return render_template('admin.html')

@app.route('/api/stats')
def get_stats():
    # Devuelve el archivo JSON con los datos al frontend
    return jsonify(load_stats())

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=8000, use_reloader=False)