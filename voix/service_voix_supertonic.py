# -*- coding: utf-8 -*-
"""VOIX DE REPLI — Supertonic 3 (Supertone), voix 2 par défaut.

⚠️ PLUS LA VOIX ACTIVE depuis le 21/07 tard le soir : Utilisateur a éliminé
Supertonic à l'oreille après essai réel (« fabriqué, accent étrange » — 4 voix
essayées). La voix d'Alice est Pocket + 5476 (service_voix_pocket.py). Ce
service reste un REPLI sûr : il ne peut pas halluciner, 0 Go de VRAM, 0,2x.
L'historique ci-dessous date de son installation.

CHOISIE PAR UTILISATEUR à l'écoute (tests\SUPERTONIC_A_ECOUTER, script
tests\ecoute_supertonic.py) : « la 2 et la 4 ont l'accent le plus discret,
ma préférée reste la 4 ». Léger accent assumé — À SURVEILLER : une future
voix française NATIVE chez Supertone (releases k2-fsa/sherpa-onnx et
Supertone/supertonic-3 sur Hugging Face).

POURQUOI CE MOTEUR (recherche + mesures du 21/07) :
  - NON-AUTORÉGRESSIF (flow-matching, durées explicites) : il ne PEUT pas
    inventer de mots, répéter, ni produire de sons fantômes — le défaut
    incurable de la famille XTTS/Pocket/CosyVoice est ABSENT PAR CONSTRUCTION.
    Vérifié au juge Parakeet : 91-100 % des mots réentendus sur les 10 voix,
    « Ouais. » (6 caractères) rendu sans le moindre bruit.
  - PROCESSEUR PUR : 0 Go de VRAM, facteur 0,20-0,28x mesuré (4 à 5 fois plus
    vite que la lecture), modèle int8 de 122,8 Mo, chargé en ~0,7 s.
  - Il tourne dans sherpa-onnx — LE MÊME RUNTIME QUE L'OREILLE Parakeet.
    C'est pourquoi ce service vit dans la bulle ecoute\venv_parakeet (pas de
    nouvelle bulle : mêmes dépendances exactement, processus séparés).
  - Chiffres : « 3 heures » est bien lu « trois heures » (testé) — la faiblesse
    annoncée par la doc ne s'est pas montrée en français courant. À l'oreille
    en usage réel ; un filtre déterministe sera ajouté si besoin.

LE PROTOCOLE — identique aux services piper/pocket : port 8081, POST /parler,
blocs PCM 16 bits préfixés (big-endian), bloc vide = fin. ⚠️ SEULE DIFFÉRENCE :
ce service émet en 44100 Hz NATIF — la boucle règle SR_VOIX selon le moteur
(corrigé le 21/07 au soir : la première version rééchantillonnait vers 24000,
et Utilisateur a entendu la différence dès la première session : « le timbre
n'est pas le bon ». La moitié des aigus partait à la poubelle.)
REPLIS : MOTEUR_VOIX="pocket" ou "piper" dans ecoute\boucle_alice.py.

LE STREAMING : découpe en PHRASES, une génération par phrase (~0,25x), un bloc
envoyé par phrase. Premier son = la première phrase seule (~0,3-0,8 s de
fabrication). Pas de minimum de taille : ce moteur ne bruite pas les textes
courts (c'était une parade XTTS, inutile ici).

LE VOLUME : RMS mesuré 0,04-0,06 en sortie brute, l'oreille de Utilisateur est
calée sur ~0,16 -> même gain adaptatif que Pocket (RMS cible, borné par le
pic, jamais d'écrêtage).
"""
import json
import os
import re
import struct
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

sys.stdout.reconfigure(errors="replace")   # l'instrument ne casse pas le mécanisme

PROJET = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8081
SR_SORTIE = 44100          # le NATIF de Supertonic — la boucle suit (SR_VOIX)

MODELE = (PROJET + r"\modeles\voix\supertonic"
          r"\sherpa-onnx-supertonic-3-tts-int8-2026-05-11")

# ─── LA VOIX ────────────────────────────────────────────────────────────────
# Le 21/07 au soir : la 4 choisie sur échantillons, mais jugée « trop grave »
# en vraie session (c'est la plus grave des cinq, 172 Hz — et la chaîne est
# vérifiée : 44,1 kHz de bout en bout, ce qu'il entend est le vrai timbre).
# -> Essai de la 2 (176 Hz, l'autre « accent discret » de son tri).
# Rappel du casting (tests\SUPERTONIC_A_ECOUTER) : féminines = 0 (186 Hz),
# 1 (219 Hz, la plus aiguë), 2 (176), 3 (212), 4 (172).
# ALICE_VOIX_SUPERTONIC ne sert qu'aux BANCS D'ESSAI (comparer des voix sans
# éditer ce fichier) ; en usage normal la variable n'existe pas.
VOIX = int(os.environ.get("ALICE_VOIX_SUPERTONIC", 2))
LANGUE = {"lang": "fr"}

