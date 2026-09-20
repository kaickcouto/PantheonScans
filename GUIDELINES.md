# 📐 Diretrizes e Lições Aprendidas de Scanlation (PantheonScans)

Documentação obrigatória de regras de arquitetura para prevenir regressões visuais em balões, inpainting e tipografia.

---

## 1. Regra de Ouro do Inpainting: Zero Curativos Brancos

* **O Erro Anterior**: Tentou-se cobrir resíduos de texto desenhando um casco convexo (`_convex_hull`) e retângulos arredondados (`draw.rounded_rectangle`) com `fill=(255, 255, 255, 255)`.
* **A Falha**: Balões com degradê cinza/creme, fundos sombreados e arte sofreram com adesivos brancos artificiais chapados e bordas cortadas ("curativos").
* **A Regra Absoluta**:
  1. **NUNCA** desenhar `rounded_rectangle`, polígonos sólidos ou preenchimentos manuais sobre os balões.
  2. Usar **exclusivamente** o patch de inpainting neural nativo enviado pelo Google Lens (`tline.background_image_data`), que reconstrói a textura, gradiente e arte original com precisão foto-realista.
  3. Para esconder qualquer serrilhado residual das letras originais, usar apenas contorno de texto (`stroke_width = 2`) na cor de fundo do balão (`background_primary_color`).

---

## 2. Regra de Ouro do Lettering: Fusão de Balão Anti-Sobreposição

* **O Erro Anterior**: O Google Lens detecta balões com múltiplas sentenças como 2 ou mais parágrafos distintos. Cada parágrafo era centralizado independentemente no seu próprio `center_y`.
* **A Falha**: As linhas inferiores do primeiro parágrafo colidiam diretamente com as linhas do segundo parágrafo, desenhando textos sobrepostos (ex: "NERVOSO!" e "VOCÊ VAI FICAR BEM!" misturados no mesmo espaço).
* **A Regra Absoluta**:
  1. Antes de renderizar, executar **fusão espacial** (`merge_overlapping_bubble_paragraphs`): parágrafos com sobreposição horizontal (`> 38%`), proximidade vertical (`gap <= 1.6 * avg_line_h`) e mesma luminosidade de fundo devem ser unificados em um único bloco contínuo.
  2. O texto concatenado deve ser diagramado dentro da **bounding box unificada** do balão completo via `comic_wrap()`.
  3. Padding de canvas de texto deve ser estritamente contido (`pad_x = 8, pad_y = 6`), evitando margens infladas que colidam com quadros adjacentes.
  4. Entrelinha fixada em `1.12 * em` (padrão de quadrinhos).

---

## 3. Regra de Ouro da Fidelidade de Cor: Sem Distorção de Inpainting

* **O Erro Anterior**: Aplicou-se realce de contraste (1.16x) e nitidez (1.22x) antes de enviar a fatia ao Google Lens com a intenção de melhorar o OCR.
* **A Falha**: O Google Lens gerava os patches de inpainting usando as cores saturadas/alteradas, fazendo com que a colagem na imagem original ficasse com bordas e descoloração visíveis.
* **A Regra Absoluta**:
  1. Enviar sempre a imagem nativa sem alterações de contraste para a API do Lens.
  2. Manter a paleta de cores original intacta para garantir fusão sub-pixel sem costuras.

---

## 4. Regra de Ouro de Orientação e Tradução

* **O Erro Anterior**: Forçar `writing_direction = 2` (direção vertical de kanji/cjk) em manhwas ocidentais/em inglês.
* **A Falha**: Desativava o mapeamento nativo de linhas e forçava reflow inadequado.
* **A Regra Absoluta**:
  1. Manhwas em inglês e português são estritamente horizontais.
  2. Quebra de palavras longas em balões estreitos deve ser feita via hifenização silábica fonética PT-BR (`split_long_word`), nunca esmagando a fonte para menos de 11px.

---

## 5. Regra de Ouro do Fatiamento de Webtoon: Zero Cortes em Balões ou Arte

