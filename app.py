import os
import time
import json
import threading
import re
import uuid
import yt_dlp
import imageio_ffmpeg
from flask import Flask, render_template, request, send_file, jsonify, Response, stream_with_context
from werkzeug.utils import secure_filename
from moviepy.editor import VideoFileClip
from openai import OpenAI

app = Flask(__name__)

# Configuración
DOWNLOAD_FOLDER = 'descargas'
UPLOAD_FOLDER = 'uploads'
COOKIES_FILE = 'cookies.txt' 
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024 

ALLOWED_EXTENSIONS = {'mov', 'mp4'}
tasks = {}

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
# MOTOR 2: PROCESADOR IA Y ARCHIVOS LOCALES
# ==========================================
@app.route('/process_media', methods=['POST'])
def process_media():
    if 'video' not in request.files: return "No se encontró el archivo", 400
    file = request.files['video']
    action = request.form.get('action', 'txt')

    if file.filename == '': return "Ningún archivo seleccionado", 400
    if file and allowed_file(file.filename):
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
            clip = VideoFileClip(input_path)
            
            if action == 'txt':
                # Extraemos el audio en MP3 ligero para mandarlo a OpenAI
                clip.audio.write_audiofile(temp_audio, fps=16000, nbytes=2, codec='libmp3lame', bitrate='64k', logger=None)
                clip.close()
                
                api_key = os.environ.get("OPENAI_API_KEY")
                if not api_key:
                    text = "[Error: Falta la clave API de OpenAI. Configúrala en Render como 'OPENAI_API_KEY']"
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
                clip.audio.write_audiofile(output_path, fps=44100, nbytes=2, codec='libmp3lame', bitrate='192k', logger=None)
                clip.close()
            elif action == 'wav':
                clip.audio.write_audiofile(output_path, fps=44100, nbytes=2, codec='pcm_s16le', logger=None)
                clip.close()
            else:
                clip.write_videofile(output_path, codec="libx264", audio_codec="aac", logger=None)
                clip.close()

            # Blindaje Final
            response_file = send_file(output_path, mimetype=mimetype_str, as_attachment=True, download_name=download_name)
            return response_file
            
        except Exception as e: return f"Error en el proceso: {str(e)}", 500
        finally:
            for p in [input_path, temp_audio]:
                if os.path.exists(p):
                    try: os.remove(p)
                    except: pass
    else: return "Formato no válido. Sube un archivo .mov o .mp4", 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True, port=8000, use_reloader=False)