import urllib.request
import re
import os
import asyncio
import httpx
from chrome_lens_py import LensAPI

class MangaEngine:
    def __init__(self, data_dir=None):
        if not data_dir:
            data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)

    def extract_chapter_info(self, url):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://kaynscans.com/"
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode("utf-8")

        # Extrai blocos Next.js
        chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL)
        all_text = "".join(chunks).replace('\\"', '"').replace('\\\\', '\\')

        # Extrai imagens
        image_paths = re.findall(r'"imageUrl":"(/uploads/[^"]+)"', all_text)
        seen = set()
        pages = []
        for p in image_paths:
            if p not in seen:
                seen.add(p)
                pages.append(p)

        # Extrai nome da serie e capitulo
        series_match = re.search(r'"series":\{"id":"[^"]+","slug":"([^"]+)","title":"([^"]+)"', all_text)
        if series_match:
            series_slug = series_match.group(1)
            series_title = series_match.group(2)
        else:
            parts = [p for p in url.split("/") if p]
            series_slug = parts[-3] if len(parts) >= 3 else "manga"
            series_title = series_slug.replace("-", " ").title()

        # Numero do capitulo
        cap_match = re.search(r'/chapter/(\d+)', url)
        cap_num = cap_match.group(1) if cap_match else "1"

        # Tenta achar proximo capitulo
        next_cap_num = str(int(cap_num) + 1) if cap_num.isdigit() else None
        base_series_url = url.split("/chapter/")[0] if "/chapter/" in url else url
        next_url = f"{base_series_url}/chapter/{next_cap_num}" if next_cap_num else None
        prev_url = f"{base_series_url}/chapter/{int(cap_num)-1}" if cap_num.isdigit() and int(cap_num) > 1 else None

        return {
            "series_slug": series_slug,
            "series_title": series_title,
            "chapter_num": cap_num,
            "pages": pages,
            "next_url": next_url,
            "prev_url": prev_url,
            "url": url
        }

    async def download_image(self, client, img_url, out_path, headers):
        if os.path.exists(out_path):
            return out_path
        resp = await client.get(img_url, headers=headers, timeout=30.0)
        if resp.status_code == 200:
            with open(out_path, "wb") as f:
                f.write(resp.content)
            return out_path
        raise Exception(f"Status {resp.status_code}")

    async def process_chapter(self, url, progress_callback=None):
        """
        Download e traducao turbo paralela com LensAPI
        """
        def report(status, step, percent, message):
            if progress_callback:
                progress_callback({
                    "status": status,
                    "step": step,
                    "percent": percent,
                    "message": message
                })

        report("running", "inspecting", 5, "Conectando ao site do mangá...")
        info = self.extract_chapter_info(url)
        pages = info["pages"]
        if not pages:
            report("error", "inspecting", 100, "Nenhuma página encontrada para este capítulo.")
            return None

        total_pages = len(pages)
        report("running", "downloading", 10, f"Encontradas {total_pages} páginas. Iniciando download Turbo...")

        # Cria pastas organizadas: data/<series_slug>/<chapter_num>/
        chapter_dir = os.path.join(self.data_dir, info["series_slug"], f"capitulo_{info['chapter_num']}")
        orig_dir = os.path.join(chapter_dir, "original")
        trad_dir = os.path.join(chapter_dir, "traduzido")
        os.makedirs(orig_dir, exist_ok=True)
        os.makedirs(trad_dir, exist_ok=True)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://kaynscans.com/"
        }

        # 1. Download paralelo das imagens originais
        downloaded_paths = [None] * total_pages
        download_sem = asyncio.Semaphore(6)

        async def worker_download(i, path):
            img_url = "https://kaynscans.com" + path
            filename = f"pagina_{i+1:03d}.webp"
            filepath = os.path.join(orig_dir, filename)
            async with download_sem:
                await self.download_image(client, img_url, filepath, headers)
                downloaded_paths[i] = filepath

        async with httpx.AsyncClient() as client:
            tasks = [worker_download(i, p) for i, p in enumerate(pages)]
            await asyncio.gather(*tasks)

        report("running", "download_done", 30, f"Todas as {total_pages} páginas baixadas! Iniciando Tradução Turbo...")

        # 2. Tradução paralela das imagens com Google Lens
        # Usamos 3 workers simultaneos para maxima velocidade sem throttling
        trans_sem = asyncio.Semaphore(3)
        lens = LensAPI()
        completed_count = 0
        translated_paths = [None] * total_pages

        async def worker_translate(i, orig_path):
            nonlocal completed_count
            filename = f"pagina_{i+1:03d}_pt.png"
            out_path = os.path.join(trad_dir, filename)

            if os.path.exists(out_path):
                translated_paths[i] = out_path
                completed_count += 1
                return

            async with trans_sem:
                try:
                    await lens.process_image(
                        image_path=orig_path,
                        target_translation_language="pt",
                        output_overlay_path=out_path,
                        manga_mode=True
                    )
                    translated_paths[i] = out_path
                except Exception as e:
                    # Em caso de falha pontual, usa o original
                    print(f"Aviso traducao pag {i+1}: {e}")
                    translated_paths[i] = orig_path

                completed_count += 1
                prog = 30 + int((completed_count / total_pages) * 65)
                report("running", "translating", prog, f"Traduzindo com IA: {completed_count}/{total_pages} páginas...")

        trans_tasks = [worker_translate(i, downloaded_paths[i]) for i in range(total_pages)]
        await asyncio.gather(*trans_tasks)
        await lens.aclose()

        # Salva metadata do capitulo
        meta = {
            "series_slug": info["series_slug"],
            "series_title": info["series_title"],
            "chapter_num": info["chapter_num"],
            "total_pages": total_pages,
            "next_url": info["next_url"],
            "prev_url": info["prev_url"],
            "url": url,
            "translated_pages": [os.path.basename(p) for p in translated_paths],
            "cover": os.path.basename(translated_paths[0]) if translated_paths else None
        }

        import json
        with open(os.path.join(chapter_dir, "metadata.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        report("completed", "done", 100, f"Capítulo {info['chapter_num']} 100% pronto!")
        return meta
