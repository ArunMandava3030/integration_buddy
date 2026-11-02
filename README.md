# Local CPI knowledge assistant

A local website for asking questions about your CPI iFlow documentation. Your
documents, search index, and language model run on this computer. No OpenAI API
key or external inference service is required.

This is retrieval-augmented generation (RAG), not model training. The application
indexes document passages, finds evidence for a question, and asks a local model
to answer using that evidence. Source passages remain available for verification.

## Start on Windows

If setup has already been completed, double-click **start.cmd**. The website opens
at **http://127.0.0.1:8765**. Keep the terminal window open while using it; press
**Ctrl+C** there to stop the website.

If the website is already running on the chosen port, the launcher reloads
`config.json`, starts the configured folder import when enabled, opens that
existing session, and exits. It does not start a second copy or stop the existing
server. If an import is already running, wait for it to finish and launch again
to apply changed settings. The terminal instructions above apply when the
launcher starts a new server itself.

For a fresh installation, open PowerShell in this project folder and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -InstallOllama -DownloadModels
.\start.cmd
```

Setup requires an internet connection to download Python packages, the official
portable Ollama release, and the selected models. It keeps the Python environment
and portable runtime inside the project, without an administrator installation
or permanent PATH changes. Python 3.10 or newer must already be installed.

After setup, document indexing and answers can run offline. The application uses
only the local Ollama endpoint at `127.0.0.1:11434` for model requests. The launcher
sets `OLLAMA_NO_CLOUD=1` for Ollama processes it starts. The site uses local assets.

The default models are `qwen3:4b` for answers and `nomic-embed-text` for embeddings.
Setup keeps matching models that are already installed and skips downloading them.
Downloading a model does not train it on your documents. The initial downloads
and extracted runtime require several gigabytes of free disk space, in addition
to your documents and index.

## Add your documents

You can supply your documents later. To choose the folder in the setup file,
open **setup.ps1** in a text editor and edit this line near the top:

```powershell
[string]$DocumentsFolder = 'C:\CPI-Documents',
```

Paste your real folder path between the single quotes, keep the comma, and save
the file. Then run setup again and open the website:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -SkipPython
.\start.cmd
```

