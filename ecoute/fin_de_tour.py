# -*- coding: utf-8 -*-
"""FIN DE TOUR + TRANSCRIPTION ANTICIPÉE — installés le 21/07/2026.

LE DOUBLE PROBLÈME DE UTILISATEUR (ses mots) : la retranscription « fait trop
d'erreurs », et il faut choisir entre se faire couper en pleine réflexion
(silence court) et attendre après chaque phrase (silence long). SILENCE_FIN
était le compromis qui ratait les deux — et les phrases TRONQUÉES par la
coupure sont précisément ce qui fait dérailler la transcription (recherche du
21/07 : Parakeet v3 est 2e au monde en français à 5,38 % d'erreurs — le
suspect n'est pas le moteur, c'est la coupure).

LA RÉPONSE DE LA COMMUNAUTÉ 2025-2026, installée ici :

1. **smart-turn v3** (Pipecat, open à 100 %) : un modèle de 8 Mo qui écoute
   L'INTONATION des 8 dernières secondes et répond « il a fini son tour » ou
   « il cherche ses mots ». 23 langues dont le français, ~30 ms sur CPU.
   Usage dans les boucles : au bout de SILENCE_COURT de silence (réglé dans
   boucle_alice.py — 0,6 s depuis le 21/07 au soir), on le consulte UNE fois —
   phrase finie -> on conclut tout de suite (au lieu d'attendre 1,3 s) ;
   suspendue -> on attend jusqu'à SILENCE_PLAFOND (2,8 s) sans couper. (Le
   rejuger sur le même silence est inutile : même audio, même verdict — seule
   une reprise de parole change la donne.)

2. **La transcription anticipée** : dès le début du silence, on lance la
   transcription du tampon EN PARALLÈLE du jugement. Si le tour est fini, le
   texte est déjà prêt (ou presque) quand on conclut — l'attente ressentie
   tombe à ~0. Si Utilisateur reprend la parole, on jette le résultat (coût :
   ~0,5 s de calcul Parakeet pour rien, sans gravité).

REPLI AUTOMATIQUE : si le modèle smart-turn manque ou échoue, a_fini() rend
None et les boucles retombent sur l'ancien SILENCE_FIN fixe. Rien ne casse.

LE PRÉTRAITEMENT est celui de Whisper, à l'identique (spectrogramme mel 80
bandes × 800 trames sur 8 s, filtres officiels openai/whisper) — vérifié par
l'auto-test en bas de fichier (voix de synthèse) : phrase finie 0,98,
suspendue 0,06, coupée en plein mot 0,06.

Modèle : modeles\ecoute\smart-turn\smart-turn-v3.0.onnx — LA v3.0, les exports
v3.1/v3.2 sont inutilisables (voir l'avertissement sur MODELE plus bas).
Auto-test : ecoute\venv_parakeet\Scripts\python.exe ecoute\fin_de_tour.py
"""
import os
import threading

import numpy as np

PROJET = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOSSIER = os.path.join(PROJET, "modeles", "ecoute", "smart-turn")
# ⚠️ LA v3.0, PAS les v3.1/v3.2 — mesuré le 21/07/2026 avec le prétraitement
# officiel (vérifié à 1e-6 près contre l'extracteur Whisper de transformers) :
#     v3.0 : phrase finie 0,98 · coupée en plein mot 0,10  -> ELLE JUGE
#     v3.1-cpu et v3.2-cpu : ~0,98 POUR TOUT, même du bruit blanc — ces
#     exports n'obéissent pas au code d'inférence publié par Pipecat.
# Si une mise à jour propose un nouvel export : rejouer l'auto-test d'abord.
MODELE = os.path.join(DOSSIER, "smart-turn-v3.0.onnx")
FILTRES = os.path.join(DOSSIER, "mel_filters.npz")

SR = 16000
N_FFT = 400
SAUT = 160
N_ECHANTILLONS = 8 * SR          # le modèle regarde les 8 dernières secondes


