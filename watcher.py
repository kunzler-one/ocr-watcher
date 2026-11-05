import os
import time
import json
import subprocess
import requests
import re
from datetime import datetime, timedelta
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434/api/generate")
MODEL_NAME = os.getenv("MODEL_NAME", "qwen2.5:1.5b")

PROMPT_TEMPLATE = """
Analysiere den OCR-Text dieses Dokuments. Generiere einen sauberen Dateinamen im Format: YYYY-MM-DD_[Absender]_[Dokumententyp].
Nutze das exakte Ausstellungsdatum des Dokuments. Erlaube NUR Buchstaben, Zahlen, Unterstriche und Bindestriche im Namen. 
Antworte AUSSCHLIESSLICH mit dem nackten Dateinamen (OHNE Dateiendung, ohne Anführungszeichen, ohne Einleitung oder Erklärung).

Text:
---
{document_text}
---
"""

def clean_filename(filename):
    filename = filename.strip().replace('"', '').replace("'", "")
    filename = re.sub(r'[^a-zA-Z0-9_\-]', '', filename)
    return filename if filename else "unbekanntes_dokument"

class OCRWatcherHandler(FileSystemEventHandler):
    def __init__(self, output_dir):
        self.output_dir = output_dir

    def on_created(self, event):
        # Ignoriere Verzeichnisse und alle Dateien, die nicht auf .pdf enden
        if event.is_directory or not event.src_path.lower().endswith('.pdf'):
            return
        
        # SPERRE 1: Ignoriere temporäre Dateien im Live-Betrieb, um Kaskaden zu verhindern
        if "_tmp_ocr.pdf" in event.src_path:
            return
            
        input_path = event.src_path
        time.sleep(2) # Warten, falls Datei noch kopiert wird
        
        print(f"[{self.output_dir}] Neue Datei erkannt: {os.path.basename(input_path)}")
        
        # SPERRE 2: Die temporäre Datei wird in einem geschützten Namen erstellt
        temp_ocr_path = input_path.replace(".pdf", "_tmp_ocr.pdf")
        
        try:
            print("-> Starte OCR...")
            subprocess.run([
                "ocrmypdf", "-l", "deu+eng", "--skip-text", 
                input_path, temp_ocr_path
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            text_result = subprocess.run(["pdftotext", "-l", "2", temp_ocr_path, "-"], capture_output=True, text=True, check=True)
            doc_text = text_result.stdout[:3000]
            
            print("-> Befrage KI für Namensgenerierung...")
            prompt = PROMPT_TEMPLATE.format(document_text=doc_text)
            
            response = requests.post(OLLAMA_URL, json={
                "model": MODEL_NAME, "prompt": prompt, "stream": False,
                "options": {"temperature": 0.1}
            }, timeout=30)
            
            ai_name = response.json().get("response", "unbekannt").strip()
            final_name = clean_filename(ai_name)
            
            final_path = os.path.join(self.output_dir, f"{final_name}.pdf")
            if os.path.exists(final_path):
                final_path = os.path.join(self.output_dir, f"{final_name}_{int(time.time())}.pdf")
            
            os.rename(temp_ocr_path, final_path)
            os.remove(input_path)
            print(f"-> ERFOLG: Verschoben nach {final_path}\n")
            
        except Exception as e:
            print(f"-> FEHLER bei der Verarbeitung von {os.path.basename(input_path)}: {e}")
            # Falls ein Fehler auftritt, temporäre Datei löschen, um Ordner sauber zu halten
            if os.path.exists(temp_ocr_path):
                try:
                    os.remove(temp_ocr_path)
                except Exception:
                    pass

if __name__ == "__main__":
    print("Starte Coolify OCR-AI-Watcher Service...")
    
    folders_json = os.getenv("WATCH_FOLDERS")
    if not folders_json:
        print("FEHLER: Die Umgebungsvariable WATCH_FOLDERS ist nicht definiert!")
        exit(1)
        
    try:
        watch_folders = json.loads(folders_json)
    except Exception as e:
        print(f"FEHLER: WATCH_FOLDERS ist kein gültiges JSON! {e}")
        exit(1)
    
    # CUTOFF-DATE BERECHNEN (Gestern um 00:00 Uhr)
    gestern = datetime.now() - timedelta(days=1)
    cutoff_date = gestern.replace(hour=0, minute=0, second=0, microsecond=0)
    print(f"Cutoff-Zeitpunkt für Bestandsdateien gesetzt auf: {cutoff_date.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 1. BESTEHENDE DATEIEN BEIM START VERARBEITEN (MIT FILTERN)
    print("Prüfe Ordner auf bestehende Dateien vor dem Start...")
    for folder in watch_folders:
        input_dir = folder["input"]
        output_dir = folder["output"]
        
        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)
        
        initial_handler = OCRWatcherHandler(output_dir=output_dir)
        
        for filename in os.listdir(input_dir):
            # Ignoriere Nicht-PDFs und Reste von alten temporären Dateien
            if not filename.lower().endswith('.pdf') or "_tmp_ocr.pdf" in filename:
                continue
                
            full_path = os.path.join(input_dir, filename)
            
            try:
                # Letzte Änderung der Datei ermitteln
                mtime = os.path.getmtime(full_path)
                file_date = datetime.fromtimestamp(mtime)
                
                # Wenn die Datei älter als das Cutoff-Datum ist, überspringen
                if file_date < cutoff_date:
                    continue
                    
                print(f"Bestehende Datei gefunden (ab gestern): {filename} (Geändert am: {file_date.strftime('%Y-%m-%d')}) -> Starte Nachverarbeitung.")
                
                class MockEvent:
                    def __init__(self, src_path):
                        self.src_path = src_path
                        self.is_directory = False
                
                initial_handler.on_created(MockEvent(full_path))
                
            except Exception as e:
                print(f"Fehler beim Prüfen der Bestandsdatei {filename}: {e}")

    # 2. DAUERHAFTE ÜBERWACHUNG STARTEN
    print("Alle relevanten Bestandsdateien verarbeitet. Wechsel zu 'Dauerhafte Überwachung'...")
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
