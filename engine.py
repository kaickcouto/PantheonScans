import json
import re
import os
import base64
import asyncio
import urllib.parse
import subprocess
import socket
import struct
import zlib
import shutil
from datetime import datetime, timezone
import httpx
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops
from chrome_lens_py import LensAPI
import chrome_lens_py.utils.font_fallback as ff
import chrome_lens_py.core.text_renderer as tr
from chrome_lens_py.core.text_renderer import render_translation_overlay

# REGRAS DE ARQUITETURA: Consulte GUIDELINES.md antes de alterar inpainting ou lettering.
# PROIBIDO: desenhar polígonos/rounded_rectangles sólidos sobre balões ou alterar paleta de cores.

# 1. Força uso prioritário das fontes de scanlation no Windows sem fallback silencioso para Segoe UI
ff.resolve_font_for_text = lambda text, preferred=None: preferred or (ff._candidates_for_text(text)[0].path if ff._candidates_for_text(text) else None)

# 2. Algoritmo Scanlation de Hifenização Silábica e Quebra Diamante / Oval
def split_long_word(word, font, max_w):
    """Divide palavras longas que excedem a largura do balão com hifenização elegante em português"""
    if font.getlength(word) <= max_w or len(word) < 5:
        return [word]
    w_lower = word.lower()
    n = len(word)
    candidates = []
    for i in range(2, n - 2):
        c1 = w_lower[i - 1]
        c2 = w_lower[i]
        if (c1 in 'rsx' and c2 in 'rscç') or (c1 == 'n' and c2 in 'st'):
            candidates.append(i)
        elif c1 in 'aeiouáéíóúâêôãõ' and c2 not in 'aeiouáéíóúâêôãõ' and i + 1 < n and w_lower[i + 1] in 'aeiouáéíóúâêôãõ':
            candidates.append(i)
        elif c1 in 'aeiouáéíóúâêôãõ' and c2 in 'bcdfptg' and i + 1 < n and w_lower[i + 1] in 'rl':
            candidates.append(i)

    best_split = None
    for cand in sorted(candidates, reverse=True):
        prefix = word[:cand] + '-'
        if font.getlength(prefix) <= max_w:
            best_split = cand
            break

    if best_split is None:
        for i in range(len(word) - 1, 1, -1):
            if font.getlength(word[:i] + '-') <= max_w:
                best_split = i
                break

    if best_split and best_split < len(word):
        part1 = word[:best_split] + '-'
        remainder = word[best_split:]
        return [part1] + split_long_word(remainder, font, max_w)
    return [word]

def comic_wrap(text, font, max_w, per_char=False, allow_split=False):
    effective_w = max_w * 0.88
    raw_words = text.split()
    if not raw_words:
        return [text]

    # Previne quebra de palavras normais durante a busca de tamanho de fonte
    for w in raw_words:
        if font.getlength(w) > effective_w:
            if not allow_split:
                return [' '.join(raw_words)]

    words = []
    for w in raw_words:
        if allow_split and font.getlength(w) > effective_w:
            words.extend(split_long_word(w, font, effective_w))
        else:
            words.append(w)

    if len(words) <= 1:
        return words if words else [text]
    if len(words) == 2:
        if font.getlength(text) <= effective_w:
            return [text]
        return words
    if len(words) == 3:
        if font.getlength(text) <= effective_w:
            return [text]
        w1, w2, w3 = words
        if font.getlength(w1 + ' ' + w2) <= effective_w:
            return [w1 + ' ' + w2, w3]
        return [w1, w2, w3]
    if len(words) == 4:
        w1, w2, w3, w4 = words
        l2 = w2 + ' ' + w3
        if font.getlength(l2) <= effective_w:
            return [w1, l2, w4]
        return [w1 + ' ' + w2, w3 + ' ' + w4]
    lines, cur = [], []
    for w in words:
        if font.getlength(' '.join(cur + [w])) <= effective_w:
            cur.append(w)
        else:
            if cur:
                lines.append(' '.join(cur))
            cur = [w]
    if cur:
        lines.append(' '.join(cur))

    # Anti-órfã: reequilibra última linha isolada para manter formato oval/diamante
    if len(lines) >= 2:
        last_w = lines[-1].split()
        if len(last_w) == 1:
            prev_w = lines[-2].split()
            if len(prev_w) >= 2:
                cand_prev = ' '.join(prev_w[:-1])
                cand_last = prev_w[-1] + ' ' + last_w[0]
                if font.getlength(cand_last) <= effective_w:
                    lines[-2] = cand_prev
                    lines[-1] = cand_last
    return lines

tr._wrap_text = comic_wrap

