# -*- coding: utf-8 -*-
"""
LE CHAT ÉCRIT — parler à Alice au clavier, sans micro ni voix.

Ce script n'est PAS un test scripte. Utilisateur discute librement, comme il veut.
Le script se contente de :
  - lancer le cerveau via moteur.py (llama.cpp, les MEMES reglages que la boucle)
  - transmettre ce qu'il tape, afficher la reponse
  - TOUT ecrire dans tests\logs\ au fur et a mesure (rien n'est perdu si ca plante)
  - eteindre le cerveau a la sortie (la VRAM est liberee)

Usage : double-clic sur ECRIRE_A_ALICE.bat
Ce fichier fournit AUSSI les filtres partages avec le service vocal
(nettoyer_pour_voix, retirer_tic_ouverture, limiter_longueur, PARAMS).

⚠️ RÉPARÉ le 19/07/2026 (audit) : depuis le passage du cerveau sur llama.cpp
(18/07 au soir), ce script chargeait ENCORE le modele dans LM Studio... tout en
envoyant ses questions au port 8095 de llama.cpp, ou personne n'ecoutait. Le
chat ecrit etait donc casse, et occupait 15 Go pour rien. Meme lecon que la
memoire le meme soir : quand on deplace un service, chercher QUI D'AUTRE lui
parlait. Il passe desormais par moteur.py — plus aucune divergence possible.
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# La mémoire longue vit dans la bulle "memoire\" (Mem0 + Chroma). Ce script tourne
# desormais sous memoire\venv, donc l'import fonctionne.
sys.path.insert(0, os.path.join(PROJET, r"memoire"))
from memoire_alice import MemoireAlice, portier  # noqa: E402


def retirer_tic_ouverture(txt):
    """Garde-fou déterministe contre le tic d'ouverture « Ah… ».

    Le modèle Mistral ouvre presque toujours par « Ah… X » en jeu de rôle, et
    AUCUNE consigne du prompt n'arrive à l'en empêcher (testé : 8 réponses sur 8
    malgré une interdiction en tête de prompt). On coupe donc l'interjection au
    montage, comme on coupe les astérisques.
    « Ah... Le silence. » -> « Le silence. »   « Ah oui ? Tu... » -> « Tu... »
    """
    # ⚠️ Le tiret CADRATIN « — » manquait dans cette liste : « Ah — donc… » passait
    # au travers, et le tic est revenu par cette porte le 18/07/2026. Les trois
    # tirets sont désormais couverts : — (cadratin), – (demi-cadratin), - (trait).
    nouv = re.sub(r"^\s*ah\b\s*(oui|non|bon|tiens|bien)?\s*[…\.\,\!\?:—–-]+\s*", "",
                  txt, count=1, flags=re.IGNORECASE)
    # 2e passe — « Ah bon sang », « Ah tiens tu reviens » : AUCUNE ponctuation après
    # le « Ah ». On enlève alors UNIQUEMENT l'interjection. Sans cette distinction,
    # la 1re passe avalait le « bon » de « bon sang » et laissait « sang, tu
    # reviens » (régression constatée puis corrigée le 18/07/2026).
    if nouv == txt:
        nouv = re.sub(r"^\s*ah\s+(?=\w)", "", txt, count=1, flags=re.IGNORECASE)
    if nouv and nouv != txt:
        nouv = nouv[0].upper() + nouv[1:]
    return nouv or txt


def nettoyer_pour_voix(txt):
    """Retire tout ce qui ne doit pas etre PRONONCE par la synthese vocale.

    Le cerveau glisse parfois une didascalie entre asterisques (« *claque* »,
    « *rit* ») malgre l'interdiction du prompt. XTTS lirait « asterisque claque
    asterisque » a voix haute. Ce filtre est le garde-fou robuste : meme si le
    modele derape une fois sur cinquante, rien de bizarre ne sera jamais dit.
    On enleve aussi le gras markdown et on remet les espaces d'aplomb pour ne
    pas laisser de double espace ni d'espace avant une virgule.
    """
    t = re.sub(r"\*{1,3}[^*\n]*?\*{1,3}", "", txt)   # *didascalie* et **gras**
    t = t.replace("*", "")                             # asterisques isoles restants
    # Les apartes entre parentheses : le prompt les interdit, elle en glisse quand
    # meme (2 fois sur 50 au test v7). Mesure du 18/07 : un aparte DOUBLE presque la
    # duree de la replique a l'oral, et c'est une convention d'ECRIT qui n'a aucun
    # sens dit a voix haute. On ne garde que les parentheses courtes (une precision
    # d'un ou deux mots passe encore).
    t = re.sub(r"\s*\([^)]{12,}\)", "", t)
    # L'écriture inclusive, prononcée telle quelle par la voix — banc des 50
    # échanges du 22/07 : « Fièr.e », « un.e noctambule », « gros.se
    # joueur/jeuse » -> la synthèse dirait « fièr point e ». On garde le
    # masculin (le point n'est reconnu que COLLÉ à un suffixe court en
    # minuscules : « fière. Et toi » — point puis espace — n'est pas touché).
    t = re.sub(r"(\w)\.(?:es?|se|ne|le|te|ère|rice|euse)s?\b(?=[\s,.!?…]|$)", r"\1", t)
    t = re.sub(r"(\w{3,})/(?:se|es?|euse|jeuse|trice|ère|rice)s?\b", r"\1", t)
    t = re.sub(r"[ \t]{2,}", " ", t)                   # espaces multiples -> un seul
    t = re.sub(r"[ \t]+([,.)])", r"\1", t)             # pas d'espace avant , . ) (typo FR)
    t = re.sub(r"\n{3,}", "\n\n", t)                   # lignes vides en trop
    return t.strip()


MOIGNON = 22        # en dessous, une réponse coupée n'est plus une réponse


def limiter_longueur(txt, plafond=85, cible=72):
    """Filet de sécurité contre les pavés — coupe TOUJOURS sur une phrase entière.

    ⚠️ DESSERRÉ de 40 à 65 mots le 19/07/2026 (aujourd'hui : plafond 85,
    cible 72 — les valeurs par défaut ci-dessus), correction importante.
    Utilisateur, après une longue session : « j'ai l'impression d'avoir un tout petit
    LLM sous la main ». Il avait raison, et ce n'était pas le modèle — c'était CE
    FILTRE. Mesuré sur sa session : 80 % de ses réponses raccourcies, 36 % du texte
    perdu. Le pire cas, une vraie réplique de 49 mots rendue en 9 :
        elle disait : « Antois hein ? T'es sûr de ton histoire toi... Mais passons :
        t'as trié mes vieilleries comme un brocanteur et tu me le dis avec une joie
        d'enfant qui trouve deux euros sous son canapé. Tu m'expliques en quoi c'est
        mieux maintenant, ou je dois deviner toute seule ? »
        il entendait : « Antois hein ? T'es sûr de ton histoire toi... »
    La 2e phrase faisait 28 mots ; 9 + 28 dépassait la cible de 34, donc elle sautait
    ENTIÈREMENT. Il ne restait qu'un moignon. C'est exactement la sensation d'un
    petit modèle : des débuts de phrases sans suite.

    ⚠️ POURQUOI LE PLAFOND SERRÉ N'A PLUS DE RAISON D'ÊTRE. Il avait DEUX
    justifications, toutes deux mesurées, et toutes deux devenues caduques :
      1. « 48 mots = 16 s de synthèse vocale » -> c'était XTTS. Piper fabrique une
         réplique entière en 0,13 s (facteur 0,028x). La voix ne coûte plus rien.
      2. « 78 mots = 35 s de cerveau, et son jeu lague » -> c'était la mémoire vive
         saturée, corrigée le 18/07 par --no-mmap. Le cerveau répond en 1,5 s.
    LEÇON, déjà écrite le 19/07 et re-vérifiée ici : une décision prise sur une
    mesure doit être REJOUÉE quand le contexte change. Ce plafond a survécu trois
    jours aux conditions qui l'avaient justifié.

    LE GARDE-FOU CONTRE LES MOIGNONS : si la coupe laisse moins de MOIGNON mots
    alors qu'il y avait une suite, on garde la phrase suivante malgré le dépassement.
    Mieux vaut une réponse un peu longue qu'une phrase amputée : la première se
    supporte, la seconde donne l'impression d'un modèle bête.

    POURQUOI PAS UN PLAFOND DE JETONS (max_tokens) : déjà essayé le 17/07, c'est un
    FAUX AMI — ça coupait 7 réponses sur 8 EN PLEIN MOT. Ici on ne coupe jamais au
    milieu : on garde des phrases entières et on jette le surplus.
    """
    mots = txt.split()
    if len(mots) <= plafond:
        return txt
    phrases = re.split(r"(?<=[\.\!\?…])\s+", txt.strip())
    garde, total = [], 0
    for p in phrases:
        n = len(p.split())
        # On s'arrête si on dépasse la cible — SAUF si ce qu'on a gardé jusqu'ici
        # ne tient pas debout tout seul. Dans ce cas on prend la phrase suivante.
        if garde and total + n > cible and total >= MOIGNON:
            break
        garde.append(p)
        total += n

    # ═══ LA PHRASE-TREMPLIN — 20/07/2026, le reproche n°1 de la session réelle ═══
    # Quand on coupe, la dernière phrase gardée est parfois une AMORCE dont la
    # suite vient d'être supprimée : « Enfin, si ça te fait plaisir... », ou une
    # phrase qui finit sur « : ». À l'oral, ça sonne comme une pensée abandonnée —
    # Utilisateur le lui a reproché TROIS fois (« termine tes phrases »), et elle
    # s'excusait en inventant des raisons (« j'ai oublié en cours de route ») :
    # elle couvrait NOTRE coupe. On ne laisse donc jamais une amorce en dernière
    # position d'une réplique coupée.
    if garde and len(garde) < len(phrases):          # une coupe a bien eu lieu
        while len(garde) > 1:
            fin = garde[-1].strip()
            if (fin.endswith(("...", "…", ":", "—", "–"))
                    or re.match(r"^(et puis|mais bon|enfin|alors|et sinon|bref)\b[^.!?]*$",
                                fin, re.IGNORECASE)):
                garde.pop()
            else:
                break
    return " ".join(garde) if garde else txt


def retirer_fuites_de_consignes(txt):
    """Efface les bouts de ses consignes internes qu'elle recopie dans sa réponse.

    CONSTATÉ au test de 60 échanges du 18/07/2026 — elle a répondu :
        « T'es Claude, la copine numérique de Utilisateur ! [...]
          [TU N'AS AUCUN SOUVENIR DE CE QU'ELLE A DIT] »
        « Claude ! T'es en train d'oublier qui t'est [...] [QUI TU AS EN FACETE — Claude] »
    Ce sont des morceaux du bloc de mémoire qu'on lui glisse avant sa question :
    il est écrit entre crochets ([QUI TU AS EN FACE], [MÉMOIRE]), et elle imite
    le format au lieu de le lire. Dit à voix haute, c'est incompréhensible.

    On efface donc TOUT segment entre crochets. Aucune perte : Alice n'a aucune
    raison légitime d'en employer — elle parle, elle n'annote pas.
    Même principe que le filtre des astérisques : une consigne de prompt ne tient
    pas, un filtre si. C'est le seul levier qui ait jamais tenu sur ce projet.
    """
    t = re.sub(r"\[[^\]]*\]?", "", txt)      # crochet ouvert même non refermé

    # ═══ LA MÉTA-COMMENTAIRE — trouvée dans la 1re session Piper (19/07/2026) ═══
    #
    # Sa mémoire venait d'être remise à zéro. Elle a répondu ceci, et l'a DIT :
    #     « Je parlais d'hier... Et t'es passé où, toi ?
    #       (Note : Alice a mal lu un mot et fait une hypothèse sur le sens.)
    #       ⚠️ Elle ne se trompe pas exprès pour animer la conversation.
    #       Elle n'est JAMAIS volontairement maladroite... »
    #
    # ⚠️ CE TEXTE N'EXISTE NULLE PART DANS LE PROJET — vérifié par recherche sur
    # tout le dossier. Elle ne recopie donc PAS ses consignes : elle en INVENTE
    # de nouvelles, en imitant la FORME du bloc qu'on lui injecte (qui contient
    # lui-même une ligne commençant par ⚠️). Exactement le même mécanisme que les
    # crochets ci-dessus, avec une autre ponctuation.
    #
    # Ces annotations arrivent TOUJOURS APRÈS la vraie réplique — elle finit de
    # parler, puis se met à commenter. On coupe donc à partir du premier marqueur
    # jusqu'à la fin, plutôt que de traquer chaque forme une par une.
    #
    # Aucune perte : Alice parle, elle n'annote pas. Elle n'a aucune raison
    # légitime d'écrire « ⚠️ » ni « (Note : ... ) » au milieu d'une conversation.
    coupe = re.search(r"(?:⚠️|\(\s*(?:Note|Remarque|NB)\s*[:.]|"
                      r"^\s*\((?:Note|Remarque)\b)", t, re.MULTILINE | re.IGNORECASE)
    if coupe:
        t = t[:coupe.start()]

    # Les PUCES du bloc de mémoire, recrachées telles quelles. Trouvé le 19/07 en
    # relisant ses vraies répliques :
    #     « "Pareil", hein ? Et un mot-test ? - L'utilisateur a un chat appelé un mot-test »
    #     « ... - L'utilisateur dit jouer (2026-7-19) - "toujours", par Utilisateur »
    # Ni crochets ni ⚠️ : les deux filtres précédents passaient à côté. C'est le
    # bloc mémoire qui est écrit en puces « - L'utilisateur ... », et elle en
    # reprend le format. Toujours le même mécanisme, une troisième forme.
    # Alice parle de Utilisateur à la DEUXIÈME personne (« tu ») : « - L'utilisateur »
    # n'appartient jamais à sa voix, c'est toujours de la tuyauterie.
    t = re.sub(r"\s*-\s*L'utilisateur\b.*", "", t, flags=re.IGNORECASE | re.DOTALL)

    # ═══ GÉNÉRALISÉ le 21/07/2026 (test de l'humeur) : le FIL épisodique fuit
    # aussi (« - Le soir du lundi 20 juillet, vous avez évoqué une rupture »,
    # « - Tu as l'air DEMANDEUSE ») — d'autres débuts que « L'utilisateur »,
    # même mécanisme. Le prompt lui interdit les listes : TOUTE ligne à puce
    # dans sa bouche est de la tuyauterie recrachée. On retire aussi les
    # séparateurs « --- » (markdown) et son tic « (...) » écrit tel quel.
    t = re.sub(r"(?m)^[ \t]*[-—–][ \t].*$", "", t)
    t = re.sub(r"[ \t]*-[ \t]+(?=[A-ZÀÉÈ])[^.!?\n]{10,}[.!?]?\s*$", "", t)
    t = re.sub(r"(?m)^[ \t]*-{2,}[ \t]*$", "", t)
    t = t.replace("---", " ")
    t = re.sub(r"\(\s*(?:\.{2,}|…)\s*\)", "", t)

    # ═══ LE BLOC RECRACHÉ AU MILIEU DE LA RÉPONSE — 19/07/2026, 5e forme ═══════
    #
    # Elle a produit ceci, et ma vraie réponse était APRÈS le bloc :
    #     Pardon ? T'es en train de me faire un test de Turing là ou quoi...
    #     [CONVERSATION]
    #     — Son nom : Utilisateur.
    #     - C'est un être humain, pas une IA. Il t'a construit sur sa machine.
    #     - Vous êtes le dimanche 19 juillet 2026 à 20h35.
    #     Je suis là, oui — mais c'est toi qui m'as fait exister... Tu vas me dire
    #     comment tu t'es retrouvé dans cette situation ?
    #
    # ⚠️ MA PREMIÈRE PARADE ÉTAIT MAUVAISE : couper « du marqueur jusqu'à la fin »
    # marchait pour les annotations (⚠️, « (Note : ») qui arrivent APRÈS la réplique,
    # mais ici le bloc est AU MILIEU — et la règle jetait sa vraie suite avec.
    # 112 mots rendus en 33, dont la moitié de sa réponse perdue.
    #
    # On retire donc les LIGNES du bloc, sur place, et on garde ce qui suit. Le
    # motif ne vise que les puces qui reprennent le socle : Alice parle, elle ne
    # fait pas de listes à puces sur elle-même.
    t = re.sub(
        r"(?m)^[ \t]*[-—–][ \t]*(?=.*(?:Son nom|être humain|dans sa situation|te construit|"
        r"t'a construit|Vous êtes le|Nous sommes le|dit par|PAS une IA)).*$",
        "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)

    # LES GUILLEMETS QUI EMBALLENT TOUTE LA RÉPLIQUE — 19/07/2026, quatrième forme.
    # Le prompt (depuis le v2) donne des exemples de sa voix. Je les avais écrits entre « », et
    # elle s'est mise à emballer CHACUNE de ses réponses dans des guillemets :
    #     « Dimanche 19. T'étais dans les vapes ces derniers jours ou quoi ? »
    # Encore l'imitation du format, et cette fois c'est le prompt CENSÉ CORRIGER
    # l'imitation qui l'a causée. Les guillemets ont été retirés des exemples ;
    # ce filtre est la ceinture en plus des bretelles.
    # On ne retire QUE la paire qui entoure toute la réplique : une citation au
    # milieu d'une phrase (« "Ouais", hein ? ») est sa signature, on n'y touche pas.
    t = t.strip()
    for ouvre, ferme in (("«", "»"), ('"', '"'), ("“", "”")):
        if len(t) > 2 and t.startswith(ouvre) and t.endswith(ferme) \
                and ouvre not in t[1:-1] and ferme not in t[1:-1]:
            t = t[1:-1].strip()

    # ⚠️ NE PAS AJOUTER ICI un filtre « elle parle d'elle à la 3e personne ».
    # Essayé le 19/07 et RETIRÉ aussitôt : la règle supprimait « Elle est où ta
    # motivation ? » et « Elle est bonne ta blague » — des répliques normales où
    # « elle » désigne tout autre chose. Elle n'apportait rien de plus : la coupe
    # sur le marqueur ci-dessus attrape déjà la totalité du cas réel observé,
    # puisque ces annotations arrivent toujours après un ⚠️ ou un « (Note : ».
    # Le filtre le plus étroit qui règle le problème est le bon.

    t = re.sub(r"[ \t]{2,}", " ", t)
    # ⚠️ Seulement la virgule et le point : en français, l'espace AVANT « ! ? : ; »
    # est correct et doit rester. Ma 1re version rendait « Claude! » au lieu de
    # « Claude ! ». Même règle que dans nettoyer_pour_voix.
    t = re.sub(r"[ \t]+([,.])", r"\1", t)
    return t.strip()


def corriger_appellation(txt):
    """Elle l'appelle « surnom » alors que c'est son pseudo, pas un nom.

    Le prompt l'interdisait explicitement — elle l'a fait 8 fois sur 50 quand meme.
    Enieme confirmation : une interdiction de prompt ne tient pas, un filtre si.
    On remet simplement « Utilisateur » (toujours capitalise : c'est un nom propre).
    """
    return re.sub(SURNOM_A_CORRIGER, "Utilisateur", txt, flags=re.IGNORECASE)


def limiter_le_prenom(txt, precedente="", nom="Utilisateur"):
    """Retire le prénom en ouverture quand la réplique d'avant l'employait déjà.

    MESURÉ sur la 1re vraie session du 20/07/2026 (73 répliques) : « Utilisateur »
    dans 64 % des répliques, EN OUVERTURE 20 fois sur 73, « mon p'tit Utilisateur »
    ×25. Personne ne répète le prénom de son interlocuteur deux phrases sur
    trois — surtout quand ils ne sont que deux dans la pièce.

    La règle est douce et mécanique : si SA réplique précédente employait déjà
    le prénom, celui d'OUVERTURE saute (le vocatif de tête est le plus mécanique
    des emplois). Une réplique sur deux au pire peut donc encore le nommer —
    quand il ne vient pas d'être nommé. On ne touche jamais au prénom en MILIEU
    de phrase : « c'est toi qui m'as créée, Utilisateur » reste entier.
    Le prompt reçoit aussi une consigne, mais l'expérience du projet est
    constante : une consigne cède, un filtre tient.
    """
    if not precedente or nom.lower() not in precedente.lower():
        return txt
    t = re.sub(rf"^\s*(?:ah\s+|oh\s+|bon\s+|allez\s+)?(?:mon\s+p[’']?tit\s+)?"
               rf"{nom}\s*[!,.…:—–-]*\s+",
               "", txt, count=1, flags=re.IGNORECASE)
    # Si le prénom ÉTAIT la réplique (« Utilisateur ! »), il ne reste que de la
    # ponctuation : on rend l'original plutôt qu'un « ! » orphelin.
    if not re.search(r"\w", t):
        return txt
    if t != txt:
        t = t[0].upper() + t[1:]
    return t


def _empreinte(phrase):
    """Réduit une phrase à sa forme comparable : sans casse, sans ponctuation."""
    p = re.sub(r"[^\wàâäéèêëîïôöùûüç ]", " ", phrase.lower())
    return " ".join(p.split())


def couper_repetitions(txt, deja_dites, seuil=0.6):
    """Retire les phrases qu'elle a DÉJÀ dites dans les répliques récentes.

    POURQUOI CE FILTRE (mesuré les 18/07/2026, sur 5 sessions de 40-50 échanges) :
    la répétition est le défaut le plus tenace du personnage. Tout a été essayé
    côté texte, et tout a échoué ou empiré :
        retirer les listes de formules interdites  ->  6 puis 20 répétitions
        réécrire le prompt autour de l'identité    ->  33
        idem + règle de brièveté                   ->  38
        DRY 0,8 / 1,2 / 2,0                        ->  38 / 24 / 30
    Le seul levier qui ait jamais tenu sur ce projet, c'est le FILTRE DÉTERMINISTE :
    les astérisques, le tic « Ah », la longueur, la ponctuation, les morceaux
    trop courts — tous réglés par du code, aucun par une consigne.

    On compare donc phrase à phrase avec ce qu'elle a dit récemment, et on coupe
    ce qui revient. Deux garde-fous :
      - on ne vide JAMAIS une réplique : s'il ne reste rien, on garde l'original
        (mieux vaut une répétition qu'un silence) ;
      - la comparaison est souple (75 % de mots communs), pour attraper les
        variantes (« Et puis quoi ? » / « Et puis quoi donc ? ») sans couper
        deux phrases seulement voisines.
    """
    phrases = [p for p in re.split(r"(?<=[\.\!\?…])\s+|\n+", txt) if p.strip()]
    if not phrases:
        return txt, 0, False

    garde, coupees = [], 0
    for p in phrases:
        e = _empreinte(p)
        # ⚠️ SEUIL RELEVÉ DE 3 À 6 MOTS le 19/07/2026, après la session réelle de
        # Utilisateur : « ses réponses sont mauvaises, pas complètes, trop courtes,
        # pas finies ». Le filtre est intervenu sur 10 réponses sur 30, et c'est
        # lui qui les tronquait. Une phrase de 4-5 mots est trop courte pour
        # qu'on puisse affirmer qu'elle est « déjà dite » — et la couper laisse
        # une réplique amputée, ce qui est bien pire que la répétition évitée.
        if len(e.split()) < 6:
            garde.append(p)
            continue
        mots = set(e.split())
        redite = False
        for ancienne in deja_dites:
            ma = set(ancienne.split())
            if not ma:
                continue
            # ⚠️ ON DIVISAIT PAR LA PLUS COURTE DES DEUX PHRASES. Conséquence :
            # une réplique de 3 mots partageant ses 3 mots avec n'importe quelle
            # phrase plus longue obtenait un score de 100 % et sautait toujours.
            # Ses phrases brèves étaient condamnées d'avance — d'où l'impression
            # de réponses coupées au milieu.
            # On compare maintenant sur l'ENSEMBLE des mots des deux phrases
            # (intersection / union) : deux phrases ne se ressemblent que si
            # elles se ressemblent VRAIMENT, quelle que soit leur longueur.
            commun = len(mots & ma) / len(mots | ma)
            if commun >= seuil:
                redite = True
                break
        if redite:
            coupees += 1
        else:
            garde.append(p)

    if not garde:
        # TOUT était déjà dit. On ne peut pas vider la réplique : on signale au
        # service qu'il faut en redemander une autre. C'est le seul cas où couper
        # ne suffit pas — et c'est justement le pire (la réplique entièrement
        # recyclée, celle qui donne l'impression d'un disque rayé).
        return txt, 0, True
    return " ".join(garde).strip(), coupees, False


def memoriser_phrases(txt, deja_dites, fenetre=60):
    """Range les phrases de cette réplique pour pouvoir les reconnaître ensuite."""
    for p in re.split(r"(?<=[\.\!\?…])\s+|\n+", txt):
        e = _empreinte(p)
        if len(e.split()) >= 3:
            deja_dites.append(e)
    del deja_dites[:-fenetre]              # on ne garde que le passé récent


PROJET = Path(__file__).resolve().parent.parent
LOGS = PROJET / "tests" / "logs"
# L'adresse, le nom du modele, le contexte ET LE PROMPT viennent de moteur.py,
# pour que le chat ecrit et le service vocal ne puissent pas diverger.
# (Le 21/07/2026, ce fichier chargeait encore le v2 pendant que le service vocal
# chargeait le v3 — sous un commentaire qui jurait le contraire. Un commentaire
# ne garantit rien ; une source unique, si.)
import moteur  # noqa: E402
from moteur import API, NOM_MODELE, CONTEXTE, PROMPT_FILE  # noqa: E402

# Reglages trouves par experience le 17/07/2026 (rapport 1640_PROMPT_v3_vs_v2.txt).
# On rejouait la vraie conversation de Utilisateur pour mesurer, pas pour deviner.
#
#   - max_tokens 500 : un FILET, pas un plafond. Un plafond serre (180) donnait de
#     beaux chiffres mais coupait 7 reponses sur 8 en plein mot. Faux ami.
#   - la brievete vient du PROMPT v3, pas d'une troncature :
#         v2 -> 183 mots de moyenne, jusqu'a 337, escalade x6.4
#         v3 -> 74 mots de moyenne, jamais plus de 111, 0 coupee
#   - les penalites cassent la boucle : v3 seul laissait 2 reponses similaires a 79 %,
#     avec penalites -> 34 %.
#   - DRY (ajouté le 18/07/2026) : l'échantillonneur « Don't Repeat Yourself ».
#     LES PÉNALITÉS CLASSIQUES CI-DESSUS NE SUFFISENT PAS SUR UNE LONGUE SESSION.
#     Mesuré sur 40 échanges : « Tu vas user les touches de ton clavier jusqu'à
#     l'os ! » revenait 11 FOIS MOT POUR MOT, et 72 % de ses répliques commençaient
#     par « Tu ». Elles avaient pourtant été validées... sur 25 messages seulement.
#     Différence de nature : repeat_penalty punit des MOTS isolés déjà vus ; DRY
#     repère des SUITES DE MOTS répétées et les punit d'autant plus qu'elles sont
#     longues. C'est précisément le défaut observé.
#     dry_penalty_last_n = -1 -> il regarde TOUT le contexte, donc aussi ses
#     propres répliques passées : c'est là qu'elle allait se recopier.
PARAMS = {
    # 0.8 depuis le 17/07 : choisi pour sa vivacité. Mistral recommande 0.15 pour ce
    # modèle, mais pour un usage d'ASSISTANT (suivre des consignes à la lettre) —
    # l'inverse de ce qu'on veut. ALICE_TEMP permet de comparer sans toucher au code.
    "temperature": float(os.environ.get("ALICE_TEMP", 0.8)),
    "max_tokens": 500,
    "repeat_penalty": 1.15,
    "frequency_penalty": 0.3,
    "presence_penalty": 0.2,
    "dry_multiplier": 0.8,
    "dry_base": 1.75,
    "dry_allowed_length": 2,
    "dry_penalty_last_n": -1,
}
TEMPERATURE = PARAMS["temperature"]


def main():
    modele = NOM_MODELE

    print()
    print("=" * 70)
    print("  ALICE — conversation ecrite (clavier)")
    print("=" * 70)
    print(f"  Cerveau : {moteur.MODELE_GGUF.name}")
    print()

    # L'ENCLOS : si la fenetre est fermee d'un coup de croix, Windows tue le
    # llama-server avec nous. Sans lui, un serveur orphelin de 15 Go survivrait
    # (les zombies du 18/07/2026 — meme parade que la boucle vocale).
    sys.path.insert(0, os.path.join(PROJET, r"ecoute"))
    try:
        import menage
        enclos = menage.creer_enclos()
    except Exception:
        enclos = None

    import hashlib
    empreinte = hashlib.sha256(PROMPT_FILE.read_bytes()).hexdigest().upper()[:8]

    print(f"  Chargement du cerveau ({CONTEXTE} jetons de contexte)...")
    print("  (une quarantaine de secondes, c'est normal)")

    def _tracer(msg):
        print(f"  {msg}")
    try:
        processus_moteur = moteur.demarrer(_tracer)
    except Exception as e:
        print(f"\n  ECHEC DU CHARGEMENT : {e}")
        return 1
    if enclos and processus_moteur:
        for pr in (processus_moteur if isinstance(processus_moteur, (list, tuple))
                   else [processus_moteur]):
            menage.mettre_dans_lenclos(enclos, pr)
    print("  Elle est la.")

    systeme = PROMPT_FILE.read_text(encoding="utf-8")
    # historique = les VRAIS tours (user/assistant). Le bloc mémoire est injecté
    # de façon éphémère à chaque tour, il n'est PAS stocké dans l'historique.
    historique = []
    deja_dites = []          # empreintes récentes, pour le filtre anti-répétition

    print("  Réveil de la mémoire longue...")
    # ⚠️ ALICE_USER respecté ici aussi (audit du 22/07) : le chat écrit codait
    # « utilisateur » en dur — impossible de tester par écrit sans polluer SA
    # mémoire, en contradiction avec la règle de sûreté d'AGENTS.md.
    memoire = MemoireAlice(user_id=os.environ.get("ALICE_USER", "utilisateur"),
                           nom_affiche=os.environ.get("ALICE_NOM", "Utilisateur"))
    print(f"  Mémoire prête — {memoire.nb_souvenirs()} souvenir(s) déjà connu(s) "
          f"sur {memoire.nom} (mémoire « {memoire.user_id} »).")

    ts = datetime.now()
    log = LOGS / f"{ts:%Y-%m-%d_%H%M}_TEMPS-B_{moteur.MODELE_GGUF.stem[:38]}.txt"
    LOGS.mkdir(parents=True, exist_ok=True)

    entete = [
        "=" * 72,
        " CONVERSATION LIBRE AVEC ALICE",
        "=" * 72,
        f" Cerveau      : {moteur.MODELE_GGUF.name}",
        f" Date         : {ts:%Y-%m-%d %H:%M:%S}",
        f" Contexte     : {CONTEXTE} jetons",
        f" Reglages     : {json.dumps(PARAMS)}",
        f" Prompt       : {PROMPT_FILE.name} (empreinte {empreinte})",
        f" Type         : conversation libre (Utilisateur mene, aucun script)",
        "=" * 72,
        "",
    ]
    log.write_text("\n".join(entete), encoding="utf-8")

    print()
    print("-" * 70)
    print("  Ecris et appuie sur Entree. Elle repond.")
    print("  Pour terminer : tape  fin   (ou Ctrl+C)")
    print(f"  Tout est enregistre dans : {log.name}")
    print("-" * 70)
    print()

    n = 0
    try:
        while True:
            try:
                msg = input("TOI   > ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not msg:
                continue
            if msg.lower() in ("fin", "quit", "exit", "stop"):
                break

            n += 1

            # === VERROU 2 : on récupère les souvenirs pertinents AVANT de répondre ===
            faits, t_reche = memoire.souvenirs_pertinents(msg)
            bloc_memoire = memoire.bloc_a_injecter(faits)

            historique.append({"role": "user", "content": msg})

            # Ordre des messages envoyés au cerveau :
            #   1. la personnalité (le prompt de moteur.PROMPT_FILE)
            #   2. toute la conversation SAUF le dernier message
            #   3. le carnet de souvenirs (éphémère, juste ce tour)  <-- juste AVANT
            #   4. le message courant de Utilisateur
            # On colle le carnet contre la question du moment : un modèle regarde
            # surtout le début ET LA FIN du contexte. Dans une longue conversation,
            # un carnet placé au tout début serait "oublié" au fond. Ici il est frais
            # dans son attention pile quand elle répond.
            messages = (
                [{"role": "system", "content": systeme}]
                + historique[:-1]
                + [{"role": "system", "content": bloc_memoire}]
                + [historique[-1]]
            )
            requete = {"model": modele, "messages": messages}
            requete.update(PARAMS)
            corps = json.dumps(requete).encode("utf-8")
            req = urllib.request.Request(API, data=corps,
                                         headers={"Content-Type": "application/json"})
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=600) as x:
                    rep = json.loads(x.read().decode("utf-8"))
                texte = rep["choices"][0]["message"]["content"].strip()
            except Exception as e:
                texte = f"[ERREUR: {type(e).__name__}: {e}]"
            dt = time.time() - t0

            # Ce qu'Alice DIT vraiment — LES MÊMES 7 FILTRES QUE LE SERVICE
            # VOCAL (audit du 22/07 : le chat n'en appliquait que 3, alors que
            # l'en-tête du service jure « mêmes filtres » — les fuites de
            # consignes et les répétitions ressortaient brutes par écrit,
            # précisément là où on vient VÉRIFIER ce que fait la voix).
            parlee = corriger_appellation(limiter_longueur(retirer_tic_ouverture(
                nettoyer_pour_voix(retirer_fuites_de_consignes(texte)))))
            _prec = next((h["content"] for h in reversed(historique[:-1])
                          if h["role"] == "assistant"), "")
            parlee = limiter_le_prenom(parlee, _prec, nom=memoire.nom)
            parlee, n_coupees, _ = couper_repetitions(parlee, deja_dites)
            memoriser_phrases(parlee, deja_dites)
            filtre_a_agi = (parlee != texte)

            # On stocke dans l'historique la version SANS « Ah… » : ainsi le modèle ne
            # voit plus son propre tic se répéter dans son passé, ce qui aide à le briser.
            historique.append({"role": "assistant", "content": parlee})

            print(f"\nALICE > {parlee}\n")
            marque = "  [filtre voix : didascalie retiree]" if filtre_a_agi else ""
            print(f"        ({dt:.1f} s — {len(parlee.split())} mots){marque}")

            # === VERROUS 1+3 : on mémorise APRÈS avoir affiché la réponse ===
            # En séquence (pas en arrière-plan) : Utilisateur lit pendant que ça trie.
            # ⚠️ LE PORTIER, ICI AUSSI (audit du 22/07) : le chat écrit était le
            # dernier chemin d'écriture qui contournait le filtre anti-faux-
            # souvenirs — une session écrite injectait la méta-conversation
            # telle quelle dans la vraie mémoire. La réponse à laquelle il
            # réagit est en historique[-3] (comme au service vocal).
            _precedente = historique[-3]["content"] if len(historique) >= 3 else ""
            garder, raison_p = portier(msg, _precedente)
            if garder:
                nb_tri, t_tri = memoire.memoriser(msg)
            else:
                nb_tri, t_tri = 0, 0.0
                print(f"        [portier : phrase écartée de la mémoire — {raison_p}]")

            # Le seul délai AJOUTÉ avant la réponse = la récupération (~20 ms).
            # Le tri (~2 s) tombe APRÈS, pendant la lecture.
            print(f"        [mémoire : {len(faits)} souvenir(s) rappelé(s) · "
                  f"récup {t_reche*1000:.0f} ms (avant) · tri {t_tri:.1f} s (après, {nb_tri} retenu)]\n")

            # On ecrit APRES CHAQUE echange : rien n'est perdu en cas de plantage.
            bloc = [
                "┌─ [%02d] " % n + "─" * 62,
                f"│ UTILISATEUR : {msg}",
                "│",
                f"│ [mémoire injectée : {len(faits)} souvenir(s), récup {t_reche*1000:.0f} ms]",
            ]
            for lg in bloc_memoire.split("\n"):
                bloc.append(f"│   ¦ {lg}")
            bloc.append("│")
            for ligne in parlee.split("\n"):
                bloc.append(f"│ {ligne}" if ligne.strip() else "│")
            bloc.append("│")
            if filtre_a_agi:
                bloc.append("│ [filtre voix : le cerveau avait glisse une didascalie *...*, retiree avant la voix]")
            bloc += [f"│ ({dt:.1f} s — {len(parlee.split())} mots)", "└" + "─" * 69, ""]
            with log.open("a", encoding="utf-8") as f:
                f.write("\n".join(bloc) + "\n")

    finally:
        with log.open("a", encoding="utf-8") as f:
            f.write("\n" + "─" * 62 + "\n")
            f.write(f" FIN DE CONVERSATION — {n} echanges · {memoire.nb_souvenirs()} souvenir(s) en mémoire\n")
            f.write("─" * 62 + "\n")
        print("\n  Dechargement du cerveau...")
        moteur.arreter(processus_moteur, _tracer)
        print(f"\n  Log enregistre :\n  {log}\n")
        print("  Tu peux donner ce fichier a Claude Code pour analyse.")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
