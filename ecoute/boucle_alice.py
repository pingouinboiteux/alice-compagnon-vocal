# -*- coding: utf-8 -*-
"""
LA BOUCLE COMPLÈTE — Utilisateur parle, Alice répond à voix haute.
(Renommé boucle_alice.py au renommage général du 20/07 — les .bat suivent.)

Le chef d'orchestre. Il lance les trois services, écoute le micro, et fait
circuler la parole d'un étage à l'autre :

    micro -> VAD (guetteur léger) -> oreille (MOTEUR_OREILLE) -> mot de réveil ?
          -> cerveau + mémoire -> voix (MOTEUR_VOIX) -> haut-parleurs -> on réécoute

POURQUOI TROIS SERVICES : chaque brique vit dans sa propre bulle Python (elles ont
des dépendances incompatibles entre elles). Chacune garde son modèle CHAUD, donc on
ne paie le chargement qu'une fois, au démarrage.

LE JOURNAL DE TRAÇAGE : chaque étage écrit l'heure et sa durée. Si quelque chose
cloche, le journal dit À QUEL ÉTAGE — micro, oreille, cerveau, ou voix.

Pour arrêter : Ctrl+C, ou fermer la fenêtre. Tout est déchargé proprement.
"""
import io
import json
import os
import re
import struct
import subprocess
import menage
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
from config import MICRO_PREFERE  # noqa: E402
import fin_de_tour
import sys
import time
import unicodedata
import wave
from collections import deque
from datetime import datetime

import numpy as np
import requests
import sounddevice as sd
from openwakeword.vad import VAD

PROJET = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WSERVER = os.path.join(PROJET, "outils", "whisper.cpp", "Release", "whisper-server.exe")
# L'oreille : whisper-large-v3-french-distil-dec8, distillé POUR LE FRANÇAIS.
# Choisi par Utilisateur le 19/07 sur mesure : 3,16 s par phrase (contre 6,1 s au
# départ) et 1 mot inventé sur 44 (contre 3 sur 47 pour turbo).
# ⚠️ Le turbo reste plus RAPIDE de 0,23 s : on paie ce quart de seconde pour du
# français mieux entendu (turbo écrivait « disparairent », qui n'existe pas ;
# dec8 écrit « disparaissent », du vrai français mal conjugué).
MODELE_ECOUTE = os.path.join(PROJET, "modeles", "ecoute", "ggml-french-dec8.bin")

# Le vocabulaire souffle a l'oreille : les noms propres et mots de Utilisateur que
# whisper ecorchait (« Dundring », « Sabbatone », « Perceau » — une faute est
# meme devenue un souvenir). Fichier enrichissable a la main, une ligne = un mot.
# Mesure le 20/07/2026 : +0,1 s par phrase, aucune phrase normale modifiee (5/5).
try:
    VOCABULAIRE = "Conversation en francais avec Alice. Mots frequents : " + ", ".join(
        l.strip() for l in open(os.path.join(PROJET, "donnees", "vocabulaire_oreille.txt"),
                                encoding="utf-8")
        if l.strip() and not l.strip().startswith("#")) + "."
except Exception:
    VOCABULAIRE = ""

# LA VOIX — interrupteur à trois positions, toutes sur PROCESSEUR (0 Go de VRAM) :
#   "pocket"     : Pocket TTS (Kyutai, MIT), VOIX 5476 — française native adulte
#                  (185 Hz) choisie par Utilisateur le 21/07 au soir au casting des
#                  voix cml-tts. Le moteur le plus naturel/émotif validé à son
#                  oreille. C'est la position active. Défaut connu : ticket
#                  n°221 (mots répétés/insérés par intermittence — parades en
#                  place dans le service).
#   "supertonic" : Supertonic 3 (voix 2). Ne PEUT pas halluciner, 0,2x, MAIS
#                  éliminé à l'oreille le 21/07 : « fabriqué, accent étrange »,
#                  registre jamais juste (4 voix essayées). Reste un repli sûr.
#   "piper"      : fr_FR-siwis-medium, choisie à l'oreille le 19/07. Facteur
#                  0,028x. LE REPLI LE PLUS SÛR ET LE PLUS ANCIEN.
MOTEUR_VOIX = "pocket"
if MOTEUR_VOIX == "supertonic":
    PY_VOIX = os.path.join(PROJET, "ecoute", "venv_parakeet", "Scripts", "python.exe")
    SRV_VOIX = os.path.join(PROJET, "voix", "service_voix_supertonic.py")
elif MOTEUR_VOIX == "pocket":
    PY_VOIX = os.path.join(PROJET, "voix", "venv_pocket", "Scripts", "python.exe")
    SRV_VOIX = os.path.join(PROJET, "voix", "service_voix_pocket.py")
else:
    PY_VOIX = os.path.join(PROJET, "voix", "venv_piper", "Scripts", "python.exe")
    SRV_VOIX = os.path.join(PROJET, "voix", "service_voix_piper.py")
PY_CERVEAU = os.path.join(PROJET, "memoire", "venv", "Scripts", "python.exe")
SRV_CERVEAU = os.path.join(PROJET, "cerveau", "service_cerveau.py")

# L'OREILLE — interrupteur à deux positions, les deux sur PROCESSEUR :
#   "parakeet" : Parakeet TDT 0.6B v3 (sherpa-onnx). 0,4-0,9 s par phrase
#                (whisper : 3,4 s), n'invente pas de mots sur le silence.
#                À l'ESSAI depuis le 20/07/2026 — demandé par Utilisateur
#                (« trop lent pour un résultat trop mauvais »).
#   "whisper"  : dec8 + -ac 768 + -bs 5. LE REPLI SÛR : remettre
#                MOTEUR_OREILLE = "whisper" et rien d'autre.
MOTEUR_OREILLE = "parakeet"
PY_OREILLE = os.path.join(PROJET, "ecoute", "venv_parakeet", "Scripts", "python.exe")

URL_ECOUTE = "http://127.0.0.1:8080/inference"
URL_VOIX = "http://127.0.0.1:8081/parler"
URL_CERVEAU = "http://127.0.0.1:8082/repondre"

# Délais d'attente réseau, en secondes. Étaient à 900 (un quart d'heure !) :
# un service planté gelait toute la boucle, micro coupé, sans aucune reprise
# possible (audit du 19/07/2026). 300 s couvrent très largement le pire cas
# jamais mesuré (~130 s de cerveau un très mauvais jour de jeu), tout en
# rendant la main en quelques minutes si un service est réellement mort —
# la boucle trace alors l'erreur et RÉÉCOUTE, au lieu de rester figée.
TIMEOUT_CERVEAU = 300
TIMEOUT_VOIX = 300

SR = 16000
FRAME = 480
SEUIL_PAROLE = 0.5

