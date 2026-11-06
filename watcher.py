import os
import time
import json
import subprocess
import requests
import re
from datetime import datetime, timedelta
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- Environment Configurations ---
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower().strip()
MODEL_NAME = os.getenv("MODEL_NAME", "qwen2.5:1.5b")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434/api/generate")
OPENAI_URL = os.getenv("OPENAI_URL", "https://openai.com")
API_KEY = os.getenv("API_KEY", "")

# --- German Prompt Template ---
PROMPT_TEMPLATE = """
Du bist ein präziser Dokumenten-Analysator für ein Dateisystem. Deine Aufgabe ist es, den OCR-Text einer PDF zu lesen und strukturierte Metadaten im JSON-Format zu extrahieren.

ANLEITUNG ZUR PARAMETER-ERMITTLUNG:
1. DATUM (Format: YYYY-MM-DD): 
   - Suche nach dem Ausstellungsdatum, Rechnungsdatum, Leistungsdatum oder Briefdatum.
   - Typische Signalwörter: "Datum:", "Vom:", "Rechnungsdatum:", "Leistungszeitraum:".
   - Ignoriere das heutige Datum oder Geburtsdaten. Wenn kein Jahr gefunden wird, nutze "0000-00-00".

2. ABSENDER (Maximal 2-3 prägnante Wörter):
   - Wer hat das Dokument ausgestellt? (z.B. Vodafone, Telekom, Allianz, Finanzamt, Amazon, BMW).
   - Suche ganz oben im Text, im Briefkopf, bei den Absenderzeilen im Adressfenster oder im Impressum.
   - Nutze den Eigennamen der Firma oder Behörde. Keine Rechtsformen wie GmbH, AG, e.V.

3. DOKUMENTENTYP (Exakt 1 Wort):
   - Wähle den zutreffendsten Typ aus: Rechnung, Gutschrift, Vertrag, Mahnung, Kündigung, Lohnabrechnung, Versicherungsschein, Bescheid, Brief, Quittung.
   - Falls nichts passt, nutze "Dokument".

STRIKTE FORMATVORGABE:
Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt. Verwende keine Markdown-Formatierung (keine ```json Blöcke). Keine Erklärungen vor oder nach dem JSON.

Gewünschtes Ausgabeformat:
{{
  "datum": "YYYY-MM-DD",
  "absender": "FIRMENNAME",
  "typ": "TYP"
}}

Hier ist der OCR-Text des Dokuments:
---
{document_text}
---
"""

def clean_filename(filename):
    """Removes unsupported characters from the generated filename."""
    filename = filename.strip().replace('"', '').replace("'", "")
    filename = re.sub(r'[^a-zA-Z0-9_\-]', '', filename)
    return filename if filename else "unbekanntes_dokument"

