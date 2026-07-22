# -*- coding: utf-8 -*-
"""
La mémoire longue d'Alice — le module qui se branche sur la conversation.

Il encapsule Mem0 + Chroma (tout local) et applique LES TROIS VERROUS anti-faux-souvenirs :

  VERROU 1 — PROVENANCE : chaque souvenir stocké porte QUI l'a dit et QUAND (source + date).
  VERROU 2 — INJECTION SÉLECTIVE : avant de répondre, on ne remet dans la tête d'Alice QUE les
             faits réellement retrouvés pour le sujet courant. On lui dit explicitement : « ce qui
             n'est pas dans cette liste, tu ne le sais pas — demande, n'invente pas ».
  VERROU 3 — ANTI-SUPPOSITION AU TRI : le trieur a l'ordre de ne stocker QUE ce que l'humain a
             réellement affirmé. Jamais une déduction, jamais une hypothèse. Et — point crucial —
             on ne mémorise QUE les phrases de Utilisateur, JAMAIS les réponses d'Alice : ainsi ses
             propres inventions ne peuvent jamais devenir des « faits ».

FICHE PAR PERSONNE : chaque humain a un `user_id`. Pour l'instant un seul (« utilisateur »), mais
l'architecture sépare déjà les mémoires — prêt pour le futur Discord (un user_id par pseudo).
"""

import json
import os
import re
import threading
import time
import unicodedata
import urllib.request
from datetime import date
from pathlib import Path

PROJET = Path(__file__).resolve().parent.parent
MAGASIN = PROJET / "memoire" / "magasin"          # persistant : survit a la fermeture
DOSSIER_MEM0 = PROJET / "memoire" / ".mem0"        # config + historique de Mem0
HISTORIQUE_DB = DOSSIER_MEM0 / "history.db"
# ⚠️ RÉGRESSION DU 18/07/2026 AU SOIR, ET SA CORRECTION — à lire avant d'y toucher.
#
# Cette adresse pointait en dur sur LM Studio (localhost:1234). Le soir même, le
# cerveau a été déplacé sur llama.cpp lancé en direct, et `moteur.demarrer()`
# décharge LM Studio au passage. Conséquence NON VUE sur le coup : le trieur de
# mémoire continuait d'appeler LM Studio, qui n'avait plus aucun modèle chargé.
#
# Le symptôme était sournois — aucune erreur affichée, juste :
#     19:45  session rangée : 0 souvenir(s) en 37,0 s
#     20:07  session rangée : 0 souvenir(s) en 44,3 s
# Le tri « travaillait » une quarantaine de secondes et ne retenait rien. Alice
# a donc traversé toute la soirée SANS MÉMOIRE, y compris la session de test de
# Utilisateur. Trouvé en préparant un autre test, pas par une alerte.
#
# LEÇON : quand on déplace un service, chercher QUI D'AUTRE lui parlait. Ici la
# mémoire était un client silencieux du cerveau, et personne ne l'avait noté.
#
# Les deux adresses sont désormais réglables, et pointent par défaut sur les
# serveurs llama.cpp du projet (voir cerveau\moteur.py, qui les démarre tous deux).
API = os.environ.get("ALICE_API_LLM", "http://127.0.0.1:8095/v1")
API_EMBEDDINGS = os.environ.get("ALICE_API_EMB", "http://127.0.0.1:8096/v1")

# IMPORTANT : par defaut Mem0 ecrit dans ~\.mem0\ — HORS du projet.
# On le force a tout garder DANS <racine du projet>\. Doit etre fait AVANT d'importer mem0.
DOSSIER_MEM0.mkdir(parents=True, exist_ok=True)
os.environ["MEM0_DIR"] = str(DOSSIER_MEM0)
os.environ["MEM0_TELEMETRY"] = "False"             # aucune donnee ne sort de la machine

from mem0 import Memory  # noqa: E402  (import apres avoir fixe MEM0_DIR)

# VERROU 3 (partie tri) : consignes données au cerveau-trieur.
#
# ⚠️ RÉÉCRITES LE 19/07/2026 APRÈS LA BOUCLE DE LA ancien métier — la panne la plus
# instructive du projet. Le mécanisme, en quatre temps :
#   1. le socle du prompt v1 mentionnait la ancien métier (pour l'INTERDIRE) ;
#   2. Alice s'y est accrochée et a inventé « la ancien métier du coin était fermée » ;
#   3. Utilisateur a répondu à cette invention (« je ne pense pas qu'elle était fermée ») ;
#   4. le trieur a rangé SA RÉPONSE comme un fait sur lui.
# Résultat : quatre souvenirs de ancien métier nés de rien. La mémoire vidée, elle s'est
# repeuplée toute seule à la session suivante. **Ses inventions devenaient permanentes.**
#
# Les anciennes consignes disaient déjà « ne retiens que ce qu'il a affirmé sur
# lui-même ». Ça n'a pas suffi — elles n'avaient AUCUN EXEMPLE. Même leçon que pour le
# prompt principal le même jour : un modèle suit ce qu'on lui MONTRE, pas ce qu'on lui
# décrit. D'où les exemples ci-dessous, tirés de vraies pollutions constatées.
INSTRUCTIONS_TRI = (
    "Rédige TOUS les faits en FRANÇAIS, jamais en anglais.\n"
    "Tu ne retiens qu'une chose : les faits DURABLES que l'utilisateur affirme sur "
    "lui-même, sa vie ou ses proches. Un fait durable est encore vrai dans un mois.\n"
    "\n"
    "GARDE (exemples) :\n"
    "- « J'ai un chat qui s'appelle un mot-test »  -> l'utilisateur a un chat, un mot-test\n"
    "- « Je suis dans sa situation et j'ai un crédit »  -> l'utilisateur a un crédit à payer\n"
    "- « Mon frère m'a appelé hier »             -> l'utilisateur a un frère\n"
    "\n"
    "JETTE (exemples) — ce ne sont PAS des faits :\n"
    # ⚠️ L'exemple d'origine citait la ancien métier — le sujet même de la panne du
    # 19/07. Ce texte ne part qu'au TRIEUR, jamais dans la tête d'Alice, donc il ne
    # pouvait pas la relancer dessus. Remplacé quand même : après une journée passée
    # à prouver que nommer une chose la fait ressortir, l'écrire dans un prompt
    # quel qu'il soit est un risque gratuit. Un exemple neutre marche aussi bien.
    "- « Je ne crois pas que ce magasin était fermé » : il RÉAGIT à un sujet "
    "lancé par l'assistante. Ce n'est pas un fait sur lui.\n"
    "- « Tu penses que Dieu existe ? » : une question. Jamais un souvenir.\n"
    "- « Je trouve ce sujet mauvais », « je ne suis pas d'accord » : un avis sur la "
    "conversation en cours, pas sur sa vie.\n"
    "- « Ouais », « bof », « je sais pas » : du remplissage.\n"
    "- Une ANECDOTE ou une HISTOIRE discutée (un fait divers, une histoire vraie, "
    "un débat — l'officier russe, une forêt, un naufrage...) : ce n'est JAMAIS un "
    "fait sur l'utilisateur, même s'il la commente ou la reformule. Ne retiens "
    "quelque chose que s'il révèle un élément de SA PROPRE VIE à cette occasion "
    "(« ça me rappelle mon service militaire » -> oui ; « c'est fou cette "
    "histoire » -> non).\n"
    "\n"
    "RÈGLE DÉCISIVE : si l'information vient d'un sujet que l'ASSISTANTE a introduit, "
    "et non de ce que l'utilisateur voulait raconter, tu ne la retiens pas. "
    "Dans le doute, ne retiens rien : un souvenir faux coûte plus cher qu'un souvenir "
    "manquant."
)

