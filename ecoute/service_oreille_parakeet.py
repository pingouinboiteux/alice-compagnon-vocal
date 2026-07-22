# -*- coding: utf-8 -*-
"""L'OREILLE D'ALICE — Parakeet TDT 0.6B v3 (NVIDIA, via sherpa-onnx), 20/07/2026.

POURQUOI CE REMPLACEMENT (demandé par Utilisateur) : whisper dec8 était « trop
lent pour un résultat trop mauvais — erreurs, mots seuls lancés alors que je
n'ai rien dit ». Parakeet est l'oreille des projets temps réel (GLaDOS) :
  - MESURÉ sur nos fichiers de contenu connu : 0,4-0,9 s par phrase contre
    3,4 s pour whisper (5-8x plus vite), chargement 2 s.
  - justesse : égalité parfaite avec whisper sur la voix naturelle d'une voix commerciale,
    léger retrait sur la voix robotique de Piper. À valider sur SA voix.
  - son architecture (transducteur) n'INVENTE PAS de mots sur le silence —
    les « merci d'avoir regardé » fantômes sont une maladie de whisper.
  - processeur uniquement (onnxruntime int8), 0 Go de VRAM, ~700 Mo de disque.

LE PROTOCOLE — identique au whisper-server que ce service remplace : port
8080, POST /inference avec un wav en multipart, réponse JSON {"text": ...}.
La boucle n'a RIEN à changer ; revenir à whisper est une ligne
(MOTEUR_OREILLE dans ecoute\\boucle_alice.py).

LIMITE CONNUE : pas de « prompt » de vocabulaire comme whisper — les noms
propres (un nom propre, un mot-test...) passent par la justesse brute du modèle.
sherpa-onnx sait faire du « hotwords boosting » : piste notée si les noms
propres souffrent, pas activée d'emblée.
"""
import cgi
import io
import json
import sys
import threading
import time
import wave
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

sys.stdout.reconfigure(errors="replace")   # l'instrument ne casse pas le mécanisme

PROJET = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8080
MODELE = (PROJET + r"\modeles\ecoute\sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8")

LOG = (PROJET + r"\tests\logs" + "\\" +
       f"service_oreille_{datetime.now():%Y-%m-%d_%H%M}.txt")


def tracer(msg):
    ligne = f"[{datetime.now():%H:%M:%S.%f}"[:-4] + f"] {msg}"
    print(ligne, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(ligne + "\n")
    except Exception:
        pass


RECO = None
# ⚠️ UNE transcription à la fois — 21/07/2026. Le service reçoit à la fois la
# parole de Utilisateur (boucle) et les vérifications de la voix d'Alice
# (service voix). En simultané, sherpa-onnx rendait des transcriptions
# POUBELLE (couvertures de 3-6 % sur des prises saines -> fausses reprises en
# série dans la session de 11h56). Le verrou sérialise : chacun attend son
# tour, ~0,5 s au pire.
VERROU = threading.Lock()


class Poignee(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"oreille prete")

    def do_POST(self):
        try:
            form = cgi.FieldStorage(
                fp=self.rfile, headers=self.headers,
                environ={"REQUEST_METHOD": "POST",
                         "CONTENT_TYPE": self.headers.get("Content-Type", "")})
            brut = form["file"].value
        except Exception as e:
            tracer(f"ERREUR lecture demande : {type(e).__name__}: {e}")
            self.send_response(400)
            self.end_headers()
            return

        try:
            with wave.open(io.BytesIO(brut), "rb") as w:
                sr = w.getframerate()
                x = np.frombuffer(w.readframes(w.getnframes()),
                                  dtype="<i2").astype("float32") / 32768
            if sr != 16000:
                from scipy.signal import resample_poly
                x = resample_poly(x, 16000, sr)
            t0 = time.time()
            with VERROU:
                s = RECO.create_stream()
                s.accept_waveform(16000, x)
                RECO.decode_stream(s)
                texte = s.result.text.strip()
            dt = time.time() - t0
        except Exception as e:
            tracer(f"ERREUR transcription : {type(e).__name__}: {e}")
            self.send_response(500)
            self.end_headers()
            return

        tracer(f"{len(x)/16000:.1f} s d'audio -> {dt:.2f} s : \"{texte[:80]}\"")
        corps = json.dumps({"text": texte}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(corps)


def main():
    global RECO
    import sherpa_onnx
    t = time.time()
    RECO = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=MODELE + r"\encoder.int8.onnx",
        decoder=MODELE + r"\decoder.int8.onnx",
        joiner=MODELE + r"\joiner.int8.onnx",
        tokens=MODELE + r"\tokens.txt",
        model_type="nemo_transducer", num_threads=8)
    tracer(f"Parakeet v3 chargé en {time.time()-t:.2f} s")

    # Rodage : la première inférence paie les allocations.
    t = time.time()
    s = RECO.create_stream()
    s.accept_waveform(16000, np.zeros(16000, dtype="float32"))
    RECO.decode_stream(s)
    tracer(f"rodage fait en {time.time()-t:.2f} s")

    tracer(f"oreille prête (processeur, 0 Go de VRAM) — service sur le port {PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Poignee).serve_forever()


if __name__ == "__main__":
    main()
