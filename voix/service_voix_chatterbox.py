# -*- coding: utf-8 -*-
"""LA VOIX D'ALICE — Chatterbox multilingue sur carte graphique.

Service ALTERNATIF à `service_voix_piper.py`. Les deux parlent le MÊME
protocole et écoutent le MÊME port : on choisit la voix d'Alice en lançant
l'un ou l'autre, jamais les deux. Rien d'autre dans le projet ne change.

┌──────────────────────────────────────────────────────────────────────────┐
│                        Piper (processeur)     Chatterbox (carte)         │
│  facteur temps réel      0,025x                 0,94x                    │
│  mémoire vidéo           0 Go                   3,4 Gio                  │
│  chargement              1,25 s                 ~15 s                    │
│  licence                 MIT                    MIT                      │
│  voix                    corpus SIWIS           clonée (CML-TTS, CC BY)  │
└──────────────────────────────────────────────────────────────────────────┘
Mesures du 05/08/2026 : 8 essais après 2 rodages, médiane 0,94x (0,89-0,96).
Le bruit de mesure sur cette machine est de ±15 % : ne jamais conclure d'un
écart plus petit que ça.

POURQUOI UN BLOC PAR PHRASE — et pas un seul bloc comme Piper.
À 0,94x, fabriquer toute la réponse avant de parler ferait attendre Alice
aussi longtemps qu'elle parlera. `parler()` dans `boucle_alice.py` joue déjà
chaque bloc dès son arrivée : en émettant une phrase par bloc, l'utilisateur
entend la première après ~1 s au lieu de ~8 s.

Et comme on fabrique PLUS VITE qu'on ne parle (0,94x < 1), l'avance grandit
à chaque phrase au lieu de fondre : pas de blanc entre les phrases. C'est
toute la différence avec XTTS, qui était à 0,44x mais avec 5,1 Go de VRAM.

CE QUI EST REPRIS TEL QUEL DE PIPER, et pourquoi :
  • le protocole : port 8181, POST /parler, blocs PCM 16 bits 24000 Hz
    préfixés de leur longueur (>I), bloc vide = fin. La boucle et
    l'interface n'ont RIEN à changer d'un moteur à l'autre.
  • la PREMIÈRE phrase est fabriquée AVANT l'en-tête 200 : un échec rend un
    vrai 500 au lieu d'un « succès vide » qui laisserait Alice muette sans
    trace d'erreur (défaut trouvé à l'audit du 19/07).
  • le rodage au démarrage : la 1re synthèse d'un processus est plus lente.
    On la paie ici, pas sur la 1re réplique.

PAS DE RÉÉCHANTILLONNAGE : Chatterbox sort déjà en 24000 Hz, exactement ce
qu'attend la boucle. On vérifie quand même au démarrage plutôt que de le
supposer.
"""
import copy
import json
import os
import struct
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# MIOpen (la bibliothèque de calcul AMD) écrit des centaines d'avertissements
# « IsEnoughWorkspace » à chaque synthèse. Ils sont sans conséquence, mais si
# la sortie standard est un tuyau que personne ne vide, elle se remplit et le
# service se BLOQUE en écrivant dedans. Constaté le 05/08/2026 : le service
# restait muet après « modèle chargé ». À couper avant d'importer torch.
os.environ.setdefault("MIOPEN_LOG_LEVEL", "1")      # 1 = erreurs seulement
# MIOpen regle ses noyaux a CHAQUE nouvelle forme de phrase : mesure le 06/08,
# une replique de forme inedite payait jusqu'a 5 s de recherche de noyaux au
# premier passage. FIND_MODE=FAST prend un noyau correct tout de suite au lieu
# de chercher le meilleur — la perte de vitesse brute est negligeable devant
# ces pointes de latence en pleine conversation.
os.environ.setdefault("MIOPEN_FIND_MODE", "FAST")
# 🔴 LE CACHE DE NOYAUX A SON PROPRE DOSSIER — et c'est CE reglage, pas
# FIND_MODE, qui rend la voix a sa vitesse. Mesure du 07/08/2026, meme banc,
# meme machine, meme code, quatre passages en dix minutes :
#     chemin par defaut ................ 0,72 a 1,78x   ECART A L'ETALON
#     MIOPEN_USER_DB_PATH dedie ........ 0,58 a 0,73x   SONDE FIDELE
#     retour au defaut ................. 0,72 a 1,78x   le defaut revient
#     FIND_MODE=FAST seul .............. 0,82 a 1,61x   AUCUN effet
# Au-dessus de 1x elle parle plus vite qu'elle ne fabrique : ce sont les TROUS
# dans sa voix, et ils venaient de la.
#
# CE QUI S'ETAIT PASSE : l'ancien service XTTS posait les DEUX variables (voir
# CLAUDE.md 9 decies, 18/07). Chatterbox a herite de FIND_MODE et perdu le
# chemin du cache. Le dossier par defaut (~\.miopen\db) contient un fichier
# texte de 910 Ko qui n'etait plus ecrit depuis le 06/08 a 00h57 : MIOpen le
# relisait sans jamais pouvoir le tenir a jour, et repayait sa recherche.
# ⚠️ Un dossier VIDE suffit a retrouver l'etalon — ce n'est donc pas un cache
# qui se rechauffe, c'est le chemin lui-meme. Verifie par un temoin, parce que
# « le cache s'est rechauffe » etait l'explication la plus tentante et la plus
# fausse.
#
# ═══════════════════════════════════════════════════════════════════════════
# 🔴 08/08/2026 — POURQUOI CE CORRECTIF SAUTAIT A CHAQUE FOIS, ET CE QUI LE
#    FIXE POUR DE BON. Constat de l'utilisateur : « un correctif qui a deja ete
#    fait 3 fois mais qui saute a chaque fois ».
#
#    MIOPEN TIENT **DEUX** BASES, PAS UNE, ET ELLES ONT DEUX VARIABLES :
#      MIOPEN_USER_DB_PATH     -> `*.ufdb.txt`, les CHOIX de noyaux mesures
#      MIOPEN_CUSTOM_CACHE_DIR -> `*.ukdb`, les noyaux COMPILES
#    On ne posait que la premiere. Les noyaux compiles continuaient donc de
#    partir a l'emplacement par defaut, pendant que les choix allaient dans le
#    dossier dedie. Le cache etait coupe en deux, et une moitie se perdait a
#    chaque changement de moteur de voix.
#
#    ETAT TROUVE LE 08/08 AU MATIN, apres une vraie session de l'utilisateur :
#      voix\cache_miopen_chatterbox\  -> 1 fichier : un VERROU VIDE de 0 octet
#      ~\.miopen\db\...ufdb.txt       -> 910 Ko, plus ecrits depuis le 06/08
#      voix\.cache_miopen\...ukdb     ->  90 Ko, dates du 31/07
#    Alice cherchait donc sa connaissance dans un dossier vide. Elle
#    recalculait tout, a chaque phrase, sans jamais rien retenir.
#
#    CE QUE CA COUTAIT, MESURE SUR SA SESSION DE 06:27 :
#      242 caracteres -> 14,1 s d'audio en 21,6 s   facteur 1,535x
#      109 caracteres ->  5,3 s d'audio en 16,1 s   facteur 3,050x
#      premier son a 11,6 s et 11,9 s   (etalon : 0,9 a 1,9 s)
#    Et le pire n'est pas la voix : pendant qu'elle recalculait, elle tenait
#    LA CARTE. Le cerveau, sur la meme carte, attendait derriere — 8,5 s
#    CONSTANTES a chaque lecture de prompt, quelle que soit sa longueur.
#    Correlation relevee 5 fois sur 5 dans `llamacpp_cerveau.txt`. C'est ce
#    que l'utilisateur ressentait comme « Alice est devenue invivable, 30 s ».
#
#    LES DEUX BASES VIVENT DESORMAIS DANS LE MEME DOSSIER, a cote du service.
#    Elles suivent le code, elles se sauvegardent ensemble, et une prochaine
#    voix qui heriterait de ce fichier heritera des deux.
# ═══════════════════════════════════════════════════════════════════════════
_CACHE_MIOPEN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cache_miopen_chatterbox")
os.environ.setdefault("MIOPEN_USER_DB_PATH", _CACHE_MIOPEN)
os.environ.setdefault("MIOPEN_CUSTOM_CACHE_DIR", _CACHE_MIOPEN)
os.makedirs(os.environ["MIOPEN_USER_DB_PATH"], exist_ok=True)
os.makedirs(os.environ["MIOPEN_CUSTOM_CACHE_DIR"], exist_ok=True)