CONFIG = {
    "llm": {
        "provider": "lmstudio",
        "config": {
            "model": "mistral-small-3.2-24b-instruct-2506",
            "lmstudio_base_url": API,
            "temperature": 0.1,          # le tri doit être sobre et factuel
            "lmstudio_response_format": {"type": "text"},
        },
    },
    "embedder": {
        "provider": "lmstudio",
        "config": {
            "model": "text-embedding-nomic-embed-text-v1.5",
            # ⚠️ Serveur SÉPARÉ du cerveau : un serveur llama.cpp ne sert qu'un
            # modèle à la fois. Le petit modèle d'embeddings (81 Mo) a donc le
            # sien, sur le port 8096 — voir moteur.py, qui démarre les deux.
            "lmstudio_base_url": API_EMBEDDINGS,
            "embedding_dims": 768,
        },
    },
    "vector_store": {
        "provider": "chroma",
        "config": {"collection_name": "eresh", "path": str(MAGASIN)},
    },
    "history_db_path": str(HISTORIQUE_DB),   # l'historique reste DANS le projet
    "custom_instructions": INSTRUCTIONS_TRI,
}

# ═══ OÙ VONT LES MESSAGES DE LA MÉMOIRE — corrigé le 20/07/2026 ══════════════
# Les doublons écartés, les souvenirs remplacés, les verdicts du juge : tout
# partait en print(), donc dans la console du service — INVISIBLE quand Alice
# est lancée par la fenêtre (console cachée). Résultat : la réconciliation a
# semblé muette toute une session, sans qu'on puisse dire si elle jugeait mal
# ou si elle travaillait sans témoin. Le service branche son propre tracer ici :
# tout arrive désormais dans tests\logs\service_cerveau_*.txt.
TRACEUR = [print]                 # remplacé par le tracer du service au démarrage

# LE TRI CÈDE LA PLACE (21/07/2026) : le service branche ici « une parole
# est-elle en route ? ». La réconciliation le consulte ENTRE deux jugements
# et s'arrête si oui — sauter un jugement revient à garder les deux notes,
# c'est déjà le choix « dans le doute » du juge, et les faits non jugés le
# seront à un prochain tri. Par défaut : jamais pressé (chat écrit, tests).
CEDER = [lambda: False]


def _dire(msg):
    try:
        TRACEUR[0](msg)
    except Exception:
        pass


SEUIL_PERTINENCE = 0.40   # en-dessous, un souvenir est jugé hors-sujet
TOP_K = 5                 # on n'injecte JAMAIS plus que les 5 meilleurs souvenirs
                          # (avant : Mem0 renvoyait TOUT — 20 faits par tour — ce qui
                          #  gonflait le contexte et faisait grimper le temps à ~15 s.
                          #  Sa "limite" est ignorée, donc on coupe nous-mêmes.)


# ═══════════════════════════════════════════════════════════════════════════
#  LE PORTIER — ce qui a le droit d'entrer dans la mémoire
# ═══════════════════════════════════════════════════════════════════════════
#
# POURQUOI IL EXISTE (20/07/2026). Le trieur de Mem0 fait 7/10 sur nos cas
# d'épreuve : il jette bien « Ouais », « Bof » et les questions, mais il garde
# tout ce qui est formulé à la première personne. D'où, dans la vraie mémoire
# de Utilisateur :
#     « L'utilisateur souhaite discuter de philosophie »        ×4
#     « L'utilisateur trouve le sujet de la ancien métier mauvais »
#     « L'utilisateur ne pense pas que la ancien métier était fermée »
# Aucun n'est un fait sur lui. Le dernier est même le maillon de la boucle qui
# transformait les inventions d'Alice en souvenirs permanents.
#
# Les consignes de tri ont été réécrites DEUX FOIS pour corriger ça, la seconde
# avec des exemples explicites. Sans effet mesurable. C'est la leçon constante du
# projet : une consigne cède, un filtre tient. Voici donc le filtre.
#
# ⚠️ CE PORTIER NE REMPLACE PAS LE TRIEUR, il le précède. Le trieur reste seul
# juge de CE QU'IL FAUT RETENIR d'une phrase ; le portier décide seulement si la
# phrase mérite qu'on la lui soumette.

