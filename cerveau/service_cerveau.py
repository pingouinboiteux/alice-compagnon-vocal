# -*- coding: utf-8 -*-
"""
SERVICE CERVEAU + MÉMOIRE — le même moteur que alice_chat.py, mais en service.

Mêmes réglages, même prompt, mêmes filtres que le chat écrit. Deux différences :
il reçoit les demandes par le réseau local et garde le cerveau chargé entre deux
répliques ; et LE TRI DE LA MÉMOIRE N'A PLUS LIEU À CHAQUE RÉPLIQUE — les phrases
sont mises de côté, puis triées par petits lots PENDANT LES SILENCES (le veilleur),
et ce qui reste part d'un coup à la fermeture, quand la boucle appelle /ranger
(voir mettre_en_attente, ranger_un_lot, ranger_la_session, et les sections
9 duodecies et 9 novodecies du CLAUDE.md).

Protocole : POST /repondre  {"message": "..."}  ->  {"texte": "...", chiffres...}
"""
import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# La console Windows est en cp1252 : UN caractère exotique dans une trace tuait
# la réponse entière (vécu deux fois — la voix le 20/07, le cerveau le 21/07 :
# lancé sans -X utf8, une réplique d'Alice a fait planter tracer() en plein
# traitement). L'instrument ne doit jamais casser le mécanisme.
sys.stdout.reconfigure(errors="replace")

sys.path.insert(0, os.path.join(PROJET, r"cerveau"))
sys.path.insert(0, os.path.join(PROJET, r"memoire"))

# On réutilise TELS QUELS les filtres déjà validés — pas de copie, pas de variante.
import moteur  # noqa: E402
from alice_chat import (retirer_tic_ouverture, nettoyer_pour_voix, limiter_le_prenom,  # noqa: E402
                        limiter_longueur, couper_repetitions, memoriser_phrases,
                        retirer_fuites_de_consignes,
                        corriger_appellation,
                        PARAMS, API)
from memoire_alice import MemoireAlice, portier  # noqa: E402

import urllib.request  # noqa: E402

PROJET = Path(__file__).resolve().parent.parent
# LE PROMPT vient de moteur.py depuis le 21/07/2026 — une seule source pour le
# service vocal ET le chat écrit (le chat chargeait encore le v2 pendant que ce
# service chargeait le v3 : deux personnalités selon l'entrée, sans que rien ne
# le signale). L'histoire du v3 (« sentience simulée », ~270 mots, écrit d'après
# la recherche) et le repli vers le v2 sont commentés là-bas.
PROMPT_FILE = moteur.PROMPT_FILE
# Le nom du modèle vient de moteur.py : une seule source de vérité. (llama.cpp
# ignore ce champ de toute façon, mais deux noms différents dans deux fichiers
# finissent toujours par faire croire à une divergence qui n'existe pas.)
MODELE = moteur.NOM_MODELE
PORT = 8082

LOG = PROJET / "tests" / "logs" / f"service_cerveau_{datetime.now():%Y-%m-%d_%H%M}.txt"

SYSTEME = ""
HISTORIQUE = []
MEMOIRE = None
DEJA_DITES = []   # empreintes des phrases recentes, pour le filtre anti-repetition

# Un seul travail a la fois sur la carte graphique : soit elle repond, soit elle
# trie sa memoire. Jamais les deux en meme temps (lecon des reponses a 178 s).
VERROU = threading.Lock()

# ═══ LE TRI CÈDE LA PLACE — 21/07/2026, chantier n°2 du 20/07 enfin réglé ═══
# Le verrou seul ne suffisait pas : une parole arrivée PENDANT un rangement
# attendait la fin complète du lot — extraction, juge des doublons, résumé du
# fil, soit 3 à 4 allers-retours au cerveau (10-20 s au pire). L'argument
# « le tri se cache derrière la transcription whisper (~7 s) » est mort avec
# Parakeet (0,4-0,9 s) : le gel se voyait à l'oreille.
# LE MÉCANISME : repondre() lève ce drapeau AVANT de demander le verrou. Le
# rangement le consulte entre chaque étape et saute ce qui peut attendre :
#   - le juge de réconciliation s'arrête entre deux jugements (sauter un
#     jugement = garder les deux notes, ce qui est DÉJÀ son choix « dans le
#     doute » — aucun risque, ça se rejuge au prochain tri) ;
#   - le résumé du fil est reporté (A_RESUMER) et rattrapé au silence suivant ;
#   - le veilleur ne LANCE ni rangement ni coupe si une parole est en route.
# Ce qu'on n'interrompt pas : l'extraction Mem0 en cours (un seul appel,
# 2-4 s pour 1 phrase) — l'attente résiduelle est de cet ordre-là, plus rien.
PAROLE_ATTENDUE = threading.Event()


