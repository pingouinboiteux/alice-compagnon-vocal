# -*- coding: utf-8 -*-
"""LA VOIX D'ALICE — Pocket TTS (Kyutai), voix 5476, architecture du 22/07/2026.

Pocket TTS : 100M de paramètres, MIT, français natif, PROCESSEUR — 0 Go de
VRAM. Voix clonée : la 5476 (française native adulte, 185 Hz), voir LA VOIX
plus bas. L'histoire complète des réglages est en section 14 ter bis de
CLAUDE.md — dont les deux leçons chères : max_tokens compte des tokens de
TEXTE (ne jamais le passer), et le rembourrage « . . . . » était prononcé
(remplacé par frames_after_eos).

LE PROTOCOLE — identique aux autres services de voix : port 8081, POST /parler
{"texte"}, blocs PCM 16 bits 24000 Hz préfixés (big-endian), bloc vide = fin.
(Seul service_voix_supertonic émet en 44100 — SR_VOIX de la boucle suit.)
REPLIS : MOTEUR_VOIX="supertonic" ou "piper" dans ecoute\\boucle_alice.py.

L'ARCHITECTURE (rebâtie le 22/07 après la voix d'homme et le bruit des enfers) :
  1. le texte est découpé en UNITÉS : la 1re phrase seule (premier son
     rapide), puis des groupes de 40 à 130 caractères — la zone de confort
     du modèle (~50 tokens de texte). Nombres convertis en toutes lettres.
  2. chaque unité est GÉNÉRÉE (graine figée, frames_after_eos=8, deux
     plafonds anti-blanc : 1,5 s de calme après parole = fin, 4 s sans
     parole = prise refaite), ROGNÉE (tête et queue calmes), puis JUGÉE par
     le PORTIER (transcription Parakeet, port 8080) AVANT diffusion : prise
     suspecte -> refaite avec une autre graine, la MEILLEURE part. Unités de
     moins de 4 mots : injugeables, elles passent (limite assumée).
  3. jamais d'erreur pour une prise ratée : brut rogné en dernier recours,
     ou 0,3 s de silence — un 500 casserait TOUTE la phrase d'Alice.
  4. les répliques ALTERNENT d'exemplaire de modèle (isolation entre appels)
     et sont copiées dans tests\\logs\\voix_sessions\\ (purge auto, 7 jours).
  ⚠️ la REPRISE AUDIBLE (« Pardon, je m'emmêle ») a été RETIRÉE le 21/07 à la
  demande de Utilisateur (insupportable) — le portier refait AVANT de jouer,
  jamais après. Ne pas la réintroduire.
"""
import json
import os
import re
import struct
import sys
import threading
import time
import wave
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

# « Longueur maximale atteinte sans fin de parole » devient une vraie ERREUR
# (que le portier attrape : la prise est refaite en silencieux) au lieu d'un
# avertissement muet dans un coin. À poser AVANT l'import de pocket_tts.
os.environ.setdefault("KPOCKET_TTS_ERROR_WITHOUT_EOS", "1")

# La console Windows est en cp1252 : un caractère exotique dans une trace a
# déjà tué une réponse entière. L'instrument ne casse jamais le mécanisme.
sys.stdout.reconfigure(errors="replace")

PROJET = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8081
SR_SORTIE = 24000

