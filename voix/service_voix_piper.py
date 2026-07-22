# -*- coding: utf-8 -*-
"""LA VOIX D'ALICE — Piper (processeur), depuis le 19/07/2026.

Remplace XTTS, choisi à l'oreille par Utilisateur après comparaison des deux sur
ses vraies répliques : « la voix est claire, elle prononce bien, un français
sans accent avec une voix féminine ».

┌──────────────────────────────────────────────────────────────────────────┐
│                        XTTS (carte)      Piper (processeur)              │
│  facteur temps réel      0,44x               0,028x    (16x plus rapide) │
│  mémoire vidéo           5,1 Go              0 Go                        │
│  chargement              8,7 s               1,25 s                      │
│  licence                 non commerciale     MIT                         │
└──────────────────────────────────────────────────────────────────────────┘
Les 5,1 Go rendus vont au jeu de Utilisateur : il passe de ~1,6 à ~6,7 Go libres.
C'était le but de toute la journée du 19/07, et la solution ne venait pas d'où
on la cherchait.

CE QUE PIPER REND INUTILE — et qu'on ne réécrit donc PAS ici :
  • le découpage en morceaux, le MINI_ABSOLU de 40 caractères, la taille du
    premier morceau : tout ça existait parce qu'XTTS fabriquait plus lentement
    qu'on ne parle (facteur > 1) et inventait du bruit sous 40 caractères.
    À 0,028x, une réplique entière est prête en 0,13 s. On envoie tout d'un bloc.
  • `couper_silence_finale()` : les ~0,6 s de silence que XTTS collait à chaque
    morceau n'existent pas ici, et il n'y a plus qu'un seul bloc de toute façon.
  • `nettoyer_typographie()` : MESURÉ le 19/07 — Piper ignore « » … — (3,47 s
    contre 3,88 s sans). XTTS, lui, passait 20 % de son temps à les ânonner.

CE QUI EST CONSERVÉ, et pourquoi :
  • le MÊME protocole que les autres services de voix (port 8081, POST /parler,
    blocs PCM 16 bits 24000 Hz préfixés de leur longueur, bloc vide = fin). La
    boucle et l'interface n'ont donc RIEN à changer d'un moteur à l'autre.
    (L'ancien service_voix.py — XTTS — a été supprimé au grand ménage du 21/07.)
  • la synthèse AVANT l'en-tête HTTP : un échec rend un vrai 500 au lieu d'un
    « succès vide » qui laissait Alice muette sans erreur nulle part
    (défaut trouvé à l'audit du 19/07 sur l'ancien service).

⚠️ RÉÉCHANTILLONNAGE : Piper sort à 22050 Hz, la boucle attend 24000 Hz
(SR_VOIX). On convertit ici, exactement (160/147), plutôt que de changer la
boucle : le contrat reste identique des deux côtés, donc rien ne peut diverger
si on rebascule sur XTTS.
"""
import json
import os
import struct
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from scipy.signal import resample_poly

from piper import PiperVoice, SynthesisConfig

# ─── LA VITESSE DE PAROLE ───────────────────────────────────────────────────
# Utilisateur, 20/07/2026 : « son rythme de parole est légèrement trop rapide,
# ralentis-la très légèrement ». length_scale étire la durée : 1.0 = vitesse
# d'origine de la voix, 1.05 = 5 % plus lent. C'est LE réglage prévu par Piper
# pour ça — le timbre ne bouge pas, seulement le débit.
# Choisi À L'OREILLE par Utilisateur le 20/07/2026 parmi 5 échantillons (la n°4).
# ⚠️ L'échelle SATURE entre 1.05 et 1.10 (quasi aucun effet mesuré dans cette
# zone) : pour ralentir davantage viser 1.20+, pour accélérer repasser à 1.0.
LENTEUR = 1.08
REGLAGES_VOIX = SynthesisConfig(length_scale=LENTEUR)

PROJET = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8081
SR_SORTIE = 24000                     # ce que la boucle attend, ne pas changer seul
MODELE = os.path.join(PROJET, "modeles", "voix", "piper", "fr_FR-siwis-medium.onnx")

LOG = os.path.join(PROJET, "tests", "logs",
                   f"service_voix_{datetime.now():%Y-%m-%d_%H%M}.txt")


def tracer(msg):
    ligne = f"[{datetime.now():%H:%M:%S.%f}"[:-4] + f"] {msg}"
    print(ligne, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(ligne + "\n")
    except Exception:
        pass


VOIX = None


def fabriquer(texte):
    """Rend du PCM 16 bits à 24000 Hz, mono. Lève si Piper échoue."""
    morceaux = list(VOIX.synthesize(texte, REGLAGES_VOIX))
    if not morceaux:
        raise RuntimeError("Piper n'a rien produit")
    brut = b"".join(m.audio_int16_bytes for m in morceaux)
    x = np.frombuffer(brut, dtype="<i2")
    sr = morceaux[0].sample_rate
    if sr != SR_SORTIE:
        # 22050 -> 24000 : rapport exact 160/147, pas d'approximation.
        y = resample_poly(x.astype("float32"), 160, 147)
        x = np.clip(y, -32768, 32767).astype("<i2")
    return x


class Poignee(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                      # on tient notre propre journal

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

        # On fabrique AVANT de répondre 200 : sinon un échec passerait pour un
        # succès vide et Alice resterait muette sans trace d'erreur.
        t0 = time.time()
        try:
            pcm = fabriquer(texte)
        except Exception as e:
            tracer(f"ERREUR synthèse : {type(e).__name__}: {e}")
            self.send_response(500)
            self.end_headers()
            return
        fab = time.time() - t0
        duree = len(pcm) / SR_SORTIE

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        brut = pcm.tobytes()
        try:
            self.wfile.write(struct.pack(">I", len(brut)))
            self.wfile.write(brut)
            self.wfile.write(struct.pack(">I", 0))      # 0 = fin
            self.wfile.flush()
        except Exception:
            tracer("le client a coupé pendant la lecture")
            return
        tracer(f"{len(texte)} caractères -> {duree:.2f} s d'audio "
               f"fabriqués en {fab:.3f} s (facteur {fab/duree:.3f}x)")


def main():
    global VOIX
    if not os.path.exists(MODELE):
        tracer(f"MODÈLE INTROUVABLE : {MODELE}")
        sys.exit(1)
    t = time.time()
    VOIX = PiperVoice.load(MODELE)
    tracer(f"voix chargée en {time.time()-t:.2f} s ({os.path.basename(MODELE)})")

    # Rodage : la toute première synthèse d'un processus est un peu plus lente
    # (allocations, caches). On la paie ici plutôt que sur la 1re réplique
    # d'Alice — même raison que le préchauffage du cerveau.
    t = time.time()
    fabriquer("Bien. Maintenant, parle.")
    tracer(f"rodage fait en {time.time()-t:.2f} s")

    tracer(f"voix prête (processeur, 0 Go de mémoire vidéo) — service sur le port {PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Poignee).serve_forever()


if __name__ == "__main__":
    main()