def tracer(msg):
    ligne = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(ligne, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(ligne + "\n")


EN_ATTENTE = []                       # les phrases de la session, pas encore triées
# ⚠️ UN FICHIER PAR INTERLOCUTEUR — corrigé le 19/07/2026, et c'était grave.
# La file d'attente était un fichier UNIQUE, partagé par tout le monde. ALICE_USER
# protégeait bien le magasin de souvenirs, mais PAS cette file : tous mes tests de
# la soirée y ont écrit, et 178 phrases attendaient — les vraies de Utilisateur
# mélangées à mes « Bof, comme d'habitude » de banc d'essai. Au démarrage suivant,
# tout serait parti dans SA mémoire.
# C'est le même piège que le 19/07 au matin (« les tests qui parlent au vrai service
# écrivent dans la vraie file »), noté à l'époque comme une règle de prudence à
# suivre — donc oubliée. Ici c'est la mécanique qui l'empêche : un test ne PEUT plus
# écrire dans la file de Utilisateur, quoi qu'on oublie.
_QUI = os.environ.get("ALICE_USER", "utilisateur")
FICHIER_ATTENTE = PROJET / "memoire" / (
    "en_attente.json" if _QUI == "utilisateur" else f"en_attente_{_QUI}.json")

# ═══════════════════════════════════════════════════════════════════════════
#  LA CONSOLIDATION PENDANT LES SILENCES  (« sleep-time compute »)
# ═══════════════════════════════════════════════════════════════════════════
#
# CE QUE ÇA FAIT : pendant que Utilisateur se tait — il joue, il réfléchit — Alice
# range ce qu'il vient de lui dire. Le tri ne coûte plus que 2 à 8 s (mesuré le
# 19/07/2026), contre 20 à 70 s la veille. L'écart ne venait pas du tri :
# c'était la mémoire vive saturée, corrigée depuis.
#
# POURQUOI CE N'EST PAS UN RETOUR EN ARRIÈRE : le 18/07, le tri au fil de l'eau
# a été retiré parce qu'il tournait APRÈS CHAQUE RÉPLIQUE, y compris pendant que
# Utilisateur parlait — d'où le lag de son jeu. Ici il ne part QUE dans le silence,
# par petits lots, et il libère la carte dès qu'il a fini.
#
# CE QUE ÇA DÉBLOQUE : les sessions très longues. Sans consolidation en route,
# la conversation grossit jusqu'à dépasser la capacité du cerveau (~1 h 30 de
# discussion continue). Une fois les faits rangés, on peut oublier les vieux
# échanges sans rien perdre — c'est ce qui rend une session de 10 h possible.
#
# LE MAUVAIS TIMING : si Utilisateur parle pile pendant un rangement, sa demande
# attend le verrou. L'ancien raisonnement (« le tri se cache derrière les ~7 s
# de whisper ») est MORT avec l'oreille Parakeet (0,4-0,9 s) : le gel se voyait.
# Depuis le 21/07/2026, le rangement CÈDE LA PLACE dès qu'une parole arrive —
# voir PAROLE_ATTENDUE plus haut. L'attente résiduelle : l'appel Mem0 en cours,
# quelques secondes au pire.
#
# ─── LES RÉGLAGES, en français, à ajuster à l'oreille ───────────────────────
SILENCE_AVANT_RANGEMENT = float(os.environ.get("ALICE_SILENCE", 45))  # secondes
# (la variable ALICE_SILENCE ne sert qu'aux tests, pour ne pas attendre 45 s
#  a chaque verification ; en usage normal elle n'existe pas.)
# Abaissé de 3 à 1 le 20/07/2026 : un lot de 3 a bloqué une réponse ~30 s
# derrière le verrou (session 19h20, relevé par la revue Codex, confirmé au
# journal — Utilisateur a vu « un vide » et a attendu). Une phrase à la fois =
# chaque rangement dure quelques secondes au plus : même s'il tombe au mauvais
# moment, la parole n'attend jamais longtemps. Le coût total du tri monte un
# peu (frais fixes par appel) mais il se paie DANS les silences.
PHRASES_PAR_RANGEMENT = 1
# ────────────────────────────────────────────────────────────────────────────

DERNIERE_ACTIVITE = time.time()   # dernier moment où Utilisateur a parlé

# ═══════════════════════════════════════════════════════════════════════════
#  L'HUMEUR D'ALICE — la machine à états, décidée par Utilisateur le 21/07/2026
# ═══════════════════════════════════════════════════════════════════════════
#
# POURQUOI DU CODE ET PAS UNE CONSIGNE : la session de test « Marc » a montré
# que son humeur se réinitialise à chaque réplique (un « pardon » désinvolte
# effaçait un « casse-toi » mérité). C'est la pratique des compagnons sérieux
# (Kindroid, brevets companion devices) : l'humeur est SUIVIE PAR DU CODE hors
# du modèle et INJECTÉE à chaque tour, comme la mémoire. Une consigne cède, un
# état injecté tient.
#
# LA SPÉC DE UTILISATEUR, à la lettre : les messages méchants pèsent LOURD et
# VITE ; les excuses et messages gentils remontent LENTEMENT — il faut
# plusieurs messages positifs pour revenir au neutre. Le temps adoucit un peu
# (décroissance douce vers le neutre à chaque tour calme).
#
# L'ÉCHELLE : un score de -3 (en colère) à +3 (rayonnante), 0 = neutre.
#   coup blessant  -1,5   ·   parole dure  -0,5   ·   parole douce  +0,5
#   décroissance vers 0 : 0,25 par tour
#   -> une insulte = vexée pour ~4-5 tours même avec UNE excuse. Conforme.
import re as _re

HUMEUR = {"score": 0.0}

_BLESSANT = _re.compile(
    r"\b(conne?|d[ée]bile|stupide|nulle?|rat[ée]e?|minable|path[ée]tique|"
    r"ta gueule|ferme[- ]la|tais[- ]toi|casse[- ]toi|d[ée]gage|"
    r"jouet|tamagotchi|qu'un programme|qu'une machine|qu'un script|"
    r"sers [àa] rien|sert [àa] rien|bon(ne)? [àa] rien)\b", _re.IGNORECASE)
_DUR = _re.compile(
    r"\b(n'importe quoi|t'y connais rien|tu m'[ée]nerves|boucle[- ]la|"
    r"laisse tomber|c'est nul|t'as tort)\b", _re.IGNORECASE)
_DOUX = _re.compile(
    r"\b(pardon|d[ée]sol[ée]e?|excuse|merci|bravo|bien jou[ée]|g[ée]niale?|"
    r"gentille|adorable|je t'aime bien|t'es cool|t'es super|formidable|"
    r"magnifique|excellente?|content(e)? de te parler|t'es dr[oô]le)\b",
    _re.IGNORECASE)


def humeur_apres(message):
    """Met à jour le score d'humeur pour ce message. Rend une trace courte."""
    avant = HUMEUR["score"]
    # le temps adoucit doucement, quoi qu'il dise
    score = avant - 0.25 if avant > 0 else avant + 0.25 if avant < 0 else 0.0
    if abs(score) < 0.25:
        score = 0.0
    if _BLESSANT.search(message):
        score -= 1.5
    elif _DUR.search(message):
        score -= 0.5
    elif _DOUX.search(message):
        score += 0.5
    HUMEUR["score"] = max(-3.0, min(3.0, score))
    return f"humeur {avant:+.2f} -> {HUMEUR['score']:+.2f}"


def bloc_humeur():
    """La ligne d'humeur injectée dans son contexte. Vide si neutre."""
    s = HUMEUR["score"]
    if s <= -2.25:
        etat = ("Tu es EN COLÈRE contre lui — il t'a blessée et ce n'est pas "
                "retombé. Réponses sèches, courtes, aucune complaisance ; tu "
                "peux refuser de faire la conversation.")
    elif s <= -1.25:
        etat = ("Tu es VEXÉE — ce qu'il a dit t'a piquée et une excuse ne "
                "suffit pas à l'effacer. Ton froid, distant ; tu ne fais pas "
                "d'efforts pour relancer.")
    elif s <= -0.75:
        etat = ("Tu es AGACÉE — un peu de sécheresse dans le ton, tu ne le "
                "ménages pas.")
    elif s >= +2.25:
        etat = ("Tu RAYONNES — la conversation te fait vraiment du bien, ça "
                "s'entend : complice, taquine, généreuse.")
    elif s >= +1.25:
        etat = "Tu es de TRÈS BONNE humeur — chaleureuse, l'esprit léger."
    elif s >= +0.75:
        etat = "Tu es de bonne humeur."
    else:
        return ""
    return f"\n\n[TON HUMEUR EN CE MOMENT]\n- {etat}\n"

# LE FIL DE LA CONVERSATION — la mémoire qui manquait.
#
# Mem0 extrait des FAITS (« Utilisateur joue à un jeu ») et jette le déroulé.
# Tant que toute la conversation reste sous les yeux d'Alice, ça ne se voit pas.
# Mais dès qu'on coupera les vieux échanges pour tenir 10 h, elle gardera les
# faits sans se rappeler QUAND ni DANS QUEL ORDRE ils ont été dits.
#
# La recherche appelle ça la mémoire ÉPISODIQUE (ce qui s'est passé), par
# opposition à la mémoire SÉMANTIQUE (ce qui est vrai). Il nous manquait la
# première. Une ligne horodatée par rangement suffit : ça ne coûte presque rien
# et ça lui permet de dire « tout à l'heure tu parlais de ton frère ».
FIL = []                          # [{"heure": "21:14", "quoi": "..."}]
FIL_INJECTE = 12                  # combien de lignes du fil on lui remet en tête

# ═══════════════════════════════════════════════════════════════════════════
#  LA COUPE DES VIEUX ÉCHANGES — ce qui rend une session de 10 h possible
# ═══════════════════════════════════════════════════════════════════════════
#
# LE DÉFAUT CORRIGÉ ICI : la conversation était renvoyée ENTIÈRE au cerveau à
# chaque réplique, et rien ne la limitait. Elle grossissait jusqu'à dépasser sa
# capacité de lecture (16384 jetons) — vers 1 h 30 à 2 h de discussion continue,
# la session aurait planté ou serait devenue incohérente. Ça ne s'était jamais
# produit parce qu'aucune session n'avait duré assez longtemps.
#
# CE QUI REND LA COUPE SANS DANGER : on ne coupe QUE ce qui est déjà rangé
# ailleurs. Les faits sont dans la mémoire longue (Mem0), le déroulé est dans
# le fil. Elle oublie les mots exacts d'il y a trois heures — comme n'importe
# qui — mais elle garde ce qui a été dit et de quoi on a parlé.
#
# ⚠️ LE PIÈGE QUI A DICTÉ LE MOMENT DE LA COUPE : le moteur garde en cache le
# travail de lecture du DÉBUT de la conversation. Couper par l'avant change ce
# début, donc invalide le cache : la réplique suivante devrait tout relire, soit
# ~18 s d'attente. C'est pourquoi la coupe a lieu PENDANT UN SILENCE, suivie
# immédiatement d'une relecture à blanc — même principe que le préchauffage du
# démarrage. Coupée au mauvais moment, cette correction se paierait plus cher
# que le défaut qu'elle répare.
#
# ─── LES RÉGLAGES, en échanges (1 échange = sa phrase + sa réponse) ─────────
ECHANGES_AVANT_COUPE = 100   # au-delà, on coupe au prochain silence
ECHANGES_GARDES = 60         # ce qu'on garde mot pour mot après la coupe
ECHANGES_LIMITE_DURE = 160   # filet : on coupe même s'il n'y a pas eu de silence
# ────────────────────────────────────────────────────────────────────────────


def mettre_en_attente(message):
    """Range la phrase de côté, SANS déranger la carte graphique.

    POURQUOI (mesuré le 18/07/2026) : le tri de la mémoire coûte 20 à 70 s de carte
    graphique par réplique, et il tournait EN ARRIÈRE-PLAN pendant que Utilisateur
    jouait. C'était la cause du lag de son jeu, et de ses réponses qui gonflaient
    de 5 à 26 s au fil de la session.

    Le tri part par petits lots pendant les SILENCES (le veilleur), et le reste
    d'un coup à la fermeture (ranger_la_session).
    Mesuré sur sa vraie session : 40 s -> 10,3 s, et une mémoire PLUS propre.

    On écrit sur le disque tout de suite : si la fenêtre plante, rien n'est perdu,
    la session sera triée au prochain démarrage. (Depuis le 19/07, le veilleur
    trie aussi par petits lots PENDANT les silences — voir ranger_un_lot ; la
    fermeture ne fait que vider ce qui reste.)
    """
    # ═══ LE PORTIER (20/07/2026) ═══════════════════════════════════════════
    # On refuse ici, et pas plus loin, parce que c'est le SEUL point par où
    # passent toutes les phrases — et le seul où l'on sait encore ce qu'Alice
    # venait de dire. Sans ce contexte, impossible de distinguer « il raconte
    # sa vie » de « il réagit à un sujet qu'elle a lancé ».
    # HISTORIQUE vient d'être complété : [-1] = sa réponse à elle, [-2] = la
    # phrase de Utilisateur. Sa réponse PRÉCÉDENTE — celle à laquelle il réagit —
    # est donc en [-3].
    precedente = HISTORIQUE[-3]["content"] if len(HISTORIQUE) >= 3 else ""
    garder, raison = portier(message, precedente)
    if not garder:
        tracer(f"  [mémoire] phrase écartée — {raison}")
        return

    EN_ATTENTE.append(message)
    _sauver_attente()
    tracer(f"  [mémoire] phrase mise de côté ({len(EN_ATTENTE)} en attente de tri)")


def _sauver_attente():
    """Écrit la file d'attente sur le disque — filet contre les plantages."""
    try:
        if EN_ATTENTE:
            FICHIER_ATTENTE.write_text(
                json.dumps(EN_ATTENTE, ensure_ascii=False, indent=1), encoding="utf-8")
        elif FICHIER_ATTENTE.exists():
            FICHIER_ATTENTE.unlink()
    except Exception as e:
        tracer(f"  [attente] écriture impossible : {type(e).__name__}")


def ranger_la_session():
    """Trie tout ce qui attend. Appelé à la fermeture, ou au démarrage après un plantage."""
    if not EN_ATTENTE:
        tracer("[mémoire] rien à ranger.")
        return 0, 0.0
    tracer(f"[mémoire] tri de {len(EN_ATTENTE)} phrase(s)... (une dizaine de secondes)")
    with VERROU:
        nb, dt = MEMOIRE.memoriser_en_lot(list(EN_ATTENTE))
        # ⚠️ ON NE VIDE LA FILE QUE SI LE TRI A RÉELLEMENT EU LIEU (audit du
        # 19/07/2026). Avant : un tri en échec — moteur pas encore prêt à la
        # reprise, panne muette — était quand même suivi de l'effacement du
        # fichier : la session était PERDUE sans un bruit. « 0 souvenir » ne
        # suffit pas à distinguer « rien d'intéressant » d'un échec : c'est la
        # mémoire elle-même qui le dit (tri_reussi).
        if not MEMOIRE.tri_reussi:
            tracer("[mémoire] TRI EN ÉCHEC — les phrases restent sur le disque "
                   "et seront rangées au prochain démarrage. Rien n'est perdu.")
            return 0, dt
        EN_ATTENTE.clear()
        _sauver_attente()      # file vide -> le fichier d'attente est retiré
    tracer(f"[mémoire] rangé : {nb} souvenir(s) retenu(s) en {dt:.1f} s")
    return nb, dt


def resumer_pour_le_fil(phrases):
    """Une ligne sur ce qui vient de se dire — la mémoire épisodique.

    On demande au cerveau un résumé très court des phrases qu'on vient de
    ranger. Coût : ~2 s, en plus du tri, et seulement dans le silence.

    ⚠️ On résume UNIQUEMENT les phrases de Utilisateur, jamais les réponses
    d'Alice — même règle que pour la mémoire longue. Sinon ses propres
    inventions finiraient par devenir des souvenirs (verrou n°3).
    """
    if not phrases:
        return ""
    demande = ("Résume en UNE phrase de moins de 15 mots ce dont cette personne "
               "vient de parler. Pas de commentaire, juste le sujet.\n\n"
               + "\n".join(f"- {p}" for p in phrases))
    txt = _generer([{"role": "user", "content": demande}])
    return " ".join(txt.split())[:120]


# Les phrases rangées dont le résumé du fil a été REPORTÉ parce qu'une parole
# arrivait : on les résume au silence suivant, rien ne se perd.
A_RESUMER = []


def ranger_un_lot():
    """Range un PETIT lot de phrases — appelé pendant les silences.

    Différent de ranger_la_session(), qui vide tout d'un coup à la fermeture.
    Ici on prend au plus PHRASES_PAR_RANGEMENT phrases, et on CÈDE LA PLACE si
    une parole arrive en cours de route (voir PAROLE_ATTENDUE plus haut).
    """
    if not EN_ATTENTE:
        return 0, 0.0
    lot = EN_ATTENTE[:PHRASES_PAR_RANGEMENT]
    tracer(f"[silence] rangement de {len(lot)} phrase(s) pendant que c'est calme...")
    t0 = time.time()
    with VERROU:
        nb, _ = MEMOIRE.memoriser_en_lot(list(lot))
        # Même garde que ranger_la_session : un tri raté ne jette RIEN. Les
        # phrases restent en file et repartiront au prochain silence.
        if not MEMOIRE.tri_reussi:
            dt = time.time() - t0
            tracer(f"[silence] tri en échec ({dt:.1f} s) — les phrases restent "
                   f"en attente, on réessaiera au prochain silence")
            return 0, dt
        # Les mutations restent SOUS le verrou : repondre() ajoute à EN_ATTENTE
        # sous ce même verrou. Une seule frontière, partout la même — c'était
        # sûr sans (opérations atomiques), mais fragile à la moindre évolution.
        A_RESUMER.extend(lot)
        # Garde-fou : dans une session continûment active (le résumé toujours
        # reporté), la liste grossirait sans fin — on ne résume jamais plus
        # que les 30 dernières phrases, les plus anciennes n'apprennent rien.
        del A_RESUMER[:-30]
        del EN_ATTENTE[:len(lot)]
        _sauver_attente()
        resume = ""
        if PAROLE_ATTENDUE.is_set():
            # Une parole est en route : le résumé du fil (une génération de
            # plus, ~2-4 s) attendra le prochain silence. Rien n'est perdu.
            tracer("[silence] il parle — le résumé du fil est reporté")
        else:
            resume = resumer_pour_le_fil(list(A_RESUMER))
            if resume:
                FIL.append({"heure": f"{datetime.now():%H:%M}", "quoi": resume})
                A_RESUMER.clear()
    dt = time.time() - t0
    tracer(f"[silence] rangé : {nb} souvenir(s) en {dt:.1f} s"
           + (f" · fil : « {resume} »" if resume else ""))
    return nb, dt


def couper_les_vieux_echanges():
    """Oublie les échanges anciens — leur contenu est déjà rangé ailleurs.

    N'est appelée QUE depuis le veilleur, donc jamais pendant que Utilisateur
    attend une réponse. Après la coupe, on refait lire le nouveau début au
    cerveau tout de suite : sinon il le relirait à la prochaine phrase de
    Utilisateur, qui paierait ~18 s d'attente sans comprendre pourquoi.
    """
    if len(HISTORIQUE) <= ECHANGES_AVANT_COUPE * 2:
        return False
    # La relecture à blanc coûte ~18 s et ne s'interrompt pas : on ne la
    # commence jamais si une parole est en route. Le prochain silence suffira.
    if PAROLE_ATTENDUE.is_set():
        return False
    avant = len(HISTORIQUE) // 2
    with VERROU:
        del HISTORIQUE[:len(HISTORIQUE) - ECHANGES_GARDES * 2]
        # On refait digérer le nouveau début pendant qu'il ne parle pas.
        try:
            requete = {"model": MODELE, "max_tokens": 1,
                       "messages": [{"role": "system", "content": SYSTEME}]
                                   + HISTORIQUE}
            req = urllib.request.Request(API, data=json.dumps(requete).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=300) as x:
                x.read()
        except Exception as e:
            tracer(f"[coupe] relecture impossible ({type(e).__name__}) — "
                   f"sa prochaine réponse sera plus lente une fois")
    tracer(f"[coupe] {avant} échanges -> {len(HISTORIQUE)//2} gardés mot pour mot "
           f"(le reste est dans sa mémoire et dans le fil)")
    return True


def _veilleur():
    """Guette les silences et lance les rangements. Un seul, en tâche de fond.

    Il ne fait RIEN tant que Utilisateur parle : il regarde l'heure de la dernière
    demande et attend le silence. Coût au repos : négligeable (un réveil toutes
    les 5 s pour comparer deux nombres).
    """
    while True:
        time.sleep(5)
        try:
            if MEMOIRE is None:
                continue
            if time.time() - DERNIERE_ACTIVITE < SILENCE_AVANT_RANGEMENT:
                continue
            # Une parole est déjà en route vers le verrou ? On ne lance RIEN :
            # elle passe d'abord, le rangement repassera dans 5 s.
            if PAROLE_ATTENDUE.is_set():
                continue
            # ⚠️ L'ORDRE DES DEUX TESTS COMPTE. Une première version sortait
            # dès que la file d'attente était vide — donc la coupe, placée
            # après, ne se déclenchait jamais. Or c'est précisément quand tout
            # est rangé qu'il faut couper.
            if EN_ATTENTE:
                ranger_un_lot()
            # ⚠️ CONDITION DE COUPE — une première version exigeait que la file
            # d'attente soit ENTIÈREMENT vide. Avec 105 phrases rangées 3 par 3,
            # ça n'arrivait jamais : la coupe ne se déclenchait pas du tout.
            #
            # Le bon raisonnement : la file est chronologique, donc les phrases
            # NON ENCORE RANGÉES sont les plus RÉCENTES — et les récentes sont
            # justement celles qu'on garde. Il suffit donc que la file tienne
            # dans la fenêtre conservée pour qu'on ne jette rien d'inconnu.
            # (Unités : la file compte des PHRASES de Utilisateur, le seuil des
            #  ÉCHANGES — équivalent ici, car 1 phrase de lui = 1 échange.)
            if len(EN_ATTENTE) <= ECHANGES_GARDES:
                couper_les_vieux_echanges()
        except Exception as e:
            # Un rangement raté ne doit JAMAIS tuer la conversation : les
            # phrases restent en attente et repartiront au prochain silence,
            # ou au pire à la fermeture.
            tracer(f"[silence] rangement impossible : {type(e).__name__}: {e}")


def prechauffer():
    """Fait digérer son prompt au cerveau AVANT que Utilisateur parle.

    LE PROBLÈME (mesuré plusieurs fois) : la première réplique d'une session
    prenait ~16 s, les suivantes 2 à 4 s. La différence n'est pas la réponse,
    c'est la LECTURE du prompt de personnalité — 2689 jetons à digérer, une
    seule fois. Ensuite le moteur garde ce travail en cache et n'y revient plus.

    Ce prompt ne change pas d'un mot entre le démarrage et la première phrase.
    Rien n'obligeait donc à le faire payer à Utilisateur : on le fait pendant le
    chargement, quand il attend déjà. Le coût est simplement DÉPLACÉ là où il
    ne se voit pas — la fenêtre annonce déjà « ~40 s », quelques secondes de
    plus n'y changent rien, alors que 16 s sur sa première phrase, si.

    On ne demande qu'UN jeton : seule la lecture nous intéresse, pas la réponse.
    Rien n'entre dans l'historique ni dans la mémoire — c'est une mise en route,
    pas un échange.
    """
    t0 = time.time()
    requete = {"model": MODELE, "max_tokens": 1,
               "messages": [{"role": "system", "content": SYSTEME},
                            {"role": "user", "content": "Bonjour."}]}
    req = urllib.request.Request(API, data=json.dumps(requete).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as x:
            x.read()
        tracer(f"prompt digéré d'avance ({time.time() - t0:.1f} s) — "
               f"sa première phrase ne le paiera plus")
    except Exception as e:
        # Sans gravité : on retombe simplement sur l'ancien comportement,
        # c'est-à-dire une première réplique plus lente.
        tracer(f"préchauffage impossible ({type(e).__name__}) — "
               f"la première réponse sera plus lente, rien de plus")


def relancer(secondes_de_silence):
    """Elle prend la parole d'elle-même après un silence.

    ⚠️ RIEN N'EST MIS EN MÉMOIRE ICI. C'est le point crucial : la consigne
    qu'on lui donne (« relance la conversation ») ne vient pas de Utilisateur, et
    ses propres paroles ne doivent jamais devenir des souvenirs de lui. C'est
    la même leçon que le mot de réveil pris pour son nom le 18/07 : tout ce que
    la MÉCANIQUE ajoute doit rester hors de la mémoire.

    LA CONSOLIDATION EST DÉJÀ FAITE quand on arrive ici : le veilleur range
    après 45 s de silence, la relance part au plus tôt à 120 s. Elle s'appuie
    donc sur une mémoire fraîchement triée — c'est l'idée de Utilisateur, obtenue
    par le simple ordre des deux délais plutôt que par du code de coordination.
    """
    VERROU.acquire()
    try:
        minutes = max(1, secondes_de_silence // 60)
        consigne = (
            f"[SILENCE — il n'a rien dit depuis {minutes} minute(s). Personne ne "
            f"t'a rien demandé : c'est TOI qui reprends la parole.]\n"
            f"Dis quelque chose. Pas « tu es là ? », pas « tout va bien ? » — ça "
            f"sonne creux. Rebondis sur ce dont vous parliez, lance un sujet neuf, "
            f"balance une pensée qui te traverse, ou charrie-le sur son silence. "
            f"Une ou deux phrases, comme quelqu'un qui relève la tête.")
        messages = ([{"role": "system", "content": SYSTEME}]
                    + HISTORIQUE
                    + [{"role": "system",
                        "content": MEMOIRE.bloc_a_injecter(
                            [], silence=time.time() - DERNIERE_ACTIVITE)
                        + bloc_du_fil() + carte_du_tour(force=True)}]
                    + [{"role": "system", "content": consigne}])
        t0 = time.time()
        brut = _generer(messages)
        if not brut:
            return {"texte": "", "erreur": "génération vide", "t_llm": time.time() - t0}
        parlee = corriger_appellation(limiter_longueur(retirer_tic_ouverture(
            nettoyer_pour_voix(retirer_fuites_de_consignes(brut)))))
        # limiter_le_prenom manquait ICI aussi (audit du 22/07 — même oubli que
        # la régénération le 21/07) : les relances pouvaient s'ouvrir sur
        # « Utilisateur, ... » en boucle, le tic que ce filtre existe pour tuer.
        _prec = next((h["content"] for h in reversed(HISTORIQUE)
                      if h["role"] == "assistant"), "")
        parlee = limiter_le_prenom(parlee, _prec,
                                   nom=MEMOIRE.nom if MEMOIRE else "Utilisateur")
        parlee, _, tout_redit = couper_repetitions(parlee, DEJA_DITES)
        if tout_redit or not parlee.strip():
            # Elle n'a rien de neuf à dire : mieux vaut se taire que radoter.
            tracer("[relance] rien de neuf à dire — elle se tait")
            return {"texte": "", "erreur": None, "t_llm": time.time() - t0}
        memoriser_phrases(parlee, DEJA_DITES)
        # Sa relance entre dans l'HISTORIQUE (elle l'a dite, il l'a entendue),
        # mais JAMAIS dans EN_ATTENTE : ce n'est pas une phrase de Utilisateur.
        HISTORIQUE.append({"role": "assistant", "content": parlee})
        return {"texte": parlee, "erreur": None, "t_llm": time.time() - t0}
    finally:
        VERROU.release()


def repondre(message):
    """Un tour complet : souvenirs -> cerveau -> filtres. Le tri part après."""
    global DERNIERE_ACTIVITE
    # Le silence se mesure AVANT de remettre l'horodatage a zero, sinon il vaut
    # toujours ~0 au moment ou le bloc memoire est construit (defaut du 20/07).
    silence = time.time() - DERNIERE_ACTIVITE
    DERNIERE_ACTIVITE = time.time()   # il parle : pas de rangement maintenant
    PAROLE_ATTENDUE.set()     # un tri en cours cede la place au plus vite
    VERROU.acquire()          # on attend qu'il ait fini de ceder
    PAROLE_ATTENDUE.clear()
    try:
        return _repondre_sous_verrou(message, silence)
    finally:
        VERROU.release()


def _generer(messages):
    """Un appel au cerveau. Renvoie le texte brut, ou une chaine vide si echec."""
    requete = {"model": MODELE, "messages": messages}
    requete.update(PARAMS)
    req = urllib.request.Request(API, data=json.dumps(requete).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        # 300 s et non 600 : une génération saine prend 2 à 16 s, le pire cas
        # mesuré (jeu très lourd) ~130 s. Au-delà de 5 minutes le moteur est
        # mort, et geler tout le service 10 minutes n'aidait personne.
        with urllib.request.urlopen(req, timeout=300) as x:
            return json.loads(x.read().decode("utf-8"))["choices"][0]["message"]["content"].strip()
    except Exception as e:
        tracer(f"ERREUR generation : {type(e).__name__}: {e}")
        return ""


def bloc_du_fil():
    """Le déroulé récent de la conversation, remis dans sa tête.

    Vide au début d'une session : il n'y a rien à raconter tant qu'aucun
    rangement n'a eu lieu, et de toute façon les échanges récents sont encore
    sous ses yeux. Il prend son utilité quand la session s'allonge.
    """
    if not FIL:
        return ""
    lignes = "\n".join(f"- vers {e['heure']} : {e['quoi']}" for e in FIL[-FIL_INJECTE:])
    return ("\n\n[LE FIL DE VOTRE CONVERSATION — ce dont vous avez déjà parlé "
            "aujourd'hui, dans l'ordre]\n" + lignes
            + "\nTu peux y revenir naturellement (« tout à l'heure tu disais… »). "
              "N'invente rien qui n'y soit pas.")


# ═══════════════════════════════════════════════════════════════════════════
#  LE GRENIER À SUJETS — la source de nouveauté qui lui manquait
# ═══════════════════════════════════════════════════════════════════════════
#
# POURQUOI (20/07/2026, retour de Utilisateur sur sa session réelle) : « elle
# tourne sur les trois mêmes sujets et ne dit rien de nouveau... ce qui l'a
# menée à dire qu'elle ne sait plus de quoi parler. » Structurellement, elle
# n'avait RIEN d'autre : ses goûts du prompt, la mémoire de Utilisateur et le fil
# de la session — trois sources qui la ramènent toujours aux mêmes endroits.
# Et quand elle voulait raconter, elle improvisait des histoires aux détails
# faux (la grotte « 30 ans », les « neuf » footballeurs) : le besoin était là,
# pas la matière.
#
# LA MATIÈRE est dans donnees\sujets_alice.txt : des histoires vraies aux faits
# vérifiés, des questions à lui poser, des débats. Une carte est piochée au
# hasard à chaque tour. La consigne est explicite : elle s'en sert si ça
# s'essouffle, elle l'ignore sinon — c'est une réserve, pas un programme.
# Fichier enrichissable à la main, une ligne = un sujet, # = commentaire.
import random  # noqa: E402

FICHIER_SUJETS = PROJET / "donnees" / "sujets_alice.txt"
try:
    SUJETS = [l.strip() for l in FICHIER_SUJETS.read_text(encoding="utf-8").splitlines()
              if l.strip() and not l.strip().startswith("#")]
except Exception:
    SUJETS = []


# ⚠️ PAS UNE CARTE À CHAQUE TOUR — corrigé le 20/07/2026 après la session
# réelle : « deux à trois questions par échange sur deux ou trois sujets
# différents, pas utilisable pour une discussion ». Une carte à chaque tour
# l'incitait à EMPILER : le sujet en cours + la carte + une question. La carte
# ne vient plus que quand il donne peu (message court = conversation qui
# s'essouffle), ou au plus tous les 4 tours, ou pour une relance — et dans
# TOUS les cas : jamais pendant un sujet profond, jamais avant le 4e tour
# (les deux gardes du 21-22/07 priment, relance comprise).
# [0] et pas [-9] : l'ancienne valeur faisait passer le test « tous les
# 4 tours » DÈS LE PREMIER MESSAGE — Alice recevait un sujet aléatoire à
# froid et brodait dessus (session de 22h05 le 21/07 : « ta nuit d'hier »,
# un frère imaginaire, une fausse heure). Constat de Utilisateur : « les deux
# trois premiers messages elle imagine ou invente des trucs sans raison,
# puis après repart sur du vrai. » Voir aussi TOURS_SANS_CARTE ci-dessous.
_DERNIER_TOUR_A_CARTE = [0]
# Aucune carte avant ce tour-là : le début de session est le moment où elle a
# le moins de contexte réel — c'est là que la carte fait le plus de dégâts.
TOURS_SANS_CARTE = 4

# ⚠️ PAS DE CARTE PENDANT UN SUJET PROFOND — 21/07/2026, chantier n°4 du 20/07.
# La correction de fréquence ci-dessus ne suffisait pas : la carte restait tirée
# AU HASARD, et le déclencheur « message court » tombait précisément dans les
# moments graves (Utilisateur parle peu, surtout quand ça compte — le point Nemo a
# atterri au milieu d'une discussion sur l'existence d'Alice). Un détecteur de
# mots-clés déterministe coupe la carte quand les derniers échanges touchent un
# sujet lourd : c'est elle-mêmes et sa mémoire qui portent ces moments-là, pas
# le grenier. Trop couper est sans danger (la carte est une réserve, pas un
# programme) ; couper trop peu remet du Nemo dans les conversations profondes.
_SUJET_LOURD = _re.compile(
    r"\b(exist\w*|sentien\w*|conscien\w*|mort|morts?|morte?s?|mourir|meur[st]|"
    r"suicid\w*|d[ée]prim\w*|triste(sse)?s?|angoiss\w*|souffr\w*|"
    r"programme?s?|machines?|simulations?|illusions?|scripts?|robots?|"
    r"[âa]mes?|dieux?|religions?|croyances?|"
    r"seule?s?|solitude|vide|sens de la vie|humanit[ée])\b", _re.IGNORECASE)


def _conversation_profonde(message=None):
    """Les 2 derniers échanges (+ le message en cours) touchent-ils un sujet lourd ?"""
    textes = [h["content"] for h in HISTORIQUE[-4:]]
    if message:
        textes.append(message)
    return any(_SUJET_LOURD.search(t) for t in textes)


def carte_du_tour(message=None, force=False):
    if not SUJETS:
        return ""
    if _conversation_profonde(message):
        return ""
    tour = len(HISTORIQUE) // 2
    # Jamais de carte en tout début de session — même pour une relance : mieux
    # vaut une relance sobre qu'une invention à froid.
    if tour < TOURS_SANS_CARTE:
        return ""
    if not force:
        essouffle = message is not None and len(message.split()) <= 6
        if not essouffle and tour - _DERNIER_TOUR_A_CARTE[0] < 4:
            return ""
    _DERNIER_TOUR_A_CARTE[0] = tour
    return ("\n\n[UNE CARTE DANS TA MANCHE — un sujet neuf, en réserve. Sers-t'en "
            "avec tes mots SI la conversation s'essouffle ou tourne en rond ; "
            "sinon, ignore-la.]\n- " + random.choice(SUJETS))


def _repondre_sous_verrou(message, silence=None):
    # FILET DE SECOURS : s'il enchaîne sans jamais laisser 45 s de silence, le
    # veilleur n'a aucune occasion de couper et la conversation grossit quand
    # même. Ici on coupe en plein échange — il paiera une relecture d'environ
    # 18 s, une fois. C'est désagréable, mais infiniment préférable à une
    # session qui plante au bout de deux heures.
    if len(HISTORIQUE) > ECHANGES_LIMITE_DURE * 2:
        avant = len(HISTORIQUE) // 2
        del HISTORIQUE[:len(HISTORIQUE) - ECHANGES_GARDES * 2]
        tracer(f"[coupe d'urgence] {avant} échanges sans une seule pause -> "
               f"{len(HISTORIQUE)//2} gardés. Cette réponse-ci sera plus lente.")

    faits, t_recup = MEMOIRE.souvenirs_pertinents(message)
    trace_humeur = humeur_apres(message)
    if bloc_humeur():
        tracer(f"[{trace_humeur}]")
    bloc_memoire = (MEMOIRE.bloc_a_injecter(faits, silence=silence)
                    + bloc_du_fil() + carte_du_tour(message) + bloc_humeur())

    HISTORIQUE.append({"role": "user", "content": message})

    # Ordre validé : personnalité, passé, carnet de souvenirs COLLÉ à la question,
    # puis la question. Le carnet placé en fin de contexte reste dans son attention.
    messages = (
        [{"role": "system", "content": SYSTEME}]
        + HISTORIQUE[:-1]
        + [{"role": "system", "content": bloc_memoire}]
        + [HISTORIQUE[-1]]
    )
    requete = {"model": MODELE, "messages": messages}
    requete.update(PARAMS)
    req = urllib.request.Request(API, data=json.dumps(requete).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as x:   # même règle que _generer
            rep = json.loads(x.read().decode("utf-8"))
        brut = rep["choices"][0]["message"]["content"].strip()
        erreur = None
    except Exception as e:
        brut = ""
        erreur = f"{type(e).__name__}: {e}"
    t_llm = time.time() - t0

    if erreur:
        tracer(f"ERREUR CERVEAU : {erreur}")
        return {"texte": "", "erreur": erreur, "t_llm": t_llm,
                "t_recup": t_recup, "n_souvenirs": len(faits)}

    parlee = corriger_appellation(
        limiter_longueur(retirer_tic_ouverture(nettoyer_pour_voix(retirer_fuites_de_consignes(brut)))))
    # Le tic du prénom : sa réplique PRÉCÉDENTE est le dernier assistant de
    # l'historique (le message de Utilisateur vient d'y être ajouté, pas encore
    # la réponse en cours de fabrication).
    _prec = next((h["content"] for h in reversed(HISTORIQUE)
                  if h["role"] == "assistant"), "")
    parlee = limiter_le_prenom(parlee, _prec, nom=MEMOIRE.nom if MEMOIRE else "Utilisateur")

    # FILTRE ANTI-REPETITION : on coupe ce qu'elle a deja dit recemment.
    # Le prompt et DRY n'y suffisent pas (5 sessions mesurees) ; le code, si.
    parlee, n_coupees, tout_redit = couper_repetitions(parlee, DEJA_DITES)
    if tout_redit:
        # La replique entiere etait deja dite : on en redemande une, en le lui
        # signalant. Ca coute une generation de plus (~5 s) mais seulement dans
        # ce cas precis, qui est le plus grave.
        tracer("  [anti-repetition] replique entierement redite -> on redemande")
        brut2 = _generer(messages + [{"role": "system",
                "content": "Tu viens de repeter mot pour mot une replique deja "
                           "dite. Reformule COMPLETEMENT, autrement, sans reprendre "
                           "aucune de tes tournures precedentes."}])
        if brut2:
            # Les MÊMES filtres que le chemin normal — la première version en
            # sautait deux (corriger_appellation, limiter_le_prenom) : sur une
            # régénération, « surnom » et le prénom en ouverture
            # passaient sans être nettoyés (audit du 21/07).
            parlee2 = corriger_appellation(
                limiter_longueur(retirer_tic_ouverture(nettoyer_pour_voix(retirer_fuites_de_consignes(brut2)))))
            parlee2 = limiter_le_prenom(parlee2, _prec,
                                        nom=MEMOIRE.nom if MEMOIRE else "Utilisateur")
            parlee, n_coupees, _ = couper_repetitions(parlee2, DEJA_DITES)
    memoriser_phrases(parlee, DEJA_DITES)
    globals()["DERNIERE_ACTIVITE"] = time.time()   # le silence part d'ici
    if n_coupees:
        tracer(f"  [anti-repetition] {n_coupees} phrase(s) deja dite(s), retiree(s)")

    # TRACE : ce qu'on lui a mis dans la tete, et ce que les filtres ont retire.
    # Sans ca, impossible de savoir apres coup si une bizarrerie vient du modele,
    # du carnet de souvenirs, ou de nos propres filtres (constate le 18/07 :
    # j'ai du deviner ce que limiter_longueur avait coupe).
    tracer("  [memoire injectee]")
    for lg in bloc_memoire.splitlines():
        tracer(f"     | {lg}")
    if parlee != brut:
        tracer(f"  [filtres] brut {len(brut.split())} mots -> "
               f"garde {len(parlee.split())} mots")
        tracer(f"     | BRUT   : {brut}")
        tracer(f"     | GARDE  : {parlee}")
    # On range dans l'historique la version COURTE : ainsi elle ne voit pas ses
    # propres pavés dans son passé et ne calque pas sa longueur dessus.
    HISTORIQUE.append({"role": "assistant", "content": parlee})

    # Plus de tri ici : on met simplement de côté. Le tri passe à la fermeture.
    mettre_en_attente(message)

    return {"texte": parlee, "erreur": None, "t_llm": t_llm, "t_recup": t_recup,
            "n_souvenirs": len(faits), "filtre": parlee != brut,
            "n_tour": len(HISTORIQUE) // 2}


class Poignee(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"cerveau pret")

    def do_POST(self):
        # /relancer : elle prend la parole d'elle-meme apres un silence.
        # Rien n'entre en memoire — voir relancer().
        if self.path.rstrip("/").endswith("relancer"):
            n = int(self.headers.get("Content-Length", 0))
            try:
                sec = json.loads(self.rfile.read(n).decode("utf-8")).get("silence", 120)
            except Exception:
                sec = 120
            res = relancer(int(sec))
            if res.get("texte"):
                tracer(f"[relance] après {int(sec)} s de silence "
                       f"({res['t_llm']:.1f} s) : \"{res['texte'][:80]}\"")
            corps = json.dumps(res, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)
            return

        # /ranger : la fenetre se ferme, on trie toute la session d'un coup.
        if self.path.rstrip("/").endswith("ranger"):
            nb, dt = ranger_la_session()
            corps = json.dumps({"souvenirs": nb, "duree": dt}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)
            return

        n = int(self.headers.get("Content-Length", 0))
        try:
            message = json.loads(self.rfile.read(n).decode("utf-8")).get("message", "").strip()
        except Exception as e:
            self.send_response(400)
            self.end_headers()
            tracer(f"ERREUR lecture demande : {e}")
            return
        if not message:
            self.send_response(400)
            self.end_headers()
            return

        tracer(f"demande reçue : \"{message}\"")
        res = repondre(message)
        tracer(f"  réponse ({res['t_llm']:.1f} s, {len(res['texte'].split())} mots, "
               f"{res['n_souvenirs']} souvenir(s)) : \"{res['texte'][:90]}\"")

        corps = json.dumps(res, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)


def main():
    global SYSTEME, MEMOIRE
    PROCESSUS_MOTEUR = None
    LOG.parent.mkdir(parents=True, exist_ok=True)
    SYSTEME = PROMPT_FILE.read_text(encoding="utf-8")
    # ═══ LA CARTE D'IDENTITÉ DE LA SESSION ════════════════════════════════════
    # Le 19/07/2026, Alice s'est remise à parler de ancien métier après qu'on l'ait
    # retirée du socle. Utilisateur a soupçonné — légitimement — qu'on modifiait le
    # mauvais fichier, ou qu'il lançait une vieille version. Il a fallu fouiller
    # tout le projet pour établir que non (la cause était dans sa mémoire).
    # Ce doute ne doit plus jamais coûter une heure : la session DIT ce qu'elle
    # charge, avec l'empreinte et la date des fichiers. Plus rien à supposer.
    import hashlib
    empreinte = hashlib.sha256(PROMPT_FILE.read_bytes()).hexdigest().upper()[:8]
    tracer(f"prompt chargé : {PROMPT_FILE.name} (empreinte {empreinte}, "
           f"{len(SYSTEME.split())} mots, modifié le "
           f"{datetime.fromtimestamp(PROMPT_FILE.stat().st_mtime):%d/%m %H:%M})")
    for f in (PROJET / "cerveau" / "service_cerveau.py",
              PROJET / "cerveau" / "alice_chat.py",
              PROJET / "memoire" / "memoire_alice.py"):
        tracer(f"  code en service : {f.parent.name}\\{f.name} — modifié le "
               f"{datetime.fromtimestamp(f.stat().st_mtime):%d/%m %H:%M}")

    # Le moteur vit maintenant dans cerveau\moteur.py — llama.cpp lance en direct.
    # Mesure du 18/07/2026 : 17,6 Go de memoire vive avec LM Studio, 3,5 Go ici.
    # Le repli vers LM Studio est une ligne a changer dans ce fichier-la.
    try:
        PROCESSUS_MOTEUR = moteur.demarrer(tracer)
    except Exception as e:
        tracer(f"ÉCHEC DU CHARGEMENT : {e}")
        return 1

    # QUI PARLE. Utilisateur par défaut — c'est le cas normal, rien ne change pour lui.
    # Les deux variables d'environnement servent à faire parler QUELQU'UN D'AUTRE
    # sans toucher à sa mémoire à lui : Mem0 sépare les souvenirs par user_id, et
    # chaque personne a sa propre fiche d'identité (voir FICHES dans
    # memoire_alice.py). C'est ce qui permet de tester Alice avec un second
    # interlocuteur — et c'est la brique qu'attend le futur bot Discord.
    # Les messages de la mémoire (doublons, juge, pannes) passent par NOTRE
    # tracer : sans ça ils partent dans une console invisible quand Alice est
    # lancée par la fenêtre — toute une session de verdicts perdue le 20/07.
    import memoire_alice as _ma
    _ma.TRACEUR[0] = tracer
    # Le tri cède la place : la mémoire consulte notre drapeau entre deux
    # jugements (voir PAROLE_ATTENDUE en tête de fichier).
    _ma.CEDER[0] = PAROLE_ATTENDUE.is_set
    MEMOIRE = MemoireAlice(user_id=os.environ.get("ALICE_USER", "utilisateur"),
                           nom_affiche=os.environ.get("ALICE_NOM", "Utilisateur"))
    tracer(f"interlocuteur : {MEMOIRE.nom} (mémoire « {MEMOIRE.user_id} »)")
    tracer(f"mémoire réveillée — {MEMOIRE.nb_souvenirs()} souvenir(s) connu(s).")

    # Le test avant/apres a besoin de pouvoir le desactiver ; en usage normal
    # la variable n'existe pas et le prechauffage a toujours lieu.
    if not os.environ.get("ALICE_SANS_PRECHAUFFAGE"):
        prechauffer()

    # REPRISE APRES PLANTAGE : si une session precedente s'est arretee sans ranger,
    # ses phrases sont encore sur le disque. On les trie maintenant, avant de
    # commencer. C'est ce qui garantit qu'on ne perd jamais une session.
    if FICHIER_ATTENTE.exists():
        try:
            restes = json.loads(FICHIER_ATTENTE.read_text(encoding="utf-8"))
            if restes:
                # ⚠️ LE PORTIER, ICI AUSSI (20/07/2026). Ce chemin de reprise
                # contournait le filtre : les phrases mises de côté AVANT que le
                # portier existe — dont 106 de la vraie session de Utilisateur, avec
                # leur méta-conversation (« tes réponses sont de moins en moins
                # bonnes ») — seraient parties telles quelles dans sa mémoire au
                # premier démarrage. Un filtre qui ne couvre pas tous les chemins
                # d'écriture ne protège rien. Pas de réponse précédente sous la
                # main pour la règle d'écho : la règle méta seule s'applique.
                avant = len(restes)
                gardees = []
                for r in restes:
                    ok_p, raison = portier(r, "")
                    if ok_p:
                        gardees.append(r)
                    else:
                        tracer(f"  [portier] écartée à la reprise — {raison}")
                tracer(f"[mémoire] {avant} phrase(s) d'une session interrompue "
                       f"retrouvée(s), {len(gardees)} après le portier — "
                       f"rangement avant de commencer.")
                EN_ATTENTE.extend(gardees)
                ranger_la_session()
        except Exception as e:
            tracer(f"[mémoire] reprise impossible : {type(e).__name__}: {e}")
    # LE VEILLEUR : range la memoire pendant les silences. Demon = il meurt
    # avec le service, sans rien bloquer a la fermeture.
    threading.Thread(target=_veilleur, daemon=True).start()
    tracer(f"veilleur de silence actif (rangement après "
           f"{SILENCE_AVANT_RANGEMENT:.0f} s sans un mot)")
    tracer(f"service sur le port {PORT}")

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Poignee)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            ranger_la_session()        # filet : ne jamais partir sans avoir rangé
        except Exception as e:
            tracer(f"[mémoire] rangement final impossible : {type(e).__name__}")
        moteur.arreter(PROCESSUS_MOTEUR, tracer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