# ─── LA VOIX ────────────────────────────────────────────────────────────────
# LA VOIX 5476 (185 Hz), choisie par Utilisateur le 21/07 au soir au casting des
# 11 voix françaises natives ADULTES de la banque Kyutai (cml-tts/fr, CC-BY,
# vraies lectrices — zéro accent par construction) : « ce sont les deux que
# j'accepte et qui sonnent bien » — et il a trouvé ses deux prises « presque
# identiques » : une voix STABLE d'une phrase à l'autre. Registre « femme/
# maman » demandé après l'élimination de Supertonic (fabriqué, accent).
# Prononciation vérifiée au juge : 100 % / 100 % sur les deux prises.
# Le clonage passe par le jeton Hugging Face de Utilisateur (déjà en place).
# REPLI : VOIX = "estelle" (la voix native du 20/07, plus jeune de timbre —
# get_state_for_audio_prompt accepte un NOM de la banque kyutai aussi bien
# qu'un chemin de wav, c'est ainsi qu'estelle tournait).
# ⚠️ UNE VOIX COMMERCIALE RESTE INCOMPATIBLE avec ce moteur (référence anglophone -> il
# TRADUIT ; 6 dérapages sur 10, preuve du 20/07). Jamais de référence à
# accent anglais ici sans re-mesurer le taux de dérapage.
VOIX = os.path.join(PROJET, r"modeles\voix\voix_reference.wav")

# Deux exemplaires du modèle : les répliques ALTERNENT (isolation entre
# appels : le décodeur peut laisser des restes) — jamais en simultané.
OUVRIERES = []
NB_OUVRIERES = 2
COMPTEUR_REPLIQUES = [0]

# ⚠️ GAIN AU VOLUME PERÇU (RMS), plus au pic — 21/07/2026. Le gain plafonné
# par les pics laissait la voix à RMS 0,07-0,13 quand l'oreille de Utilisateur
# est réglée sur Piper (0,19) : « sa voix est trop basse, je l'entends pas
# très bien » — ET Parakeet l'entendait mal aussi, d'où des reprises à tort.
# On vise RMS_CIBLE, borné par le pic (jamais d'écrêtage : les « chhh » du
# 20/07 sont payés une fois pour toutes).
RMS_CIBLE = 0.16
PIC_MAXI = 0.95

# La taille des blocs de diffusion (les miettes de 80 ms coupaient les mots).
BLOC_S = 1.0

# ─── L'ÉCOUTE DE CONTRÔLE (Parakeet, le service oreille de la boucle) ───────
URL_OREILLE = "http://127.0.0.1:8080/inference"

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