def _log_mel(audio):
    """L'audio 16 kHz -> le spectrogramme log-mel de Whisper (80 x 800).

    C'est la recette EXACTE de Whisper (fenêtre de Hann périodique, saut 160,
    filtres mel officiels, écrêtage à max-8, normalisation (x+4)/4). Toute
    divergence ici fausserait le modèle en silence — d'où l'auto-test.

    ⚠️ do_normalize : smart-turn passe do_normalize=True à l'extracteur de
    Whisper — l'AUDIO est d'abord ramené à moyenne nulle et variance 1, AVANT
    le rembourrage. Sans cette étape (oubliée à la première version), le
    modèle rendait 0,99 pour tout, même une phrase coupée en plein mot :
    l'auto-test l'a attrapée sur-le-champ.
    """
    x = audio[-N_ECHANTILLONS:].astype(np.float32)
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    if len(x) < N_ECHANTILLONS:
        x = np.concatenate([x, np.zeros(N_ECHANTILLONS - len(x), dtype=np.float32)])

    fenetre = np.hanning(N_FFT + 1)[:-1].astype(np.float32)   # Hann périodique
    x = np.pad(x, N_FFT // 2, mode="reflect")
    n_trames = 1 + (len(x) - N_FFT) // SAUT
    trames = np.lib.stride_tricks.as_strided(
        x, shape=(n_trames, N_FFT),
        strides=(x.strides[0] * SAUT, x.strides[0])).copy()
    spectre = np.abs(np.fft.rfft(trames * fenetre, axis=1)) ** 2   # (trames, 201)
    spectre = spectre[:-1]                                         # comme Whisper

    mel = np.load(FILTRES)["mel_80"].astype(np.float32)            # (80, 201)
    m = mel @ spectre.T                                            # (80, 800)
    m = np.log10(np.maximum(m, 1e-10))
    m = np.maximum(m, m.max() - 8.0)
    return ((m + 4.0) / 4.0).astype(np.float32)


class FinDeTour:
    """Le juge d'intonation. a_fini() -> probabilité 0-1, ou None si indisponible."""

    def __init__(self):
        self.session = None
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 2      # 12 ms : inutile de mobiliser les cœurs
            self.session = ort.InferenceSession(
                MODELE, sess_options=opts, providers=["CPUExecutionProvider"])
        except Exception:
            self.session = None                # repli : les boucles gardent SILENCE_FIN

    @property
    def disponible(self):
        return self.session is not None

    def a_fini(self, audio_16k):
        """audio float32 (ou int16) 16 kHz -> probabilité que le tour soit fini."""
        if self.session is None:
            return None
        try:
            x = np.asarray(audio_16k)
            if x.dtype == np.int16:
                x = x.astype(np.float32) / 32768.0
            entree = _log_mel(x)[np.newaxis, :, :]                 # (1, 80, 800)
            sortie = self.session.run(None, {"input_features": entree})[0]
            p = float(sortie.reshape(-1)[0])
            # La sortie s'appelle « logits » mais les versions publiées rendent
            # déjà une probabilité. Si un futur export rendait un vrai logit,
            # on le rattrape ici au lieu de juger n'importe quoi.
            if p < 0.0 or p > 1.0:
                p = 1.0 / (1.0 + np.exp(-p))
            return p
        except Exception:
            return None


class OreilleAnticipee:
    """La transcription lancée en avance, pendant qu'on juge le silence.

    UNE transcription à la fois. `base` identifie le tour (le nombre de
    bouffées de PAROLE au moment du lancement — stable pendant tout le
    silence qui suit). Si la parole reprend, oublier() invalide le résultat.
    """

    def __init__(self, transcrire):
        self._transcrire = transcrire
        self._fil = None
        self._base = -1
        self._texte = None
        self._ok = False

    def deja_lancee(self, base):
        return self._base == base

    def lancer(self, echantillons, base):
        """Part en tâche de fond. Refuse si une transcription tourne encore."""
        if self._fil is not None and self._fil.is_alive():
            return False
        self._base = base
        self._texte, self._ok = None, False

        def _travail():
            try:
                self._texte = self._transcrire(echantillons)
                self._ok = True
            except Exception:
                self._ok = False

        self._fil = threading.Thread(target=_travail, daemon=True)
        self._fil.start()
        return True

    def oublier(self):
        """La parole a repris : le pari est perdu, le résultat ne vaut rien."""
        self._base = -1

    def recolter(self, base, patience=30):
        """Le texte si le pari tenait sur CE tour (attend la fin si besoin), sinon None."""
        if self._fil is None or self._base != base:
            return None
        self._fil.join(patience)
        return self._texte if self._ok else None


if __name__ == "__main__":
    # AUTO-TEST : le juge distingue-t-il une phrase finie d'une phrase coupée ?
    # On fabrique les deux avec une voix de synthèse (Supertonic, le repli —
    # pas la voix d'Alice, mais peu importe : on teste le JUGE) — sans micro.
    import sys
    sys.stdout.reconfigure(errors="replace")
    import sherpa_onnx

    M = (PROJET + r"\modeles\voix\supertonic"
         r"\sherpa-onnx-supertonic-3-tts-int8-2026-05-11")
    cfg = sherpa_onnx.OfflineTtsConfig()
    mm = cfg.model.supertonic
    mm.duration_predictor = M + r"\duration_predictor.int8.onnx"
    mm.text_encoder = M + r"\text_encoder.int8.onnx"
    mm.vector_estimator = M + r"\vector_estimator.int8.onnx"
    mm.vocoder = M + r"\vocoder.int8.onnx"
    mm.tts_json = M + r"\tts.json"
    mm.unicode_indexer = M + r"\unicode_indexer.bin"
    mm.voice_style = M + r"\voice.bin"
    cfg.model.num_threads = 8
    tts = sherpa_onnx.OfflineTts(cfg)

    def voix(texte):
        g = sherpa_onnx.GenerationConfig()
        g.sid = 4
        g.extra = {"lang": "fr"}
        a = tts.generate(texte, g)
        x = np.asarray(a.samples, dtype="float32")
        n = int(len(x) * SR / a.sample_rate)
        return x[(np.arange(n) * a.sample_rate / SR).astype(int)]

    juge = FinDeTour()
    print("modèle chargé :", juge.disponible)
    import time
    cas = [
        ("phrase finie      ", voix("Tu demandes à une déesse de t'aider pour "
                                    "quelque chose d'aussi trivial ?")),
        ("finie, affirmative", voix("Franchement, je suis fatiguée de tes questions.")),
        ("texte suspendu    ", voix("Alors en fait je voulais te dire que")),
    ]
    complete = voix("Il faudrait que tu me racontes ta journée maintenant.")
    cas.append(("coupée en plein mot", complete[:int(len(complete) * 0.55)]))
    for nom, x in cas:
        t0 = time.time()
        p = juge.a_fini(x)
        print(f"  {nom} : probabilité de fin {p:.2f}  ({(time.time()-t0)*1000:.0f} ms)")
