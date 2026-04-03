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
STATS_FILE = 'stats.json'
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 

ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'webm', 'flv', 'm4v', 'wmv'}
tasks = {}

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

def download_video_task(url, task_id, quality):
    import urllib.request, urllib.parse, json, os
    tasks[task_id] = {'progress': 0, 'status': 'running'}
    
    # 1. BALA DE PLATA PARA TIKTOK
    if "tiktok.com" in url or "vt.tiktok.com" in url:
        try:
            api_url = "https://www.tikwm.com/api/?url=" + urllib.parse.quote(url)
            req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())
            
            if data.get("code") == 0:
                play_url = data["data"]["play"]
                expected_filename = os.path.join(DOWNLOAD_FOLDER, f'{task_id}_raw.mp4')
                tasks[task_id]['progress'] = 50 
                
                req_vid = urllib.request.Request(play_url, headers={'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X)'})
                with urllib.request.urlopen(req_vid) as vid_resp, open(expected_filename, 'wb') as f:
                    f.write(vid_resp.read())
                
                tasks[task_id]['progress'] = 100
                tasks[task_id]['file_path'] = expected_filename
                tasks[task_id]['final_name'] = "TikTok_Video.mp4"
                tasks[task_id]['mime_type'] = 'video/mp4'
                tasks[task_id]['status'] = 'success'
                return
        except Exception as e:
            pass 

    # 2. MOTOR LIGERO PARA YT Y PINTEREST (Adiós cuelgues de memoria)
    ydl_opts = {
        # Magia: Pedimos el archivo ya unido. Ignoramos FFmpeg por completo.
        'format': 'best[ext=mp4]/best',
        'outtmpl': os.path.join(DOWNLOAD_FOLDER, f'{task_id}_raw.%(ext)s'),
        'restrictfilenames': True,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'cookiefile': COOKIES_FILE, 
        'progress_hooks': [lambda d: progress_hook(d, task_id)],
        'geo_bypass': True,
        'extractor_retries': 3,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'es-MX,es-ES;q=0.9,es;q=0.8,en-US;q=0.7,en;q=0.6',
        },
    }

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
        if "Private video" in clean_error or "login" in clean_error.lower(): tasks[task_id]['error_message'] = "❌ Video privado o necesita cookies."
        elif "Unsupported URL" in clean_error: tasks[task_id]['error_message'] = "❌ Enlace no soportado."
        else: tasks[task_id]['error_message'] = f"❌ Error: {clean_error[:50]}..."

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
    if task_id not in tasks:
        return jsonify({"error": "Tarea no encontrada."}), 404
        
    task = tasks[task_id]
    if task['status'] == 'success':
        return send_file(task['file_path'], mimetype=task.get('mime_type', 'video/mp4'), as_attachment=True, download_name=task.get('final_name', 'video.mp4'))
    elif task['status'] == 'error':
        # LE ENVIAMOS EL ERROR EXACTO A LA APP CON CÓDIGO 400
        return jsonify({"error": task.get('error_message', '❌ Error desconocido en el servidor.')}), 400
    else:
        return jsonify({"error": "Sigue procesando..."}), 404

@app.route('/process_media', methods=['POST'])
def process_media():
    if 'video' not in request.files: return "No se encontró el archivo", 400
    file = request.files['video']
    action = request.form.get('action', 'txt')

    if file.filename == '': return "Ningún archivo seleccionado", 400
    if file and allowed_file(file.filename):
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

@app.route('/panel-admin')
def admin_panel():
    return render_template('admin.html')

@app.route('/api/stats')
def get_stats():
    return jsonify(load_stats())

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=8000, use_reloader=False)