import numpy as np
import torch

PROJET = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PORT = 8181
SR_SORTIE = 24000                 # ce qu'attend la boucle, ne pas changer seul

# 🔴 LE DÉCOUPAGE EN PHRASES N'EST PLUS ICI — 08/08/2026. Il vit dans
# `controle_alice\commun\decoupe_voix.py`, et le LECTEUR de l'interface lit le
# MÊME fichier : c'est ce qui lui permet de savoir quel texte correspond au
# bloc audio qu'il joue, donc d'afficher le bon sous-titre au bon moment. Deux
# copies de ces règles et le sous-titre du stream afficherait la phrase
# d'à côté, sans que rien ne le signale.
# ⚠️ ÉCHEC BRUYANT VOULU : si ce fichier manque, le service refuse de démarrer.
# Une voix qui découpe autrement que ce que croit le lecteur serait une panne
# muette, et ce projet a appris qu'elles coûtent plus cher (9 septendecies).
sys.path.insert(0, os.path.join(PROJET, "controle_alice"))
from commun.decoupe_voix import morceaux as decouper_en_phrases  # noqa: E402

# La voix : un extrait de référence que Chatterbox imite. Choisi à l'oreille
# par l'utilisateur le 05/08/2026 parmi 19 voix françaises (la n°17, 186 Hz).
# Corpus CML-TTS, licence CC BY 4.0 — usage commercial autorisé AVEC crédit.
REFERENCE = os.path.join(PROJET, "modeles", "voix", "chatterbox",
                         "reference_alice.wav")