# ⚠️ SILENCE_FIN — combien de silence signifie « il a fini de parler ».
# Était à 0,7 s. Utilisateur, le 18/07/2026 : « parfois j'ai pas fini de parler mais
# il envoie le message ». 0,7 s, c'est le temps de reprendre son souffle ou de
# chercher un mot : on le coupait en pleine phrase. Il parle peu et lentement,
# il a besoin de marge.
# ⚠️ RELEVÉ À 1,7 s LE 20/07/2026 : à 1,3 s, sa session réelle envoyait encore
# des débuts de phrase seuls (« Est-ce que... », « Elle... », « Ce. ») — il
# marque une pause en cherchant ses mots, et l'oreille croyait qu'il avait fini.
# Lui-même : « mon traducteur vocal bug, il envoie un mot seul sans raison ».
# Le coût est de 0,4 s de latence par échange — il l'a accepté explicitement
# (« même si on perd une demi-seconde pour éviter les erreurs »).
# Abaissé de 1,7 à 1,3 le 20/07/2026 sur le reproche de Utilisateur (« temps de
# coupure excessif à la fin »). Le filet des FRAGMENTS recolle les phrases
# coupées trop tôt ; si les recollages deviennent fréquents dans le journal,
# remonter vers 1,5.
# ⚠️ DEPUIS LE 21/07/2026, SILENCE_FIN n'est plus que LE REPLI (smart-turn
# absent ou en erreur). En temps normal, la fin de tour est jugée à
# l'INTONATION — voir l'encadré ci-dessous.
SILENCE_FIN = 1.3

# ═══ LA FIN DE TOUR INTELLIGENTE — 21/07/2026 (ecoute\fin_de_tour.py) ═══════
# Le dilemme de Utilisateur : à 1,3 s de silence fixe, on le coupe quand il
# cherche ses mots ; plus long, et chaque échange traîne. Le compromis ratait
# les deux — et les phrases TRONQUÉES sont la vraie source des erreurs de
# transcription (Parakeet est 2e au monde en français : le moteur n'y est
# pour rien). La réponse de la communauté 2025-2026, branchée ici :
#   smart-turn v3.0 (8 Mo, ~30 ms CPU) écoute L'INTONATION des 8 dernières
#   secondes. Au bout de SILENCE_COURT de silence, on le consulte UNE fois :
#     « il a fini »          -> on conclut tout de suite (gain ~0,9 s) ;
#     « il cherche ses mots »-> on attend jusqu'à SILENCE_PLAFOND sans couper.
#   Et pendant ce temps, la TRANSCRIPTION EST DÉJÀ PARTIE (le pari anticipé) :
#   quand on conclut, le texte est prêt — l'attente d'oreille tombe à ~0.
# Auto-test : phrase finie 0,98 · suspendue 0,06 · coupée en plein mot 0,06.
# ⚠️ RESSERRÉ le 21/07 au soir après la 1re session réelle : des phrases de
# Utilisateur ont été coupées sur des verdicts « fini » à 0,80-0,98. Deux causes
# mêlées : (1) le seuil officiel de 0,5 est trop permissif pour SA voix lente ;
# (2) SON MICRO EST DERRIÈRE UNE PORTE ANTI-BRUIT (zéros parfaits au journal,
# une capture de 9,2 s rendue VIDE) — quand la porte claque en pleine phrase,
# le juge entend un silence net après un audio tronqué et conclut « fini » de
# bonne foi. Le juge ne court-circuite donc plus qu'en cas de quasi-certitude ;
# le reste attend le plafond. La porte de bruit, elle, se règle dans le pilote du micro.
SILENCE_COURT = 0.6       # on juge l'intonation après ce silence
SILENCE_PLAFOND = 2.8     # « il cherche ses mots » : on lui laisse jusqu'à ça
SEUIL_TOUR = 0.9          # court-circuit seulement en quasi-certitude

# Durée minimale pour qu'un bruit soit considéré comme de la parole. Relevée de
# 0,3 à 0,5 s : en dessous, un claquement de clavier ou un soupir partait chez
# whisper — qui, ne trouvant pas de mots, en inventait (voir HALLUCINATIONS).
MIN_PAROLE = 0.5
PREROLL = 0.3
# ⚠️ 44,1 kHz pour Supertonic — corrigé le 21/07 au soir après le premier test
# réel de Utilisateur (« le timbre n'est pas le bon ») : le service rabaissait sa
# sortie 44,1 kHz vers les 24 kHz historiques de la chaîne — la moitié des
# aigus partait à la poubelle. Les échantillons validés à l'oreille étaient en
# 44,1 : la boucle joue désormais au format NATIF du moteur choisi.
SR_VOIX = 44100 if MOTEUR_VOIX == "supertonic" else 24000

# ═══ LES GARDE-FOUS DE L'OREILLE — 20/07/2026, après la session gelée ═══════
# Session de 16h24 : après 5 échanges parfaits, PLUS UNE SEULE capture pendant
# 94 s, zéro erreur, zéro trace. Alice « n'entendait plus » sans rien dire.
# Deux pannes muettes possibles, indistinguables faute de traces — on blinde
# les deux, et les traces VEILLE diront laquelle c'était si ça se reproduit :
#   CAPTURE_MAXI : si le détecteur de voix reste coincé en « il parle » (bruit
#     de fond, écho...), la capture n'avait AUCUNE limite — elle pouvait durer
#     éternellement sans que rien ne l'écrive nulle part. Au-delà de cette
#     durée, on force la fin de phrase et on transcrit ce qu'on a.
#   MICRO_MORT_S : si le flux micro meurt en silence (il rend des zéros
#     parfaits — un vrai micro capte toujours un souffle), on le relance.
# Desserré de 25 à 90 s le 21/07 (idée nocturne de Utilisateur : « si je fais de
# longues phrases ça se fait couper ») : la limite de 25 s datait de l'oreille
# whisper (~15 s de fenêtre). Parakeet transcrit des monologues entiers sans
# broncher. 90 s reste un garde-fou contre le détecteur coincé, plus un
# plafond de parole.
CAPTURE_MAXI = 90
MICRO_MORT_S = 30
PULSATION_S = 60          # toutes les 60 s d'écoute : une ligne VEILLE au journal

# Une fois réveillée, elle reste à l'écoute SANS qu'on redise son nom pendant ce
# temps-là. Sinon il faudrait dire « Alice » à chaque phrase, ce qui rendrait
# toute vraie conversation pénible. Passé ce délai de silence, elle se rendort.
FENETRE_CONVERSATION = 90        # secondes

# ═══════════════════════════════════════════════════════════════════════════
#  ELLE PREND LA PAROLE — la relance après un silence
# ═══════════════════════════════════════════════════════════════════════════
#
# Utilisateur parle peu et joue en même temps. Une compagne qui ne parle que
# lorsqu'on l'interroge n'est pas une présence, c'est un service — et le
# cahier des charges dit l'inverse depuis le premier jour.
#
# ⚠️ POURQUOI PAS UN SIMPLE DÉLAI FIXE COURT : à 30 s de silence, elle
# prendrait la parole ~120 fois par heure pendant une session de jeu. Même
# excellente, elle deviendrait insupportable — et ça irait contre le but, qui
# est de jouer tranquille avec quelqu'un à côté. Dans une vraie conversation,
# 30 s de silence n'est pas un silence : c'est une pause.
#
# LA RÈGLE RETENUE — le silence s'allonge tant qu'il ne répond pas, exactement
# comme quelqu'un qui relance une fois, deux fois, puis vous laisse tranquille :
#     il répond      -> le compteur revient au premier palier
#     il ne dit rien -> palier suivant, de plus en plus espacé
#     dernier palier -> elle se tient tranquille jusqu'à ce qu'il reparle
# Résultat : présente quand il est bavard, discrète quand il est concentré.
# Ça se règle tout seul, sans qu'il ait rien à faire.
#
# ─── LES RÉGLAGES, en minutes, à ajuster à l'oreille ────────────────────────
PALIERS_RELANCE = [120, 240, 480]   # 2 min, puis 4, puis 8 — puis silence
# ────────────────────────────────────────────────────────────────────────────

