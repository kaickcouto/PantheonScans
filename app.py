import http.server
import json
import os
import re
import urllib.parse
import uuid
import threading
import asyncio
import webbrowser
import subprocess
import time
from engine import MangaEngine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

engine = MangaEngine(data_dir=DATA_DIR)

# Estado dos jobs de traducao em memoria
JOBS = {}

LAST_UPDATE_CHECK = 0
CACHED_UPDATE_INFO = None

def check_update(base_dir, force=False):
    global LAST_UPDATE_CHECK, CACHED_UPDATE_INFO
    now = time.time()
    if not force and CACHED_UPDATE_INFO and (now - LAST_UPDATE_CHECK < 300):
        return CACHED_UPDATE_INFO

    local_version = "1.0.0"
    pkg_path = os.path.join(base_dir, "package.json")
    if os.path.exists(pkg_path):
        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                local_version = json.load(f).get("version", "1.0.0")
        except Exception:
            pass

    is_git = os.path.exists(os.path.join(base_dir, ".git"))
    local_commit = None
    if is_git:
        try:
            res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=base_dir, capture_output=True, text=True, encoding="utf-8", errors="ignore")
            if res.returncode == 0 and res.stdout:
                local_commit = res.stdout.strip()
        except Exception:
            pass

    repo = "kaickcouto/PantheonScans"
    remote_commit = None
    commit_message = None
    commit_date = None
    has_update = False

    try:
        cmd = ["curl.exe", "-s", "-H", "User-Agent: PantheonScans-App", f"https://api.github.com/repos/{repo}/commits/main"]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            remote_commit = data.get("sha")
            commit_obj = data.get("commit", {})
            commit_message = commit_obj.get("message", "").splitlines()[0] if commit_obj.get("message") else ""
            commit_date = commit_obj.get("author", {}).get("date")
    except Exception:
        pass

    remote_version = local_version
    try:
        cmd_v = ["curl.exe", "-s", f"https://raw.githubusercontent.com/{repo}/main/package.json"]
        res_v = subprocess.run(cmd_v, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if res_v.returncode == 0 and res_v.stdout:
            pkg_rem = json.loads(res_v.stdout)
            remote_version = pkg_rem.get("version", local_version)
    except Exception:
        pass

    if local_commit and remote_commit:
        has_update = (local_commit != remote_commit)
    elif remote_version and local_version:
        has_update = (remote_version != local_version)

    info = {
        "has_update": has_update,
        "local_version": local_version,
        "remote_version": remote_version,
        "local_commit": local_commit[:7] if local_commit else None,
        "remote_commit": remote_commit[:7] if remote_commit else None,
        "commit_message": commit_message,
        "commit_date": commit_date,
        "is_git": is_git,
        "repo_url": f"https://github.com/{repo}"
    }

    LAST_UPDATE_CHECK = now
    CACHED_UPDATE_INFO = info
    return info

def apply_update(base_dir):
    is_git = os.path.exists(os.path.join(base_dir, ".git"))
    if not is_git:
        return {
            "success": False,
            "manual": True,
            "download_url": "https://github.com/kaickcouto/PantheonScans/archive/refs/heads/main.zip",
            "message": "Instalação sem Git detectada. Baixe a versão mais recente em formato ZIP no repositório."
        }

    try:
        res = subprocess.run(["git", "pull", "origin", "main"], cwd=base_dir, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        if res.returncode != 0:
            return {
                "success": False,
                "message": f"Erro no git pull: {res.stderr or res.stdout}"
            }

        try:
            subprocess.run(["pip", "install", "-r", "requirements.txt", "--quiet"], cwd=base_dir, capture_output=True, text=True, encoding="utf-8", errors="ignore")
        except Exception:
            pass

        global CACHED_UPDATE_INFO, LAST_UPDATE_CHECK
        CACHED_UPDATE_INFO = None
        LAST_UPDATE_CHECK = 0

        return {
            "success": True,
            "message": "PantheonScans atualizado com sucesso para a versão mais recente do GitHub! Reinicie o servidor para carregar as alterações.",
            "output": res.stdout.strip()
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Falha ao executar atualização: {str(e)}"
        }

def run_async_task(coro):
    """Executa uma corotina asyncio em uma nova thread"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

class MangaAppHandler(http.server.BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 1. Rota Principal (Dashboard)
        if path == "/" or path == "/index.html":
            index_file = os.path.join(TEMPLATES_DIR, "index.html")
            with open(index_file, "r", encoding="utf-8") as f:
                content = f.read().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        # 2. Rota de Biblioteca (API - Agrupada por Obra)
        if path == "/api/library":
            series_dict = {}
            chapters = []
            if os.path.exists(DATA_DIR):
                for series_slug in os.listdir(DATA_DIR):
                    series_path = os.path.join(DATA_DIR, series_slug)
                    if os.path.isdir(series_path):
                        for cap_folder in os.listdir(series_path):
                            meta_file = os.path.join(series_path, cap_folder, "metadata.json")
                            if os.path.exists(meta_file):
                                try:
                                    with open(meta_file, "r", encoding="utf-8") as mf:
                                        mdata = json.load(mf)
                                        cover_file = mdata.get("cover")
                                        cap_num = str(mdata.get("chapter_num", "1"))
                                        cover_url = f"/images/{series_slug}/{cap_num}/{cover_file}" if cover_file else None
                                        chap_obj = {
                                            "series_slug": mdata.get("series_slug", series_slug),
                                            "series_title": mdata.get("series_title", series_slug.replace("-", " ").title()),
                                            "chapter_num": cap_num,
                                            "total_pages": mdata.get("total_pages", 0),
                                            "cover_url": cover_url
                                        }
                                        chapters.append(chap_obj)

                                        if series_slug not in series_dict:
                                            series_dict[series_slug] = {
                                                "series_slug": series_slug,
                                                "series_title": chap_obj["series_title"],
                                                "cover_url": cover_url,
                                                "chapters": []
                                            }
                                        series_dict[series_slug]["chapters"].append(chap_obj)
                                        if cap_num == "1" or not series_dict[series_slug]["cover_url"]:
                                            series_dict[series_slug]["cover_url"] = cover_url
                                except Exception as e:
                                    print("Erro lendo metadata:", e)

            # Processa e ordena cada série
            series_list = []
            for s_slug, s_data in series_dict.items():
                s_data["chapters"].sort(key=lambda x: int(x["chapter_num"]) if str(x["chapter_num"]).isdigit() else 0, reverse=True)
                s_data["total_chapters"] = len(s_data["chapters"])
                s_data["latest_chapter"] = s_data["chapters"][0]["chapter_num"] if s_data["chapters"] else "0"
                s_data["first_chapter"] = s_data["chapters"][-1]["chapter_num"] if s_data["chapters"] else "0"
                s_data["total_pages"] = sum(c.get("total_pages", 0) for c in s_data["chapters"])
                series_list.append(s_data)

            series_list.sort(key=lambda x: x["series_title"].lower())
            chapters.sort(key=lambda x: int(x["chapter_num"]) if str(x["chapter_num"]).isdigit() else 0, reverse=True)

            res_json = json.dumps({"series": series_list, "chapters": chapters}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res_json)))
            self.end_headers()
            self.wfile.write(res_json)
            return

        # 3. Rota de Progresso (API)
        if path == "/api/progress":
            job_id = query.get("job_id", [None])[0]
            job = JOBS.get(job_id, {"status": "error", "percent": 0, "message": "Job não encontrado"})
            res_json = json.dumps(job, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res_json)))
            self.end_headers()
            self.wfile.write(res_json)
            return

        # 3.2 Rota de Verificação de Atualização (GitHub)
        if path == "/api/check_update":
            force = query.get("force", ["0"])[0] in ["1", "true"]
            info = check_update(BASE_DIR, force=force)
            res_json = json.dumps(info, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res_json)))
            self.end_headers()
            self.wfile.write(res_json)
            return

        # 3.1 Rota de Exportação para CBZ (Comic Book Zip)
        if path == "/api/export/cbz":
            series_slug = query.get("series", [None])[0]
            cap_num = query.get("chapter", [None])[0]
            if not series_slug or not cap_num:
                self.send_error(400, "Parâmetros 'series' e 'chapter' são obrigatórios")
                return

            trad_dir = os.path.join(DATA_DIR, series_slug, f"capitulo_{cap_num}", "traduzido")
            if not os.path.exists(trad_dir):
                self.send_error(404, "Capítulo traduzido não encontrado para exportar")
                return

            import io
            import zipfile
            buffer = io.BytesIO()
            
            # Carrega metadata do capitulo se disponivel
            meta_file = os.path.join(DATA_DIR, series_slug, f"capitulo_{cap_num}", "metadata.json")
            mdata = {}
            if os.path.exists(meta_file):
                try:
                    with open(meta_file, "r", encoding="utf-8") as mf:
                        mdata = json.load(mf)
                except Exception:
                    pass

            stitle = mdata.get("series_title", series_slug.replace("-", " ").title())
            orig_url = mdata.get("url", "")
            img_files = sorted([f for f in os.listdir(trad_dir) if os.path.isfile(os.path.join(trad_dir, f))])

            comic_info = f"""<?xml version="1.0" encoding="utf-8"?>
<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Title>Capítulo {cap_num}</Title>
  <Series>{stitle}</Series>
  <Number>{cap_num}</Number>
  <LanguageISO>pt</LanguageISO>
  <Format>Webtoon</Format>
  <PageCount>{len(img_files)}</PageCount>
  <Translator>PantheonScans Turbo Engine</Translator>
  <Web>{orig_url}</Web>
  <Manga>Yes</Manga>
</ComicInfo>"""

            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
                zf.writestr("ComicInfo.xml", comic_info.strip().encode("utf-8"))
                for img_name in img_files:
                    img_path = os.path.join(trad_dir, img_name)
                    zf.write(img_path, arcname=img_name)

            data = buffer.getvalue()
            filename = f"{series_slug}_cap_{cap_num}.cbz"
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.comicbook+zip")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # 3.2 Rota de Exportação de Série Completa (.ZIP contendo todos os capítulos em CBZ)
        if path == "/api/export/series_cbz":
            series_slug = query.get("series", [None])[0]
            if not series_slug:
                self.send_error(400, "Parâmetro 'series' é obrigatório")
                return

            series_dir = os.path.join(DATA_DIR, series_slug)
            if not os.path.exists(series_dir):
                self.send_error(404, "Série não encontrada")
                return

            import io
            import zipfile
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf_master:
                for cap_folder in sorted(os.listdir(series_dir)):
                    trad_dir = os.path.join(series_dir, cap_folder, "traduzido")
                    if os.path.isdir(trad_dir):
                        cap_match = re.search(r'(\d+)', cap_folder)
                        c_num = cap_match.group(1) if cap_match else cap_folder
                        cap_buf = io.BytesIO()
                        img_files = sorted([f for f in os.listdir(trad_dir) if os.path.isfile(os.path.join(trad_dir, f))])
                        with zipfile.ZipFile(cap_buf, "w", zipfile.ZIP_STORED) as zf_cap:
                            cinfo = f"""<?xml version="1.0" encoding="utf-8"?>
<ComicInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Title>Capítulo {c_num}</Title>
  <Series>{series_slug.replace('-', ' ').title()}</Series>
  <Number>{c_num}</Number>
  <LanguageISO>pt</LanguageISO>
  <Format>Webtoon</Format>
  <PageCount>{len(img_files)}</PageCount>
  <Translator>PantheonScans Turbo Engine</Translator>
</ComicInfo>"""
                            zf_cap.writestr("ComicInfo.xml", cinfo.strip().encode("utf-8"))
                            for img_name in img_files:
                                zf_cap.write(os.path.join(trad_dir, img_name), arcname=img_name)
                        zf_master.writestr(f"{series_slug}_cap_{c_num}.cbz", cap_buf.getvalue())

            data = buffer.getvalue()
            filename = f"{series_slug}_todos_capitulos.zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # 4. Rota do Leitor (Visualizador Webtoon)
        if path == "/read":
            series_slug = query.get("series", [None])[0]
            cap_num = query.get("chapter", [None])[0]

            if not series_slug or not cap_num:
                self.send_error(400, "Parâmetros 'series' e 'chapter' são obrigatórios")
                return

            meta_file = os.path.join(DATA_DIR, series_slug, f"capitulo_{cap_num}", "metadata.json")
            if not os.path.exists(meta_file):
                self.send_error(404, f"Capítulo {cap_num} não encontrado na biblioteca local.")
                return

            with open(meta_file, "r", encoding="utf-8") as mf:
                mdata = json.load(mf)

            reader_file = os.path.join(TEMPLATES_DIR, "reader.html")
            with open(reader_file, "r", encoding="utf-8") as rf:
                template = rf.read()

            # Renderização de templates simples
            page_urls = [f"/images/{series_slug}/{cap_num}/{p}" for p in mdata.get("translated_pages", [])]
            rendered_images = "".join([f'<img src="{u}" alt="Página {i+1}" loading="lazy">' for i, u in enumerate(page_urls)])

            # Substitui placeholders
            html = template.replace("{{ series_title }}", mdata.get("series_title", series_slug))
            html = html.replace("{{ series_slug }}", series_slug)
            html = html.replace("{{ chapter_num }}", str(cap_num))
            html = html.replace("{{ total_pages }}", str(mdata.get("total_pages", len(page_urls))))

            # Injeta imagens de forma robusta independente de quebras de linha
            html = re.sub(r'\{%\s*for\s+img_src\s+in\s+page_urls\s*%\}.*?\{%\s*endfor\s*%\}', rendered_images, html, flags=re.DOTALL)

            # Botoes de navegacao
            prev_url = mdata.get("prev_url")
            next_url = mdata.get("next_url")
            
            if prev_url:
                html = re.sub(r'\{%\s*if\s+prev_url\s*%\}(.*?)\{%\s*endif\s*%\}', r'\1', html, flags=re.DOTALL)
                html = html.replace("{{ prev_url }}", prev_url)
            else:
                html = re.sub(r'\{%\s*if\s+prev_url\s*%\}.*?\{%\s*endif\s*%\}', '', html, flags=re.DOTALL)
                html = html.replace("{{ prev_url }}", "")

            if next_url:
                html = re.sub(r'\{%\s*if\s+next_url\s*%\}(.*?)\{%\s*endif\s*%\}', r'\1', html, flags=re.DOTALL)
                html = html.replace("{{ next_url }}", next_url)
            else:
                html = re.sub(r'\{%\s*if\s+next_url\s*%\}.*?\{%\s*endif\s*%\}', '', html, flags=re.DOTALL)
                html = html.replace("{{ next_url }}", "")

            content = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        # 5. Servir Imagens Locais
        # Formato: /images/<series_slug>/<chapter_num>/<filename>
        if path.startswith("/images/"):
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 4:
                series_slug = parts[1]
                cap_num = parts[2]
                filename = parts[3]
                
                # Procura primeiro em traduzido, depois original
                img_path = os.path.join(DATA_DIR, series_slug, f"capitulo_{cap_num}", "traduzido", filename)
                if not os.path.exists(img_path):
                    img_path = os.path.join(DATA_DIR, series_slug, f"capitulo_{cap_num}", "original", filename)

                if os.path.exists(img_path):
                    stat = os.stat(img_path)
                    last_modified = self.date_time_string(stat.st_mtime)
                    
                    if self.headers.get('If-Modified-Since') == last_modified:
                        self.send_response(304)
                        self.end_headers()
                        return

                    mime = "image/png" if filename.endswith(".png") else "image/webp"
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(stat.st_size))
                    self.send_header("Last-Modified", last_modified)
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    
                    with open(img_path, "rb") as f:
                        while chunk := f.read(64 * 1024):
                            self.wfile.write(chunk)
                    return

            self.send_error(404, "Imagem não encontrada")
            return

        self.send_error(404, "Não encontrado")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # Rota de Auto Update
        if path == "/api/update":
            result = apply_update(BASE_DIR)
            res_json = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res_json)))
            self.end_headers()
            self.wfile.write(res_json)
            return

        if path == "/api/translate":
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body.decode('utf-8'))
                url = data.get("url")
            except Exception:
                self.send_error(400, "JSON inválido")
                return

            if not url:
                self.send_error(400, "URL é obrigatória")
                return

            # 1. Se o capítulo já foi traduzido no disco, retorna instantaneamente
            url_match = re.search(r'/([^/]+)/chapter/(\d+)', url.rstrip('/'))
            if url_match:
                s_slug, c_num = url_match.group(1), url_match.group(2)
                meta_path = os.path.join(DATA_DIR, s_slug, f"capitulo_{c_num}", "metadata.json")
                if os.path.exists(meta_path):
                    try:
                        with open(meta_path, "r", encoding="utf-8") as mf:
                            mdata = json.load(mf)
                        trad_first = os.path.join(DATA_DIR, s_slug, f"capitulo_{c_num}", "traduzido", mdata.get("translated_pages", ["none"])[0])
                        if mdata.get("translated_pages") and os.path.exists(trad_first):
                            res_json = json.dumps({
                                "job_id": None,
                                "status": "completed",
                                "series_slug": s_slug,
                                "chapter_num": c_num,
                                "already_completed": True
                            }).encode("utf-8")
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(res_json)))
                            self.end_headers()
                            self.wfile.write(res_json)
                            return
                    except Exception:
                        pass

            # 2. Se já existe um job em andamento ou concluído para esta URL, reutiliza
            for existing_id, existing_job in JOBS.items():
                if existing_job.get("url") == url and existing_job.get("status") in ("running", "completed"):
                    res_json = json.dumps({
                        "job_id": existing_id,
                        "status": existing_job.get("status"),
                        "series_slug": existing_job.get("series_slug"),
                        "chapter_num": existing_job.get("chapter_num"),
                        "already_running": True
                    }).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(res_json)))
                    self.end_headers()
                    self.wfile.write(res_json)
                    return

            job_id = str(uuid.uuid4())
            JOBS[job_id] = {
                "url": url,
                "status": "running",
                "percent": 5,
                "message": "Iniciando processamento...",
                "series_slug": None,
                "chapter_num": None,
                "next_url": None
            }

            def progress_callback(status_dict):
                JOBS[job_id].update(status_dict)

            def task_runner():
                try:
                    res = run_async_task(engine.process_chapter(url, progress_callback))
                    if res:
                        JOBS[job_id]["series_slug"] = res["series_slug"]
                        JOBS[job_id]["chapter_num"] = res["chapter_num"]
                        JOBS[job_id]["next_url"] = res.get("next_url")
                        JOBS[job_id]["status"] = "completed"
                        JOBS[job_id]["percent"] = 100
                    else:
                        JOBS[job_id]["status"] = "error"
                        JOBS[job_id]["message"] = "Falha ao processar capítulo."
                except Exception as e:
                    JOBS[job_id]["status"] = "error"
                    JOBS[job_id]["message"] = f"Erro: {str(e)}"

            threading.Thread(target=task_runner, daemon=True).start()

            # Evita acumulo de jobs em memoria
            if len(JOBS) > 50:
                oldest = list(JOBS.keys())[:20]
                for k in oldest:
                    del JOBS[k]

            res_json = json.dumps({"job_id": job_id}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res_json)))
            self.end_headers()
            self.wfile.write(res_json)
            return

        if path == "/api/delete":
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body.decode('utf-8'))
                series_slug = data.get("series_slug")
                chapter_num = data.get("chapter_num")
            except Exception:
                self.send_error(400, "JSON inválido")
                return

            if not series_slug:
                self.send_error(400, "series_slug é obrigatório")
                return

            import shutil
            if chapter_num and str(chapter_num).lower() != "all":
                cap_dir = os.path.join(DATA_DIR, series_slug, f"capitulo_{chapter_num}")
                if os.path.exists(cap_dir):
                    shutil.rmtree(cap_dir, ignore_errors=True)
                series_dir = os.path.join(DATA_DIR, series_slug)
                if os.path.exists(series_dir) and not os.listdir(series_dir):
                    shutil.rmtree(series_dir, ignore_errors=True)
            else:
                series_dir = os.path.join(DATA_DIR, series_slug)
                if os.path.exists(series_dir):
                    shutil.rmtree(series_dir, ignore_errors=True)

            res_json = json.dumps({"status": "deleted"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res_json)))
            self.end_headers()
            self.wfile.write(res_json)
            return

        self.send_error(404, "Rota não encontrada")

def free_port(port):
    """Libera a porta matando processos zumbis anteriores se existirem"""
    try:
        import subprocess
        cmd = f'powershell -NoProfile -Command "$p = (Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue).OwningProcess; if ($p -and $p -ne $PID) {{ Stop-Process -Id $p -Force }}"'
        subprocess.run(cmd, shell=True, capture_output=True)
        import time
        time.sleep(0.5)
    except Exception:
        pass

def start_server(port=5000):
    free_port(port)
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    try:
        server = http.server.ThreadingHTTPServer(('127.0.0.1', port), MangaAppHandler)
    except OSError:
        free_port(port)
        try:
            server = http.server.ThreadingHTTPServer(('127.0.0.1', port), MangaAppHandler)
        except Exception as e:
            print(f"\n[!] Erro fatal ao abrir porta {port}: {e}")
            input("\nPressione Enter para sair...")
            return

    print(f"[*] Manga Translator Web App ativo em: http://localhost:{port}")
    webbrowser.open(f"http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Servidor encerrado.")

if __name__ == "__main__":
    start_server(5000)