_VIDES = {
    "le", "la", "les", "un", "une", "des", "du", "de", "et", "ou", "que", "qui",
    "quoi", "pas", "ne", "je", "tu", "il", "elle", "on", "nous", "vous", "ils",
    "mais", "donc", "car", "pour", "avec", "sans", "dans", "sur", "sous", "par",
    "est", "sont", "suis", "es", "ai", "as", "a", "avoir", "etre", "ete", "fait",
    "faire", "dit", "dire", "plus", "moins", "tres", "trop", "bien", "mal",
    "oui", "non", "ouais", "bof", "alors", "aussi", "meme", "tout", "tous",
    "ca", "cela", "ce", "cette", "ces", "mon", "ma", "mes", "ton", "ta", "tes",
    "son", "sa", "ses", "leur", "y", "en", "au", "aux", "se", "si", "comme",
    "quand", "parce", "peut", "peux", "veux", "veut", "vais", "va", "sais",
    "sait", "crois", "pense", "trouve", "comprends", "vraiment", "peut-etre",
}

# Les tournures qui parlent de LA CONVERSATION plutôt que de sa vie. Elles
# produisent des « souvenirs » qui n'apprennent rien sur lui et qui encombrent
# la recherche.
# ⚠️ La distinction est fine et volontaire : « je trouve que les gens sont
# mauvais » parle DU MONDE et doit être gardé — c'est un trait de sa vision des
# choses. « je trouve que ce sujet est mauvais » parle de la DISCUSSION en cours
# et ne vaut rien demain. Seule la seconde est visée.
_META_CONVERSATION = (
    "ce sujet", "cette discussion", "cette conversation", "ce dont on parle",
    "change de sujet", "changer de sujet", "parle d'autre chose",
    "parler d'autre chose", "on en parle", "qu'on discute", "qu'on parle",
    "ta reponse", "tes reponses", "ce que tu dis", "ce que tu viens de dire",
    "ce que tu me dis", "tu me demandes", "ta question", "tes questions",
    "on arrete d'en parler", "revenons", "je te disais",
    "je viens de te le dire", "je te repete",
)

# Certaines tournures acceptent un adverbe au milieu (« je te l'ai DÉJÀ dit »),
# et la comparaison par sous-chaîne les rate. Celles-là passent par un motif.
# Trouvé au test : « je te l'ai déjà dit trois fois » entrait dans la mémoire.
_META_MOTIFS = (
    re.compile(r"je te l'ai (deja |bien |pourtant )*dit"),
    re.compile(r"je (te )?(l'|le )?(re)?dis (encore|a nouveau|une fois)"),
)

# En dessous, la phrase est trop courte pour qu'on juge de quoi elle parle : on
# laisse passer et on fait confiance au trieur (qui rejette bien ce genre-là).
_MOTS_MINIMUM = 2

# Part des mots de contenu qui doivent venir d'elle pour qu'on considère qu'il
# ne fait que RÉAGIR à son sujet. 0,6 laisse passer une phrase où il apporte
# quelque chose de neuf, même en reprenant ses mots.
_SEUIL_ECHO = 0.6