# 3. Renderizador de bloco de texto com margem de respiro vertical anti-corte
def custom_render_text_block(lines, font, box_w, box_h, fill, outline, outline_color, align="center"):
    ascent, descent = font.getmetrics()
    line_spacing = int((ascent + descent) * 1.12)
    total_text_h = len(lines) * line_spacing
    max_line_w = max((font.getlength(line) for line in lines), default=box_w)

    pad_x = 8
    pad_y = 6

    content_w = max(box_w, max_line_w)
    content_h = max(box_h, total_text_h)

    tile_w = max(1, round(content_w + pad_x * 2))
    tile_h = max(1, round(content_h + pad_y * 2))

    tile = Image.new("RGBA", (tile_w, tile_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)

    y = pad_y + max(2, int((content_h - total_text_h) / 2))
    for line in lines:
        advance = font.getlength(line)
        x = pad_x + (content_w - advance) / 2
        draw.text(
            (x, y),
            line,
            font=font,
            fill=fill,
            stroke_width=outline if outline_color else 0,
            stroke_fill=outline_color,
        )
        y += line_spacing
    return tile

tr._render_text_block = custom_render_text_block

# Salvaguarda 1: impede que linhas isoladas espremam letras contra as bordas
_orig_fit_font = tr.fit_font_size
tr.fit_font_size = lambda text, lf, bw, bh: _orig_fit_font(text, lf, bw * 1.15, bh * 1.10)

# Salvaguarda 2: normaliza rotação para IMPEDIR texto invertido/de cabeça para baixo (180 graus)
def safe_paste_rotated(canvas, tile, cx, cy, deg):
    while deg > 90:
        deg -= 180
    while deg < -90:
        deg += 180
    if abs(deg) < 15:
        deg = 0.0
    if deg != 0:
        tile = tile.rotate(deg, expand=True, resample=Image.Resampling.BICUBIC)
    canvas.alpha_composite(
        tile, (round(cx - tile.width / 2), round(cy - tile.height / 2))
    )

tr._paste_rotated = safe_paste_rotated

# Salvaguarda 3: suprime NATIVAMENTE retângulos/curativos por linha (elimina patches retangulares feios)
def safe_draw_background(canvas, tline, box_w, box_h, cx, cy, deg, aspect):
    if tline.HasField("background_image_data"):
        try:
            import io
            bgd = tline.background_image_data
            pad_w = bgd.horizontal_padding * box_h / aspect
            pad_h = bgd.vertical_padding * box_h
            patch_w = max(1, round(box_w + pad_w))
            patch_h = max(1, round(box_h + pad_h))
            patch = Image.open(io.BytesIO(bgd.background_image)).convert("RGBA")
            patch = patch.resize((patch_w, patch_h), Image.Resampling.BILINEAR)
            if patch_w > 8 and patch_h > 8:
                border = 3
                mask = Image.new('L', (patch_w - 2 * border, patch_h - 2 * border), 255)
                full_mask = Image.new('L', (patch_w, patch_h), 0)
                full_mask.paste(mask, (border, border))
                full_mask = full_mask.filter(ImageFilter.BoxBlur(1))
                orig_a = patch.split()[3]
                patch.putalpha(ImageChops.darker(full_mask, orig_a))
            safe_paste_rotated(canvas, patch, cx, cy, deg)
            return True
        except Exception:
            pass
    # NUNCA desenha retângulos sólidos por linha
    return False

tr._draw_background = safe_draw_background

# Salvaguarda 4: Pipeline Scanlation Limpo (Inpainting 100% Nativo + Fusão de Balão Anti-Sobreposição)
def scanlation_render_overlay(
    image,
    objects_response,
    font_path=None,
    draw_background=True,
    vertical_text="auto",
    erase_mode="patch",
    hull_padding=0.45,
    outline_scale=1.0,
    min_readable_px=14.0,
    text_align="center",
    manga_mode=True,
    manga_box_growth=1.10,
):
    import math
    canvas = image.convert("RGBA")
    width, height = canvas.size
    aspect = width / height

    paragraphs = objects_response.text.text_layout.paragraphs
    gleams = objects_response.deep_gleams

    # =========================================================================
    # FASE 1: Inpainting IA Puro (ZERO patches brancos sólidos ou curativos)
    # =========================================================================
    if draw_background:
        for index, paragraph in enumerate(paragraphs):
            if index >= len(gleams):
                break
            gleam = gleams[index]
            if not gleam.HasField("translation"):
                continue
            data = gleam.translation
            if data.status.code != 1 or not data.line or not data.translation.strip():
                continue

            for line_index, tline in enumerate(data.line):
                if line_index < len(paragraph.lines) and paragraph.lines[line_index].HasField("geometry"):
                    box = paragraph.lines[line_index].geometry.bounding_box
                    safe_draw_background(
                        canvas, tline, box.width * width, box.height * height,
                        box.center_x * width, box.center_y * height,
                        -math.degrees(box.rotation_z), aspect
                    )

    # =========================================================================
    # FASE 2: Agrupamento Inteligente de Parágrafos no Mesmo Balão (Anti-Colisão)
    # =========================================================================
    items = []
    for index, paragraph in enumerate(paragraphs):
        if index >= len(gleams):
            break
        gleam = gleams[index]
        if not gleam.HasField("translation"):
            continue
        data = gleam.translation
        if data.status.code != 1:
            continue

        raw_text = data.translation.strip()
        if not raw_text:
            continue

        if paragraph.HasField("geometry"):
            pbox = paragraph.geometry.bounding_box
        elif paragraph.lines:
            boxes = [l.geometry.bounding_box for l in paragraph.lines if l.HasField("geometry")]
            if boxes:
                min_x = min(b.center_x - b.width / 2 for b in boxes)
                max_x = max(b.center_x + b.width / 2 for b in boxes)
                min_y = min(b.center_y - b.height / 2 for b in boxes)
                max_y = max(b.center_y + b.height / 2 for b in boxes)
                pbox = type(boxes[0])()
                pbox.center_x = (min_x + max_x) / 2
                pbox.center_y = (min_y + max_y) / 2
                pbox.width = max(0.02, max_x - min_x)
                pbox.height = max(0.02, max_y - min_y)
                pbox.rotation_z = boxes[0].rotation_z
            else:
                continue
        else:
            continue

        style = data.line[0].style if data.line else None
        text_color = tr._argb_to_rgba(style.text_color) if style else (0, 0, 0, 255)
        bg_col = tr._argb_to_rgba(style.background_primary_color) if style else (255, 255, 255, 255)

        deg = -math.degrees(pbox.rotation_z)
        while deg > 90:
            deg -= 180
        while deg < -90:
            deg += 180
        if abs(deg) < 12:
            deg = 0.0

        x1 = (pbox.center_x - pbox.width / 2) * width
        x2 = (pbox.center_x + pbox.width / 2) * width
        y1 = (pbox.center_y - pbox.height / 2) * height
        y2 = (pbox.center_y + pbox.height / 2) * height

        items.append({
            "text": raw_text,
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "cx": pbox.center_x * width,
            "cy": pbox.center_y * height,
            "w": max(20.0, x2 - x1),
            "h": max(15.0, y2 - y1),
            "deg": deg,
            "num_lines": max(1, len(paragraph.lines)),
            "text_color": text_color,
            "bg_col": bg_col,
            "bg_lum": sum(bg_col[:3]),
        })

    # Agrupa parágrafos adjacentes que pertencem ao mesmo balão de diálogo
    merged_items = []
    used = [False] * len(items)

    for i in range(len(items)):
        if used[i]:
            continue
        cur = dict(items[i])
        used[i] = True

        changed = True
        while changed:
            changed = False
            for j in range(len(items)):
                if used[j]:
                    continue
                other = items[j]
                ox = max(0, min(cur["x2"], other["x2"]) - max(cur["x1"], other["x1"]))
                min_w = min(cur["w"], other["w"])
                horiz_match = (ox / max(1.0, min_w)) > 0.30 or (abs(cur["cx"] - other["cx"]) < max(cur["w"], other["w"]) * 0.55)

                gap_y = max(0, max(cur["y1"], other["y1"]) - min(cur["y2"], other["y2"]))
                avg_lh = max(16.0, (cur["h"] / max(1, cur["num_lines"]) + other["h"] / max(1, other["num_lines"])) / 2)
                vert_match = gap_y <= avg_lh * 2.2

                angle_match = abs(cur["deg"] - other["deg"]) < 15
                cur_light = cur["bg_lum"] > 250
                other_light = other["bg_lum"] > 250
                polarity_match = (cur_light == other_light)

                if horiz_match and vert_match and angle_match and polarity_match:
                    if other["cy"] > cur["cy"]:
                        cur["text"] = cur["text"].rstrip() + " " + other["text"].lstrip()
                    else:
                        cur["text"] = other["text"].rstrip() + " " + cur["text"].lstrip()
                    cur["x1"] = min(cur["x1"], other["x1"])
                    cur["x2"] = max(cur["x2"], other["x2"])
                    cur["y1"] = min(cur["y1"], other["y1"])
                    cur["y2"] = max(cur["y2"], other["y2"])
                    cur["w"] = cur["x2"] - cur["x1"]
                    cur["h"] = cur["y2"] - cur["y1"]
                    cur["cx"] = (cur["x1"] + cur["x2"]) / 2
                    cur["cy"] = (cur["y1"] + cur["y2"]) / 2
                    cur["num_lines"] += other["num_lines"]
                    used[j] = True
                    changed = True

        merged_items.append(cur)

    # =========================================================================
    # FASE 3: Lettering Scanlation Elegante (Sem cortes nem sobreposição)
    # =========================================================================
    for item in merged_items:
        text = item["text"].strip()
        if not text:
            continue

        pw = max(45.0, item["w"] * 1.08)
        ph = max(22.0, item["h"] * 1.05)

        line_font = tr.font_for_text(text, font_path)

        bg_lum = item["bg_lum"]
        if bg_lum > 320:
            text_color = (15, 15, 15, 255)
            outline_color = None
            outline_width = 0 # Tipografia limpa sem halo/névoa branca artificial
        else:
            text_color = (245, 245, 245, 255)
            outline_color = (item["bg_col"][0], item["bg_col"][1], item["bg_col"][2], 255)
            outline_width = 1

        is_shout = ('!' in text) or ('?!' in text) or (len(text.split()) <= 3 and text.isupper() and len(text) > 2)
        min_sz = 11 if not is_shout else 13
        max_sz = max(min_sz, min(int(ph * 0.70), int(pw * 0.40), 34 if is_shout else 28))

        best_font = None
        best_lines = [text]
        best_size = min_sz

        for sz in range(max_sz, min_sz - 1, -1):
            f = tr._load_font(line_font, sz)
            lines = comic_wrap(text, f, pw, allow_split=False)
            asc, dsc = f.getmetrics()
            spacing = int((asc + dsc) * 1.12)
            tot_h = len(lines) * spacing
            max_w = max((f.getlength(ln) for ln in lines), default=0)
            if max_w <= pw and tot_h <= ph * 1.18:
                best_size = sz
                best_font = f
                best_lines = lines
                break

        if best_font is None:
            best_size = min_sz
            best_font = tr._load_font(line_font, best_size)
            best_lines = comic_wrap(text, best_font, pw, allow_split=True)

        tile = custom_render_text_block(
            best_lines,
            best_font,
            pw,
            ph,
            text_color,
            outline_width,
            outline_color,
            text_align
        )

        safe_paste_rotated(
            canvas,
            tile,
            item["cx"],
            item["cy"],
            item["deg"]
        )

    return canvas

tr.render_translation_overlay = scanlation_render_overlay
render_translation_overlay = scanlation_render_overlay

# 4. Limpeza Avançada de Erros de OCR em Balões de Quadrinhos
def clean_ocr_artifacts(text):
    if not text:
        return ""
    t = text.strip()
    # 1. Hífen de quebra de linha de balão (ex: trans- formation -> transformation)
    t = re.sub(r'(\b\w+)-\s+(\w+\b)', r'\1\2', t)
    # 2. Contratações despedaçadas por OCR (ex: don ' t -> don't, I ' m -> I'm, do n't -> don't)
    t = re.sub(r"\b(do|ca|wo|did|could|would|should|is|are|was|were|has|have|had)\s+n['`´\"]t\b", r"\1n't", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(do|ca|wo|did|could|would|should|is|are|was|were|has|have|had)\s*['`´\"]\s*t\b", r"\1n't", t, flags=re.IGNORECASE)
    t = re.sub(r"(\b\w+)\s*['`´\"]\s*(t|s|m|d|ll|ve|re)\b", r"\1'\2", t, flags=re.IGNORECASE)
    # 3. OCR de 'I' maiúsculo lido como 'l', '1' ou '|'
    t = re.sub(r"(?:\b[l1]|\|)['`´\"](m|ll|ve|d)\b", r"I'\1", t, flags=re.IGNORECASE)
    t = re.sub(r"(?:\b[l1]|\|)\s+(am|have|had|will|would|can|could|did|do|was|were)\b", r"I \1", t, flags=re.IGNORECASE)
    # 4. Confusão de glifos e ligaduras comuns de OCR em fontes de balões
    t = re.sub(r'\bdarnn\b', 'damn', t, flags=re.IGNORECASE)
    t = re.sub(r'\bcorne\b', 'come', t, flags=re.IGNORECASE)
    t = re.sub(r'\bnarne\b', 'name', t, flags=re.IGNORECASE)
    t = re.sub(r'\bclirnb\b', 'climb', t, flags=re.IGNORECASE)
    t = re.sub(r'\brnan\b', 'man', t, flags=re.IGNORECASE)
    t = re.sub(r'\brnaster\b', 'master', t, flags=re.IGNORECASE)
    t = re.sub(r'\bfrorn\b', 'from', t, flags=re.IGNORECASE)
    t = re.sub(r'\bsornething\b', 'something', t, flags=re.IGNORECASE)
    t = re.sub(r'\bsorne\b', 'some', t, flags=re.IGNORECASE)
    t = re.sub(r'\bbecorne\b', 'become', t, flags=re.IGNORECASE)
    t = re.sub(r'\bwelcorne\b', 'welcome', t, flags=re.IGNORECASE)
    t = re.sub(r'\broorn\b', 'room', t, flags=re.IGNORECASE)
    t = re.sub(r'\btherr\b', 'them', t, flags=re.IGNORECASE)
    t = re.sub(r'\btirne\b', 'time', t, flags=re.IGNORECASE)
    t = re.sub(r'\bfirrn\b', 'firm', t, flags=re.IGNORECASE)
    t = re.sub(r'\bburnp\b', 'bump', t, flags=re.IGNORECASE)
    t = re.sub(r'\bthirn\b', 'him', t, flags=re.IGNORECASE)
    t = re.sub(r'\bbotton\b', 'bottom', t, flags=re.IGNORECASE)
    t = re.sub(r'\balrn(?:o|a)st\b', 'almost', t, flags=re.IGNORECASE)
    t = re.sub(r'\bvv([a-z]+)\b', r'w\1', t, flags=re.IGNORECASE)
    # 5. Normalização de termos de RPG / Hunter / Murim fragmentados por OCR
    t = re.sub(r'\b([SABCDEX]{1,3})\s*-\s*rank\b', r'Rank \1', t, flags=re.IGNORECASE)
    t = re.sub(r'\brank\s*-\s*([SABCDEX]{1,3})\b', r'Rank \1', t, flags=re.IGNORECASE)
    t = re.sub(r'\b([SABCDEX]{1,3})\s*-\s*class\b', r'Classe \1', t, flags=re.IGNORECASE)
    t = re.sub(r'\bclass\s*-\s*([SABCDEX]{1,3})\b', r'Classe \1', t, flags=re.IGNORECASE)
    t = re.sub(r'\blv\s*\.?\s*(\d+)\b', r'Lv. \1', t, flags=re.IGNORECASE)
    t = re.sub(r'\bqi\s+devi-?\s*ation\b', 'Qi Deviation', t, flags=re.IGNORECASE)
    t = re.sub(r'\bdan-?\s*tian\b', 'Dantian', t, flags=re.IGNORECASE)
    # 6. Pontuação espaçada, reticências fragmentadas e ruídos
    t = re.sub(r'\s+([?!.,;:])', r'\1', t)
    t = re.sub(r'([?!.,;:])(?=[A-Za-z])', r'\1 ', t)
    t = re.sub(r'\.{4,}', '...', t)
    t = re.sub(r'\.\s*\.\s*\.', '...', t)
    t = re.sub(r'\?\s*!+|\!\s*\?+', '?!', t)
    t = re.sub(r'[\*\~]+([a-zA-Z\s]+)[\*\~]+', r'\1', t)
    # 7. Aspas coladas na palavra errada ('inagre -> vinegar)
    t = re.sub(r"(?:\b|^|(?<=[\s\'\`´\"]))[\'`´\"]*[íi]nagre\b", 'vinegar', t, flags=re.IGNORECASE)
    # 8. Inversão de ordem de leitura de balão estreito
    t = re.sub(r'\b(?:chatter\s+idle|idle\s+chatter)\b', 'idle chatter', t, flags=re.IGNORECASE)
    # 9. Limpeza de ruídos nas bordas do balão
    t = re.sub(r'^[\s|_~^>§•·*°]+|[\s|_~^<§•·*°]+$', '', t)
    return t.strip()

# 5. Cache Persistente em Disco de Sentenças Traduzidas
_TRANSLATION_CACHE = {}
_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "translation_cache.json")

def _load_translation_cache():
    global _TRANSLATION_CACHE
    if os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                _TRANSLATION_CACHE = json.load(f)
        except Exception:
            _TRANSLATION_CACHE = {}

def _save_translation_cache():
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_TRANSLATION_CACHE, f, ensure_ascii=False)
        os.replace(tmp, _CACHE_FILE)
    except Exception:
        pass

_load_translation_cache()

# 6. Tradução Contextual Híbrida (Cache -> Google GTX com detecção automática de idioma)
async def translate_gtx_sentence(client, text):
    if not text or len(text.strip()) < 2:
        return None

    cleaned = clean_ocr_artifacts(text)
    cache_key = cleaned.lower().strip()
    if cache_key in _TRANSLATION_CACHE:
        cached = _TRANSLATION_CACHE[cache_key]
        if cached and cached.strip().lower() != cache_key:
            return cached

    # Tentativa Google GTX com detecção automática de origem (qualquer idioma -> pt)
    url_gtx = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=pt&dt=t&q=" + urllib.parse.quote(cleaned)
    try:
        res = await client.get(url_gtx, headers={"User-Agent": "Mozilla/5.0"}, timeout=7.0)
        if res.status_code == 200:
            data = res.json()
            translated = "".join([p[0] for p in data[0] if p[0]])
            if translated and len(translated.strip()) > 0 and translated.strip().lower() != cache_key:
                _TRANSLATION_CACHE[cache_key] = translated
                return translated
    except Exception:
        pass

    return None

def polish_scanlation_text(text, glossary=None):
    if not text:
        return ""
    t = text.strip()

    # Heurística Scanlation: Correção de traduções literais robóticas para PT-BR natural de quadrinhos
    scanlation_rules = [
        (r'\b(?:cortejando|buscando)\s+a\s+morte\b', 'procurando a morte'),
        (r'\b(?:corte\s+a\s+porcaria|pare\s+com\s+essa\s+merda)\b', 'corta essa'),
        (r'\bsegure\s+(?:sua|a)\s+l[ií]ngua\b', 'cuidado com a língua'),
        (r'\bpeda[cç]o\s+de\s+bolo\b', 'moleza'),
        (r'\b(?:sua\s+|a\s+)?(?:conversa\s+ociosa|conversa\s+in[uú]til)\b', 'o papo furado'),
        (r'\b(?:sua\s+|a\s+)?(?:idle\s+chatter|chatter\s+idle)\b', 'o papo furado'),
        (r'\b(?:idle\s+chatter|chatter\s+idle)\b', 'papo furado'),
        (r'\bchatter\b', 'papo'),
        (r'\bidle\b', 'furado'),
        (r'\b(?:voc[eê]\s+[eé]\s+)?carne\s+morta\b', 'já era'),
        (r'\bcoma\s+is[st]o\b', 'tome isso'),
        (r'\bsobre\s+o\s+meu\s+cad[aá]ver\b', 'só por cima do meu cadáver'),
        (r'\bn[aã]o\s+empurre\s+sua\s+sorte\b', 'não abuse da sorte'),
        (r'\bfale\s+do\s+diabo\b', 'falando no diabo'),
        (r'\b(?:d[eê]-me|me\s+d[eê])\s+um\s+tempo\b', 'dá um tempo'),
        (r'\bpegue\s+um\s+aperto\b', 'controle-se'),
        (r'\b(?:voltar|fique)\s+para\s+baixo\b', 'recue'),
        (r'\bsaia\s+do\s+meu\s+caminho\b', 'saia da minha frente'),
        (r'\bcustou\s+um\s+bra[cç]o\s+e\s+uma\s+perna\b', 'custou os olhos da cara'),
        (r'\b(?:no|em\s+um)\s+piscar\s+de\s+um\s+olho\b', 'em um piscar de olhos'),
        (r'\b(?:voc[eê]\s+tem\s+que\s+estar|s[oó]\s+pode\s+estar)\s+brincando\b', 'só pode estar de brincadeira'),
        (r'\b(?:eu\s+)?n[aã]o\s+dou\s+uma\s+droga\b', 'não ligo a mínima'),
        (r'\bquebrar\s+um\s+suor\b', 'suar a camisa'),
        (r'\bprejudique\s+seu\s+cultivo\b', 'destrua a cultivação dele'),
        (r'\bprejudicar\s+o\s+cultivo\b', 'destruir a cultivação'),
        (r'\bdesvio\s+de\s+qi\b', 'Desvio de Qi'),
        (r'\bmestre\s+(?:igual|like|lyca)\b', 'Mestre Lyka'),
        (r'\bjanela\s+de\s+status\b', 'Janela de Status'),
        (r'\bsubir\s+de\s+n[ií]vel\b', 'Subir de Nível'),
        (r'\bpontos?\s+de\s+status\b', 'Pontos de Atributo'),
        (r'\bvoc[eê]\s+se\s+atreve\?!', 'Como se atreve?!'),
        (r'\bvoc[eê]\s+ousa\?!', 'Como ousa?!'),
        (r'\b(?:voc[eê]\s+est[aá]\s+louco|est[aá]\s+louco|voc[eê]\s+enlouqueceu)\b', 'ficou louco'),
        (r'\bpoupe\s+a\s+minha\s+vida\b', 'poupe minha vida'),
        (r'\bcala\s+a\s+boca\b', 'Cala a boca'),
        (r'\bcala\s+essa\s+boca\b', 'Cala essa boca'),
        (r'\bn[aã]o\s+olhe\s+para\s+mim\s+de\s+cima\b', 'não me subestime'),
        (r'\bolha\s+s[oó]\s+quem\s+fala\b', 'olha só quem fala'),
        (r'\bvou\s+te\s+ensinar\s+uma\s+li[cç][aã]o\b', 'vou te ensinar uma lição'),
        (r'\bponha-se\s+no\s+seu\s+lugar\b', 'conheça o seu lugar'),
        (r'\bconhe[cç]a\s+o\s+seu\s+lugar\b', 'conheça o seu lugar'),
        (r'\bfique\s+esperto\b', 'fique esperto'),
        (r'\beu\s+vou\s+te\s+matar\b', 'eu vou te matar'),
        (r'\bt[aá]\s+olhando\s+o\s+qu[eê]\b', 'tá olhando o quê'),
        (r"(?:\b|^|(?<=[\s\'\`´\"]))[\'`´\"]*[íi]nagre\b", "VINAGRE"),
        (r'\blyca\b', 'LYKA'),
        (r'^\s*n[aã]o\s+(?:usar|usa)\s+(?:uma\s+)?\b', 'NÃO VAI USAR '),
        (r'^\s*n[aã]o\s+(?:ter|tem)\b', 'NÃO TEM'),
        (r'^\s*n[aã]o\s+(?:poder|pode)\b', 'NÃO PODE'),
        (r'^\s*n[aã]o\s+(?:saber|sabe)\b', 'NÃO SABE'),
        (r'\bbastardo\b', 'desgraçado'),
        (r'\bbastardos\b', 'desgraçados'),
        (r'\bpeda[cç]o\s+de\s+merda\b', 'desgraçado'),
        (r'\bfilho\s+da\s+m[aã]e\b', 'filho da mãe'),
        (r'\b(?:o\s+que\s+voc[eê]\s+quer\s+dizer|o\s+que\s+quer\s+dizer)\b', 'como assim'),
        (r'\bquem\s+diabos\s+[eé]\s+voc[eê]\b', 'quem diabos é você'),
        (r'\bque\s+(?:diabos|inferno|raios)\b', 'que diabos'),
        (r'\b(?:n[aã]o\s+me\s+fa[cç]a\s+rir|n[aã]o\s+me\s+fa[cç]a\s+dar\s+risada)\b', 'não me faça rir'),
        (r'\bisso\s+[eé]\s+imposs[ií]vel\b', 'impossível'),
        (r'\bn[aã]o\s+[eé]\s+nada\s+de\s+mais\b', 'não é nada demais'),
        (r'\b(?:voc[eê]\s+vai\s+pagar\s+por\s+isso|eu\s+vou\s+te\s+fazer\s+pagar)\b', 'você vai pagar por isso'),
        (r'\bvai\s+pagar\s+caro\b', 'vai pagar caro'),
        (r'\b(?:eu\s+)?nunca\s+vou\s+te\s+perdoar\b', 'nunca vou te perdoar'),
        (r'\bgra[cç]as\s+a\s+deus\b', 'graças aos céus'),
        (r'\bvelho\s+(?:gag[aá]|desgra[cç]ado)\b', 'velho desgraçado'),
        (r'\bmaldito\s+pirralho\b', 'maldito pirralho'),
        (r'\bpequeno\s+mestre\b', 'jovem mestre'),
        (r'\bl[ií]der\s+de\s+seita\b', 'Líder da Seita'),
        (r'\birm[aã]o\s+mais\s+velho\b', 'irmão sênior'),
        (r'\birm[aã]o\s+mais\s+novo\b', 'irmão júnior'),
        (r'\birm[aã]\s+mais\s+velha\b', 'irmã sênior'),
        (r'\birm[aã]\s+mais\s+nova\b', 'irmã júnior'),
        (r'\b(?:espere|aguente)\s+firme\b', 'aguente firme'),
        (r'\bcuidado\s+com\s+o\s+que\s+diz\b', 'cuidado com a língua'),
        (r'\bn[aã]o\s+se\s+intrometa\b', 'não se meta'),
        (r'\bvoc[eê]\s+est[aá]\s+morto\b', 'você já era'),
        (r'\bv[aá]\s+para\s+o\s+inferno\b', 'vá para o inferno'),
        (r'\bfique\s+longe\s+de\s+mim\b', 'fique longe de mim'),
        (r'\beu\s+n[aã]o\s+posso\s+acreditar\b', 'não acredito nisso'),
        (r'\bvoc[eê]\s+me\s+ouviu\b', 'ouviu bem'),
        (r'\bn[aã]o\s+[eé]\s+da\s+sua\s+conta\b', 'não é da sua conta'),
        (r'\bvai\s+sonhando\b', 'vai sonhando'),
        (r'\bnem\s+em\s+sonho\b', 'nem pensar'),
        (r'\bde\s+jeito\s+nenhum\b', 'de jeito nenhum'),
        (r'\bacabe\s+com\s+isso\b', 'acabe logo com isso'),
        (r'\bn[aã]o\s+baixe\s+a\s+guarda\b', 'não baixe a guarda'),
        (r'\beu\s+n[aã]o\s+tenho\s+escolha\b', 'não tenho escolha'),
        (r'\bn[aã]o\s+tem\s+como\b', 'não tem jeito'),
        (r'\bde\s+uma\s+vez\s+por\s+todas\b', 'de uma vez por todas'),
    ]

    for pat, rep in scanlation_rules:
        t = re.sub(pat, rep, t, flags=re.IGNORECASE)

    # Pontuação expressiva de mangá/manhwa
    t = re.sub(r'\?!+|\!\?+', '?!', t)
    t = re.sub(r'\?{2,}', '?!', t)
    t = re.sub(r'\!{2,}', '!!', t)

    # Dicionário do usuário / glossário da obra
    if glossary:
        for term, rep in glossary.items():
            pattern = re.compile(re.escape(term), re.IGNORECASE)
            t = pattern.sub(rep, t)

    return t.upper()

def enhance_image_for_ocr(img):
    """Retorna a imagem nativa para que os patches de inpainting da Lens combinem 100% com a paleta original"""
    return img

def safe_save_page(im, path, quality=92):
    """Garante que dimensões não ultrapassem o limite estrito de 16.383px do formato WebP"""
    if im.height > 16380 or im.width > 16380:
        scale = min(16380.0 / im.width, 16380.0 / im.height)
        nw = max(1, int(im.width * scale))
        nh = max(1, int(im.height * scale))
        im = im.resize((nw, nh), Image.Resampling.LANCZOS)
    try:
        im.save(path, format="WEBP", quality=quality, method=6)
    except Exception:
        im.save(path, format="JPEG", quality=quality)

class MangaEngine:
    def __init__(self, data_dir=None):
        if not data_dir:
            data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)

    def load_glossary(self):
        """Carrega dicionario de nomes proprios e termos protegidos"""
        glossary_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glossary.json")
        if os.path.exists(glossary_path):
            try:
                with open(glossary_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("glossary", data)
            except Exception as e:
                print(f"[!] Erro ao ler glossary.json: {e}")
        return {}

    def get_font_path(self, fallback_path=None):
        """Retorna caminho da melhor fonte de scanlation disponível (Anime Ace > Comic Neue)"""
        anime_font = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "animeace.ttf")
        if os.path.exists(anime_font):
            return anime_font
        custom_font = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "manga.ttf")
        return custom_font if os.path.exists(custom_font) else fallback_path

    @staticmethod
    def find_cuts(im, target_chunk_h=2800, min_chunk_h=1800, max_chunk_h=3400):
        """Detecta sarjetas vazias entre quadros com penalidade severa para nunca cortar balões de fala ou texto"""
        w, h = im.size
        sample_xs = list(range(10, w - 10, 4))
        cuts = [0]
        while cuts[-1] < h:
            if cuts[-1] + max_chunk_h >= h:
                cuts.append(h)
                break
            search_start = cuts[-1] + min_chunk_h
            search_end = min(cuts[-1] + max_chunk_h, h)
            best_y = None
            min_penalty = float('inf')
            for y in range(search_start, search_end, 2):
                row = [im.getpixel((x, y)) for x in sample_xs]
                # Picos de contraste = letras de texto, bordas de balões, contornos de arte
                spikes = sum(1 for i in range(1, len(row)) if abs(row[i][0] - row[i-1][0]) + abs(row[i][1] - row[i-1][1]) + abs(row[i][2] - row[i-1][2]) > 60)
                row_prev = [im.getpixel((x, y - 3)) for x in sample_xs]
                row_next = [im.getpixel((x, y + 3)) for x in sample_xs]
                v_grad = sum(abs(p[0] - n[0]) + abs(p[1] - n[1]) + abs(p[2] - n[2]) for p, n in zip(row_prev, row_next))
                h_var = sum(abs(row[i][0] - row[i-1][0]) + abs(row[i][1] - row[i-1][1]) + abs(row[i][2] - row[i-1][2]) for i in range(1, len(row)))
                dist_from_target = abs(y - (cuts[-1] + target_chunk_h)) / target_chunk_h
                penalty = spikes * 50000 + v_grad * 2.0 + h_var * 1.5 + dist_from_target * 500
                if penalty < min_penalty:
                    min_penalty = penalty
                    best_y = y
            cuts.append(best_y if best_y else search_end)
        return cuts

    def apply_glossary(self, text, glossary):
        """Substitui termos evitando traducoes literais incorretas"""
        if not text or not glossary:
            return text
        for term, replacement in glossary.items():
            pattern = re.compile(re.escape(term), re.IGNORECASE)
            text = pattern.sub(replacement, text)
        return text

    async def refine_scanlation_objects(self, raw_objs, glossary, gtx_client):
        """Traduz e refina sentenças via Lens/GTX multilíngue, filtra marcas d'água e ativa reflow inteligente"""
        if not raw_objs or not raw_objs.text:
            return

        tasks = []
        para_indices = []
        watermarks = ['UTOON', 'RESET-SCAN', 'RESET SCAN', 'KAYNSCANS', 'ASURASCANS', 'DISCORD.GG', 'WEBSITE ARE JUST PREVIEWS']

        for p_idx, para in enumerate(raw_objs.text.text_layout.paragraphs):
            src = " ".join([word.plain_text for l in para.lines for word in l.words if word.plain_text]).strip()
            if not src or len(src) < 2:
                continue
            if any(wm in src.upper() for wm in watermarks):
                if p_idx < len(raw_objs.deep_gleams) and raw_objs.deep_gleams[p_idx].HasField("translation"):
                    raw_objs.deep_gleams[p_idx].translation.translation = ""
                    raw_objs.deep_gleams[p_idx].translation.line.clear()
                continue
            # Detecção de onomatopeias asiáticas (CJK) na arte do manhwa (ex: 輪ーーーーっ)
            if any('\u4e00' <= char <= '\u9fff' or '\u3040' <= char <= '\u30ff' or '\uac00' <= char <= '\ud7af' for char in src):
                if p_idx < len(raw_objs.deep_gleams) and raw_objs.deep_gleams[p_idx].HasField("translation"):
                    raw_objs.deep_gleams[p_idx].translation.translation = ""
                    raw_objs.deep_gleams[p_idx].translation.line.clear()
                continue

            # 1. Verifica se o Google Lens já traduziu nativamente para português (multilíngue de alta precisão)
            lens_trans = ""
            has_lens_trans = False
            if p_idx < len(raw_objs.deep_gleams) and raw_objs.deep_gleams[p_idx].HasField("translation"):
                lens_trans = raw_objs.deep_gleams[p_idx].translation.translation.strip()
                if lens_trans and lens_trans.lower() != src.lower():
                    has_lens_trans = True

            if has_lens_trans:
                # O Google Lens já traduziu o idioma (Francês, Japonês, Coreano, Inglês, etc.)
                polished = polish_scanlation_text(lens_trans, glossary)
                raw_objs.deep_gleams[p_idx].translation.translation = polished
                raw_objs.deep_gleams[p_idx].translation.writing_direction = 2  # Ativa reflow de balão
                raw_objs.deep_gleams[p_idx].translation.target_language = "pt"
                raw_objs.deep_gleams[p_idx].translation.status.code = 1
            else:
                # Fallback: só requisita GTX se o Lens não traduziu este parágrafo
                para_indices.append(p_idx)
                tasks.append(translate_gtx_sentence(gtx_client, src))

            # Assegura que o parágrafo tenha bounding box de geometria para reflow
            if not para.HasField("geometry") and para.lines:
                boxes = [l.geometry.bounding_box for l in para.lines if l.HasField("geometry")]
                if boxes:
                    min_x = min(b.center_x - b.width / 2 for b in boxes)
                    max_x = max(b.center_x + b.width / 2 for b in boxes)
                    min_y = min(b.center_y - b.height / 2 for b in boxes)
                    max_y = max(b.center_y + b.height / 2 for b in boxes)
                    para.geometry.bounding_box.center_x = (min_x + max_x) / 2
                    para.geometry.bounding_box.center_y = (min_y + max_y) / 2
                    para.geometry.bounding_box.width = max(0.01, max_x - min_x)
                    para.geometry.bounding_box.height = max(0.01, max_y - min_y)

        if tasks:
            translated_texts = await asyncio.gather(*tasks, return_exceptions=True)
            for p_idx, t_res in zip(para_indices, translated_texts):
                para = raw_objs.text.text_layout.paragraphs[p_idx]
                src = " ".join([word.plain_text for l in para.lines for word in l.words if word.plain_text]).strip()
                if isinstance(t_res, str) and t_res.strip() and t_res.strip().lower() != src.lower():
                    polished = polish_scanlation_text(t_res, glossary)
                    while len(raw_objs.deep_gleams) <= p_idx:
                        raw_objs.deep_gleams.add()
                    raw_objs.deep_gleams[p_idx].translation.translation = polished
                    raw_objs.deep_gleams[p_idx].translation.writing_direction = 2
                    raw_objs.deep_gleams[p_idx].translation.target_language = "pt"
                    raw_objs.deep_gleams[p_idx].translation.status.code = 1

    async def _extract_nexus_chapter(self, url):
        supabase_url = "https://supabase.nexusmangas.com"
        anon_key = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJzdXBhYmFzZSIsImlhdCI6MTc4NzgwMjAwMCwiZXhwIjo0OTQzNDc1NjAwLCJyb2xlIjoiYW5vbiJ9.Cnl8Jw2DeKe84OAkmJYfO33xlcZsw0TC2Nw_il0tpRs"
        
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.strip('/')
        parts = [p for p in path.split('/') if p]
        
        chapter_id = None
        slug = None
        numero = None
        
        if "read" in parts:
            idx = parts.index("read")
            if idx + 1 < len(parts):
                chapter_id = parts[idx + 1]
        elif "obra" in parts and "capitulo" in parts:
            idx_o = parts.index("obra")
            idx_c = parts.index("capitulo")
            if idx_o + 1 < len(parts):
                slug = parts[idx_o + 1]
            if idx_c + 1 < len(parts):
                numero = parts[idx_c + 1]
        elif "capitulo" in parts:
            idx_c = parts.index("capitulo")
            if idx_c + 1 < len(parts):
                slug = parts[idx_c + 1]
            if idx_c + 2 < len(parts):
                numero = parts[idx_c + 2]
        elif "obra" in parts:
            idx_o = parts.index("obra")
            if idx_o + 1 < len(parts):
                slug = parts[idx_o + 1]
                numero = "1"
                
        work_title = None
        work_id = None
        
        def _curl_json(endpoint, method="GET", body=None, extra_headers=None):
            cmd = ["curl.exe", "-s", "-L"]
            hdrs = {
                "apikey": anon_key,
                "Authorization": f"Bearer {anon_key}"
            }
            if extra_headers:
                hdrs.update(extra_headers)
            for k, v in hdrs.items():
                cmd.extend(["-H", f"{k}: {v}"])
            if method == "POST":
                cmd.extend(["-X", "POST"])
                if body:
                    cmd.extend(["-H", "Content-Type: application/json", "-d", json.dumps(body)])
            cmd.append(endpoint)
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
            if res.returncode == 0 and res.stdout:
                try:
                    return json.loads(res.stdout)
                except Exception:
                    pass
            return None

        if not chapter_id and slug and numero:
            works = _curl_json(f"{supabase_url}/rest/v1/works?slug=eq.{slug}&select=id,title,slug")
            if works and isinstance(works, list) and len(works) > 0:
                work_id = works[0].get("id")
                work_title = works[0].get("title")
            
            if work_id:
                now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
                ch_url = f"{supabase_url}/rest/v1/chapters?work_id=eq.{work_id}&number=eq.{numero}&published_at=lte.{now_iso}&select=id,number,title&limit=1"
                chapters = _curl_json(ch_url)
                if chapters and isinstance(chapters, list) and len(chapters) > 0:
                    chapter_id = chapters[0].get("id")

        if not chapter_id:
            return None

        fn_res = _curl_json(
            f"{supabase_url}/functions/v1/read-chapter",
            method="POST",
            body={"chapterId": chapter_id},
            extra_headers={"x-nexus-client": "reader-v3"}
        )

        if not fn_res or not fn_res.get("success"):
            return None

        ch_data = fn_res.get("chapter", {})
        pages = ch_data.get("pages", [])
        cap_num = str(numero or ch_data.get("number", "1"))
        
        next_num = int(cap_num) + 1 if cap_num.isdigit() else None
        prev_num = int(cap_num) - 1 if cap_num.isdigit() and int(cap_num) > 1 else None
        
        next_url = f"https://www.nexusmangas.com/capitulo/{slug}/{next_num}" if (next_num and slug) else None
        prev_url = f"https://www.nexusmangas.com/capitulo/{slug}/{prev_num}" if (prev_num and slug) else None

        return {
            "series_slug": slug or "nexus-work",
            "series_title": work_title or (slug or "").replace("-", " ").title(),
            "chapter_num": cap_num,
            "pages": pages,
            "next_url": next_url,
            "prev_url": prev_url,
            "url": url
        }

    def _extract_scanmanga_chapter(self, url, dest_dir=None):
        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            shutil.which("msedge") or "",
            shutil.which("chrome") or ""
        ]
        edge_exe = next((p for p in edge_paths if p and os.path.exists(p)), None)
        if not edge_exe:
            return None

        def _ws_connect(ws_url):
            parts = ws_url.replace("ws://", "").split("/", 1)
            host, port_str = parts[0].split(":")
            path = "/" + parts[1]
            s = socket.create_connection((host, int(port_str)), timeout=10)
            key = base64.b64encode(os.urandom(16)).decode()
            handshake = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port_str}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            s.sendall(handshake.encode())
            res = s.recv(4096)
            if not res.startswith(b"HTTP/1.1 101"):
                raise RuntimeError(f"WebSocket upgrade failed: {res}")
            return s

        def _ws_send(s, data_dict):
            payload = json.dumps(data_dict).encode("utf-8")
            length = len(payload)
            mask = os.urandom(4)
            masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))
            if length <= 125:
                header = bytes([0x81, 0x80 | length]) + mask
            elif length <= 65535:
                header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", length) + mask
            else:
                header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", length) + mask
            s.sendall(header + masked)

        def _ws_recv(s):
            b1, b2 = s.recv(2)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", s.recv(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", s.recv(8))[0]
            masked = (b2 & 0x80) != 0
            mask = s.recv(4) if masked else None
            payload = bytearray()
            while len(payload) < length:
                chunk = s.recv(min(length - len(payload), 65536))
                if not chunk:
                    break
                payload.extend(chunk)
            if masked:
                payload = bytearray(b ^ mask[i % 4] for i, b in enumerate(payload))
            return json.loads(payload.decode("utf-8", errors="ignore"))

        import time
        port = 9330 + (int(time.time() * 100) % 500)
        temp_dir = os.path.join(os.environ.get("TEMP", "C:\\Temp"), f"edge_sm_{port}")
        os.makedirs(temp_dir, exist_ok=True)

        cmd = [
            edge_exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={temp_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            url
        ]

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            ws_url = None
            for _ in range(25):
                time.sleep(0.4)
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as resp:
                        targets = json.loads(resp.read().decode())
                        for t in targets:
                            if t.get("type") == "page" and "webSocketDebuggerUrl" in t:
                                ws_url = t["webSocketDebuggerUrl"]
                                break
                        if ws_url:
                            break
                except Exception:
                    pass

            if not ws_url:
                return None

            s = _ws_connect(ws_url)
            s.settimeout(5.0)

            _ws_send(s, {"id": 1, "method": "Network.enable"})
            _ws_send(s, {"id": 2, "method": "Page.enable"})

            post_req_id = None
            chapter_pages = []
            raw_title = None

            start_time = time.time()
            while time.time() - start_time < 20:
                try:
                    msg = _ws_recv(s)
                except socket.timeout:
                    continue

                method = msg.get("method", "")
                params = msg.get("params", {})

                if method == "Network.requestWillBeSent":
                    req = params.get("request", {})
                    req_url = req.get("url", "")
                    if "/lel/" in req_url and req.get("method") == "POST":
                        post_req_id = params.get("requestId")

                elif method == "Network.loadingFinished" and post_req_id:
                    if params.get("requestId") == post_req_id:
                        _ws_send(s, {"id": 100, "method": "Network.getResponseBody", "params": {"requestId": post_req_id}})

                elif msg.get("id") == 100:
                    body_data = msg.get("result", {}).get("body", "").strip()
                    if body_data:
                        m_idc = re.search(r'_(\d+)\.html', url)
                        idc = int(m_idc.group(1)) if m_idc else 0

                        compressed = base64.b64decode(body_data)
                        inflated = zlib.decompress(compressed).decode('latin1')
                        hex_idc = hex(idc)[2:]
                        cleaned = inflated[:-len(hex_idc)] if inflated.endswith(hex_idc) else inflated
                        reversed_str = cleaned[::-1]
                        padding = (-len(reversed_str)) % 4
                        reversed_str_padded = reversed_str + ('=' * padding)
                        data_obj = json.loads(base64.b64decode(reversed_str_padded).decode('utf-8', errors='ignore'))

                        dN = data_obj.get("dN")
                        base_url = f"https://{dN}/{data_obj.get('s')}/{data_obj.get('v')}/{data_obj.get('c')}"
                        p = data_obj.get("p", {})
                        for k in sorted(p.keys(), key=lambda x: int(x) if x.isdigit() else 0):
                            page = p[k]
                            chapter_pages.append(f"{base_url}/{page['f']}.{page['e']}")

                        _ws_send(s, {"id": 101, "method": "Runtime.evaluate", "params": {"expression": "document.title", "returnByValue": True}})

                elif msg.get("id") == 101:
                    raw_title = msg.get("result", {}).get("result", {}).get("value")
                    break

            if dest_dir and chapter_pages:
                os.makedirs(dest_dir, exist_ok=True)
                for idx, p_url in enumerate(chapter_pages):
                    f_path = os.path.join(dest_dir, f"pagina_{idx+1:03d}.webp")
                    if os.path.exists(f_path) and os.path.getsize(f_path) > 1000:
                        continue
                    expr = f"""(async () => {{
                        const res = await fetch("{p_url}");
                        const buf = await res.arrayBuffer();
                        let binary = '';
                        const bytes = new Uint8Array(buf);
                        const len = bytes.byteLength;
                        for (let i = 0; i < len; i++) {{
                            binary += String.fromCharCode(bytes[i]);
                        }}
                        return btoa(binary);
                    }})()"""
                    _ws_send(s, {"id": 200 + idx, "method": "Runtime.evaluate", "params": {"expression": expr, "awaitPromise": True, "returnByValue": True}})
                    while True:
                        m = _ws_recv(s)
                        if m.get("id") == 200 + idx:
                            b64 = m.get("result", {}).get("result", {}).get("value")
                            if b64:
                                with open(f_path, "wb") as f:
                                    f.write(base64.b64decode(b64))
                            break

            s.close()

            m_slug = re.search(r'/lecture-en-ligne/(.*?)-Chapitre', url, re.I)
            series_slug = m_slug.group(1).lower() if m_slug else "scanmanga-manga"
            m_chap = re.search(r'Chapitre[-_ ](\d+)', url, re.I)
            chapter_num = m_chap.group(1) if m_chap else "1"

            clean_title = raw_title or ""
            if "»" in clean_title:
                clean_title = clean_title.split("»")[0].strip()
            elif "Chapitre" in clean_title:
                clean_title = clean_title.split("Chapitre")[0].strip()
            if "|" in clean_title:
                clean_title = clean_title.split("|")[0].strip()
            clean_title = clean_title.strip(" -_") or series_slug.replace("-", " ").title()

            return {
                "series_slug": series_slug,
                "series_title": clean_title,
                "chapter_num": chapter_num,
                "pages": chapter_pages,
                "next_url": None,
                "prev_url": None,
                "url": url
            }
        except Exception as e:
            print(f"[!] Erro no _extract_scanmanga_chapter: {e}")
            return None
        finally:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

    async def extract_chapter_info(self, url, client=None):
        domain = urllib.parse.urlparse(url).netloc

        # 0. Suporte dedicado a NexusMangas / NexusToons via backend Supabase
        if any(d in domain.lower() for d in ["nexusmangas.com", "nexustoons.com"]):
            try:
                nexus_info = await self._extract_nexus_chapter(url)
                if nexus_info and nexus_info.get("pages"):
                    return nexus_info
            except Exception as e:
                print(f"[!] Erro no extrator Nexus: {e}")

        # 0.1 Suporte dedicado a Scan-Manga via sessão DevTools (Cloudflare Turnstile)
        if any(d in domain.lower() for d in ["scan-manga.com"]):
            try:
                sm_info = await asyncio.to_thread(self._extract_scanmanga_chapter, url)
                if sm_info and sm_info.get("pages"):
                    return sm_info
            except Exception as e:
                print(f"[!] Erro no extrator Scan-Manga: {e}")

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"https://{domain}/" if domain else "https://kaynscans.com/"
        }
        
        def _curl_get(target_url, hdrs):
            try:
                cmd = ["curl.exe", "-s", "-L"]
                for k, v in hdrs.items():
                    cmd.extend(["-H", f"{k}: {v}"])
                cmd.append(target_url)
                res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
                if res.returncode == 0 and res.stdout and len(res.stdout) > 200:
                    return res.stdout
            except Exception:
                pass
            return ""

        html = ""
        try:
            if client:
                resp = await client.get(url, headers=headers, timeout=25.0)
            else:
                async with httpx.AsyncClient(follow_redirects=True) as c:
                    resp = await c.get(url, headers=headers, timeout=25.0)
            if resp.status_code == 200 and "Just a moment..." not in resp.text:
                html = resp.text
            else:
                html = _curl_get(url, headers)
        except Exception:
            html = _curl_get(url, headers)

        # Extrai blocos Next.js
        chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL)
        all_text = "".join(chunks).replace('\\"', '"').replace('\\\\', '\\')

        # 1. Extrai imagens de Next.js (KaynScans, etc.)
        image_paths = []
        next_url = None
        prev_url = None

        if all_text:
            image_paths = re.findall(r'"imageUrl":"([^"]+\.(?:webp|jpg|jpeg|png)(?:\?[^"]*)?)"', all_text)
            if not image_paths:
                image_paths = re.findall(r'"imageUrl":"(/uploads/[^"]+)"', all_text)

        # 2. Extrai de ts_reader (MangaStream, MangaThemesia, ArenaScans, etc.)
        if not image_paths:
            ts_matches = re.findall(r'ts_reader\.run\((\{.*?\})\);', html, re.DOTALL)
            if ts_matches:
                try:
                    ts_data = json.loads(ts_matches[0])
                    for s in ts_data.get('sources', []):
                        for img in s.get('images', []):
                            if img and isinstance(img, str) and img.startswith('http'):
                                image_paths.append(img.strip())
                    next_url = ts_data.get('nextUrl') or None
                    prev_url = ts_data.get('prevUrl') or None
                except Exception:
                    pass

        # 3. Fallback: Suporte a Scrapers Universais (AstraToons, Madara, MangaDex, SSR tradicional)
        if not image_paths:
            # AstraToons e leitores com canvas/storage direto
            storage_chaps = re.findall(r'<(?:img|canvas)[^>]+(?:data-src|src)=["\']([^"\']*/storage/chapters/[^"\']+)["\']', html, re.I)
            if storage_chaps:
                image_paths = storage_chaps
            else:
                reader_areas = re.findall(r'(?:<div[^>]+id=["\'](?:readerarea|reader-container)["\'][^>]*>|<div[^>]+class=["\'][^"\']*(?:reading-content|page-break|chapter-images|entry-content)[^"\']*["\'][^>]*>)(.*?)</div>', html, re.DOTALL | re.IGNORECASE)
                search_html = "".join(reader_areas) if reader_areas else html
                candidates = re.findall(r'<(?:img|canvas)[^>]+(?:data-src|data-lazy-src|data-original|src)=["\']([^"\']+\.(?:webp|jpg|jpeg|png)(?:\?[^"\']*)?)["\']', search_html, re.IGNORECASE)
                ignored_patterns = ['logo', 'avatar', 'icon', 'banner', 'discord', 'cover', 'favicon', 'badge', 'widget', 'thumbnail']
                image_paths = [c.strip() for c in candidates if not any(ign in c.lower() for ign in ignored_patterns)]

        seen = set()
        pages = []
        for p in image_paths:
            if p not in seen:
                seen.add(p)
                pages.append(p)

        # Título e slug da série (Compatível com URLs aninhadas e planas)
        series_slug = None
        series_title = None

        series_match = re.search(r'"series":\{"id":"[^"]+","slug":"([^"]+)","title":"([^"]+)"', all_text)
        if series_match:
            series_slug = series_match.group(1)
            series_title = series_match.group(2)
        else:
            all_titles = re.findall(r'<title>(.*?)</title>', html, re.IGNORECASE)
            clean_title = None
            for t in reversed(all_titles):
                t_cand = t.split("-")[0].split("|")[0].split("Chapter")[0].split("Capítulo")[0].strip()
                if t_cand and len(t_cand) > 2 and t_cand.lower() not in ["astratoons", "manga", "home", "leitor", "reader"]:
                    clean_title = t_cand
                    break
            if not clean_title and all_titles:
                clean_title = all_titles[-1].split("-")[0].split("|")[0].strip()

            parts = [p for p in urllib.parse.urlparse(url).path.split("/") if p]
            if parts:
                last = parts[-1]
                if re.search(r'[-_](?:chapter|capitulo)[-_]\d+', last, re.I):
                    series_slug = re.sub(r'[-_](?:chapter|capitulo)[-_]\d+.*$', '', last, flags=re.I)
                elif len(parts) >= 3 and parts[-1].isdigit() and parts[-2].lower() in ['chapter', 'capitulo']:
                    series_slug = parts[-3]
                else:
                    series_slug = parts[0]
            else:
                series_slug = "manga"

            series_slug = re.sub(r'[^a-zA-Z0-9_\-]', '', series_slug or "manga")
            series_title = clean_title or series_slug.replace("-", " ").title()

        # Número do capítulo (compatível com /chapter/X e -chapter-X)
        cap_match = re.search(r'[-/](?:chapter|capitulo)[-/](\d+)', url, re.IGNORECASE)
        cap_num = cap_match.group(1) if cap_match else "1"

        # Next / Prev URL se ainda não resolvido via ts_reader
        if not next_url:
            next_cap = int(cap_num) + 1 if cap_num.isdigit() else None
            if next_cap:
                if re.search(r'[-/](?:chapter|capitulo)[-/]\d+', url, re.I):
                    next_url = re.sub(r'([-_/](?:chapter|capitulo)[-_/])\d+', rf'\g<1>{next_cap}', url, flags=re.I)
                else:
                    next_url = f"{url.rstrip('/')}/chapter/{next_cap}"

        if not prev_url:
            prev_cap = int(cap_num) - 1 if cap_num.isdigit() and int(cap_num) > 1 else None
            if prev_cap:
                if re.search(r'[-/](?:chapter|capitulo)[-/]\d+', url, re.I):
                    prev_url = re.sub(r'([-_/](?:chapter|capitulo)[-_/])\d+', rf'\g<1>{prev_cap}', url, flags=re.I)

        return {
            "series_slug": series_slug,
            "series_title": series_title,
            "chapter_num": cap_num,
            "pages": pages,
            "next_url": next_url,
            "prev_url": prev_url,
            "url": url
        }

    async def download_image(self, client, img_url, out_path, headers, retries=3):
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
        for attempt in range(retries):
            try:
                resp = await client.get(img_url, headers=headers, timeout=25.0)
                if resp.status_code == 200:
                    with open(out_path, "wb") as f:
                        f.write(resp.content)
                    return out_path
                elif resp.status_code in [403, 503]:
                    # Bypass Cloudflare TLS fingerprint com curl nativo do Windows
                    cmd = ["curl.exe", "-s", "-L"]
                    for k, v in headers.items():
                        cmd.extend(["-H", f"{k}: {v}"])
                    cmd.extend(["-o", out_path, img_url])
                    res = subprocess.run(cmd, capture_output=True)
                    if res.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                        return out_path
            except Exception:
                if attempt == retries - 1:
                    try:
                        cmd = ["curl.exe", "-s", "-L"]
                        for k, v in headers.items():
                            cmd.extend(["-H", f"{k}: {v}"])
                        cmd.extend(["-o", out_path, img_url])
                        res = subprocess.run(cmd, capture_output=True)
                        if res.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                            return out_path
                    except Exception:
                        pass
                    raise
                await asyncio.sleep(1.5)
        raise Exception(f"Falha no download após {retries} tentativas: {img_url}")

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
        domain = urllib.parse.urlparse(url).netloc
        if any(d in domain.lower() for d in ["scan-manga.com"]):
            m_slug = re.search(r'/lecture-en-ligne/(.*?)-Chapitre', url, re.I)
            s_slug = m_slug.group(1).lower() if m_slug else "scanmanga-manga"
            m_chap = re.search(r'Chapitre[-_ ](\d+)', url, re.I)
            c_num = m_chap.group(1) if m_chap else "1"
            chapter_dir = os.path.join(self.data_dir, s_slug, f"capitulo_{c_num}")
            pre_orig = os.path.join(chapter_dir, "original")
            os.makedirs(pre_orig, exist_ok=True)
            report("running", "inspecting", 8, "Bypassing proteção Cloudflare e carregando páginas...")
            info = await asyncio.to_thread(self._extract_scanmanga_chapter, url, dest_dir=pre_orig)
        else:
            info = await self.extract_chapter_info(url)

        pages = info["pages"] if info else []
        if not pages:
            report("error", "inspecting", 100, "Nenhuma página encontrada para este capítulo.")
            return None

        glossary = self.load_glossary()
        total_pages = len(pages)
        report("running", "downloading", 10, f"Encontradas {total_pages} páginas. Iniciando download Turbo...")

        # Cria pastas organizadas: data/<series_slug>/<chapter_num>/
        chapter_dir = os.path.join(self.data_dir, info["series_slug"], f"capitulo_{info['chapter_num']}")
        orig_dir = os.path.join(chapter_dir, "original")
        trad_dir = os.path.join(chapter_dir, "traduzido")
        os.makedirs(orig_dir, exist_ok=True)
        os.makedirs(trad_dir, exist_ok=True)

        domain = urllib.parse.urlparse(url).netloc
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": f"https://{domain}/" if domain else "https://kaynscans.com/"
        }

        # 1. Download paralelo das imagens originais
        downloaded_paths = [None] * total_pages
        download_sem = asyncio.Semaphore(6)

        async def worker_download(i, path):
            img_url = path if path.startswith("http") else f"https://{domain}{path}"
            filename = f"pagina_{i+1:03d}.webp"
            filepath = os.path.join(orig_dir, filename)
            if os.path.exists(filepath) and os.path.getsize(filepath) > 1000:
                downloaded_paths[i] = filepath
                return
            async with download_sem:
                try:
                    await self.download_image(client, img_url, filepath, headers)
                    downloaded_paths[i] = filepath
                except Exception as e:
                    print(f"[!] Erro ao baixar página {i+1}: {e}")

        async with httpx.AsyncClient() as client:
            tasks = [worker_download(i, p) for i, p in enumerate(pages)]
            await asyncio.gather(*tasks)

        report("running", "download_done", 30, f"Todas as {total_pages} páginas baixadas! Iniciando Tradução Turbo...")

        # 2. Tradução paralela das imagens com Google Lens (Inpaint + Glossário + Alinhamento Central)
        trans_sem = asyncio.Semaphore(3)
        lens = LensAPI()
        completed_count = 0
        translated_paths = [None] * total_pages

        global _ACTIVE_GLOSSARY
        _ACTIVE_GLOSSARY = glossary or {}

        # Pool compartilhado com keep-alive persistente para requisições de tradução
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=30)
        async with httpx.AsyncClient(timeout=8.0, limits=limits) as shared_gtx_client:
            async def worker_translate(i, orig_path):
                nonlocal completed_count
                filename = f"pagina_{i+1:03d}_pt.webp"
                out_path = os.path.join(trad_dir, filename)

                # Aproveita cache WebP existente
                if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                    translated_paths[i] = out_path
                    completed_count += 1
                    return

                async with trans_sem:
                    try:
                        font_to_use = self.get_font_path(lens._get_font_path())
                        with Image.open(orig_path).convert("RGB") as full_img:
                            w, h = full_img.size

                            if h > 1500:
                                # 1. Fatiamento inteligente de webtoon (mantem resolucao 100% nativa)
                                cuts = self.find_cuts(full_img)
                                canvas = Image.new("RGB", (w, h))

                                # 2. Processamento concorrente de fatias 100% em RAM (zero I/O em disco)
                                chunk_sem = asyncio.Semaphore(3)
                                chunk_results = [None] * (len(cuts) - 1)

                                async def process_one_chunk(ci, y1, y2):
                                    chunk = full_img.crop((0, y1, w, y2))
                                    async with chunk_sem:
                                        try:
                                            res = await lens.process_image(
                                                image_path=enhance_image_for_ocr(chunk),
                                                target_translation_language="pt",
                                                output_overlay_path=None,
                                                include_raw_response=True
                                            )
                                            raw_objs = res.get("raw_response_objects")
                                            if raw_objs:
                                                # Tradução contextual em lote via pool persistente GTX + Polimento de Scanlation
                                                await self.refine_scanlation_objects(raw_objs, glossary, shared_gtx_client)

                                                c_trans = render_translation_overlay(
                                                    chunk,
                                                    raw_objs,
                                                    font_path=font_to_use,
                                                    manga_mode=True,
                                                    manga_box_growth=1.12,
                                                    text_align="center",
                                                    min_readable_px=14.0
                                                )
                                                chunk_results[ci] = c_trans.convert("RGB")
                                            else:
                                                chunk_results[ci] = chunk
                                        except Exception as e_c:
                                            print(f"[!] Erro fatia {ci}: {e_c}")
                                            chunk_results[ci] = chunk

                                await asyncio.gather(*[
                                    process_one_chunk(ci, cuts[ci], cuts[ci + 1])
                                    for ci in range(len(cuts) - 1)
                                ])

                                # 3. Remonta as fatias traduzidas no canvas
                                for ci in range(len(cuts) - 1):
                                    if chunk_results[ci]:
                                        canvas.paste(chunk_results[ci], (0, cuts[ci]))

                                # 4. Salva em WebP otimizado (80% menor que PNG sem perder qualidade)
                                safe_save_page(canvas, out_path)
                            else:
                                # Imagem padrao (< 1500px)
                                res = await lens.process_image(
                                    image_path=enhance_image_for_ocr(full_img),
                                    target_translation_language="pt",
                                    output_overlay_path=None,
                                    include_raw_response=True
                                )
                                raw_objs = res.get("raw_response_objects")
                                if raw_objs:
                                    await self.refine_scanlation_objects(raw_objs, glossary, shared_gtx_client)

                                    with Image.open(orig_path) as orig_img:
                                        overlay_img = render_translation_overlay(
                                            orig_img,
                                            raw_objs,
                                            font_path=font_to_use,
                                            manga_mode=True,
                                            manga_box_growth=1.12,
                                            text_align="center",
                                            min_readable_px=14.0
                                        )
                                        safe_save_page(overlay_img, out_path)
                                else:
                                    with Image.open(orig_path) as orig_img:
                                        safe_save_page(orig_img, out_path)

                        translated_paths[i] = out_path
                    except Exception as e:
                        print(f"[!] Aviso tradução pág {i+1}: {e}")
                        translated_paths[i] = orig_path

                    completed_count += 1
                    prog = 30 + int((completed_count / total_pages) * 65)
                    report("running", "translating", prog, f"Traduzindo com IA: {completed_count}/{total_pages} páginas...")

            trans_tasks = [worker_translate(i, downloaded_paths[i]) for i in range(total_pages)]
            await asyncio.gather(*trans_tasks)

        await lens.aclose()
        _save_translation_cache()

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
