import json
import re
import os
import asyncio
import urllib.parse
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

# 6. Tradução Contextual Híbrida (Cache -> Google GTX -> MyMemory Fallback)
async def translate_gtx_sentence(client, text):
    if not text or len(text.strip()) < 2:
        return text

    cleaned = clean_ocr_artifacts(text)
    cache_key = cleaned.lower().strip()
    if cache_key in _TRANSLATION_CACHE:
        return _TRANSLATION_CACHE[cache_key]

    # 1. Tentativa Primária: Google GTX com detecção automática de origem
    url_gtx = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=pt&dt=t&q=" + urllib.parse.quote(cleaned)
    try:
        res = await client.get(url_gtx, headers={"User-Agent": "Mozilla/5.0"}, timeout=7.0)
        data = res.json()
        translated = "".join([p[0] for p in data[0] if p[0]])
        if translated and len(translated.strip()) > 0:
            _TRANSLATION_CACHE[cache_key] = translated
            return translated
    except Exception:
        pass

    # 2. Fallback de Alta Disponibilidade: MyMemory Translation API
    try:
        url_mm = f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(cleaned)}&langpair=en|pt-BR"
        res_mm = await client.get(url_mm, headers={"User-Agent": "Mozilla/5.0"}, timeout=6.0)
        data_mm = res_mm.json()
        translated = data_mm.get("responseData", {}).get("translatedText")
        if translated and len(translated.strip()) > 0:
            _TRANSLATION_CACHE[cache_key] = translated
            return translated
    except Exception:
        pass

    return text

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
        """Traduz sentenças contextualmente via GTX, filtra marcas d'água e ativa reflow inteligente de balão"""
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
            para_indices.append(p_idx)
            tasks.append(translate_gtx_sentence(gtx_client, src))

        if tasks:
            translated_texts = await asyncio.gather(*tasks, return_exceptions=True)
            for p_idx, t_res in zip(para_indices, translated_texts):
                if p_idx < len(raw_objs.deep_gleams) and raw_objs.deep_gleams[p_idx].HasField("translation"):
                    fallback_lens = raw_objs.deep_gleams[p_idx].translation.translation
                    text_to_polish = t_res if isinstance(t_res, str) and t_res else fallback_lens
                    polished = polish_scanlation_text(text_to_polish, glossary)
                    raw_objs.deep_gleams[p_idx].translation.translation = polished
                    raw_objs.deep_gleams[p_idx].translation.writing_direction = 2  # Força reflow seguro
                    raw_objs.deep_gleams[p_idx].translation.target_language = "pt"

                    # Assegura que o parágrafo tenha bounding box de geometria para reflow
                    para = raw_objs.text.text_layout.paragraphs[p_idx]
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

    async def extract_chapter_info(self, url, client=None):
        domain = urllib.parse.urlparse(url).netloc
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"https://{domain}/" if domain else "https://kaynscans.com/"
        }
        
        if client:
            resp = await client.get(url, headers=headers, timeout=25.0)
            html = resp.text
        else:
            async with httpx.AsyncClient() as c:
                resp = await c.get(url, headers=headers, timeout=25.0)
                html = resp.text

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

        # 3. Fallback: Suporte a Scrapers Universais (Madara, MangaDex, SSR tradicional)
        if not image_paths:
            reader_areas = re.findall(r'(?:<div[^>]+id=["\']readerarea["\'][^>]*>|<div[^>]+class=["\'][^"\']*(?:reading-content|page-break|chapter-images|entry-content)[^"\']*["\'][^>]*>)(.*?)</div>', html, re.DOTALL | re.IGNORECASE)
            search_html = "".join(reader_areas) if reader_areas else html
            candidates = re.findall(r'<img[^>]+(?:data-src|data-lazy-src|data-original|src)=["\']([^"\']+\.(?:webp|jpg|jpeg|png)(?:\?[^"\']*)?)["\']', search_html, re.IGNORECASE)
            ignored_patterns = ['logo', 'avatar', 'icon', 'banner', 'discord', 'cover', 'favicon', 'badge', 'widget']
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
            title_m = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
            clean_title = None
            if title_m:
                t_raw = title_m.group(1).split("-")[0].split("|")[0].split("Chapter")[0].split("Capítulo")[0].strip()
                if t_raw and len(t_raw) > 2:
                    clean_title = t_raw

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
            except Exception:
                if attempt == retries - 1:
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
        info = await self.extract_chapter_info(url)
        pages = info["pages"]
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