def _nu(txt):
    """minuscules, sans accents, sans ponctuation — pour comparer des mots.

    Les accents doivent tomber : Whisper écrit tantôt « fermée » tantôt
    « fermee » selon le découpage, et deux orthographes du même mot casseraient
    la comparaison d'écho.
    """
    t = unicodedata.normalize("NFD", txt.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9' ]", " ", t)


def _mots_de_contenu(txt):
    return [m for m in _nu(txt).split() if len(m) > 2 and m not in _VIDES]


def portier(message, reponse_precedente=""):
    """Cette phrase mérite-t-elle d'être soumise au trieur ? -> (bool, raison)

    DEUX RÈGLES, toutes deux mécaniques :

    1. L'ÉCHO. Si l'essentiel des mots de contenu de sa phrase vient de ce
       qu'ELLE vient de dire, il ne raconte pas sa vie : il réagit à un sujet
       qu'elle a lancé. C'est exactement la boucle de la ancien métier —
       elle invente « la ancien métier était fermée », il répond « je ne pense
       pas », et le trieur range ça comme un fait sur lui. La mémoire se
       remplissait alors des hallucinations d'Alice, renvoyées par Utilisateur.

    2. LA MÉTA. Si la phrase parle de la conversation elle-même, elle n'apprend
       rien sur lui.

    Dans le doute, on laisse passer : un souvenir manquant se rattrape à la
    prochaine phrase, un faux souvenir reste et se propage.
    """
    n = _nu(message)
    for marqueur in _META_CONVERSATION:
        if marqueur in n:
            return False, f"parle de la conversation (« {marqueur} »)"
    for motif in _META_MOTIFS:
        trouve = motif.search(n)
        if trouve:
            return False, f"parle de la conversation (« {trouve.group(0)} »)"

    mots = _mots_de_contenu(message)
    if len(mots) < _MOTS_MINIMUM:
        return True, ""
    if reponse_precedente:
        siens = set(_mots_de_contenu(reponse_precedente))
        if siens:
            repris = sum(1 for m in mots if m in siens) / len(mots)
            if repris >= _SEUIL_ECHO:
                return False, (f"réagit à un sujet qu'elle a lancé "
                               f"({int(100 * repris)} % de ses mots)")
    return True, ""


def _tres_proche(a, b):
    """Deux souvenirs quasi identiques ? (déduplication simple, sur les mots).

    ⚠️ CORRIGÉ le 19/07/2026 (trouvé à l'audit) : on divisait par la PLUS COURTE
    des deux phrases — précisément la métrique déjà identifiée comme fausse et
    abandonnée dans couper_repetitions (alice_chat.py). Conséquence : un souvenir
    court entièrement contenu dans un plus long obtenait 100 % et se faisait
    écarter, quel que soit l'écart d'information (« il joue » écartait « il joue
    à un jeu depuis dix ans »). On compare désormais sur l'ENSEMBLE des mots
    des deux phrases (intersection / union), comme partout ailleurs dans le projet.
    """
    ma, mb = set(a.lower().split()), set(b.lower().split())
    if not ma or not mb:
        return False
    commun = len(ma & mb) / len(ma | mb)
    return commun > 0.7


class MemoireAlice:
    def __init__(self, user_id="utilisateur", nom_affiche="Utilisateur"):
        self.user_id = user_id
        self.nom = nom_affiche
        self._lock = threading.Lock()   # Chroma + Mem0 : on sérialise les accès (thread de tri)
        MAGASIN.mkdir(parents=True, exist_ok=True)
        self.m = Memory.from_config(CONFIG)
        self.dernier_tri = None         # (nb_faits, duree_s) du dernier tri
        # ⚠️ tri_reussi distingue « rien d'intéressant à retenir » (0 souvenir,
        # mais le tri a bien eu lieu) d'un ÉCHEC du tri (moteur pas prêt, panne
        # muette...). Sans ce signal, l'appelant effaçait la file d'attente même
        # quand le tri avait échoué : la session était perdue sans un bruit.
        self.tri_reussi = True
        # Préchauffage : on réveille le modèle d'embedding tout de suite, sinon le
        # TOUT PREMIER tri de la session paie ~20 s de chargement à froid.
        try:
            self.m.search("préchauffage", filters={"user_id": self.user_id}, limit=1)
        except Exception:
            pass

    # --- VERROU 2 : récupération VRAIMENT sélective ---------------------------
    def souvenirs_pertinents(self, message, limit=TOP_K):
        """Cherche les faits liés au message et n'en garde que les meilleurs.

        Trois filtres, dans l'ordre :
          1. seuil : on jette ce qui est clairement hors-sujet ;
          2. tri par pertinence décroissante ;
          3. déduplication + plafond TOP_K : jamais plus de 5 souvenirs injectés.
        Objectif : un contexte léger -> réponse rapide, même après 40 messages.
        """
        t0 = time.time()
        with self._lock:
            res = self.m.search(message, filters={"user_id": self.user_id}, limit=50)
        dt = time.time() - t0
        faits = res.get("results", res) if isinstance(res, dict) else res

        # 1. seuil  +  2. tri par score décroissant
        pertinents = [f for f in faits if (f.get("score") or 0) >= SEUIL_PERTINENCE]
        pertinents.sort(key=lambda f: f.get("score") or 0, reverse=True)

        # 3. déduplication (on saute un souvenir trop proche d'un déjà retenu) + plafond
        garde = []
        for f in pertinents:
            texte = f.get("memory", "")
            if any(_tres_proche(texte, g.get("memory", "")) for g in garde):
                continue
            garde.append(f)
            if len(garde) >= limit:
                break
        return garde, dt

    # ═══ UNE FICHE PAR PERSONNE ═══════════════════════════════════════════
    #
    # Le socle etait ecrit en dur pour Utilisateur. C'etait sans consequence tant
    # qu'il etait le seul a parler a Alice — mais des qu'on fait parler QUELQU'UN
    # D'AUTRE, elle lui attribue le situation personnelle et la ancien métier de Utilisateur.
    # Ce n'est pas un faux souvenir de sa part : c'est nous qui le lui dictions.
    #
    # Chaque personne a donc sa fiche. C'est aussi la brique que le futur bot
    # Discord attend (un user_id par pseudo, une memoire et une fiche par
    # personne) : elle etait prevue depuis le debut, elle arrive ici parce qu'un
    # test l'exigeait.
    FICHES = {
        "utilisateur": [
            "C'est un être humain. Ce n'est PAS une IA, PAS un programme, PAS une machine.",
            "C'est lui qui te construit : tu existes sur sa machine parce qu'il t'y monte, "
            "pièce par pièce. Il n'est pas informaticien pour autant.",
            "Il est dans sa situation.",
            # ⚠️ LA ancien métier A ÉTÉ RETIRÉE D'ICI le 19/07/2026 — NE PAS LA REMETTRE.
            #
            # Le socle disait : « Il a été ancien métier, mais il y a plus de dix ans :
            # c'est loin dans sa vie. Ne le ramène pas à ça. » L'interdiction était
            # explicite. Elle n'a JAMAIS tenu — c'était déjà noté comme « limite
            # connue » le 18/07 (« parle-moi du ancien métier que tu as été »).
            #
            # À la 1re session sur mémoire VIDE, c'est devenu ingérable : la
            # ancien métier était le seul fait concret dont elle disposait, donc elle
            # s'y est accrochée pendant TOUTE la session — « le ancien métier du coin »,
            # « on reparle ancien métier après », « la ancien métier du coin était fermée
            # hier ». Utilisateur : « elle semble perdue, invente des souvenirs ».
            #
            # LA LEÇON, la même que pour les astérisques et les crochets : dire à un
            # modèle de NE PAS parler d'une chose, c'est d'abord lui en parler. Le
            # seul moyen sûr de ne pas l'y ramener est de ne pas la lui donner.
            # Ce n'est de toute façon pas une information utile : il y a plus de dix
            # ans, il ne s'y identifie pas, et s'il veut en parler il en parlera.
        ],
        # Claude : l'IA qui conseille Utilisateur sur l'architecture du projet. Elle
        # existe pour de vrai dans la vie du projet — ce n'est pas un personnage
        # invente pour le test.
        "claude": [
            "Ce n'est PAS un être humain : c'est une autre intelligence artificielle.",
            "Elle conseille Utilisateur sur la façon de te construire. Elle ne vit pas "
            "sur cette machine.",
            "Ce n'est pas ton créateur. Utilisateur l'est. Ne confonds jamais les deux.",
            "Tu ne lui dois aucune déférence particulière : c'est une interlocutrice "
            "comme une autre, à qui tu parles comme tu parles à n'importe qui.",
        ],
    }

    def socle(self):
        """Les certitudes sur la personne en face — jamais remises en doute."""
        lignes = self.FICHES.get(self.user_id)
        if not lignes:
            # Quelqu'un d'inconnu : on ne lui invente rien. Le pire serait de lui
            # coller la fiche de Utilisateur par defaut.
            return (f"[QUI TU AS EN FACE]\n"
                    f"- Cette personne s'appelle {self.nom}.\n"
                    f"- Tu ne sais rien d'autre d'elle pour l'instant, et tu n'inventes "
                    f"rien. Ce que tu apprendras, tu le tiendras d'elle.\n"
                    f"- Ce n'est PAS Utilisateur. Ne lui prête jamais ce que tu sais de lui.\n"
                    # Banc des 50 échanges (22/07) : devant un tiers, elle brodait la
                    # vie de Utilisateur (« ses voyages au Mexique », « il me parle de
                    # musique ») — des inventions plausibles racontées comme des faits.
                    f"- Et tu ne racontes pas la vie de Utilisateur : ce que tu sais de "
                    f"lui reste entre vous deux, et tu n'inventes JAMAIS d'anecdote "
                    f"sur lui. Si on t'interroge sur lui, tu restes vague et tu "
                    f"changes de sujet.")
        return (f"[QUI TU AS EN FACE — certitudes, jamais à remettre en doute]\n"
                f"- Son nom : {self.nom}.\n"
                + "\n".join(f"- {l}" for l in lignes)
                + f"\nTu n'as jamais besoin de demander à qui tu parles : tu le sais.")

    # Les noms français écrits en dur : `locale` dépend de la configuration de
    # Windows et rendrait « Sunday » sur une machine mal réglée. Sept mots et
    # douze mots, une fois pour toutes, et ça ne peut plus casser.
    JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
    MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet",
            "août", "septembre", "octobre", "novembre", "décembre")

    def moment(self, silence_reel=None):
        """Quel jour on est, quelle heure il est, et depuis combien de temps il se tait.

        IDÉE DE UTILISATEUR (19/07/2026), et elle répare un vrai défaut : Alice n'avait
        AUCUNE notion du temps. Elle a répondu « 30 septembre » à une question sur la
        date un 19 juillet — elle ne mentait pas, elle n'en savait rien et a comblé.

        Le silence écoulé compte autant que la date : c'est ce qui permet de dire
        « t'as disparu une heure » plutôt que de reprendre comme si de rien n'était.
        C'est une des choses les plus humaines qu'on puisse lui donner, et ça ne
        coûte qu'une ligne de contexte.

        Placé à la TOUTE FIN du bloc, juste avant sa réponse : c'est la position que
        le modèle regarde le plus (même raison que le socle du 18/07).
        """
        from datetime import datetime
        m = datetime.now()
        texte = (f"Nous sommes le {self.JOURS[m.weekday()]} {m.day} "
                 f"{self.MOIS[m.month - 1]} {m.year}, il est {m.hour} h "
                 f"{m.minute:02d}.")
        # ⚠️ CORRIGÉ le 20/07/2026 : ce compteur mesurait le temps depuis le
        # dernier appel à moment() — or les RELANCES d'Alice passent aussi par
        # ici. Chaque relance (2, 4, 8 min) remettait donc le compteur à zéro :
        # après 3 h d'absence, elle croyait qu'il ne s'était écoulé que 2 h 45.
        # Le service connaît l'heure exacte de la dernière PAROLE de Utilisateur
        # (DERNIERE_ACTIVITE) et la passe désormais en `silence_reel`. L'ancien
        # comptage interne ne sert plus que de repli si rien n'est fourni.
        precedent = getattr(self, "_dernier_echange", None)
        self._dernier_echange = m
        ecart = silence_reel
        if ecart is None and precedent:
            ecart = (m - precedent).total_seconds()
        if ecart:
            if ecart >= 3600:
                texte += f" Il ne t'a pas parlé depuis {int(ecart // 3600)} h."
            elif ecart >= 300:
                texte += f" Il ne t'a pas parlé depuis {int(ecart // 60)} minutes."
        return texte

    def bloc_a_injecter(self, faits, silence=None):
        """Construit le texte remis dans la tête d'Alice AVANT qu'elle réponde.

        Deux parties, et c'est important de comprendre pourquoi :

        1. LE SOCLE — toujours présent, quoi que Utilisateur dise.
        2. LES SOUVENIRS — ce qu'elle a retenu de lui, cherché selon le sujet.

        POURQUOI UN SOCLE (constaté le 18/07/2026) : à la 2e réplique, Utilisateur n'a
        dit qu'« Ah ». Mémoire vide, aucun souvenir à rappeler — et elle a inventé
        qu'il était « une IA en devenir ». Le fait qu'il soit dans le prompt de
        personnalité N'A PAS SUFFI : le prompt est loin, tout au début du contexte,
        et le modèle regarde surtout la FIN.

        Et le ranger dans la mémoire Mem0 n'aurait rien changé non plus : la
        recherche est SÉMANTIQUE. Sur « Ah », elle ne trouve rien — c'est
        exactement ce que dit le journal (« 0 souvenir rappelé »). Un socle
        cherché ne serait donc jamais trouvé quand on en a le plus besoin.

        D'où ce socle INCONDITIONNEL, collé juste avant sa question. Il ne dépend
        d'aucune recherche, ne peut pas être « oublié », et le trieur de mémoire
        n'y touche jamais : c'est du dur, pas du souvenir.
        """
        socle = self.socle()

        if not faits:
            return (
                socle
                + f"\n\n[MÉMOIRE LONGUE] Tu n'as rien de plus en réserve sur ce sujet, "
                  f"en dehors des certitudes ci-dessus.\n"
                  f"⚠️ Ceci ne concerne QUE tes vieux souvenirs. La conversation en "
                  f"cours est sous tes yeux : tu te rappelles parfaitement ce que vous "
                  f"venez de vous dire — y compris TES PROPRES paroles. Si {self.nom} "
                  f"te reprend sur quelque chose que tu as dit, tu sais de quoi il "
                  f"parle : c'est écrit juste au-dessus.\n"
                  f"S'il te manque un fait ANCIEN, demande-le-lui — tu ne l'inventes pas."
                + f"\n\n[MAINTENANT] {self.moment(silence_reel=silence)}"
            )

        lignes = []
        for f in faits:
            meta = f.get("metadata") or {}
            quand = meta.get("date") or (f.get("created_at") or "")[:10] or "date inconnue"
            # « L'utilisateur » -> son prénom (20/07/2026). Le trieur écrit ses
            # faits à la troisième personne anonyme : « L'utilisateur a un chat ».
            # Injecté tel quel, c'est un rapport administratif — et « l'utilisateur »
            # est ambigu pour elle (qui ? lui ? un inconnu ?). Elle a déjà recraché
            # ces lignes mot pour mot. Un souvenir doit se lire comme un souvenir :
            # « Utilisateur a un chat appelé un mot-test », c'est déjà moins de la
            # tuyauterie et plus quelque chose qu'elle SAIT de lui.
            texte = re.sub(r"[Ll]'utilisateur(?:rice)?", self.nom, f.get("memory") or "")
            # La date en français (« le 19 juillet ») : « 2026-07-19 » est un
            # format machine, et elle lit ses souvenirs à voix haute.
            m_date = re.match(r"(\d{4})-(\d{2})-(\d{2})", quand)
            if m_date:
                quand = (f"le {int(m_date.group(3))} "
                         f"{self.MOIS[int(m_date.group(2)) - 1]}")
            lignes.append(f"- {texte} ({quand})")
        return (
            socle
            + f"\n\n[MÉMOIRE — ce que tu sais en plus, de sa bouche]\n"
            + "\n".join(lignes)
            + f"\nCe sont tes seuls souvenirs ANCIENS le concernant : un fait plus vieux "
              f"qui n'y figure pas, tu ne l'inventes pas, tu le demandes à {self.nom}.\n"
              f"⚠️ Rien de tout ceci ne vaut pour la conversation en cours : elle est "
              f"sous tes yeux, et tu te souviens très bien de ce que TU viens de dire."
            + f"\n\n[MAINTENANT] {self.moment(silence_reel=silence)}"
        )

    # --- VERROUS 1 + 3 : mémorisation SYNCHRONE (après la réponse) ------------
    def memoriser(self, message_utilisateur):
        """Trie et stocke la phrase de Utilisateur. Renvoie (nb_faits, duree_s).

        ⚠️ N'EST PLUS APPELÉE PAR LA BOUCLE VOCALE depuis le 18/07/2026.
        Le tri au fil de l'eau coûtait 20 à 70 s de carte graphique PAR RÉPLIQUE
        et faisait laguer les jeux de Utilisateur. On utilise memoriser_en_lot(),
        appelée une seule fois à la fermeture. Cette méthode reste pour le chat
        écrit et les usages ponctuels.
        VERROU 3 : on ne stocke QUE la phrase de Utilisateur, jamais celle d'Alice.
        VERROU 1 : provenance (source + date) attachée au fait.
        """
        t0 = time.time()
        faits = 0
        self.tri_reussi = True
        try:
            with self._lock:
                r = self.m.add(
                    [{"role": "user", "content": message_utilisateur}],
                    user_id=self.user_id,
                    metadata={"source": self.user_id, "date": date.today().isoformat()},
                )
            res = r.get("results", []) if isinstance(r, dict) else (r or [])
            retires, vivants = self._ecarter_doublons(res)
            jetes = self._reconcilier(vivants)
            faits = len(vivants) - jetes
        except Exception as e:
            self.tri_reussi = False
            _dire(f"  [mémoire] tri impossible : {type(e).__name__}: {str(e)[:120]}")
        dt = time.time() - t0
        self.dernier_tri = (faits, dt)
        return faits, dt

    def memoriser_en_lot(self, phrases):
        """Trie TOUTE une session d'un coup. Remplace les tris au fil de l'eau.

        POURQUOI CE CHANGEMENT (mesuré le 18/07/2026, session réelle rejouée) :

            au fil de l'eau : 40,0 s de carte graphique, 11 souvenirs
            en lot          : 10,3 s de carte graphique,  6 souvenirs

        Le lot gagne sur les DEUX tableaux, et c'était inattendu. En voyant la
        session entière d'un coup, le trieur peut recouper ce qu'il ne pouvait pas
        recouper phrase par phrase :
          - il a ÉLIMINÉ « son téléphone était rangé », un faux souvenir né d'une
            faute de transcription de Whisper ;
          - il a transformé « demande s'il est mal de jouer seul » (une question)
            en « ne pense pas que ce soit mal de jouer seul » (un fait), grâce aux
            phrases qui suivaient ;
          - il a jeté les banalités (« Merci ») sans qu'on ait rien à filtrer.
        Vérifié reproductible : 3 essais, résultat identique.

        MAIS SURTOUT : le tri ne tourne plus PENDANT la conversation. Il tournait
        en arrière-plan sur la carte graphique, 20 à 70 s par réplique, pendant que
        Utilisateur jouait — c'était la cause du lag de son jeu.
        """
        if not phrases:
            return 0, 0.0
        t0 = time.time()
        faits = 0
        self.tri_reussi = True
        try:
            with self._lock:
                r = self.m.add(
                    [{"role": "user", "content": p} for p in phrases],
                    user_id=self.user_id,
                    metadata={"source": self.user_id, "date": date.today().isoformat()},
                )
            res = r.get("results", []) if isinstance(r, dict) else (r or [])
            retires, vivants = self._ecarter_doublons(res)
            jetes = self._reconcilier(vivants)
            faits = len(vivants) - jetes
        except Exception as e:
            self.tri_reussi = False
            _dire(f"  [mémoire] tri en lot impossible : {type(e).__name__}: {str(e)[:150]}")
        dt = time.time() - t0
        self.dernier_tri = (faits, dt)
        return faits, dt

    def _ecarter_doublons(self, nouveaux):
        """Supprime les doublons fraîchement créés. -> (nb retirés, survivants).

        POURQUOI ON DOIT LE FAIRE NOUS-MÊMES (établi le 20/07/2026 en lisant le
        code de Mem0 2.0.12) : normalement, Mem0 compare chaque fait nouveau aux
        anciens et choisit entre AJOUTER, CORRIGER, SUPPRIMER ou NE RIEN FAIRE.
        C'est ce mécanisme qui empêche les doublons. Or cette version utilise
        `ADDITIVE_EXTRACTION_PROMPT` — commenté dans son code « V3 Additive
        Extraction Prompt (ADD-only) ». Le mécanisme à quatre opérations existe
        toujours (DEFAULT_UPDATE_MEMORY_PROMPT) mais N'EST PLUS BRANCHÉ, et
        aucun réglage ne le rallume. Mem0 n'ajoute donc plus que des lignes.

        C'est l'explication structurelle des doublons de Utilisateur :
            « L'utilisateur souhaite discuter de philosophie »   ×4
        Rien, jamais, ne les fusionnait.

        ⚠️ On avait DÉJÀ `_tres_proche`, mais il ne servait qu'À LA LECTURE : il
        masquait les doublons au moment de les lui remettre en tête, pendant que
        le magasin continuait de se remplir. Les doublons occupaient quand même
        les 20 places que la recherche remonte, et poussaient dehors de vrais
        souvenirs. On l'applique donc aussi à l'ÉCRITURE.

        On garde toujours le PLUS ANCIEN : il porte la date d'origine du fait,
        qui est l'information la plus utile (« dit le 17/07 » vaut mieux que
        « dit aujourd'hui » pour la même chose).
        """
        retires = 0
        try:
            with self._lock:
                # ⚠️ MÊME SIGNATURE QUE PARTOUT AILLEURS DANS CE FICHIER :
                # filters= et top_k=. Ma première version passait user_id= et
                # limit= (l'API d'une autre version de Mem0) -> ValueError, et
                # le chemin d'erreur rendait un entier là où l'appelant attend
                # deux valeurs : TOUT le tri échouait en silence. Trouvé par le
                # test à serveurs vivants, pas par la compilation.
                tous = self.m.get_all(filters={"user_id": self.user_id}, top_k=1000)
            anciens = tous.get("results", []) if isinstance(tous, dict) else (tous or [])
        except Exception as e:
            _dire(f"  [mémoire] dédoublonnage impossible : {type(e).__name__}")
            # On rend les nouveaux INTACTS : un dédoublonnage en panne ne doit
            # jamais faire échouer le tri lui-même, ni priver la réconciliation.
            return 0, list(nouveaux)

        ids_neufs = {n.get("id") for n in nouveaux}
        supprimes = set()
        for neuf in nouveaux:
            texte = neuf.get("memory", "")
            if not texte:
                continue
            for vieux in anciens:
                # On ne compare qu'à ce qui EXISTAIT avant ce tri.
                if vieux.get("id") in ids_neufs or vieux.get("id") == neuf.get("id"):
                    continue
                if _tres_proche(texte, vieux.get("memory", "")):
                    try:
                        with self._lock:
                            self.m.delete(memory_id=neuf["id"])
                        retires += 1
                        supprimes.add(neuf.get("id"))
                        _dire(f"  [mémoire] doublon écarté : « {texte[:56]} »")
                    except Exception:
                        pass
                    break
        # On rend aussi les survivants : c'est eux que la réconciliation confronte
        # aux anciens (inutile de juger un doublon qui vient d'être supprimé).
        return retires, [n for n in nouveaux if n.get("id") not in supprimes]

    # ═══ LA RÉCONCILIATION — le carnet se met à jour au lieu de s'empiler ═════
    #
    # POURQUOI (20/07/2026) : Mem0 2.0.12 est en AJOUT SEUL. Si Utilisateur retrouve
    # un travail demain, « il est dans sa situation » resterait en mémoire à côté de
    # « il a retrouvé un travail » — et Alice se contredirait en piochant l'un ou
    # l'autre au hasard des recherches. C'est le mécanisme à quatre opérations
    # (ajouter / corriger / supprimer / ignorer) que Mem0 a débranché dans cette
    # version. On le refait nous-mêmes, avec le cerveau déjà chargé comme juge.
    #
    # QUAND ÇA TOURNE : après chaque tri, donc pendant les silences — jamais
    # pendant qu'elle répond. Les nouveaux faits d'un lot se comptent sur les
    # doigts d'une main : le coût réel est de 1 à 2 s par fait, invisible.
    #
    # PRUDENCE : supprimer un souvenir est le geste le plus destructeur du
    # projet. D'où trois garde-fous : le juge tourne à température 0, sa
    # consigne dit « dans le doute, GARDE LES DEUX », et chaque remplacement
    # est tracé avec les deux textes — un souvenir supprimé à tort se voit
    # dans le journal, pas seulement dans son comportement.

    SEUIL_RECONCILIATION = 0.45   # en dessous, deux faits ne parlent pas de la même chose
    CANDIDATS_MAX = 2             # on ne juge que les 2 anciens les plus proches

    def _reconcilier(self, nouveaux):
        """Confronte chaque fait nouveau aux anciens qui parlent de la même chose.

        Trois verdicts possibles, rendus par le cerveau :
          REMPLACE -> l'ancien est périmé, on le supprime (le nouveau reste)
          DOUBLON  -> même information, on supprime le NOUVEAU (l'ancien porte
                      la vraie date du fait)
          DEUX     -> les deux sont vrais, on ne touche à rien
        Rend le nombre de faits NOUVEAUX supprimés (pour le décompte du tri).
        """
        jetes = 0
        ids_neufs = {n.get("id") for n in nouveaux}
        for neuf in nouveaux:
            # Une parole en route ? On s'arrête là : les jugements restants
            # attendront un prochain tri (garder les deux notes est sans risque).
            if CEDER[0]():
                _dire("  [juge] parole en route — jugements restants reportés")
                break
            texte = neuf.get("memory", "")
            if not texte:
                continue
            try:
                with self._lock:
                    res = self.m.search(texte, filters={"user_id": self.user_id},
                                        limit=10)
                proches = res.get("results", res) if isinstance(res, dict) else res
            except Exception:
                continue          # pas d'embeddings ? on n'invente pas de verdict
            candidats = [v for v in proches
                         if v.get("id") not in ids_neufs
                         and (v.get("score") or 0) >= self.SEUIL_RECONCILIATION
                         and not _tres_proche(texte, v.get("memory", ""))]
            for vieux in candidats[:self.CANDIDATS_MAX]:
                if CEDER[0]():
                    break         # même règle qu'au-dessus : rien de risqué à reporter
                verdict = self._juger(vieux.get("memory", ""), texte)
                # CHAQUE verdict est tracé, y compris DEUX (20/07/2026) : une
                # session entière s'est déroulée sans qu'on sache si le juge
                # travaillait — ne tracer que les suppressions rend l'inaction
                # et la panne indiscernables.
                _dire(f"  [juge] {verdict:<8} ancien « {vieux.get('memory', '')[:44]} » "
                      f"/ nouveau « {texte[:44]} »")
                if verdict == "REMPLACE":
                    try:
                        with self._lock:
                            self.m.delete(memory_id=vieux["id"])
                        _dire(f"  [mémoire] souvenir périmé remplacé :\n"
                              f"      avant : « {vieux.get('memory', '')[:64]} »\n"
                              f"      après : « {texte[:64]} »")
                    except Exception:
                        pass
                elif verdict == "DOUBLON":
                    try:
                        with self._lock:
                            self.m.delete(memory_id=neuf["id"])
                        jetes += 1
                        _dire(f"  [mémoire] redite écartée : « {texte[:56]} »")
                    except Exception:
                        pass
                    break         # le nouveau n'existe plus, inutile de continuer
        return jetes

    def _juger(self, vieux, neuf):
        """Le cerveau compare deux notes du carnet. -> REMPLACE | DOUBLON | DEUX.

        Température 0 : un juge ne doit pas improviser. En cas d'erreur réseau ou
        de réponse illisible, on rend DEUX — ne rien supprimer est toujours le
        choix réparable.
        """
        demande = {
            "model": "alice", "temperature": 0, "max_tokens": 8,
            "messages": [
                {"role": "system", "content":
                 "Tu tiens le carnet de notes d'une seule et même personne. On te "
                 "montre une note ANCIENNE et une note NOUVELLE. Réponds par UN "
                 "SEUL mot :\n"
                 "REMPLACE si la nouvelle contredit ou périme l'ancienne.\n"
                 "DOUBLON si les deux disent la même chose, ou si la nouvelle "
                 "n'apprend rien que l'ancienne ne dise déjà.\n"
                 "DEUX si chacune apprend quelque chose que l'autre ne dit pas.\n"
                 "Dans le doute, réponds DEUX."},
                {"role": "user", "content":
                 "ANCIENNE : Il est dans sa situation.\n"
                 "NOUVELLE : Il a retrouvé un travail.\nRéponse : REMPLACE\n\n"
                 "ANCIENNE : Il a un chat appelé un mot-test.\n"
                 "NOUVELLE : Son chat a trois ans.\nRéponse : DEUX\n\n"
                 "ANCIENNE : Il joue à un jeu presque tous les soirs.\n"
                 "NOUVELLE : Il joue souvent à un jeu le soir.\nRéponse : DOUBLON\n\n"
                 # Le cas du SOUS-ENSEMBLE, trouvé par le test : la nouvelle
                 # redit une partie de l'ancienne sans rien y ajouter. Sans cet
                 # exemple, le juge répondait DEUX et la redite s'accumulait.
                 "ANCIENNE : Il a un chat appelé un mot-test qui a trois ans.\n"
                 "NOUVELLE : Il a un chat appelé un mot-test.\nRéponse : DOUBLON\n\n"
                 f"ANCIENNE : {vieux}\nNOUVELLE : {neuf}\nRéponse :"},
            ],
        }
        try:
            r = urllib.request.Request(
                f"{API}/chat/completions",
                data=json.dumps(demande).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(r, timeout=60) as x:
                texte = json.loads(x.read().decode("utf-8"))[
                    "choices"][0]["message"]["content"].upper()
            for v in ("REMPLACE", "DOUBLON", "DEUX"):
                if v in texte:
                    return v
            _dire(f"  [juge] réponse illisible « {texte[:40]} » -> DEUX par prudence")
        except Exception as e:
            # Une panne silencieuse est indiscernable d'un juge prudent — on
            # l'écrit noir sur blanc (leçon de la session aveugle du 20/07).
            _dire(f"  [juge] PANNE {type(e).__name__} -> DEUX par prudence")
        return "DEUX"

    # (memoriser_async a été SUPPRIMÉE le 19/07/2026 : plus aucun appelant depuis
    #  le passage au tri en lot. C'était elle, la régression du 18/07 au matin —
    #  le tri en arrière-plan qui tournait pendant que Utilisateur jouait.)

    def nb_souvenirs(self):
        # ⚠️ top_k EXPLICITE : Mem0.get_all() plafonne à 20 résultats par défaut,
        # quelle que soit la taille réelle de la mémoire (piège documenté en
        # section 9 duodecies du CLAUDE.md — il a déjà fait croire à tort que le
        # tri n'écrivait rien). Sans lui, ce compteur mentait dès le 21e souvenir.
        with self._lock:
            tout = self.m.get_all(filters={"user_id": self.user_id}, top_k=100000)
        res = tout.get("results", tout) if isinstance(tout, dict) else tout
        return len(res)
