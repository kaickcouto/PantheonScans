import http.server
import json
import os
import urllib.parse
import uuid
import threading
import asyncio
import webbrowser
from engine import MangaEngine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

engine = MangaEngine(data_dir=DATA_DIR)

# Estado dos jobs de traducao em memoria
JOBS = {}

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

        # 2. Rota de Biblioteca (API)
        if path == "/api/library":
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
                                        cover_url = f"/images/{series_slug}/{mdata.get('chapter_num')}/{cover_file}" if cover_file else None
                                        chapters.append({
                                            "series_slug": mdata.get("series_slug", series_slug),
                                            "series_title": mdata.get("series_title", series_slug.replace("-", " ").title()),
                                            "chapter_num": mdata.get("chapter_num"),
                                            "total_pages": mdata.get("total_pages", 0),
                                            "cover_url": cover_url
                                        })
                                except Exception as e:
                                    print("Erro lendo metadata:", e)

            # Ordena por capitulo decrescente
            chapters.sort(key=lambda x: int(x["chapter_num"]) if str(x["chapter_num"]).isdigit() else 0, reverse=True)
            res_json = json.dumps({"chapters": chapters}, ensure_ascii=False).encode("utf-8")
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
            html = html.replace("{{ chapter_num }}", str(cap_num))
            html = html.replace("{{ total_pages }}", str(mdata.get("total_pages", len(page_urls))))

            # Injeta imagens
            loop_marker = "{% for img_src in page_urls %}\n            <img src=\"{{ img_src }}\" alt=\"Página {{ loop.index }}\" loading=\"lazy\">\n        {% endfor %}"
            if loop_marker in html:
                html = html.replace(loop_marker, rendered_images)
            else:
                html = html.replace("{% for img_src in page_urls %}", "").replace("{% endfor %}", rendered_images)

            # Botoes de navegacao
            prev_url = mdata.get("prev_url")
            next_url = mdata.get("next_url")
            
            if prev_url:
                html = html.replace("{% if prev_url %}", "").replace("{% endif %}", "")
                html = html.replace("{{ prev_url }}", prev_url)
            else:
                # Remove bloco prev_url
                html = re.sub(r'\{% if prev_url %\}.*?\{% endif %\}', '', html, flags=re.DOTALL)

            if next_url:
                html = html.replace("{% if next_url %}", "").replace("{% endif %}", "")
                html = html.replace("{{ next_url }}", next_url)
            else:
                html = re.sub(r'\{% if next_url %\}.*?\{% endif %\}', '', html, flags=re.DOTALL)

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
                    mime = "image/png" if filename.endswith(".png") else "image/webp"
                    with open(img_path, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    self.wfile.write(data)
                    return

            self.send_error(404, "Imagem não encontrada")
            return

        self.send_error(404, "Não encontrado")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

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

            job_id = str(uuid.uuid4())
            JOBS[job_id] = {
                "status": "running",
                "percent": 5,
                "message": "Iniciando processamento...",
                "series_slug": None,
                "chapter_num": None
            }

            def progress_callback(status_dict):
                JOBS[job_id].update(status_dict)

            def task_runner():
                try:
                    res = run_async_task(engine.process_chapter(url, progress_callback))
                    if res:
                        JOBS[job_id]["series_slug"] = res["series_slug"]
                        JOBS[job_id]["chapter_num"] = res["chapter_num"]
                        JOBS[job_id]["status"] = "completed"
                        JOBS[job_id]["percent"] = 100
                    else:
                        JOBS[job_id]["status"] = "error"
                        JOBS[job_id]["message"] = "Falha ao processar capítulo."
                except Exception as e:
                    JOBS[job_id]["status"] = "error"
                    JOBS[job_id]["message"] = f"Erro: {str(e)}"

            threading.Thread(target=task_runner, daemon=True).start()

            res_json = json.dumps({"job_id": job_id}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(res_json)))
            self.end_headers()
            self.wfile.write(res_json)
            return

        self.send_error(404, "Rota não encontrada")

def start_server(port=5000):
    # Importa capitulo 2 existente para a biblioteca se existir
    scratch_dir = os.path.dirname(BASE_DIR)
    cap2_old = os.path.join(scratch_dir, "capitulo_2")
    if os.path.exists(cap2_old):
        series_dir = os.path.join(DATA_DIR, "return-of-the-first-generation", "capitulo_2")
        if not os.path.exists(series_dir):
            import shutil
            os.makedirs(os.path.join(series_dir, "traduzido"), exist_ok=True)
            old_trad = os.path.join(cap2_old, "traduzido")
            if os.path.exists(old_trad):
                for f in os.listdir(old_trad):
                    shutil.copy2(os.path.join(old_trad, f), os.path.join(series_dir, "traduzido", f))
                
                # Cria metadata para aparecer na biblioteca
                meta = {
                    "series_slug": "return-of-the-first-generation",
                    "series_title": "Return Of The First Generation The Strongest In History Reincarnates",
                    "chapter_num": "2",
                    "total_pages": 34,
                    "next_url": "https://kaynscans.com/series/comic/return-of-the-first-generation-the-strongest-in-history-reincarnates-as-his-descendant-1000-year/chapter/3",
                    "prev_url": "https://kaynscans.com/series/comic/return-of-the-first-generation-the-strongest-in-history-reincarnates-as-his-descendant-1000-year/chapter/1",
                    "url": "https://kaynscans.com/series/comic/return-of-the-first-generation-the-strongest-in-history-reincarnates-as-his-descendant-1000-year/chapter/2",
                    "translated_pages": sorted(os.listdir(os.path.join(series_dir, "traduzido"))),
                    "cover": "pagina_001_pt.png"
                }
                with open(os.path.join(series_dir, "metadata.json"), "w", encoding="utf-8") as mf:
                    json.dump(meta, mf, indent=2, ensure_ascii=False)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', port), MangaAppHandler)
    print(f"[*] Manga Translator Web App ativo em: http://localhost:{port}")
    webbrowser.open(f"http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Servidor encerrado.")

if __name__ == "__main__":
    start_server(5000)