def en_pcm16(chunk):
    x = chunk.detach().squeeze().numpy()
    return (np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes()


# ⚠️ ON N'AMPLIFIE JAMAIS UN BLOC QUASI SILENCIEUX — 22/07/2026, après les
# « bruits de fin du monde » de Utilisateur. Le gain par bloc visait le volume
# cible SUR TOUT, y compris les blocs de pause/restes du rembourrage : un bloc
# à 0,001 de niveau se voyait amplifier ~95x — du souffle et des artefacts
# gonflés à pleine puissance au début, au milieu et à la fin des phrases.
# La garde : un bloc sous SEUIL_PAROLE_BLOC reste TEL QUEL. Les blocs de vraie
# parole gardent exactement le gain validé depuis une semaine.
# (1re tentative ratée, attrapée par l'instrument avant les oreilles de
# Utilisateur : un gain unique figé sur le 1er bloc SUR-amplifiait tout le reste
# — saturation à 0,93 de niveau, juge tombé à 57 %. Le gain par bloc est le
# bon mécanisme ; il ne lui manquait que cette garde.)
# 0,012 et pas 0,02 (22/07) : la parole brute de la voix 5476 descend à
# 0,025-0,035 sur les prises douces — à 0,02 de seuil, une prise molle passait
# ENTIÈRE pour du calme et la réplique sortait VIDE (500 « flux vide »).
# Les artefacts de queue, eux, restent sous ~0,005 : la marge est nette.
SEUIL_PAROLE_BLOC = 0.012


def appliquer_gain(brut):
    x = np.frombuffer(brut, dtype="<i2").astype("float32") / 32768
    pic = float(np.abs(x).max())
    rms = float(np.sqrt((x ** 2).mean()))
    if pic < 1e-4 or rms < SEUIL_PAROLE_BLOC:
        return brut                      # pause ou artefact : on n'y touche pas
    gain = min(RMS_CIBLE / rms, PIC_MAXI / pic)
    return (np.clip(x * gain, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def couper_queue_de_silence(brut, garde_s=0.25):
    """Coupe la queue d'une unité — silence et petits artefacts de fin de prise.

    Seuil RELATIF au pic de l'unité : les restes de fin de génération ne sont
    pas du silence parfait, ce sont des petits bruits — un seuil absolu les
    laissait passer, et le gain les mettait en pleine lumière.
    """
    x = np.frombuffer(brut, dtype="<i2")
    if not len(x):
        return brut
    # 2,5 % du pic et non 5 (22/07) : à 5 %, les fins de mots douces passaient
    # sous le seuil et se faisaient rogner — « des phrases qui ne se terminent
    # pas complètement » (Utilisateur). 2,5 % tranche les artefacts de queue sans
    # toucher aux dernières syllabes.
    seuil = max(int(0.01 * 32767), int(0.025 * int(np.abs(x).max())))
    i = len(x)
    while i > 0 and abs(int(x[i - 1])) < seuil:
        i -= 1
    fin = min(len(x), i + int(garde_s * SR_SORTIE))
    return x[:fin].tobytes()


def _mots(texte):
    import unicodedata
    t = unicodedata.normalize("NFD", texte.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return [m for m in re.findall(r"[a-z0-9']+", t) if len(m) > 1]


def ecouter_par_parakeet(brut):
    """Transcrit un audio via le service oreille. None si injoignable."""
    import io
    import urllib.request
    import uuid
    tampon = io.BytesIO()
    with wave.open(tampon, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR_SORTIE)
        w.writeframes(brut)
    frontiere = uuid.uuid4().hex
    piece = (f"--{frontiere}\r\nContent-Disposition: form-data; "
             f"name=\"file\"; filename=\"u.wav\"\r\n"
             f"Content-Type: audio/wav\r\n\r\n").encode() + tampon.getvalue() + \
            f"\r\n--{frontiere}--\r\n".encode()
    req = urllib.request.Request(
        URL_OREILLE, data=piece,
        headers={"Content-Type": f"multipart/form-data; boundary={frontiere}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as x:
            return json.loads(x.read().decode("utf-8")).get("text", "")
    except Exception:
        return None


def couverture_par_loreille(brut, txt):
    """Part des mots de `txt` que Parakeet réentend dans `brut` — le juge du
    portier. None = injugeable (oreille absente, ou moins de 4 mots).

    Plancher à 4 mots et non 8 (audit du 22/07) : le portier doit couvrir
    aussi les PREMIÈRES unités — souvent courtes, et les plus audibles (la
    « voix d'homme » peut tomber n'importe où). En dessous de 4 mots, Parakeet
    rend trop souvent une transcription vide sur une prise pourtant saine
    (mesuré le 21/07 : « Ouais. Trois mots, pas plus. » -> 0 % deux fois) :
    injugeable, on laisse passer.
    """
    demandes = _mots(txt)
    if len(demandes) < 4:
        return None
    entendu = ecouter_par_parakeet(brut)
    if entendu is None:
        return None
    import collections
    cd = collections.Counter(demandes)
    ce = collections.Counter(_mots(entendu))
    return sum((cd & ce).values()) / len(demandes)


# ═══ LA GÉNÉRATION PAR UNITÉS — 21/07/2026 tard le soir ═════════════════════
# LE PLAFOND DES ~15 SECONDES, mesuré sur la session de 22h05 : avec une voix
# clonée par FICHIER, Pocket s'arrête net vers 14-15 s d'audio quelle que soit
# la longueur du texte (14,80 / 14,39 / 14,55 / 14,16 / 14,96 s pour 271 à
# 438 caractères — le témoin n'a réentendu que 64 % puis 35 % des mots).
# C'est le « elle coupe avant la fin » de Utilisateur. Parade : générer par
# UNITÉS de phrases — aucune unité n'approche jamais le plafond.
#   MINI_UNITE = 40 : en dessous, Pocket bruite (mesuré le 20/07).
#   (L'ancien rembourrage « . . . . » est MORT — voir TRAMES_APRES_FIN.)
MINI_UNITE = 40
# ⚠️ MAXI_UNITE — 22/07, après les « sursauts d'intonation » de Utilisateur :
# découpée PHRASE PAR PHRASE, chaque phrase était une prise indépendante et la
# voix « redémarrait » son intonation à chacune. Les unités regroupent donc
# plusieurs phrases : une réplique typique d'Alice = UNE prise continue.
# 130 et non 160 (recherche du 22/07 dans le code source de pocket-tts) : le
# modèle est entraîné sur des morceaux de ~50 tokens de TEXTE ≈ 16 s de parole ;
# 130 caractères français ≈ 45-55 tokens — pile dans sa zone de confort.
MAXI_UNITE = 130

# ⚠️ LE RÈGNE DU RembourrAGE « . . . . » EST FINI (recherche du 22/07) : le
# modèle PRONONÇAIT ces pauses avant d'oser conclure — c'étaient NOS queues de
# silence. Le paramètre OFFICIEL frames_after_eos fait ce travail proprement :
# N trames de 80 ms générées après la fin de parole (le yaml français en
# recommande 8). La protection des fins avalées passe par lui.
TRAMES_APRES_FIN = 8

# La GRAINE : l'API n'expose pas de seed, mais tout l'aléa passe par le RNG
# global de PyTorch. La figer rend les prises REPRODUCTIBLES — la « voix
# stable, sans variance » demandée par Utilisateur. La reprise d'une unité muette
# change de graine (sinon on refabriquerait le même raté à l'identique).
GRAINE = 5476


# ═══ LES NOMBRES EN TOUTES LETTRES — 22/07/2026, piège anticipé ═════════════
# pocket-tts n'a AUCUN normaliseur de texte : les chiffres sortent déformés
# (ticket n°113 « garbled »). Le cerveau écrit parfois « 3 heures » ou « 2 ou
# 3 mots » — on convertit AVANT la synthèse. Couvre 0-999999 et « 15h30 ».
_UNITES_FR = ["zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept",
              "huit", "neuf", "dix", "onze", "douze", "treize", "quatorze",
              "quinze", "seize"]
_DIZAINES_FR = {20: "vingt", 30: "trente", 40: "quarante", 50: "cinquante",
                60: "soixante", 80: "quatre-vingt"}


def _moins_de_cent(n):
    if n < 17:
        return _UNITES_FR[n]
    if n < 20:
        return "dix-" + _UNITES_FR[n - 10]
    if n in _DIZAINES_FR:
        return _DIZAINES_FR[n] + ("s" if n == 80 else "")
    d = (n // 10) * 10
    if d in (70, 90):                      # 70-79 et 90-99 : base 60/80
        base = _DIZAINES_FR[d - 10]
        reste = n - (d - 10)
        liaison = " et " if reste == 11 and d == 70 else "-"
        return base + liaison + _moins_de_cent(reste)
    reste = n - d
    liaison = " et " if (reste == 1 and d != 80) else "-"   # 21 « et un », 81 sans « et »
    return _DIZAINES_FR[d] + liaison + _UNITES_FR[reste]


def _nombre_fr(n):
    if n < 100:
        return _moins_de_cent(n)
    if n < 1000:
        c, reste = divmod(n, 100)
        tete = "cent" if c == 1 else _UNITES_FR[c] + " cent"
        if reste == 0:
            return tete + ("s" if c > 1 else "")
        return tete + " " + _moins_de_cent(reste)
    m, reste = divmod(n, 1000)
    tete = "mille" if m == 1 else _nombre_fr(m) + " mille"
    return tete if reste == 0 else tete + " " + _nombre_fr(reste)


def nombres_en_lettres(texte):
    # d'abord les heures « 15h30 » / « 15h »
    def _heure(m):
        h = _nombre_fr(int(m.group(1))) + " heure" + ("s" if int(m.group(1)) > 1 else "")
        return h + (" " + _nombre_fr(int(m.group(2))) if m.group(2) else "")
    texte = re.sub(r"\b(\d{1,2})\s?h(?:\s?(\d{2}))?\b", _heure, texte)
    # puis les entiers isolés (jusqu'à 6 chiffres)
    return re.sub(r"\b\d{1,6}\b", lambda m: _nombre_fr(int(m.group())), texte)


def en_unites(texte):
    """Découpe : la 1re PHRASE seule (premier son rapide malgré le portier),
    puis des unités de MINI_UNITE à ~MAXI_UNITE caractères."""
    phrases = [p.strip() for p in re.split(r"(?<=[\.\!\?…:])\s+|\n+", texte)
               if any(c.isalnum() for c in p)]
    if not phrases:
        return [texte]
    unites = [phrases[0]]
    encours = ""
    for p in phrases[1:]:
        if (encours and len(encours) >= MINI_UNITE
                and len(encours) + 1 + len(p) > MAXI_UNITE):
            unites.append(encours)
            encours = p
        else:
            encours = (encours + " " + p).strip()
    if encours:
        if len(unites) > 1 and len(encours) < MINI_UNITE and \
                len(unites[-1]) + 1 + len(encours) <= MAXI_UNITE + 25:
            unites[-1] += " " + encours
        else:
            unites.append(encours)
    return unites


def _est_calme(brut):
    x = np.frombuffer(brut, dtype="<i2").astype("float32") / 32768
    return float(np.sqrt((x ** 2).mean())) < SEUIL_PAROLE_BLOC


def _prise_de_lunite(modele, etat, unite, graine):
    """UNE prise complète d'une unité -> audio rogné (tête et queue calmes).

    Les deux plafonds pendant la génération (22/07) : 1,5 s de calme APRÈS la
    parole = queue, on arrête ; 4 s SANS parole = prise qui ne démarre pas.
    """
    import torch
    torch.manual_seed(graine)
    flux = modele.generate_audio_stream(etat, unite,
                                        frames_after_eos=TRAMES_APRES_FIN)
    brut = b""
    fenetre = b""
    pas = int(0.5 * SR_SORTIE) * 2
    calme_queue = 0.0
    a_parle = False
    stop = False
    try:
        for chunk in flux:
            fenetre += en_pcm16(chunk)
            while len(fenetre) >= pas:
                bloc, fenetre = fenetre[:pas], fenetre[pas:]
                brut += bloc
                if _est_calme(bloc):
                    calme_queue += 0.5
                else:
                    a_parle = True
                    calme_queue = 0.0
                if (a_parle and calme_queue >= 1.5) or \
                        (not a_parle and calme_queue >= 4.0):
                    stop = True
                    break
            if stop:
                break
    except Exception as e:
        tracer(f"prise interrompue ({type(e).__name__}) sur « {unite[:40]} »")
    brut += fenetre
    if not brut:
        return b"", False
    # rogner la tête calme (amorce) puis la queue
    x = np.frombuffer(brut, dtype="<i2")
    seuil = max(int(0.01 * 32767), int(0.025 * int(np.abs(x).max() or 1)))
    i0 = 0
    while i0 < len(x) and abs(int(x[i0])) < seuil:
        i0 += 1
    x = x[max(0, i0 - int(0.1 * SR_SORTIE)):]
    return couper_queue_de_silence(x.tobytes()), a_parle


# ═══ LE PORTIER DES PRISES — 22/07, après la voix d'HOMME et le bruit des
# enfers entendus par Utilisateur. Le juge Parakeet ne CONSTATE plus après coup :
# chaque unité est vérifiée AVANT d'être jouée. Couverture sous le seuil
# (mots absents, autre voix, bruit) -> la prise est REFAITE avec une autre
# graine, et c'est la MEILLEURE des deux qui part. C'est l'architecture du
# meilleur outil communautaire (tts-audiobook-tool : « cherrypick the
# generation with the least number of errors »).
# LE PRIX : le premier son attend la fabrication + vérification de la première
# unité (~2-3 s au lieu de 0,8) — la fiabilité d'abord, au choix de Utilisateur.
# Sans oreille joignable (bancs isolés), la prise 1 part sans jugement.
SEUIL_COUVERTURE_UNITE = 0.75
ESSAIS_UNITE = 2


def fabriquer_bloc_a_bloc(ouvriere, texte):
    """Générateur : les blocs PCM (gain appliqué) des prises VALIDÉES, unité par unité."""
    modele, etat, verrou = ouvriere
    with verrou:
        for unite in en_unites(texte):
            meilleure, meilleure_couv = b"", -1.0
            for essai in range(1, ESSAIS_UNITE + 1):
                brut, a_parle = _prise_de_lunite(modele, etat, unite,
                                                 GRAINE + essai)
                if not brut or not a_parle:
                    tracer(f"prise {essai} muette sur « {unite[:40]} »"
                           + (" — on refait" if essai < ESSAIS_UNITE else ""))
                    continue
                couv = couverture_par_loreille(brut, unite)
                if couv is None:
                    # oreille absente ou unité trop courte pour être jugée :
                    # on prend la prise telle quelle, comme avant.
                    meilleure, meilleure_couv = brut, 1.0
                    break
                if couv > meilleure_couv:
                    meilleure, meilleure_couv = brut, couv
                if couv >= SEUIL_COUVERTURE_UNITE:
                    break
                tracer(f"prise {essai} suspecte ({couv:.0%}) sur "
                       f"« {unite[:40]} »"
                       + (" — on refait" if essai < ESSAIS_UNITE
                          else " — on garde la meilleure"))
            if not meilleure:
                # Toutes les prises muettes : une micro-pause vaut toujours
                # mieux qu'une réplique en erreur (un 500 casse TOUTE la phrase).
                meilleure = b"\x00\x00" * int(0.3 * SR_SORTIE)
            pas = int(BLOC_S * SR_SORTIE) * 2
            for i in range(0, len(meilleure), pas):
                yield appliquer_gain(meilleure[i:i + pas])


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
        texte = nombres_en_lettres(texte)

        i_replique = COMPTEUR_REPLIQUES[0]
        COMPTEUR_REPLIQUES[0] += 1
        ouvriere = OUVRIERES[i_replique % NB_OUVRIERES]

        t0 = time.time()
        total = 0
        entete_envoye = False
        morceaux = []

        def envoyer(brut):
            nonlocal total
            self.wfile.write(struct.pack(">I", len(brut)))
            self.wfile.write(brut)
            self.wfile.flush()
            total += len(brut)
            morceaux.append(brut)

        try:
            t_premier = None
            for bloc in fabriquer_bloc_a_bloc(ouvriere, texte):
                if not entete_envoye:
                    # Le 1er bloc est fabriqué AVANT l'en-tête : un échec rend
                    # un vrai 500 au lieu d'un succès vide (leçon du 19/07).
                    t_premier = time.time() - t0
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    entete_envoye = True
                envoyer(bloc)

            if not entete_envoye:
                tracer("ERREUR synthèse : flux vide")
                self.send_response(500)
                self.end_headers()
                return

            self.wfile.write(struct.pack(">I", 0))      # 0 = fin
            self.wfile.flush()
        except Exception as e:
            tracer(f"le client a coupé pendant la lecture ({type(e).__name__})")
            return

        fab = time.time() - t0
        duree = total / 2 / SR_SORTIE
        tracer(f"{len(texte)} caractères -> {duree:.2f} s d'audio, 1er son "
               f"{t_premier:.2f} s, total {fab:.2f} s (facteur "
               f"{fab/max(duree, 0.01):.3f}x)")

        # ═══ LA COPIE DE CONTRÔLE, en tâche de fond ══════════════════════════
        # Le jugement, lui, est fait par le PORTIER avant diffusion. Ici on ne
        # fait plus qu'archiver la réplique jouée. ⚠️ La REPRISE AUDIBLE
        # (« Pardon, je m'emmêle ») reste bannie (demande de Utilisateur, 21/07).
        def _temoin(prise=b"".join(morceaux), txt=texte):
            # Le PORTIER a déjà jugé chaque unité avant diffusion (22/07) —
            # plus de re-transcription ici, on garde seulement la copie de
            # contrôle : l'enquête de demain se fait en un coup d'œil.
            try:
                dossier = os.path.join(PROJET, "tests", "logs", "voix_sessions")
                os.makedirs(dossier, exist_ok=True)
                marque = f"{datetime.now():%Y-%m-%d_%H%M%S}"
                with wave.open(os.path.join(dossier, f"{marque}.wav"), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(SR_SORTIE)
                    w.writeframes(prise)
            except Exception as e:
                tracer(f"copie de contrôle impossible ({type(e).__name__})")

        threading.Thread(target=_temoin, daemon=True).start()


def main():
    from pocket_tts import TTSModel

    # Priorité au-dessus de la normale : la fabrication garde ses cœurs.
    try:
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), 0x00008000)
    except Exception:
        pass

    for i in range(NB_OUVRIERES):
        t = time.time()
        # quantize=True (facteur 0,58-0,65x mesuré).
        # ⚠️ TEMPÉRATURE : 0,5, NI PLUS NI MOINS. L'essai 0,3 du 21/07 (pour
        # la « voix stable ») a DÉGRADÉ la génération avec la voix 5476 :
        # répliques gonflées de 2-3x leur durée en blancs, une prise entendue
        # à 0 %. La stabilité vient de la graine figée et des garde-fous, pas
        # d'ici. (Repères : défaut officiel français 0,7 ; 0,5 donnait 98 %
        # de mots couverts contre 95 % à 0,7.) ALICE_POCKET_TEMP pour comparer.
        modele = TTSModel.load_model(language="french_24l", quantize=True,
                                     temp=float(os.environ.get("ALICE_POCKET_TEMP", 0.5)))
        # La parade OFFICIELLE des textes courts (8 espaces en préfixe sous
        # 5 mots), désactivée par défaut pour le français — on l'allume.
        try:
            modele.pad_with_spaces_for_short_inputs = True
        except Exception:
            pass
        if modele.sample_rate != SR_SORTIE:
            tracer(f"ERREUR : sortie {modele.sample_rate} Hz, attendu {SR_SORTIE}")
            sys.exit(1)
        etat = modele.get_state_for_audio_prompt(VOIX)
        OUVRIERES.append((modele, etat, threading.Lock()))
        tracer(f"ouvrière {i+1}/{NB_OUVRIERES} prête en {time.time()-t:.2f} s "
               f"(voix « {os.path.basename(str(VOIX))} »)")

    # Rodage : la première synthèse d'un processus est plus lente.
    # ⚠️ Texte AU-DESSUS de MINI_UNITE : « Bien. Maintenant, parle. » (24 car.)
    # tombait dans la zone où Pocket génère du vide — le rodage rendait 0 bloc.
    t = time.time()
    for _ in fabriquer_bloc_a_bloc(
            OUVRIERES[0],
            "Bien. Maintenant parle, et raconte-moi quelque chose d'intéressant."):
        pass
    tracer(f"rodage fait en {time.time()-t:.2f} s")

    tracer(f"voix prête (processeur, 0 Go de VRAM) — service sur le port {PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Poignee).serve_forever()


if __name__ == "__main__":
    main()