# Le volume perçu (RMS), borné par le pic — même règle que Pocket (l'écrêtage
# « chhh » du 20/07 est payé une fois pour toutes).
RMS_CIBLE = 0.16
PIC_MAXI = 0.95

LOG = (PROJET + r"\tests\logs" + "\\" +
       f"service_voix_{datetime.now():%Y-%m-%d_%H%M}.txt")

TTS = None


def tracer(msg):
    ligne = f"[{datetime.now():%H:%M:%S.%f}"[:-4] + f"] {msg}"
    print(ligne, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(ligne + "\n")
    except Exception:
        pass


def en_phrases(texte):
    """Découpe en phrases pour le streaming — une génération par phrase."""
    morceaux = [p.strip() for p in re.split(r"(?<=[\.\!\?…:])\s+|\n+", texte)]
    return [p for p in morceaux if any(c.isalnum() for c in p)]


def fabriquer(texte):
    """Une phrase -> PCM 16 bits mono 44100 Hz natif, gain appliqué."""
    g = __import__("sherpa_onnx").GenerationConfig()
    g.sid = VOIX
    g.extra = dict(LANGUE)
    audio = TTS.generate(texte, g)
    x = np.asarray(audio.samples, dtype="float32")
    if audio.sample_rate != SR_SORTIE:
        raise RuntimeError(f"sortie {audio.sample_rate} Hz, attendu {SR_SORTIE}")
    pic = float(np.abs(x).max())
    rms = float(np.sqrt((x ** 2).mean()))
    if pic > 1e-4 and rms > 1e-5:
        x = x * min(RMS_CIBLE / rms, PIC_MAXI / pic)
    return (np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes()


class Poignee(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"voix prete")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            texte = json.loads(self.rfile.read(n).decode("utf-8")).get("texte", "").strip()
        except Exception as e:
            tracer(f"ERREUR lecture demande : {e}")
            self.send_response(400)
            self.end_headers()
            return
        if not texte:
            self.send_response(400)
            self.end_headers()
            return

        phrases = en_phrases(texte)
        if not phrases:
            self.send_response(400)
            self.end_headers()
            return

        t0 = time.time()
        total = 0
        t_premier = None
        try:
            # Le 1er bloc est fabriqué AVANT l'en-tête : un échec rend un vrai
            # 500 au lieu d'un succès vide (leçon du 19/07 — Alice muette sans
            # trace nulle part).
            premier = fabriquer(phrases[0])
            t_premier = time.time() - t0
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(struct.pack(">I", len(premier)))
            self.wfile.write(premier)
            self.wfile.flush()
            total += len(premier)
            for p in phrases[1:]:
                bloc = fabriquer(p)
                self.wfile.write(struct.pack(">I", len(bloc)))
                self.wfile.write(bloc)
                self.wfile.flush()
                total += len(bloc)
            self.wfile.write(struct.pack(">I", 0))      # 0 = fin
            self.wfile.flush()
        except Exception as e:
            if t_premier is None:
                tracer(f"ERREUR synthèse : {type(e).__name__}: {e}")
                try:
                    self.send_response(500)
                    self.end_headers()
                except Exception:
                    pass
            else:
                tracer(f"le client a coupé pendant la lecture ({type(e).__name__})")
            return

        fab = time.time() - t0
        duree = total / 2 / SR_SORTIE
        tracer(f"{len(texte)} caractères ({len(phrases)} phrase(s)) -> "
               f"{duree:.2f} s d'audio, 1er son {t_premier:.2f} s, total "
               f"{fab:.2f} s (facteur {fab/max(duree, 0.01):.3f}x)")


def main():
    global TTS
    import sherpa_onnx

    t = time.time()
    cfg = sherpa_onnx.OfflineTtsConfig()
    m = cfg.model.supertonic
    m.duration_predictor = MODELE + r"\duration_predictor.int8.onnx"
    m.text_encoder = MODELE + r"\text_encoder.int8.onnx"
    m.vector_estimator = MODELE + r"\vector_estimator.int8.onnx"
    m.vocoder = MODELE + r"\vocoder.int8.onnx"
    m.tts_json = MODELE + r"\tts.json"
    m.unicode_indexer = MODELE + r"\unicode_indexer.bin"
    m.voice_style = MODELE + r"\voice.bin"
    cfg.model.num_threads = 8
    TTS = sherpa_onnx.OfflineTts(cfg)
    tracer(f"Supertonic 3 chargé en {time.time()-t:.2f} s (voix {VOIX}, int8)")

    # Rodage : la première synthèse d'un processus paie les allocations.
    t = time.time()
    fabriquer("Bien. Maintenant, parle.")
    tracer(f"rodage fait en {time.time()-t:.2f} s")

    tracer(f"voix prête (processeur, 0 Go de VRAM) — service sur le port {PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Poignee).serve_forever()


if __name__ == "__main__":
    main()
