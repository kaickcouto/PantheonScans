# ⚡ Manga Translator Web

> Leitor e tradutor automático de mangás, manhwas e webtoons com interface web moderna, motor Turbo em nuvem e zero necessidade de placa de vídeo dedicada.

![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Status](https://img.shields.io/badge/status-active-emerald.svg)

---

## 🌟 Principais Recursos

* ⚡ **Motor Turbo (Sem GPU Pesada):** Utiliza a API em nuvem do Google Lens com detecção especializada para mangá (`manga_mode=True`). Traduz um capítulo de 34 páginas em **menos de 30 segundos** sem esquentar seu processador e sem precisar baixar modelos de 5 GB.
* 🖥️ **Dashboard Web Visual:** Interface moderna em Dark Mode (`localhost:5000`) com barra de progresso ao vivo.
* 📖 **Leitor Webtoon Fluido:** Rolagem vertical contínua, sem cortes e com tema escuro imersivo.
* 🔄 **Navegação Contínua:** Botão **"Próximo Capítulo"** no rodapé do leitor que baixa, traduz e abre o próximo capítulo automaticamente.
* 📚 **Biblioteca Local:** Histórico dos capítulos já traduzidos organizados por obra e com capa.
* 🚀 **Execução em 1 Clique:** Atalho para inicialização instantânea sem necessidade de abrir terminal.

---

## 🚀 Instalação e Execução

### 1. Pré-requisitos
Certifique-se de ter o **Python 3.10+** instalado em seu sistema.

### 2. Clonar o repositório
```bash
git clone https://github.com/SEU_USUARIO/manga-translator-web.git
cd manga-translator-web
```

### 3. Instalar dependências
```bash
pip install -r requirements.txt
```

### 4. Iniciar o Aplicativo
```bash
python app.py
```
O navegador abrirá automaticamente em `http://localhost:5000`.

---

## 📖 Como Usar

1. Abra o dashboard no navegador.
2. Cole a URL do capítulo que deseja ler (ex: Kayn Scans).
3. Clique em **Traduzir Turbo**.
4. Acompanhe a barra de progresso em tempo real.
5. Assim que a tradução for concluída, o leitor abrirá automaticamente com todos os balões em português!

---

## 📁 Estrutura do Projeto

```text
manga_app/
├── app.py                # Servidor HTTP local e rotas de API
├── engine.py             # Motor de download assíncrono e tradução paralela
├── templates/
│   ├── index.html        # Dashboard principal com barra de progresso
│   └── reader.html       # Leitor webtoon contínuo com navegação
├── requirements.txt      # Dependências mínimas do projeto
├── .gitignore            # Regras para ignorar cache e dados locais
└── README.md             # Documentação do projeto
```

---

## ⚖️ Aviso Legal / Disclaimer

Este projeto foi desenvolvido estritamente para fins educacionais e uso pessoal. Todo o conteúdo, imagens e direitos autorais pertencem aos seus respectivos autores, artistas e editoras.