* **O Erro Anterior**: `find_cuts` operava com janelas pequenas (800-1400px) e aplicava desconto de energia para pixels claros (`avg_lum > 240`), tratando o interior de balões brancos como zonas de baixo custo e cortando balões e linhas de texto ao meio (ex: corte em y=2954 dividindo "ROUND FIVE GOES / TO THE EAST WING").
* **A Falha**: Metade do texto ia para o chunk anterior e metade para o chunk seguinte, gerando traduções despedaçadas e costuras horizontais visíveis no meio das falas.
* **A Regra Absoluta**:
  1. Fatias com tamanho alvo de ~2800px (mínimo 1800px, máximo 3400px), respeitando a resolução nativa do Google Lens.
  2. Penalidade massiva para picos de contraste (`spikes * 50000`): qualquer linha que cruze contornos de letras, balões ou arte recebe penalidade astronômica.
  3. Cortes ocorrem estritamente em sarjetas planas entre quadros (`spikes == 0`), garantindo 100% de integridade nos balões de fala.

---

## 6. Regra de Ouro da Tipografia: Zero Halo Branco e Zero Hifenização Precoce

* **O Erro Anterior**:
  1. `outline_color` forçado para branco puro `(255, 255, 255, 255)` com `stroke_width = 2` em todos os textos claros, criando um halo/névoa esbranquiçada artificial em balões com tonalidade creme, off-white ou sombreada.
  2. `comic_wrap` executando hifenização de palavras normais (ex: "EXC-EÇÕES", "PERG-UNTO") logo nas primeiras iterações de fonte grande, em vez de simplesmente reduzir o tamanho da fonte para a palavra caber inteira.
  3. Patches de inpainting colados com bordas duras retangulares.
* **A Regra Absoluta**:
  1. Em balões normais claros (`bg_lum > 320`), usar `outline_width = 0` e `outline_color = None` (tipografia de mangá limpa, nítida e sem halo). Em balões escuros/arte, usar estritamente a cor amostrada `bg_col`.
  2. No loop de ajuste de tamanho de fonte, `allow_split=False`: nunca hifenizar enquanto houver tamanho legível onde a palavra caiba inteira. Somente permitir hifenização se atingir o tamanho mínimo (`min_sz`).
  3. Patches de inpainting do Google Lens devem ter suas bordas externas suavizadas (`border=3` via `ImageChops.darker`) para eliminar quaisquer emendas retangulares visíveis no fundo do balão.

---

## 7. Regra de Ouro de Dimensões e SFX de Arte: Limite WebP e Preservação de Arte

* **O Erro Anterior**:
  1. Manhwas em faixas verticais muito longas (ex: Página 4 com 16.834px e Página 12 com 16.742px no Capítulo 51) estouravam o limite da especificação WebP de 16.383px. A biblioteca PIL/libwebp lançava exceção `ValueError: encoding error 5: Image size exceeds WebP limit of 16383 pixels` ao tentar salvar o canvas remontado, caindo no bloco de exceção e servindo a página original 100% em inglês sem tradução!
  2. Onomatopeias asiáticas (kanji/kana/hangul como `輪ーーーーっ`) desenhadas diretamente sobre roupas, corpos ou fundos eram detectadas pela Lens como texto e cobertas por retângulos borrados de inpainting com traduções absurdas (ex: "ANEL TSU").
  3. Parágrafos sem tradução válida ou filtrados ainda executavam o inpainting na Fase 1, borrando a arte de fundo sem colocar nenhum texto por cima.
* **A Regra Absoluta**:
  1. **Limite WebP 16.380px (`safe_save_page`)**: Antes de salvar qualquer página remontada ou fatia, verificar se `height > 16380` ou `width > 16380`. Se sim, redimensionar proporcionalmente via `Image.Resampling.LANCZOS` para o teto de 16.380px, prevenindo qualquer erro de codec e garantindo que nenhuma página caia no fallback de inglês. Se o codec WebP ainda falhar por qualquer motivo, salvar como JPEG de alta qualidade (92).
  2. **Preservação de Onomatopeias CJK**: Caracteres CJK (`\u4e00-\u9fff`, `\u3040-\u30ff`, `\uac00-\ud7af`) presentes no OCR de capítulos ocidentais/ingleses indicam arte e onomatopeias originais desenhadas no desenho. Limpar `translation` e `data.line.clear()` para que a arte original permaneça 100% visível e imaculada.
  3. **Zero Inpainting sem Tradução**: Na Fase 1 de inpainting, pular qualquer parágrafo cuja tradução seja vazia (`not data.translation.strip()`). Se não há texto novo a ser escrito, a arte original nunca deve ser tocada.