def query_llm(doc_text):
    """Dispatches the prompt to either Ollama or OpenAI based on LLM_PROVIDER."""
    if LLM_PROVIDER == "openai":
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}"
        }
        payload = {
            "model": MODEL_NAME,
            "messages": [
                {
                    "role": "system",
                    "content": "Du bist ein präziser Dokumenten-Analysator. Antworte AUSSCHLIESSLICH mit einem validen JSON-Objekt ohne Markdown-Formatierung (keine Backticks). Keine Erklärungen."
                },
                {
                    "role": "user",
                    "content": PROMPT_TEMPLATE.format(document_text=doc_text)
                }
            ],
            "temperature": 0.0,
            "response_format": { "type": "json_object" } # Forces JSON output for compatible remote APIs
        }
        response = requests.post(OPENAI_URL, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
        
    else: # Default: ollama
        payload = {
            "model": MODEL_NAME,
            "prompt": PROMPT_TEMPLATE.format(document_text=doc_text),
            "stream": False,
            "options": {
                "temperature": 0.0,
                "top_p": 0.1
            }
        }
        response = requests.post(OLLAMA_URL, json=payload, timeout=30)
        response.raise_for_status()
        return response.json().get("response", "").strip()

class OCRWatcherHandler(FileSystemEventHandler):
    def __init__(self, output_dir):
        self.output_dir = output_dir

    def on_created(self, event):
        # Ignore directories and non-pdf files
        if event.is_directory or not event.src_path.lower().endswith('.pdf'):
            return
        
        # Cascade protection: Ignore temporary files being processed
        if "_tmp_ocr.pdf" in event.src_path:
            return
            
        input_path = event.src_path
        time.sleep(2) # Buffer to ensure file transfer is completed
        
        print(f"[{self.output_dir}] New file detected: {os.path.basename(input_path)}")
        temp_ocr_path = input_path.replace(".pdf", "_tmp_ocr.pdf")
        
        try:
            print("-> Running OCRmyPDF...")
            subprocess.run([
                "ocrmypdf", "-l", "deu+eng", "--skip-text", 
                input_path, temp_ocr_path
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # Extract plain text from generated OCR PDF
            text_result = subprocess.run(["pdftotext", temp_ocr_path, "-"], capture_output=True, text=True, check=True)
            full_doc_text = text_result.stdout
            
            # CPU/Token Optimization: Extract first 2000 and last 1000 characters
            if len(full_doc_text) > 3000:
                print(f"-> File text is long ({len(full_doc_text)} chars). Truncating context for LLM...")
                start_chunk = full_doc_text[:2000]
                end_chunk = full_doc_text[-1000:]
                doc_text = f"{start_chunk}\n\n[... HIER WURDEN TEILE DES MITTELTEILS AUSGELASSEN ...]\n\n{end_chunk}"
            else:
                doc_text = full_doc_text
            
            # Query Selected AI Provider
            print(f"-> Querying AI ({LLM_PROVIDER} / {MODEL_NAME})...")
            ai_response = query_llm(doc_text)
            
            # Parse JSON metadata
            try:
                # Strip out accidental markdown block wraps if present
                ai_response_clean = re.sub(r'```json\s*|```', '', ai_response).strip()
                metadata = json.loads(ai_response_clean)
                
                doc_date = clean_filename(metadata.get("datum", "0000-00-00"))
                doc_sender = clean_filename(metadata.get("absender", "Unbekannt"))
                doc_type = clean_filename(metadata.get("typ", "Dokument"))
                
                final_name = f"{doc_date}_{doc_sender}_{doc_type}"
            except Exception as json_err:
                print(f"-> Warning: Failed to parse JSON response. Using fallback parsing. Raw response: {ai_response}")
                fallback_name = clean_filename(ai_response.replace("\n", "_"))
                final_name = fallback_name if fallback_name else "unbekanntes_dokument"
            
            # Formulate final destination path
            final_path = os.path.join(self.output_dir, f"{final_name}.pdf")
            if os.path.exists(final_path):
                final_path = os.path.join(self.output_dir, f"{final_name}_{int(time.time())}.pdf")
            
            # Move final file and clean up original
            os.rename(temp_ocr_path, final_path)
            os.remove(input_path)
            print(f"-> SUCCESS: Moved to {final_path}\n")
            
        except Exception as e:
            print(f"-> ERROR processing file {os.path.basename(input_path)}: {e}")
            # Clean up temp file to avoid clogs
            if os.path.exists(temp_ocr_path):
                try:
                    os.remove(temp_ocr_path)
                except Exception:
                    pass

if __name__ == "__main__":
    print(f"Starting Coolify OCR-AI-Watcher Service using provider: {LLM_PROVIDER}")
    
    folders_json = os.getenv("WATCH_FOLDERS")
    if not folders_json:
        print("CRITICAL ERROR: WATCH_FOLDERS environment variable is missing!")
        exit(1)
        
    try:
        watch_folders = json.loads(folders_json)
    except Exception as e:
        print(f"CRITICAL ERROR: WATCH_FOLDERS contains invalid JSON syntax: {e}")
        exit(1)
    
    # Calculate Cutoff Date (Yesterday at 00:00 AM)
    yesterday = datetime.now() - timedelta(days=1)
    cutoff_date = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    print(f"Historical file cutoff filter set to: {cutoff_date.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Process files that already exist in input folders
    print("Checking directories for existing historical files...")
    for folder in watch_folders:
        input_dir = folder["input"]
        output_dir = folder["output"]
        
        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)
        
        initial_handler = OCRWatcherHandler(output_dir=output_dir)
        
        for filename in os.listdir(input_dir):
            if not filename.lower().endswith('.pdf') or "_tmp_ocr.pdf" in filename:
                continue
                
            full_path = os.path.join(input_dir, filename)
            
            try:
                # Verify last modification time against cutoff date
                mtime = os.path.getmtime(full_path)
                file_date = datetime.fromtimestamp(mtime)
                
                if file_date < cutoff_date:
                    continue
                    
                print(f"Historical file found (modified since yesterday): {filename} ({file_date.strftime('%Y-%m-%d')}) -> Catching up processing.")
                
                class MockEvent:
                    def __init__(self, src_path):
                        self.src_path = src_path
                        self.is_directory = False
                
                initial_handler.on_created(MockEvent(full_path))
                
            except Exception as e:
                print(f"Error checking file stats for {filename}: {e}")



    # 2. Initialize live real-time file system monitoring
    print("Catch-up complete. Switching over to Live Monitoring Mode...")
    observer = Observer()
    
    for folder in watch_folders:
        handler = OCRWatcherHandler(output_dir=folder["output"])
        observer.schedule(handler, path=folder["input"], recursive=False)
        print(f"Überwache aktiv: {folder['input']} -> {folder['output']}")
        
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