# ─── LE MOT DE REVEIL : « ALICE » (18/07/2026) ──────────────────────────────
# Remplace « Persephone », abandonne parce que le personnage mythologique tirait
# sans cesse le modele vers Hades, les Enfers et un vocabulaire de dix mots.
# « Alice » : deux syllabes nettes, aucune legende attachee, et surtout AUCUN mot
# francais courant ne s'y confond — « malice », « calice » et « palissade » sont
# des mots ENTIERS differents, donc la comparaison par mot entier les ecarte seule.
MOTIF_ALICE = re.compile(r"^h?al[iy][cs][ea]?s?$")
EXPLICITES = {"alice", "alyce", "halice", "alise", "alis"}

LOG = os.path.join(PROJET, "tests", "logs",
                   f"boucle_alice_{datetime.now():%Y-%m-%d_%H%M}.txt")


# Le fragment en suspens (voir LE RECOLLAGE DES FRAGMENTS dans la boucle).
fragment_en_attente = [""]
fragment_depuis = [0.0]

# Garde le handle de l'enclos en vie tant que le programme tourne.
ENCLOS = [None]


def tracer(etage, msg, ecran=True):
    ligne = f"[{datetime.now():%H:%M:%S.%f}"[:-4] + f"] {etage:<9} {msg}"
    if ecran:
        print(ligne, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(ligne + "\n")


def normaliser(txt):
    txt = unicodedata.normalize("NFD", txt.lower())
    txt = "".join(c for c in txt if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]", " ", txt)


def _match(mot):
    return mot in EXPLICITES or bool(MOTIF_ALICE.match(mot))


def contient_alice(txt):
    mots = normaliser(txt).split()
    for m in mots:
        if _match(m):
            return True, m
    for a, b in zip(mots, mots[1:]):
        if _match(a + b):
            return True, f"{a} {b}"
    return False, None


# Le nom d'appel tel qu'il apparaît dans le VRAI texte (accents et majuscules compris).
# Le `\s?` au milieu attrape le cas où Whisper coupe le nom en deux (« Alice »).
MOTIF_NOM_ECRIT = re.compile(r"\bh?al[iy][cs][ea]?s?\b", re.IGNORECASE)


def retirer_nom(txt):
    """Enlève le mot de réveil du message transmis au cerveau.

    POURQUOI C'EST INDISPENSABLE (défaut constaté le 18/07/2026) :
    en laissant la phrase entière (« Perséphone, tu es là ? »), le trieur de mémoire
    a vu ce nom dans la bouche de Utilisateur et a rangé le souvenir suivant :
        « L'utilisateur s'appelle Perséphone. »
    Un FAUX SOUVENIR — précisément ce que la V1 interdit. On appelle quelqu'un par
    son nom, on ne se le donne pas : le cerveau n'a aucun besoin de l'entendre.

    S'il n'a dit QUE son nom, il ne reste rien : on envoie un simple bonjour.
    """
    t = MOTIF_NOM_ECRIT.sub("", txt)
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"^[\s,;:!?\.…«»\-–]+", "", t)      # ponctuation restée orpheline en tête
    t = re.sub(r"\s+([,\.])", r"\1", t)            # (on garde l'espace avant ? ! ; : — usage français)
    t = re.sub(r",\s*(?=[\.\?!…])", "", t)         # « journée,. » -> « journée. »
    t = re.sub(r"\s*,\s*$", "", t)                 # virgule qui pend en fin de phrase
    t = t.strip()
    if len(t) < 2:
        return "Salut."
    return t[0].upper() + t[1:]


# ─── DÉMARRAGE DES TROIS SERVICES ───────────────────────────────────────────

def attendre(url, nom, secondes=420):
    t0 = time.time()
    while time.time() - t0 < secondes:
        try:
            requests.get(url, timeout=1)
            tracer("DÉMARRAGE", f"{nom} prêt ({time.time() - t0:.0f} s)")
            return True
        except Exception:
            time.sleep(0.5)
    tracer("DÉMARRAGE", f"ÉCHEC : {nom} n'a pas répondu en {secondes} s")
    return False