You can also set the folder without editing the script:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -SkipPython -DocumentsFolder 'C:\CPI-Documents'
```

Setup saves this location in **config.json** as `documents_folder`, preserving
the other settings. The next `start.cmd` launch applies that configuration and
imports the folder when `auto_import` is enabled, including when the website is
already running. Keep the folder blank until your documents are ready.
Document indexing reads the source files; it does not train or modify the model.

You can also add documents through the website:

1. Click **Documents** at the top of the chat.
2. Use **Choose files**, **Choose folder**, or enter a local path and click **Import folder**.
3. Wait for processing to finish before asking questions about newly added files.
4. Ask a question using the iFlow name, environment, and relevant technical terms.
5. Review the cited passages before relying on an answer.

The main screen is a simple conversation and message box. Use **New chat** to
clear the current conversation. Expand **Sources** below an answer to read its
supporting passages. Document filtering and removal are under **Documents →
Indexed documents**; model selection and optional import metadata are under
**Documents → Options**.

Plain text and DOCX processing work without optional Python packages. PDF text
extraction requires `pypdf`, which setup installs. Scanned images need OCR before
import; this application does not perform OCR. Convert older binary Office files
and unsupported formats to text, PDF with selectable text, or DOCX first.

Text formats include Markdown, XML, JSON, CSV, YAML, HTML, Groovy, and iFlow XML
files. Extract ZIP exports before importing their supported files. Browser
uploads allow up to 256 MiB per file; folder import can read larger plain-text
files directly. PDFs have a 128 MiB per-file extraction limit. DOCX extraction
limits are 64 MiB uncompressed and 32 MiB for its main document XML.

The sample in `examples/demo-iflow.md` is **fictional** and is not automatically
indexed. Import it yourself to try questions such as "How does
DEMO_CUSTOMER_REPLICATION handle duplicates?" Remove it from the library before
using the assistant solely for real organization documents.

Search remains useful when Ollama or a model is unavailable: the app can show
matching source passages. Generating a conversational answer requires a local
chat model. Semantic retrieval additionally requires the local embedding model
and `sqlite-vec`; keyword search does not need either model.

When the local embedding model is available, imports automatically build the
semantic index in SQLite. If it is unavailable, keyword indexing still completes.
After installing the embedding model, re-import the folder or files to add any
missing embeddings; unchanged text does not need to be indexed again.

Folder imports read the original files and store extracted passages in the index.
Browser uploads additionally keep a copy in `.data/uploads/`. **Remove document**
removes searchable passages and embeddings; it does not delete source files or
uploaded copies. A removed file still inside an automatically imported folder
will be added again on the next start. Likewise, removing a file from a source
folder does not automatically remove its earlier indexed copy; remove it from
the library as well.

## Local files and processes

| Location | Purpose |
| --- | --- |
| `.data/` | Uploaded copies, extracted passages, and search indexes |
| `config.json` | Documents folder, model choices, and import/search settings |
| `.venv/` | Project Python environment and packages |
| `.runtime/ollama/` | Portable Ollama executable and supporting libraries |
| `.runtime/models/` | Models used by an Ollama server launched by these scripts |
| `.runtime/downloads/` | Downloaded installation archive |
| `.runtime/*.log` | Ollama startup and runtime logs |

The launch scripts reuse an existing Ollama server on port 11434 if one is already
running. That server keeps its own model location and configuration; setting
environment variables here does not reconfigure an existing process. Quit a
previous Ollama instance first if you want these scripts to start one using
`.runtime/models/`. Existing servers are not stopped or reconfigured automatically.

Ollama is started in a hidden window and remains running when the website stops,
so subsequent launches can reuse it. Its process can be stopped through Windows
Task Manager when you no longer need it. No scheduled task or Windows service is
created.

The website binds to loopback only. This version is for use on the local computer;
it does not provide organization SSO, employee access controls, or shared hosting.

## Setup and launch options

Install the Python packages only:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
```

Add portable Ollama and the default models later, keeping an existing Python setup:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -SkipPython -InstallOllama -DownloadModels
```

Use a specific Python executable when creating the environment:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -PythonExe 'C:\Program Files\Python314\python.exe'
```

Start without opening a browser, or choose another local port:

```powershell
.\start.cmd -NoBrowser -Port 8766
```

The portable installer pins Ollama `v0.40.13` and verifies the official Windows
x64 archive's SHA-256 before extracting it. Existing portable installations are
preserved. If an organization manages Ollama separately, omit `-InstallOllama`;
the scripts can use an existing executable on PATH.

## Working with 1–2 GB of documentation

Begin with representative documents and questions, then import the remaining
collection in batches. Initial extraction and embedding take time. Disk usage
includes imported files, extracted text, keyword indexes, and embeddings; it can
exceed the original collection size. Performance also depends on document layout,
model choice, available RAM, CPU or GPU, and the number of passages.

The full collection is not placed into the model's prompt. Retrieval supplies a
limited set of passages for each answer. CPU inference is supported by Ollama but
can be slower than inference on a compatible GPU. This prototype has not been
benchmarked against your complete 1–2 GB collection.

Questions about every matching iFlow need special care: a retrieved set of
passages does not prove that a list is complete. Likewise, uploaded documentation
cannot provide current CPI monitoring status. Keep environment, version, iFlow
ID, and ownership information in the source documents to help avoid ambiguity.

## Troubleshooting

- **Python is missing:** install Python 3.10 or newer, or use `-PythonExe` during setup.
- **No local model available:** rerun setup with `-InstallOllama -DownloadModels` while online.
- **A model download was interrupted:** rerun setup with `-SkipPython -DownloadModels`.
- **Ollama does not start:** inspect `.runtime/*.err.log` and check whether port 11434 is already occupied.
- **PDF has no extracted text:** check that it has selectable text and is not password-protected; use OCR outside this app when needed.
- **The website port is occupied:** run `start.cmd -Port 8766`.
- **Answers are slow:** allow the first model load to complete; try fewer concurrent questions and check the available hardware.

If Ollama's model download service is blocked by your network or returns HTTP 403,
the project includes an alternative for the two default models. First run
`start.cmd` to make sure local Ollama is running. In a second PowerShell window,
open this project folder and run:

```powershell
.\.venv\Scripts\python.exe scripts/install_models.py
```

This setup helper downloads the model files from the authors' official
Hugging Face repositories at pinned revisions, verifies their SHA-256 checksums,
and imports them into local Ollama. It needs internet access during this download;
it does not upload your documents. Once installation finishes, refresh the
website. The defaults are `qwen3:4b` and `nomic-embed-text`.

## Primary references

- [Ollama on Windows and the standalone CLI](https://docs.ollama.com/windows)
- [Ollama local operation and configuration](https://docs.ollama.com/faq)
- [Official Ollama releases](https://github.com/ollama/ollama/releases)
- [pypdf package and Python compatibility](https://pypi.org/project/pypdf/)
- [SQLite vector extension](https://github.com/asg017/sqlite-vec)