# Étage onde distillé (meanflow) : transforme les jetons de parole en onde en
# 2 étapes sans double calcul, au lieu de 10 avec guidage. Dépôt officiel
# ResembleAI/chatterbox-turbo, licence MIT — provenance, révision et sha256
# dans s3gen_meanflow.provenance.txt à côté du fichier.
ETAGE_ONDE = os.path.join(PROJET, "modeles", "voix", "chatterbox",
                          "s3gen_meanflow.safetensors")

# Réglages d'expressivité. l'utilisateur, 05/08/2026 : « chatterbox crée des
# variations pour simuler des émotions à des moments inutiles ». `exaggeration`
# EST ce mécanisme ; on le descend au minimum utilisable. En dessous de 0,20 la
# prononciation commence à se dégrader.
EXPRESSIVITE = 0.20
TEMPERATURE = 0.35
GUIDAGE = 0.70

LOG = os.path.join(PROJET, "logs",
                   f"service_voix_{datetime.now():%Y-%m-%d_%H%M}.txt")


def tracer(msg):
    ligne = f"[{datetime.now():%H:%M:%S.%f}"[:-4] + f"] {msg}"
    print(ligne, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(ligne + "\n")
    except Exception:
        pass


MODELE = None


def accelerer(modele):
    """Rend l'attention rapide à toutes les couches sauf celles qui sont espionnées.

    LE DÉFAUT CORRIGÉ — `alignment_stream_analyzer.py`, lignes 85-89 :

        if getattr(tfmr.config, '_attn_implementation', None) == 'sdpa':
            tfmr.config._attn_implementation = 'eager'

    Pour lire les poids d'attention de TROIS couches (9, 12 et 13), Chatterbox
    bascule TOUT le transformeur en attention naïve. Leur propre commentaire,
    deux lignes plus haut, dit qu'ils voulaient l'appliquer « à une seule couche
    pour ne pas trop ralentir » — le code fait l'inverse.

    `LlamaAttention.forward` choisit son noyau d'après `self.config`, la
    configuration portée par LA COUCHE. Il suffit donc de donner aux couches
    espionnées leur propre copie en `eager`, et de rendre `sdpa` au reste.

    MESURÉ le 05/08/2026, 4 tours alternés : 1,20x -> 0,97x, soit 20 % de moins.
    PROUVÉ sans effet sur le garde : mêmes 160 appels, mêmes 160 réceptions de
    poids, même état final, même durée audio à la milliseconde.
    """
    a = getattr(modele.t3.patched_model, "alignment_stream_analyzer", None)
    if a is None:
        return "aucun analyseur : rien à faire"
    tfmr = modele.t3.tfmr
    if getattr(tfmr.config, "_attn_implementation", None) != "eager":
        return "attention déjà rapide : rien à faire"

    from chatterbox.models.t3.inference.alignment_stream_analyzer import (
        LLAMA_ALIGNED_HEADS)
    couches = sorted({i for i, _ in LLAMA_ALIGNED_HEADS})
    lente = copy.copy(tfmr.config)
    lente._attn_implementation = "eager"
    tfmr.config._attn_implementation = "sdpa"
    for c in couches:
        tfmr.layers[c].self_attn.config = lente
    return f"attention rapide partout sauf les couches {couches}"


#: Chronométrage PAR PHRASE, posé le 08/08/2026. Le journal ne mesurait que le
#: total d'une réplique ; impossible d'y voir un coût FIXE payé à chaque
#: morceau. Or c'est exactement ce que le banc montrait : 16 caractères en
#: 12,1 s (facteur 9,5x) contre 161 caractères en 6,4 s (facteur 0,76x) — le
#: facteur s'améliore quand le texte s'allonge, signature d'un coût fixe
#: amorti, jamais d'un débit lent. Mettre à "0" pour retrouver le silence.
CHRONO_PHRASE = os.environ.get("ALICE_CHRONO_PHRASE", "1") == "1"

#: ═══════════════════════════════════════════════════════════════════════════
#: 🔴 LE POULS — 08/08/2026. LA CAUSE DES « 12 SECONDES » QUE l'utilisateur SUBIT.
#:
#: MESURE, MEME TEXTE, MEME PROCESSUS, REPRODUCTIBLE :
#:     trois requetes collees   12,91 -> 2,87 -> 1,32 s
#:     apres  30 s de silence   12,60 s   puis, tout de suite apres : 2,82 s
#:     apres  60 s de silence   12,09 s   puis                        3,47 s
#:     apres 120 s de silence   11,94 s   puis                        1,30 s
#: TRENTE SECONDES DE SILENCE SUFFISENT a recreer le cout complet. Ce n'est
#: donc ni le texte, ni la longueur, ni la forme des donnees, ni le fil HTTP,
#: ni le cache disque : la carte redescend en veille des qu'Alice se tait, et
#: sa phrase suivante paie le reveil.
#:
#: ⚠️ ET C'EST PIRE QU'UN DEFAUT DE BANC : dans une VRAIE conversation il y a
#: TOUJOURS un silence avant qu'elle parle — le temps que l'utilisateur parle, que
#: l'oreille transcrive et que le cerveau redige. Elle payait donc ces 12 s a
#: presque CHAQUE replique. C'est ce qu'il decrivait par « avant elle avait
#: besoin de 2 s max, la on est a 30 s ».
#:
#: LE REMEDE : une micro-synthese jetee quand elle s'est tue depuis un moment.
#: Deux secondes de carte toutes les 15 s, contre 11 s rendues a chaque
#: replique. Mettre ALICE_POULS_VOIX=0 pour l'eteindre et retrouver l'ancien
#: comportement.
#: ═══════════════════════════════════════════════════════════════════════════
POULS_ACTIF = os.environ.get("ALICE_POULS_VOIX", "1") == "1"
#: Sous ce silence-la, on ne fait rien : elle vient de parler, la carte est
#: chaude.
#: ⚠️ 15 s D'ABORD ESSAYE, PUIS RESSERRE A 7 s — mesure du 08/08. A 15 s, le
#: POULS LUI-MEME retombait a froid une fois sur deux (13,9 s puis 0,6 s en
#: alternance dans le journal) : la carte redescend en moins de quinze
#: secondes. Le seuil doit etre plus court que ce refroidissement, sinon on
#: paie le reveil pour rien.
SILENCE_AVANT_POULS_S = 7.0
#: Le texte le plus court qui produise du son. Il n'est jamais entendu.
TEXTE_DU_POULS = "Oui."
#: Horodatage de la derniere synthese REELLE ou de pouls. Ecrit par `fabriquer`.
DERNIERE_SYNTHESE = 0.0
#: Le pouls laisse quelques secondes de marge a une vraie demande avant de
#: tenter un nouveau reveil de la carte.
GARDE_POULS_APRES_DEMANDE_S = 6.0
DERNIERE_DEMANDE_HTTP = 0.0
SERRURE_GENERATION = threading.Lock()


def _fabriquer_sans_verrou(phrase):
    """Rend du PCM 16 bits à 24000 Hz, mono, pour UNE phrase."""
    global DERNIERE_SYNTHESE
    DERNIERE_SYNTHESE = time.time()
    _t0 = time.time()
    with torch.inference_mode():
        onde = MODELE.generate(
            phrase,
            language_id="fr",
            exaggeration=EXPRESSIVITE,
            temperature=TEMPERATURE,
            cfg_weight=GUIDAGE,
        )
    _t_generation = time.time() - _t0
    _t1 = time.time()
    x = onde.squeeze().detach().cpu().numpy().astype("float32")
    if not x.size:
        raise RuntimeError("Chatterbox n'a rien produit")
    crete = float(np.abs(x).max())
    if crete > 0:
        x = x / crete * 0.89          # même niveau d'une phrase à l'autre
    pcm = np.clip(x * 32767, -32768, 32767).astype("<i2")
    if CHRONO_PHRASE:
        secondes = len(pcm) / SR_SORTIE
        apres = time.time() - _t1
        tracer(
            f"   [chrono] {len(phrase):3d} car -> {secondes:5.2f} s d'audio | "
            f"generation {_t_generation:5.2f} s | mise en forme {apres:4.2f} s | "
            f"facteur {(_t_generation + apres) / max(secondes, 0.01):5.2f}x"
        )
    return pcm


def fabriquer(phrase):
    with SERRURE_GENERATION:
        return _fabriquer_sans_verrou(phrase)


class Poignee(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"     # obligatoire pour envoyer en plusieurs fois

    def log_message(self, *a):
        pass                          # on tient notre propre journal

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "10")
        self.end_headers()
        self.wfile.write(b"voix prete")

    def _refuser(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        global DERNIERE_DEMANDE_HTTP
        n = int(self.headers.get("Content-Length", 0))
        demande_arrivee = time.time()
        DERNIERE_DEMANDE_HTTP = demande_arrivee
        try:
            texte = json.loads(self.rfile.read(n).decode("utf-8")).get("texte", "").strip()
        except Exception as e:
            tracer(f"ERREUR lecture demande : {e}")
            return self._refuser(400)
        if not texte:
            return self._refuser(400)

        morceaux = decouper_en_phrases(texte)
        if not morceaux:
            return self._refuser(400)

        # La 1re phrase est fabriquée AVANT l'en-tête 200 : un échec doit
        # rendre un vrai 500, pas un flux vide (défaut de l'audit du 19/07).
        t0 = time.time()
        try:
            premier = fabriquer(morceaux[0])
        except Exception as e:
            tracer(f"ERREUR synthèse : {type(e).__name__}: {e}")
            return self._refuser(500)

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        duree, envoyes, t_premier = 0.0, 0, None
        try:
            for rang, phrase in enumerate(morceaux):
                pcm = premier if rang == 0 else fabriquer(phrase)
                brut = pcm.tobytes()
                # chunked : chaque bloc part dans son propre morceau HTTP, et
                # on vide le tampon tout de suite — sans ça le système garderait
                # tout jusqu'à la fin et le découpage ne servirait à rien.
                self.wfile.write(b"%X\r\n" % (4 + len(brut)))
                self.wfile.write(struct.pack(">I", len(brut)))
                self.wfile.write(brut)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                if t_premier is None:
                    t_premier = time.time() - t0
                duree += len(pcm) / SR_SORTIE
                envoyes += 1
            self.wfile.write(b"4\r\n" + struct.pack(">I", 0) + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")     # fin du corps chunked
            self.wfile.flush()
        except Exception:
            tracer("le client a coupé pendant la lecture")
            return

        fab = time.time() - t0
        tracer(f"{len(texte)} caractères -> {envoyes} phrase(s), {duree:.2f} s d'audio "
               f"en {fab:.2f} s (facteur {fab/duree:.3f}x) — 1er son après {t_premier:.2f} s")


def main():
    global MODELE
    if not torch.cuda.is_available():
        tracer("AUCUNE CARTE GRAPHIQUE VUE — refus de démarrer sur processeur")
        sys.exit(1)
    if not os.path.exists(REFERENCE):
        tracer(f"VOIX DE RÉFÉRENCE INTROUVABLE : {REFERENCE}")
        sys.exit(1)
    if not os.path.exists(ETAGE_ONDE):
        tracer(f"ÉTAGE ONDE DISTILLÉ INTROUVABLE : {ETAGE_ONDE}")
        sys.exit(1)

    # Le choix de carte se fait par IDENTITÉ PHYSIQUE, plus jamais par mémoire
    # libre : mem_get_info ment sous Windows (mesuré le 06/08 — 0,15 Gio
    # annoncés occupés pendant que le cerveau tenait 5 Go). Cartographie
    # mesurée le même jour : bus PCI 8 = carte SANS écran (Gigabyte, celle
    # d'Alice) ; bus PCI 3 = carte des écrans (Sapphire, celle du jeu).
    bus_voulu = int(os.environ.get("ALICE_VOIX_PCI_BUS", "8"))
    carte = next((i for i in range(torch.cuda.device_count())
                  if getattr(torch.cuda.get_device_properties(i),
                             "pci_bus_id", -1) == bus_voulu), None)
    if carte is None:
        carte = 0
        tracer(f"ATTENTION : aucune carte au bus PCI {bus_voulu} — repli sur "
               "cuda:0. Vérifier ALICE_VOIX_PCI_BUS si le matériel a changé.")
    torch.cuda.set_device(carte)
    p = torch.cuda.get_device_properties(carte)
    nom = f"{p.name}, bus PCI {getattr(p, 'pci_bus_id', '?')}"

    from chatterbox.mtl_tts import ChatterboxMultilingualTTS
    t = time.time()
    # ⭐ MOTEUR V3 depuis le 06/08/2026 — piste trouvee par l'audit de Codex,
    # mesuree au banc calibre : 0,69x sur phrase longue (V2 : 1,2 a 1,8x) —
    # pour la premiere fois la voix fabrique PLUS VITE qu'elle ne parle.
    # Hauteur verifiee identique a la reference (194 Hz contre 192), meme
    # echantillonnage, qualite validee a l'oreille par l'utilisateur.
    # RETOUR ARRIERE : retirer t3_model="v3" et reinstaller la version notee
    # dans version_chatterbox_avant_v3.txt.
    MODELE = ChatterboxMultilingualTTS.from_pretrained(
        device=f"cuda:{carte}", t3_model="v3")
    if MODELE.sr != SR_SORTIE:
        tracer(f"ÉCHANTILLONNAGE INATTENDU : {MODELE.sr} Hz au lieu de {SR_SORTIE}")
        sys.exit(1)
    MODELE.prepare_conditionals(REFERENCE, exaggeration=EXPRESSIVITE)
    tracer(f"modèle chargé en {time.time()-t:.2f} s sur cuda:{carte} ({nom})")

    # ⭐ ÉTAGE ONDE DISTILLÉ depuis le 06/08/2026 au soir. Le S3Gen d'origine
    # redigère les 10 s de la voix de référence à CHAQUE bloc : ~1,1 s de coût
    # quasi fixe mesuré au banc apparié, même pour un bloc minuscule — c'était
    # LE plancher du premier son. La version distillée officielle fait le même
    # travail en ~0,3 s. Mêmes jetons d'entrée, même voix clonée ; validé à
    # l'oreille par l'utilisateur le 06/08 (trois paires, aucune différence
    # entendue), ressemblance et hauteur mesurées équivalentes. La condition
    # T3 est préparée AVANT la pose, par le tokeniseur d'origine : c'est
    # exactement la recette du banc validé.
    # RETOUR ARRIÈRE : supprimer d'ici jusqu'à « fin étage distillé ».
    from chatterbox.models.s3gen import S3Gen
    from chatterbox.models.s3gen import flow_matching as _fm
    from safetensors.torch import load_file
    import librosa
    # Le chemin distillé écrit une ligne et une barre de progression à CHAQUE
    # synthèse ; un tuyau que personne ne vide finit par bloquer le service
    # (leçon MIOpen du 05/08). On coupe les deux à la source.
    _fm.tqdm = lambda iterable, **_k: iterable
    _fm.print = lambda *_a, **_k: None
    t = time.time()
    etage = S3Gen(meanflow=True)
    etage.load_state_dict(load_file(ETAGE_ONDE), strict=True)
    etage.to(f"cuda:{carte}").eval()
    onde_ref, _ = librosa.load(REFERENCE, sr=SR_SORTIE)
    MODELE.conds.gen = etage.embed_ref(
        onde_ref[:MODELE.DEC_COND_LEN], SR_SORTIE, device=f"cuda:{carte}")
    ancien = MODELE.s3gen
    MODELE.s3gen = etage
    del ancien
    torch.cuda.empty_cache()
    tracer(f"étage onde distillé posé en {time.time()-t:.2f} s "
           "(2 étapes au lieu de 10)")
    # — fin étage distillé —

    t = time.time()
    # ═══════════════════════════════════════════════════════════════════════
    # 🔴 LE RODAGE COUVRE TOUT L'EVENTAIL DES LONGUEURS — 08/08/2026.
    #
    # Il ne faisait qu'UNE synthèse, de 24 caractères. Or ROCm compile ses
    # noyaux PAR FORME DE DONNÉES, et cette compilation ne survit pas à
    # l'arrêt du processus. Chaque longueur jamais rencontrée depuis le
    # démarrage payait donc sa compilation EN PLEINE CONVERSATION.
    #
    # MESURE DU 08/08 (même texte court, service fraîchement démarré) :
    #     essai 1 : 11,20 s      essai 2 : 2,47 s      essai 3 : 2,59 s
    # puis, une fois toutes les formes vues : 0,89 s — l'étalon.
    # Et après redémarrage, les 11 s reviennent : ce n'est pas un cache
    # disque, c'est la mémoire du processus. Le seul remède est de payer ces
    # compilations AU DÉMARRAGE, une fois, pendant que l'utilisateur attend déjà.
    #
    # Les longueurs sont prises sur ses vraies répliques : le banc
    # d'endurance produit des phrases de 4 à 138 caractères. On borne
    # l'éventail plutôt que de le deviner.
    #
    # ⚠️ CE N'EST PAS GRATUIT : le démarrage s'allonge. C'est l'échange
    # voulu — quelques secondes de plus au lancement, une fois, contre une
    # réplique courte à 11 s tombant au hasard en plein direct.
    # ═══════════════════════════════════════════════════════════════════════
    for _amorce in (
        "Oui.",                                             #   4 car
        "Salut l'utilisateur !",                                 #  16 car
        "Bien. Maintenant, parle.",                         #  24 car
        "Tu joues encore a ce jeu la, serieusement ?",       #  43 car
        "J'ai bien avance pendant que tu dormais, et je me "
        "suis trompee deux fois sur le portail dore.",       #  93 car
        "Bon, alors voila mon plan pour ce soir : on prend "
        "le portail dore, on evite le marchand, et on garde "
        "les reliques pour la toute fin de la partie.",      # 143 car
    ):
        fabriquer(_amorce)
    # ⚠️ ACCÉLÉRATION DÉBRANCHÉE le 06/08 : en production, l'attention ciblée
    # a fait planter DEUX synthèses (« stack expects each tensor to be equal
    # size », journal de 02:47) — Alice muette sur ces répliques. Les ~20 %
    # de gain ne valent pas une voix qui rate. `accelerer` reste défini pour
    # le jour où la cause exacte sera comprise et éprouvée sur CE chemin.
    tracer(f"rodage fait en {time.time()-t:.2f} s "
           f"({torch.cuda.memory_allocated(carte)/1024**3:.2f} Gio de mémoire vidéo)")

    # ═══════════════════════════════════════════════════════════════════════
    # 🔴 LE DERNIER RODAGE PASSE PAR LE TUYAU, PAS PAR LA FONCTION — 08/08.
    #
    # Le rodage ci-dessus appelle `fabriquer()` dans le fil PRINCIPAL. La
    # vraie requête, elle, arrive dans un fil HTTP tout neuf. Mesure du
    # 08/08 : la première requête après démarrage coûte 11,8 s MEME quand on
    # vient de roder le texte EXACTEMENT identique — et les suivantes 2 à
    # 3 s. Ce n'est donc pas la forme de la phrase qui se paie, c'est le
    # premier passage de ROCm dans un fil autre que le principal.
    #
    # On paie donc ce premier passage ici, en s'envoyant une requête à
    # soi-même, pendant que l'utilisateur attend déjà le démarrage. Alice ne le
    # paiera plus sur sa première vraie phrase.
    #
    # ⚠️ NE JAMAIS FAIRE ECHOUER LE DEMARRAGE POUR CA : si l'auto-requête
    # rate, le service doit servir quand même. C'est un confort de latence,
    # pas un organe.
    # ═══════════════════════════════════════════════════════════════════════
    serveur = ThreadingHTTPServer(("127.0.0.1", PORT), Poignee)

    def _roder_par_le_tuyau():
        import urllib.request
        try:
            t0 = time.time()
            corps = json.dumps({"texte": "Oui, je suis la."}).encode("utf-8")
            requete = urllib.request.Request(
                f"http://127.0.0.1:{PORT}/parler", data=corps,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(requete, timeout=120) as reponse:
                while True:
                    entete = reponse.read(4)
                    if len(entete) < 4:
                        break
                    taille = struct.unpack("<I", entete)[0]
                    if taille == 0:
                        break
                    reponse.read(taille)
            tracer(f"rodage par le tuyau fait en {time.time()-t0:.2f} s")
        except Exception as souci:  # noqa: BLE001 - un confort ne casse rien
            tracer(f"rodage par le tuyau impossible ({souci}) — sans gravité")

    threading.Thread(target=_roder_par_le_tuyau, daemon=True,
                     name="rodage-tuyau").start()

    def _battre_le_pouls():
        """Garde la carte eveillee pendant les silences. Voir POULS_ACTIF."""
        while True:
            time.sleep(1.5)
            try:
                repos = time.time() - DERNIERE_SYNTHESE
                if repos < SILENCE_AVANT_POULS_S:
                    continue
                battement_demande = time.time()
                if battement_demande - DERNIERE_DEMANDE_HTTP < GARDE_POULS_APRES_DEMANDE_S:
                    continue
                if not SERRURE_GENERATION.acquire(blocking=False):
                    continue
                try:
                    if DERNIERE_DEMANDE_HTTP > battement_demande:
                        continue
                    _fabriquer_sans_verrou(TEXTE_DU_POULS)     # le son part a la poubelle
                finally:
                    SERRURE_GENERATION.release()
            except Exception as souci:  # noqa: BLE001 - jamais fatal
                tracer(f"pouls impossible ({souci}) — sans gravité")
                time.sleep(30.0)

    if POULS_ACTIF:
        threading.Thread(target=_battre_le_pouls, daemon=True,
                         name="pouls-voix").start()
        tracer(f"pouls actif : une micro-synthèse après {SILENCE_AVANT_POULS_S:.0f} s "
               "de silence (mesure du 08/08 : sans lui, 12 s sur sa 1re phrase)")

    tracer(f"voix prête (carte {carte}) — service sur le port {PORT}")
    serveur.serve_forever()


if __name__ == "__main__":
    main()