def lancer_services(fenetres=True):
    """fenetres=False : les services tournent caches (cas de l'interface graphique).
    Ils continuent d'ecrire leur propre journal, on ne perd donc aucune trace."""
    drapeau = subprocess.CREATE_NEW_CONSOLE if fenetres else subprocess.CREATE_NO_WINDOW
    procs = []
    # La trace dit le VRAI moteur : elle annonçait « whisper » et « Piper » en
    # dur quel que soit l'interrupteur (audit du 21/07) — trompeur au diagnostic.
    tracer("DÉMARRAGE", f"oreille ({MOTEUR_OREILLE})...")
    if MOTEUR_OREILLE == "parakeet" and VOCABULAIRE:
        tracer("DÉMARRAGE", "note : le vocabulaire souffleur (noms propres) est "
                            "INACTIF avec Parakeet — il ne sert qu'au repli whisper")
    # -t 12 et pas 6 : whisper traite TOUJOURS une fenêtre de 30 s, même pour un
    # « Ah » de 0,7 s — son coût ne dépend donc pas de la longueur de la phrase,
    # seulement du nombre de coeurs. Mesuré le 18/07 : 6 coeurs -> 10,0 s ;
    # 12 coeurs -> 6,9 s. Trois secondes gagnées sur CHAQUE phrase, gratuitement.
    # (12 = les 6 coeurs rapides du i5-13600K en double file ; au-delà on tape dans
    #  les coeurs lents et le gain s'écroule.) Rien ne tourne en même temps : la
    # voix et le cerveau attendent leur tour.
    # creationflags=drapeau : sans lui, whisper ouvre SA PROPRE fenetre noire, meme
    # quand l'interface graphique demande que tout soit cache (oubli du 18/07/2026 —
    # Utilisateur voyait deux fenetres au lieu d'une).
    # L'ENCLOS : tout service lancé ci-dessous y est rangé, ainsi que ce qu'il
    # lance à son tour (le llama-server du cerveau). Windows tue l'enclos entier
    # dès que ce programme disparaît — croix, Ctrl+C, plantage, peu importe.
    # Sans ça, des services survivaient : 23 Go de zombies trouvés le 18/07/2026.
    menage.nettoyer_restes(tracer)
    menage.purger_vieux_enregistrements(tracer)
    ENCLOS[0] = menage.creer_enclos()

    # -ac 768 : whisper complète toujours l'audio à 30 s ; -ac raccourcit cette
    # fenêtre. Mesuré le 19/07 sur 8 extraits : 6,5 s -> 3,0 s par phrase, soit
    # 3,5 s gagnées à CHAQUE fois qu'il parle. Le prix : whisper devient un peu
    # plus bavard et invente parfois un mot en fin de phrase (4 transcriptions
    # sur 8 identiques au mot près, les 4 autres avec un mot en trop ou mal
    # entendu — jamais un changement de sens).
    # 768 = de quoi tenir ~15 s de parole. NE PAS DESCENDRE À 256 : au-delà de
    # 5 s de phrase il n'entend plus la moitié, ET il devient plus lent (10 s)
    # parce que whisper repasse le morceau en repli.
    # POUR REVENIR EN ARRIÈRE : enlever "-ac", "768" de la ligne ci-dessous.
    if MOTEUR_OREILLE == "parakeet":
        procs.append(subprocess.Popen(
            [PY_OREILLE, "-X", "utf8",
             os.path.join(PROJET, "ecoute", "service_oreille_parakeet.py")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=drapeau))
    else:
        procs.append(subprocess.Popen(
            [WSERVER, "-m", MODELE_ECOUTE, "-l", "fr", "--host", "127.0.0.1",
             "--port", "8080", "-t", "12", "-ac", "768", "-bs", "5"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=drapeau))

    tracer("DÉMARRAGE", f"voix ({MOTEUR_VOIX})...")
    procs.append(subprocess.Popen([PY_VOIX, "-X", "utf8", SRV_VOIX],
                                  creationflags=drapeau))

    tracer("DÉMARRAGE", "cerveau + mémoire (chargement du modèle, ~40 s)...")
    procs.append(subprocess.Popen([PY_CERVEAU, "-X", "utf8", SRV_CERVEAU],
                                  creationflags=drapeau))

    for _p in procs:
        menage.mettre_dans_lenclos(ENCLOS[0], _p)

    ok = (attendre("http://127.0.0.1:8080/", "oreille")
          and attendre("http://127.0.0.1:8081/", "voix")
          and attendre("http://127.0.0.1:8082/", "cerveau"))
    return procs, ok


# ─── LES ÉTAGES ─────────────────────────────────────────────────────────────

# ─── LES MOTS QUE WHISPER INVENTE QUAND IL N'ENTEND RIEN ────────────────────
#
# Utilisateur, 18/07/2026 : « il y a trop de détection de "merci" alors que je ne
# dis rien ». Ce n'est pas un défaut de réglage, c'est un comportement CONNU de
# whisper : entraîné sur des sous-titres de vidéos, il a vu des milliers de fins
# de vidéo. Devant du silence ou un bruit sourd, il ne rend pas « rien » — il
# rend la formule la plus probable en fin de bande son. En français, c'est
# « Merci », « Merci d'avoir regardé cette vidéo », ou une ligne de sous-titrage.
#
# Conséquence, avant ce filtre : Alice se réveillait sur du vide, répondait à un
# « merci » que Utilisateur n'avait jamais dit, ET ce faux « merci » partait dans sa
# mémoire. C'est exactement le « faux souvenir d'audition » du cahier des charges.
#
# On compare sur le texte NU (sans casse, sans ponctuation, sans accents) parce
# que whisper écrit « Merci. », « merci », « Merci !» indifféremment.
HALLUCINATIONS = {
    "merci", "merci a tous", "merci beaucoup", "merci de votre attention",
    "merci d avoir regarde cette video", "merci d avoir regarde",
    "sous titres realises par la communaute d amara org",
    "sous titrage societe radio canada", "sous titres realises par",
    "amara org", "abonnez vous", "a bientot", "au revoir", "bonne journee",
    "c est fini", "voila", "la suite", "musique", "generique",
}


def _nu(txt):
    """Texte réduit à sa forme comparable : sans accents, sans ponctuation."""
    t = unicodedata.normalize("NFD", txt.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", t).split())


MOTS_OUTILS_FIN = {
    "le", "la", "les", "de", "du", "des", "un", "une", "que", "qui", "et",
    "ou", "mais", "donc", "pour", "dans", "sur", "avec", "sans", "est-ce",
    "si", "quand", "parce", "vers", "chez", "mon", "ma", "mes", "ton", "ta",
    "tes", "son", "sa", "ses", "ce", "cette", "ces", "au", "aux", "je", "tu",
    "il", "elle", "on", "y", "en", "a", "c'est", "j'ai", "t'as",
}


def a_l_air_inacheve(texte):
    """Cette prise ressemble-t-elle a un debut de phrase coupe en plein elan ?

    Trois indices, dans l'ordre de fiabilite :
      - whisper l'a terminee par « ... » : il a lui-meme entendu que ca restait
        en suspens (« C'est donc le... ») ;
      - le dernier mot est un mot-outil (article, preposition, pronom) : aucune
        phrase francaise ne se FINIT sur « le », « de », « que » ;
      - tres courte ET sans aucune ponctuation finale.
    Un vrai message court (« Oui. », « Ouais. », « Non ! ») a sa ponctuation :
    il part immediatement, rien ne change pour lui.
    """
    t = texte.strip()
    if not t:
        return False
    if t.endswith(("...", "…")):
        return True
    dernier = t.rstrip(".!?,;").split()[-1].lower() if t.rstrip(".!?,;").split() else ""
    if dernier in MOTS_OUTILS_FIN:
        return True
    return len(t.split()) <= 3 and not t.endswith((".", "!", "?"))


def est_une_hallucination(texte, duree):
    """Ce que whisper vient de rendre est-il un fantôme plutôt qu'une phrase ?

    Deux critères, tous deux nécessaires pour rejeter — on préfère laisser passer
    un faux positif que de rendre Alice sourde à un vrai « merci » de Utilisateur :
      1. le texte figure dans la liste des formules fantômes ;
      2. il est COURT au regard du temps de parole (une vraie phrase de 3 s fait
         plus de 4 mots ; « Merci » sur 3 s d'audio, c'est du vide mal lu).
    """
    nu = _nu(texte)
    if not nu:
        return True

    # ═══ LES MOTS ANGLAIS FANTÔMES — 20/07/2026, avec Parakeet ═══════════════
    # Parakeet parle 25 langues SANS verrou de langue : sur une amorce de
    # parole ou un bruit, il sort parfois un mot anglais seul (« vécu par
    # Utilisateur : un mot anglais apparu sans raison quand il a voulu parler »).
    # Un capture TRÈS COURTE composée uniquement de mots anglais courants est
    # un fantôme. Volontairement étroit : « cool », « ok », « game over » et
    # tout mot partagé avec le français ne sont PAS dans la liste — on préfère
    # laisser passer un faux mot que de le rendre sourde à un vrai.
    ANGLAIS_FANTOMES = {"the", "you", "thank", "thanks", "so", "well", "yeah",
                        "what", "this", "that", "and", "but", "now", "here",
                        "right", "okay", "hello", "hey", "please", "sorry",
                        "let's", "it's", "i'm", "was", "were", "have", "will"}
    mots = nu.split()
    if len(mots) <= 3 and all(m in ANGLAIS_FANTOMES for m in mots):
        return True

    if nu not in HALLUCINATIONS:
        return False
    # ⚠️ La liste HALLUCINATIONS vise les fantômes de WHISPER (formules de fin
    # de vidéo inventées sur le silence). Parakeet n'hallucine PAS sur le
    # silence : sous lui, un « Merci. » ou « Voilà. » est une VRAIE parole de
    # Utilisateur — la règle d'époque la jetait quelle que soit la durée (mot
    # seul), le rendant sourd à ses mots courts (audit du 22/07).
    if MOTEUR_OREILLE != "whisper":
        return False
    # « Merci » dit franchement en 0,8 s reste plausible ; « merci » extrait de
    # 3 s de bruit ne l'est pas.
    return duree > 1.6 or len(nu.split()) <= 1


# ═══ LA DÉCOUPE DES MONOLOGUES — 20/07/2026 ═════════════════════════════════
#
# L'oreille est réglée pour ~15 s de parole (-ac 768). Or les vraies prises de
# Utilisateur montent à 18,3 s (journal du 19/07). MESURÉ sur trois phrases de
# 17-18 s : au-delà de la fenêtre, whisper INVENTE — bégaiements (« enfin,
# enfin, enfin, enfin »), fins de phrases perdues, et ce charabia partait au
# cerveau ET au trieur de mémoire.
#
# Deux parades possibles, mesurées toutes les deux :
#   -ac 1024 partout : les longues passent, mais CHAQUE phrase courte passe de
#     3,0 à 4,55 s. On rendrait la moitié du gain de la journée pour un cas rare.
#   LA DÉCOUPE (retenue) : si la prise dépasse 13 s, on la coupe AU CREUX DE
#     SILENCE le plus profond et on transcrit les morceaux séparément. Les
#     phrases courtes — le cas normal — restent à 3,0 s ; un monologue coûte
#     deux transcriptions (~6 s) mais sort JUSTE au lieu de faux.
#
# POURQUOI COUPER À UN CREUX ET PAS AU MILIEU : couper en pleine voyelle
# fabriquerait deux demi-mots aux extrémités. Le creux d'énergie le plus
# profond est une respiration ou une pause — on coupe entre deux mots.
SEUIL_DECOUPE = 13.0          # secondes ; en dessous, un seul envoi comme avant


def _creux_de_silence(audio, debut, fin):
    """L'indice le plus silencieux entre debut et fin (énergie glissante 50 ms)."""
    fenetre = int(0.05 * SR)
    segment = np.abs(audio[debut:fin].astype(np.float32))
    if len(segment) < fenetre * 2:
        return (debut + fin) // 2
    energie = np.convolve(segment, np.ones(fenetre) / fenetre, mode="same")
    return debut + int(np.argmin(energie))


def _envoyer(audio_int16):
    """Un morceau -> whisper -> texte brut. (Le recollage se fait plus haut.)"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(audio_int16.tobytes())
    buf.seek(0)
    r = requests.post(URL_ECOUTE, files={"file": ("a.wav", buf, "audio/wav")},
                      data={"temperature": "0", "language": "fr",
                            "prompt": VOCABULAIRE,
                            "response_format": "json"}, timeout=90)
    try:
        return r.json().get("text", "")
    except Exception:
        return r.text


# ═══ LA COPIE-TÉMOIN DU MICRO — 21/07/2026 au soir ═════════════════════════
# Après la 1re session réelle (« plus d'erreurs qu'avant »), deux hypothèses
# se contredisaient : porte de bruit dans le pilote du micro (tout y est pourtant
# désactivé, vérifié capture d'écran à l'appui) ou captures abîmées ailleurs.
# Au lieu de deviner : on GARDE ce que l'oreille entend. Chaque capture est
# écrite dans tests\logs\micro_sessions\ avec sa transcription dans
# _transcriptions.txt — on peut RÉÉCOUTER ce que le micro a vraiment livré,
# et c'est le « banc sur SA voix » qui manquait à toutes nos mesures.
# Coût : ~32 Ko par seconde de parole. Mettre à False pour arrêter.
GARDER_MICRO = True
_DOSSIER_MICRO = os.path.join(PROJET, "tests", "logs", "micro_sessions")


def _garder_micro(audio_int16, texte):
    if not GARDER_MICRO:
        return
    try:
        os.makedirs(_DOSSIER_MICRO, exist_ok=True)
        marque = f"{datetime.now():%Y-%m-%d_%H%M%S}"
        with wave.open(os.path.join(_DOSSIER_MICRO, f"{marque}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(audio_int16.tobytes())
        with open(os.path.join(_DOSSIER_MICRO, "_transcriptions.txt"), "a",
                  encoding="utf-8") as f:
            f.write(f"{marque}.wav | {len(audio_int16)/SR:5.1f} s | \"{texte}\"\n")
    except Exception:
        pass          # le témoin ne casse jamais le mécanisme


def transcrire(audio_int16):
    # ⚠️ La découpe des monologues est une règle WHISPER (fenêtre ~15 s de
    # -ac 768). Parakeet transcrit des monologues entiers sans broncher —
    # c'est même la raison du CAPTURE_MAXI à 90 s. La découper sous Parakeet
    # multipliait les appels à l'oreille et les risques de mots coupés aux
    # jointures, sans aucun bénéfice (audit du 22/07).
    if MOTEUR_OREILLE != "whisper" or len(audio_int16) / SR <= SEUIL_DECOUPE:
        texte = recoller(_envoyer(audio_int16))
        _garder_micro(audio_int16, texte)
        return texte
    # Trop long pour la fenêtre de l'oreille : on découpe aux creux de silence.
    morceaux, reste = [], audio_int16
    while len(reste) / SR > SEUIL_DECOUPE:
        # On cherche le creux entre 8 s et 12,5 s — chaque morceau reste ainsi
        # sous la fenêtre, et la queue garde toujours au moins 2 s de matière
        # (un bout minuscule ferait halluciner whisper).
        a = int(8.0 * SR)
        b = min(int(12.5 * SR), len(reste) - int(2.0 * SR))
        coupe = _creux_de_silence(reste, a, b) if a < b else len(reste) // 2
        morceaux.append(reste[:coupe])
        reste = reste[coupe:]
    morceaux.append(reste)
    textes = [recoller(_envoyer(m)) for m in morceaux]
    texte = " ".join(t for t in textes if t).strip()
    _garder_micro(audio_int16, texte)
    return texte


def recoller(texte):
    """Répare les retours à la ligne que whisper met AU MILIEU des mots.

    TROUVÉ le 20/07/2026 en relisant une vraie conversation de Utilisateur. whisper
    découpe sa transcription en segments et met un saut de ligne entre eux — mais
    la coupure tombe parfois EN PLEIN MOT. Le cerveau recevait littéralement :
        « j'ai effacé ta mémoire sans faire g⏎affe »
        « c'est juste que tes répons⏎es étaient très médiocres »
        « une IA aut⏎onome »
    Mesuré sur sa session : 20 messages sur 64 contenaient un saut de ligne, et
    14 coupaient un mot en deux.

    POURQUOI ÇA COMPTE AU-DELÀ DE L'ESTHÉTIQUE : un mot coupé n'est plus un mot.
    Le cerveau le lit comme deux fragments inconnus, et le trieur de mémoire range
    des bouts de mots. C'est un bruit qu'on lui infligeait à chaque phrase longue.

    ⚠️ Ce n'est PAS la même chose que la troncature que j'ai cru voir d'abord :
    vérifié, 0 message sur 64 était réellement coupé à la fin. Utilisateur n'est
    jamais interrompu — c'était mon extraction du journal qui ne lisait que la
    première ligne. Piège déjà documenté, et j'y suis retombé.

    La règle, AFFINÉE le 20/07/2026 après vérification sur le texte brut : whisper
    distingue lui-même les deux cas par l'ESPACE.
        « g⏎affe »          — aucune espace autour du saut  -> milieu de mot, on recolle
        « c'est⏎ terminé »  — une espace à côté du saut     -> frontière de mots, on sépare
    Ma première règle avalait l'espace avec le saut (`\s*\n\s*` entre lettres) et
    collait les mots : « c'estterminé », « suiscomplètement ». Trouvé en éprouvant
    la découpe des monologues, pas à la relecture — le texte brut a tranché.
    """
    t = re.sub(r"(?<=[^\W\d_])\n(?=[^\W\d_])", "", texte)   # saut NU entre lettres
    t = re.sub(r"\s*\n\s*", " ", t)                          # tout autre saut -> espace
    t = re.sub(r"\s+([,.])", r"\1", t)                       # « mémoire , » -> « mémoire, »
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def relancer(secondes_de_silence):
    """Elle prend la parole. Rend ce qu'elle a dit, ou "" si elle se tait.

    Elle a le droit de ne rien dire : si elle n'a rien de neuf, le service rend
    un texte vide plutot que de la faire radoter. Mieux vaut un silence qu'une
    relance creuse.
    """
    try:
        r = requests.post(URL_CERVEAU.replace("/repondre", "/relancer"),
                          json={"silence": secondes_de_silence},
                          timeout=TIMEOUT_CERVEAU).json()
    except Exception as e:
        tracer("RELANCE", f"ERREUR : {type(e).__name__}: {e}")
        return ""
    mots = (r.get("texte") or "").strip()
    if not mots:
        return ""
    try:
        parler(mots)
    except Exception as e:
        tracer("VOIX", f"ERREUR pendant la relance : {type(e).__name__}")
    return mots


def demander_au_cerveau(message):
    r = requests.post(URL_CERVEAU, json={"message": message}, timeout=TIMEOUT_CERVEAU)
    return r.json()


def _lire_exact(flux, n):
    bouts, reste = [], n
    while reste > 0:
        b = flux.read(reste)
        if not b:
            return None
        bouts.append(b)
        reste -= len(b)
    return b"".join(bouts)


def parler(texte):
    """Envoie le texte à la voix et JOUE les morceaux au fur et à mesure.

    On ne fabrique pas tout avant de jouer : le 1er morceau se fait entendre
    pendant que le suivant se fabrique. Renvoie (délai avant le 1er son, durée totale).
    """
    t0 = time.time()
    premier, total = None, 0.0
    r = requests.post(URL_VOIX, json={"texte": texte}, stream=True, timeout=TIMEOUT_VOIX)
    flux = r.raw
    # latency="high" — 20/07/2026 : depuis Pocket, la voix se FABRIQUE pendant
    # qu'elle joue (le processeur est chargé en parallèle de la lecture). Avec
    # le petit tampon par défaut, chaque à-coup du processeur faisait craquer
    # la carte son : les « zii » et « chhh » de Utilisateur, pile pendant les
    # phrases. MESURÉ côté fabrication : l'audio produit est propre (24
    # générations, énergie haute-fréquence identique aux fichiers jugés sains).
    # Le bruit naissait à la LECTURE. Un tampon large absorbe les à-coups —
    # au prix de quelques dizaines de millisecondes de latence, invisibles.
    with sd.OutputStream(samplerate=SR_VOIX, channels=1, dtype="int16",
                         latency="high") as hp:
        while True:
            entete = _lire_exact(flux, 4)
            if not entete:
                break
            taille = struct.unpack(">I", entete)[0]
            if taille == 0:
                break
            brut = _lire_exact(flux, taille)
            if brut is None:
                break
            if premier is None:
                premier = time.time() - t0
            pcm = np.frombuffer(brut, dtype="<i2")
            total += len(pcm) / SR_VOIX
            hp.write(pcm)
    return (premier or 0.0), total


# ─── LA BOUCLE ──────────────────────────────────────────────────────────────

def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "w", encoding="utf-8") as f:
        f.write("=" * 74 + "\n")
        f.write(f" BOUCLE COMPLÈTE — ALICE — {datetime.now():%Y-%m-%d %H:%M}\n")
        f.write(f" Étages : MICRO -> OREILLE ({MOTEUR_OREILLE}) -> CERVEAU (+mémoire) "
                f"-> VOIX ({MOTEUR_VOIX})\n")
        f.write("=" * 74 + "\n\n")

    micro = None
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and (MICRO_PREFERE and MICRO_PREFERE.lower() in d["name"].lower()):
            micro = i
            tracer("DÉMARRAGE", f"micro : #{i} {d['name'].strip()}")
            break
    if micro is None:
        micro = sd.default.device[0]
        tracer("DÉMARRAGE", "micro préféré non trouvé — micro par défaut")

    procs, ok = lancer_services()
    if not ok:
        tracer("DÉMARRAGE", "un service manque à l'appel — j'arrête.")
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        return 1

    vad = VAD(n_threads=1)

    # La fin de tour intelligente + le pari de transcription anticipée.
    juge_tour = fin_de_tour.FinDeTour()
    anticipee = fin_de_tour.OreilleAnticipee(transcrire)
    tracer("DÉMARRAGE",
           "fin de tour à l'intonation active (smart-turn v3.0)" if juge_tour.disponible
           else f"smart-turn ABSENT — repli sur le silence fixe {SILENCE_FIN} s")

    print("\n" + "=" * 74)
    print("  ALICE EST LÀ.")
    print()
    print("  Appelle-la par son nom :   « Alice, ... »")
    print(f"  Ensuite, tu peux continuer à parler normalement pendant {FENETRE_CONVERSATION} s")
    print("  sans redire son nom. Après ce silence, elle se rendort.")
    print()
    print("  Pour arrêter : Ctrl+C, ou ferme cette fenêtre.")
    print("=" * 74 + "\n")

    preroll_frames = int(PREROLL * SR / FRAME)
    ring = deque(maxlen=preroll_frames)

    en_parole = False
    tampon = []
    compteur_silence = 0
    verdict_tour = None             # le jugement d'intonation du silence en cours
    eveillee_jusqua = 0.0
    derniere_parole = time.time()   # dernier echange, dans un sens ou l'autre
    palier_relance = 0              # 0 = premier palier ; monte s'il ne repond pas
    n = 0

    try:
        flux = sd.InputStream(samplerate=SR, channels=1, dtype="int16",
                              blocksize=FRAME, device=micro)
        flux.start()
        # Les garde-fous de l'oreille (voir leur définition en tête de fichier).
        frames_zero = 0
        frames_zero_mort = int(MICRO_MORT_S * SR / FRAME)
        derniere_pulsation = time.time()
        while True:
            bloc, _ = flux.read(FRAME)
            audio = bloc[:, 0]
            parle = vad.predict(audio, frame_size=FRAME) > SEUIL_PAROLE

            # ── GARDE-FOU : micro mort (que des zéros parfaits) ──
            if not audio.any():
                frames_zero += 1
                if frames_zero >= frames_zero_mort:
                    tracer("VEILLE", f"micro MUET depuis {MICRO_MORT_S} s (zéros "
                                     "parfaits) — je relance le flux d'entrée")
                    try:
                        flux.stop(); flux.close()
                    except Exception:
                        pass
                    flux = sd.InputStream(samplerate=SR, channels=1, dtype="int16",
                                          blocksize=FRAME, device=micro)
                    flux.start()
                    frames_zero = 0
                    ring.clear(); tampon = []; en_parole = False
                    verdict_tour = None; anticipee.oublier()
                    continue
            else:
                frames_zero = 0

            # ── PULSATION : une trace de vie par minute d'écoute ──
            # C'est l'instrument qui manquait à la session gelée du 20/07 :
            # elle dira si le micro vit (niveau) et ce que pense le détecteur.
            if time.time() - derniere_pulsation >= PULSATION_S:
                niveau = float(np.abs(audio).mean())
                tracer("VEILLE", f"micro vivant · niveau {niveau:.0f} · "
                                 f"vad {'PAROLE' if parle else 'silence'} · "
                                 f"capture {'EN COURS' if en_parole else 'non'}",
                       ecran=False)
                derniere_pulsation = time.time()

            # ── ELLE PREND LA PAROLE ? ──
            # On regarde à chaque bouffée de micro (toutes les 30 ms) : c'est
            # gratuit, ce sont deux comparaisons de nombres.
            # Conditions : elle est réveillée (sinon elle parlerait dans le
            # vide), il ne parle pas en ce moment, et le palier est atteint.
            # ⚠️ NE PAS REMETTRE LA CONDITION « elle est éveillée » ICI.
            # Première version : elle exigeait `time.time() < eveillee_jusqua`.
            # Or la fenêtre d'éveil dure 90 s et le premier palier est à 120 s :
            # elle se rendormait 30 s avant de pouvoir parler. La relance ne
            # pouvait MATHÉMATIQUEMENT jamais se déclencher — 0 relance sur la
            # session réelle de Utilisateur, sans le moindre message d'erreur.
            #
            # C'est d'ailleurs le bon comportement : le silence est précisément
            # le moment où une présence prend la parole. Elle se réveille pour
            # relancer, et rouvre la fenêtre pour qu'il réponde sans avoir à
            # redire son nom. Il suffit qu'elle l'ait déjà entendu une fois dans
            # la session (sinon elle parlerait à une pièce vide).
            if (not en_parole and palier_relance < len(PALIERS_RELANCE)
                    and n > 0
                    and time.time() - derniere_parole >= PALIERS_RELANCE[palier_relance]):
                # MICRO COUPÉ pendant qu'elle parle d'elle-même (audit du
                # 19/07/2026) : sinon elle s'entend dans les haut-parleurs,
                # whisper transcrit SA voix, et elle se répond toute seule.
                # Le chemin normal coupait déjà le micro ; la relance, non.
                flux.stop()
                try:
                    mots = relancer(int(time.time() - derniere_parole))
                finally:
                    time.sleep(0.25)          # laisse retomber l'écho du casque
                    flux.start()
                    ring.clear()
                    tampon = []
                    en_parole = False
                    verdict_tour = None
                    anticipee.oublier()
                # Le palier monte QUOI QU'IL ARRIVE : s'il répond, la boucle
                # normale le remettra à zéro plus bas. S'il ne dit rien, la
                # prochaine relance attendra plus longtemps.
                palier_relance += 1
                derniere_parole = time.time()
                eveillee_jusqua = time.time() + FENETRE_CONVERSATION
                if mots:
                    tracer("RELANCE", f"elle a pris la parole : « {mots[:70]} »")
                continue

            if not en_parole:
                ring.append(audio.copy())
                if parle:
                    en_parole = True
                    tampon = list(ring)
                    tampon.append(audio.copy())
                    compteur_silence = 0
                continue

            tampon.append(audio.copy())
            # ── GARDE-FOU : capture sans fin ──
            # Si le détecteur reste coincé en « il parle » (bruit de fond,
            # écho...), la capture n'avait AUCUNE limite : l'oreille semblait
            # sourde alors qu'elle enregistrait sans jamais conclure. Au-delà
            # de CAPTURE_MAXI, on force la fin et on transcrit ce qu'on a —
            # le découpage aux creux de silence (SEUIL_DECOUPE) fera le tri.
            if parle and len(tampon) * FRAME / SR >= CAPTURE_MAXI:
                tracer("MICRO", f"capture forcée à {CAPTURE_MAXI} s — le "
                                "détecteur ne voyait plus de silence")
            elif parle:
                compteur_silence = 0
                verdict_tour = None      # il reparle : le verdict passé est caduc
                anticipee.oublier()      # et le pari de transcription aussi
                continue
            else:
                compteur_silence += 1
                s_silence = compteur_silence * FRAME / SR
                # base_parole = le nombre de bouffées de PAROLE (stable pendant
                # tout ce silence) : c'est l'identité du pari anticipé.
                base_parole = len(tampon) - compteur_silence
                # LE PARI : dès 0,15 s de silence, la transcription part en
                # avance. S'il reprend la parole, on la jette (0,5 s de calcul
                # perdues, rien de plus).
                if s_silence >= 0.15 and not anticipee.deja_lancee(base_parole):
                    anticipee.lancer(np.concatenate(tampon), base_parole)
                # LE JUGE : une seule fois par silence (même audio = même
                # verdict — seule une reprise de parole change la donne).
                if (verdict_tour is None and juge_tour.disponible
                        and s_silence >= SILENCE_COURT):
                    verdict_tour = juge_tour.a_fini(
                        np.concatenate(tampon[-int(8 * SR / FRAME):]))
                    if verdict_tour is not None:
                        tracer("TOUR", ("fini" if verdict_tour >= SEUIL_TOUR
                                        else "il cherche ses mots — j'attends")
                               + f" (probabilité {verdict_tour:.2f})", ecran=False)
                if verdict_tour is None:
                    attente = SILENCE_FIN          # repli : le silence fixe
                elif verdict_tour >= SEUIL_TOUR:
                    attente = SILENCE_COURT
                else:
                    attente = SILENCE_PLAFOND
                if s_silence < attente:
                    continue

            # ── fin de phrase ──
            en_parole = False
            verdict_tour = None
            base_recolte = len(tampon) - compteur_silence
            ring.clear()
            duree = (len(tampon) - preroll_frames) * FRAME / SR
            if duree < MIN_PAROLE:
                anticipee.oublier()
                continue

            n += 1
            tracer("MICRO", f"parole captée ({duree:.1f} s)")
            # Il a repris la parole : la relance repart du premier palier.
            derniere_parole = time.time()
            palier_relance = 0

            t0 = time.time()
            # Le pari anticipé d'abord : si la transcription lancée au début du
            # silence porte bien sur CE tour, le texte est déjà (presque) prêt.
            texte = anticipee.recolter(base_recolte)
            if texte is not None:
                tracer("OREILLE", f"({time.time() - t0:.1f} s, anticipée) \"{texte}\"")
            else:
                try:
                    texte = transcrire(np.concatenate(tampon))
                except Exception as e:
                    tracer("OREILLE", f"ERREUR : {type(e).__name__}: {e}")
                    continue
                tracer("OREILLE", f"({time.time() - t0:.1f} s) \"{texte}\"")

            # Whisper invente des formules de fin de vidéo devant le silence.
            # On les jette AVANT le réveil : sinon Alice répond à un « merci »
            # que Utilisateur n'a jamais dit, et le range dans sa mémoire.
            if est_une_hallucination(texte, duree):
                tracer("OREILLE", "texte fantôme (whisper a meublé du silence) — ignoré")
                continue

            # ═══ LE RECOLLAGE DES FRAGMENTS — 20/07/2026 ═══════════════════
            # Même avec la pause à 1,7 s, la session réelle envoyait encore des
            # débuts de phrase seuls (« C'est donc le... ») : Utilisateur cherche
            # ses mots plus longtemps que ça, et allonger encore l'attente
            # ralentirait CHAQUE échange. La règle, à la place :
            #   - une prise qui a l'air INACHEVÉE (courte, sans ponctuation
            #     finale, ou finissant sur un mot-outil) est mise EN SUSPENS ;
            #   - la prise suivante s'y RECOLLE (« C'est donc le... » + « boss
            #     final du jeu ») si elle arrive dans les 20 s ;
            #   - si rien ne suit, le fragment est simplement abandonné : un
            #     début de phrase jamais fini n'était pas un message. C'est
            #     précisément son reproche — elle répondait à ces bouts-là.
            if fragment_en_attente[0]:
                if time.time() - fragment_depuis[0] <= 20:
                    texte = fragment_en_attente[0] + " " + texte
                    tracer("OREILLE", f"fragment recollé -> \"{texte[:60]}\"")
                else:
                    tracer("OREILLE", f"fragment abandonné (trop vieux) : "
                                      f"\"{fragment_en_attente[0][:40]}\"")
                fragment_en_attente[0] = ""
            if a_l_air_inacheve(texte):
                fragment_en_attente[0] = texte
                fragment_depuis[0] = time.time()
                tracer("OREILLE", "début de phrase en suspens — j'attends la suite")
                continue

            eveillee = time.time() < eveillee_jusqua
            detectee, mot = contient_alice(texte)

            if not detectee and not eveillee:
                tracer("RÉVEIL", "son nom n'est pas prononcé — je continue d'écouter")
                continue
            if detectee:
                tracer("RÉVEIL", f"appelée (sur « {mot} »)")
            else:
                tracer("RÉVEIL", "déjà éveillée — pas besoin de son nom")

            # ── cerveau + mémoire ──
            # On retire son nom : le cerveau n'a pas besoin de s'entendre appeler,
            # et le garder fabriquait un faux souvenir (voir retirer_nom).
            demande = retirer_nom(texte)
            if demande != texte:
                tracer("RÉVEIL", f"transmis sans son nom : \"{demande}\"", ecran=False)
            t0 = time.time()
            try:
                res = demander_au_cerveau(demande)
            except Exception as e:
                tracer("CERVEAU", f"ERREUR : {type(e).__name__}: {e}")
                eveillee_jusqua = time.time() + FENETRE_CONVERSATION
                continue
            if res.get("erreur") or not res.get("texte"):
                tracer("CERVEAU", f"ERREUR : {res.get('erreur')}")
                eveillee_jusqua = time.time() + FENETRE_CONVERSATION
                continue
            tracer("CERVEAU", f"({res['t_llm']:.1f} s · {len(res['texte'].split())} mots · "
                              f"{res['n_souvenirs']} souvenir(s) rappelé(s) en "
                              f"{res['t_recup']*1000:.0f} ms)")
            print(f"\n  ALICE > {res['texte']}\n")
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(f"           RÉPONSE   \"{res['texte']}\"\n")

            # ── voix (on n'écoute pas pendant qu'elle parle) ──
            flux.stop()
            t0 = time.time()
            try:
                premier, total = parler(res["texte"])
                if total == 0:
                    # Le service a répondu mais aucun son n'est arrivé (échec de
                    # synthèse) : on le dit, au lieu d'un silence inexpliqué.
                    tracer("VOIX", "aucun son reçu — voir le journal du service voix")
                else:
                    tracer("VOIX", f"1er son après {premier:.1f} s · {total:.1f} s dites "
                                   f"· {time.time() - t0:.1f} s au total")
            except Exception as e:
                tracer("VOIX", f"ERREUR : {type(e).__name__}: {e}")
            finally:
                time.sleep(0.25)          # laisse retomber l'écho du casque
                flux.start()
                ring.clear()
                tampon = []
                en_parole = False

            eveillee_jusqua = time.time() + FENETRE_CONVERSATION
            tracer("ÉCOUTE", f"je t'écoute (parle librement pendant {FENETRE_CONVERSATION} s)")

    except KeyboardInterrupt:
        print("\n\n  Arrêt demandé.")
    finally:
        # LA MÉMOIRE D'ABORD, les services ensuite. C'est ICI que le tri de fin
        # de session a lieu pour la boucle vocale. DÉFAUT CORRIGÉ le 19/07/2026
        # (audit) : la boucle tuait les services SANS jamais appeler /ranger —
        # le « tri à la fermeture » n'avait donc JAMAIS lieu par ce chemin, et
        # la file d'attente grossissait de session en session sur le disque.
        # (Si la fenêtre est fermée d'un coup de croix, ce bloc ne tourne pas :
        #  l'enclos tue tout, et le PROCHAIN démarrage rangera ce qui reste sur
        #  le disque. C'est le filet — ceci est la règle.)
        tracer("ARRÊT", "elle range ses souvenirs... (une dizaine de secondes)")
        try:
            r = requests.post(URL_CERVEAU.replace("/repondre", "/ranger"),
                              timeout=600).json()
            tracer("MÉMOIRE", f"session rangée : {r.get('souvenirs')} souvenir(s) "
                              f"en {r.get('duree', 0):.1f} s")
        except Exception as e:
            tracer("MÉMOIRE", f"rangement impossible ({type(e).__name__}) — "
                              f"ce sera fait au prochain démarrage")
        tracer("ARRÊT", "fermeture des services...")
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        # Plus d'appel à LM Studio ici (retiré le 19/07/2026) : le cerveau tourne
        # sur NOTRE llama.cpp, qui vit dans l'enclos — Windows le tue, et la carte
        # graphique est rendue, dès que ce programme disparaît, quoi qu'il arrive.
        # Et décharger LM Studio d'office touchait un logiciel que Utilisateur peut
        # utiliser pour son propre compte (règle du ménage : on ne touche qu'aux
        # nôtres).
        print(f"\n  Journal complet :\n  {LOG}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
