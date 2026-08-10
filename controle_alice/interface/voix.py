"""Lecture locale des reponses ecrites, sans modele ni bibliotheque audio.

⚠️ LA LECTURE SE FAIT AU FIL DE L'EAU DEPUIS LE 06/08/2026.

Avant, ce lecteur telechargeait TOUT l'audio (`fabriquer_wav`), l'enveloppait
en WAV, puis le jouait d'un coup avec `winsound`. Rien ne pouvait donc sonner
avant la fin de la fabrication — alors que le service voix rend son premier
bloc en ~1 s quelle que soit la longueur de la reponse.

MESURE DIRECTE sur la vraie fonction (tests\\banc_lecteur_interface.py, 06/08) :
    reponse courte ( 3,3 s d'audio)  1re note possible 1,13 s  ->  jouee a  2,42 s
    reponse longue (21,2 s d'audio)  1re note possible 1,03 s  ->  jouee a 11,39 s
Soit jusqu'a 10,4 s d'attente pour rien, ET qui GRANDIT avec la longueur.
Sur la session reelle de l'utilisateur du 06/08 a 14:40, le premier son tombait a
6,4 / 5,5 / 9,6 / 12,0 / 26,0 s apres sa derniere syllabe.

POURQUOI ctypes ET L'API `waveOut` DE WINDOWS, et rien d'autre :
`winsound` ne sait jouer qu'un son ENTIER, il ne peut pas streamer. Les deux
autres voies sont fermees par la frontiere de ce sous-projet (tests\\
test_frontiere.py) : la bibliotheque PortAudio est reservee au SEUL processus
micro — et ce fichier n'a meme pas le droit d'ecrire son nom de module —, et
QtMultimedia vit dans PySide6-Addons, absent d'ici (Essentials seul) et il
doublerait le poids de l'executable. `waveOut` est le meme
sous-systeme audio que `winsound`, atteint par la bibliotheque standard, et
il accepte qu'on lui donne les morceaux au fur et a mesure.
"""

from __future__ import annotations

import ctypes
import io
import json
import struct
import threading
import time
import urllib.error
import urllib.request
import wave
from ctypes import wintypes

from PySide6.QtCore import QObject, Signal

from commun import decoupe_voix
from commun.corps_client import ClientCorps

#: LE CORPS D'ALICE, S'IL TOURNE. Un seul par processus : c'est un seul
#: avatar, et chaque client coute un fil.
#:
#: 🔴 POURQUOI LA BOUCHE SE PILOTE D'ICI, ET DE NULLE PART AILLEURS. Meme
#: raison EXACTE que les sous-titres du 08/08 : l'hote connait la replique,
#: mais pas le RYTHME. Le service de voix fabrique parfois plus lentement
#: qu'on n'ecoute (facteur mesure jusqu'a 2,15x), la carte son se retrouve a
#: sec, et une bouche animee sur un minuteur pose au depart aurait la meme
#: avance de 4,2 s que le sous-titre naif. Ce fichier est le seul a savoir
#: quand un bloc est confie a la carte et combien d'audio l'attend devant lui.
#:
#: ⚠️ Il n'estime rien et il ne peut rien ralentir : deposer un bloc dans sa
#: file est une operation de liste. Voir `commun\corps_client.py`.
_CORPS = None
_VERROU_CORPS = threading.Lock()


def corps() -> ClientCorps:
    global _CORPS
    with _VERROU_CORPS:
        if _CORPS is None:
            _CORPS = ClientCorps()
        return _CORPS

URL_VOIX = "http://127.0.0.1:8181/parler"
LIMITE_PCM = 48 * 1024 * 1024
#: Le contrat du service voix : PCM 16 bits, mono, 24000 Hz.
FREQUENCE = 24000


class VoixIndisponible(RuntimeError):
    """Le service n'a pas produit une voix exploitable."""


def _demande(texte: str, url: str):
    if not isinstance(texte, str) or not texte.strip():
        raise VoixIndisponible("texte vide")
    corps = json.dumps({"texte": texte.strip()}, ensure_ascii=False).encode("utf-8")
    return urllib.request.Request(
        url,
        data=corps,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )


def _lire_exactement(flux, combien: int) -> bytes:
    """Lit `combien` octets, ou moins si le flux se termine avant."""
    morceaux, reste = [], combien
    while reste > 0:
        bout = flux.read(reste)
        if not bout:
            break
        morceaux.append(bout)
        reste -= len(bout)
    return b"".join(morceaux)


def blocs_pcm(texte: str, *, url: str = URL_VOIX, timeout_s: float = 30.0):
    """Rend les blocs PCM AU FUR ET A MESURE qu'ils arrivent.

    Memes exigences que `_decoder_trames`, mais sans attendre la fin : un flux
    tronque, mensonger ou trop grand leve `VoixIndisponible` — on prefere une
    erreur nette a du bruit joue sur les enceintes de l'utilisateur.
    """
    total = 0
    try:
        with urllib.request.urlopen(_demande(texte, url), timeout=timeout_s) as reponse:
            while True:
                entete = _lire_exactement(reponse, 4)
                if len(entete) < 4:
                    raise VoixIndisponible("fin de flux audio absente")
                taille = struct.unpack(">I", entete)[0]
                if taille == 0:
                    return
                total += taille
                if taille % 2 or total > LIMITE_PCM:
                    raise VoixIndisponible("bloc audio incomplet ou trop grand")
                bloc = _lire_exactement(reponse, taille)
                if len(bloc) < taille:
                    raise VoixIndisponible("bloc audio tronque")
                yield bloc
    except (OSError, urllib.error.URLError) as souci:
        raise VoixIndisponible(f"service de voix injoignable ({souci})") from souci


# ─── LA SORTIE AUDIO DE WINDOWS, EN DIRECT (waveOut) ────────────────────────
# On empile les morceaux dans la file de la carte son : elle joue le premier
# pendant que le service fabrique les suivants. `waveOutReset` arrete NET, ce
# qu'exige la coupure de parole.
_WAVE_MAPPER = 0xFFFFFFFF          # « le peripherique de sortie par defaut »
_WHDR_DONE = 0x00000001            # ce morceau a fini d'etre joue
_CALLBACK_NULL = 0                 # pas de rappel : on releve le drapeau nous-memes


class _FORMAT_ONDE(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class _ENTETE_ONDE(ctypes.Structure):
    pass


_ENTETE_ONDE._fields_ = [
    ("lpData", ctypes.c_char_p),
    ("dwBufferLength", wintypes.DWORD),
    ("dwBytesRecorded", wintypes.DWORD),
    ("dwUser", ctypes.c_void_p),
    ("dwFlags", wintypes.DWORD),
    ("dwLoops", wintypes.DWORD),
    ("lpNext", ctypes.POINTER(_ENTETE_ONDE)),
    ("reserved", ctypes.c_void_p),
]


class SortieWaveOut:
    """La carte son, alimentee morceau par morceau. Un seul fil ecrit ;
    `arreter` peut venir du fil graphique, d'ou le verrou."""

    def __init__(self, frequence: int = FREQUENCE) -> None:
        self._frequence = frequence
        self._verrou = threading.RLock()
        self._appareil = wintypes.HANDLE()
        self._ouvert = False
        self._en_vol: list = []        # morceaux confies a la carte, pas encore joues

    def ouvrir(self) -> None:
        octets_par_bloc = 2            # 16 bits, mono
        format_onde = _FORMAT_ONDE(
            1, 1, self._frequence, self._frequence * octets_par_bloc,
            octets_par_bloc, 16, 0)
        code = ctypes.windll.winmm.waveOutOpen(
            ctypes.byref(self._appareil), _WAVE_MAPPER,
            ctypes.byref(format_onde), 0, 0, _CALLBACK_NULL)
        if code:
            raise VoixIndisponible(f"sortie audio indisponible (code {code})")
        self._ouvert = True

    def ecrire(self, pcm: bytes) -> None:
        tampon = ctypes.create_string_buffer(pcm, len(pcm))
        entete = _ENTETE_ONDE()
        entete.lpData = ctypes.cast(tampon, ctypes.c_char_p)
        entete.dwBufferLength = len(pcm)
        with self._verrou:
            if not self._ouvert:
                return
            taille = ctypes.sizeof(entete)
            code = ctypes.windll.winmm.waveOutPrepareHeader(
                self._appareil, ctypes.byref(entete), taille)
            if code:
                raise VoixIndisponible(f"morceau audio refuse (code {code})")
            code = ctypes.windll.winmm.waveOutWrite(
                self._appareil, ctypes.byref(entete), taille)
            if code:
                ctypes.windll.winmm.waveOutUnprepareHeader(
                    self._appareil, ctypes.byref(entete), taille)
                raise VoixIndisponible(f"lecture audio refusee (code {code})")
            # ⚠️ Garder le tampon ET l'entete vivants : la carte son lit dans
            # cette memoire. Les laisser ramasser produirait du bruit ou un
            # plantage — c'est le piege classique de waveOut.
            self._en_vol.append((entete, tampon))
        self._recycler()

    def _recycler(self) -> None:
        with self._verrou:
            if not self._ouvert:
                return
            restants = []
            for entete, tampon in self._en_vol:
                if entete.dwFlags & _WHDR_DONE:
                    ctypes.windll.winmm.waveOutUnprepareHeader(
                        self._appareil, ctypes.byref(entete),
                        ctypes.sizeof(entete))
                else:
                    restants.append((entete, tampon))
            self._en_vol = restants

    def attendre_la_fin(self, interrompu=lambda: False) -> bool:
        """Attend que tout soit joue. Rend faux si `interrompu` a tranche."""
        while True:
            self._recycler()
            with self._verrou:
                if not self._en_vol or not self._ouvert:
                    return True
            if interrompu():
                return False
            time.sleep(0.02)

    def arreter(self) -> None:
        """Coupe le son NET (coupure de parole, changement de réplique)."""
        with self._verrou:
            if self._ouvert:
                ctypes.windll.winmm.waveOutReset(self._appareil)

    def fermer(self) -> None:
        with self._verrou:
            if not self._ouvert:
                return
            ctypes.windll.winmm.waveOutReset(self._appareil)
            for entete, _tampon in self._en_vol:
                ctypes.windll.winmm.waveOutUnprepareHeader(
                    self._appareil, ctypes.byref(entete), ctypes.sizeof(entete))
            self._en_vol = []
            ctypes.windll.winmm.waveOutClose(self._appareil)
            self._ouvert = False


def fabriquer_wav(texte: str, *, url: str = URL_VOIX, timeout_s: float = 30.0) -> bytes:
    """Demande le PCM au service et l'enveloppe en WAV, uniquement en memoire.

    N'est PLUS le chemin de lecture depuis le 06/08 (voir l'entete du fichier) :
    elle attend tout l'audio par construction. Gardee parce qu'elle porte le
    contrat du format et sert aux bancs de comparaison.
    """
    demande = _demande(texte, url)
    try:
        with urllib.request.urlopen(demande, timeout=timeout_s) as reponse:
            brut = reponse.read(LIMITE_PCM + 1)
    except (OSError, urllib.error.URLError) as souci:
        raise VoixIndisponible(f"service de voix injoignable ({souci})") from souci
    if len(brut) > LIMITE_PCM:
        raise VoixIndisponible("reponse audio trop grande")
    pcm = _decoder_trames(brut)
    if not pcm or len(pcm) % 2:
        raise VoixIndisponible("audio PCM vide ou incomplet")

    sortie = io.BytesIO()
    with wave.open(sortie, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(pcm)
    return sortie.getvalue()


def _decoder_trames(brut: bytes) -> bytes:
    morceaux: list[bytes] = []
    position = 0
    termine = False
    while position < len(brut):
        if len(brut) - position < 4:
            raise VoixIndisponible("longueur de bloc audio tronquee")
        taille = struct.unpack(">I", brut[position : position + 4])[0]
        position += 4
        if taille == 0:
            termine = True
            break
        if taille > LIMITE_PCM or position + taille > len(brut):
            raise VoixIndisponible("bloc audio tronque ou trop grand")
        morceaux.append(brut[position : position + taille])
        position += taille
    if not termine or position != len(brut):
        raise VoixIndisponible("fin de flux audio absente ou donnees en trop")
    return b"".join(morceaux)


class LecteurVoix(QObject):
    """Synthese et lecture hors du fil graphique, annulables a tout moment."""

    erreur = Signal(str)
    debut = Signal()
    fin = Signal()
    #: LA PHRASE QU'ELLE COMMENCE A PRONONCER, A L'INSTANT OU ELLE SONNE.
    #: Elle alimente le sous-titre du stream (08/08/2026). Voir `_annoncer`
    #: pour la raison pour laquelle c'est ICI et nulle part ailleurs.
    phrase = Signal(str)

    def __init__(self, parent: QObject | None = None, *,
                 fabrique_sortie=SortieWaveOut, source=blocs_pcm) -> None:
        super().__init__(parent)
        self._verrou = threading.Lock()
        self._generation = 0
        self._active = False
        self._sortie = None
        self._prepare = None        # audio fabriqué d'avance, pas encore joué
        self._en_attente = None     # réplique qui attend qu'elle ait fini
        # Injectables pour les tests : ils prouvent que la 1re note part AVANT
        # la fin de la fabrication, sans toucher a la vraie carte son.
        self._fabrique_sortie = fabrique_sortie
        self._source = source

    def preparer(self, texte: str) -> None:
        """Fabrique l'audio d'une phrase SANS la jouer (06/08/2026).

        Le cerveau publie sa 1re phrase des qu'elle est ecrite, pendant qu'il
        redige la suite. On profite de cette avance (~2,2 s mesurees) pour
        payer la fabrication (~1,1 s) : quand la reponse complete arrive,
        le son est deja pret et part sans attente.

        ⚠️ RIEN N'EST JOUE ICI. La verification de la memoire peut encore
        reecrire toute la replique ; seul `parler` avec `deja_prepare` la
        fait entendre. Sinon l'audio est jete sans avoir ete entendu.
        """
        with self._verrou:
            self._annuler_prepare()
            attendu = {"texte": texte, "blocs": [], "pret": False,
                       "stop": threading.Event()}
            self._prepare = attendu

        def _fabriquer():
            blocs = []
            flux = self._source(texte)
            try:
                for bloc in flux:
                    # ⚠️ ON S'ARRETE DES QUE CETTE PREPARATION NE SERT PLUS.
                    # Mesure de la session du 07/08 a 02:37 : un candidat
                    # refuse dont la fabrication continuait a occupe la carte
                    # 5,2 s EN MEME TEMPS que la vraie replique — les deux ont
                    # mis 7,5 s au lieu de 3 a 4, et le 1er son est tombe a
                    # 3,9 s. Une avance qu'on n'utilise pas doit couter ZERO.
                    if attendu["stop"].is_set():
                        return
                    blocs.append(bloc)
            except Exception:
                blocs = []          # une panne coute l'avance, pas la parole
            finally:
                # Ferme le flux : le service de voix voit son client partir
                # et cesse de fabriquer pour rien.
                getattr(flux, "close", lambda: None)()
            with self._verrou:
                if self._prepare is attendu and not attendu["stop"].is_set():
                    attendu["blocs"] = blocs
                    attendu["pret"] = True

        threading.Thread(target=_fabriquer, name="voix-prepare",
                         daemon=True).start()

    def _annuler_prepare(self) -> None:
        """Abandonne la preparation en cours. A appeler EN TENANT le verrou."""
        if self._prepare is not None:
            self._prepare["stop"].set()
            self._prepare = None

    def _reprendre_prepare(self, texte: str):
        """Les blocs deja fabriques pour CE texte, ou None. Consomme la
        preparation dans tous les cas : elle ne sert qu'une fois."""
        with self._verrou:
            prepare, self._prepare = self._prepare, None
        if not prepare or prepare["texte"] != texte:
            if prepare is not None:
                prepare["stop"].set()   # périmée : qu'elle cesse tout de suite
            return None
        # ⚠️ ON N'ATTEND JAMAIS PLUS QUE CE QUE COUTERAIT DE REFAIRE. Le
        # service rend son 1er bloc en ~1,1 s (mesure du 06/08) : au-dela,
        # patienter est un pari perdant. Mesure a l'appui — avec une attente
        # de 3 s, une replique sur quatre finissait 0,4 s PLUS LENTE qu'avant
        # la preparation. Passe ce budget, on refabrique et on oublie.
        limite = time.perf_counter() + 1.2
        while time.perf_counter() < limite:
            with self._verrou:
                if prepare["pret"]:
                    return prepare["blocs"] or None
            time.sleep(0.01)
        prepare["stop"].set()      # trop lente : on refait, elle s'arrête
        return None

    def parler(self, texte: str, deja_prepare: str = "") -> None:
        """Dit `texte`. Si elle PARLE ENCORE, la replique ATTEND son tour.

        ⚠️ CORRECTIF DU 07/08/2026, capture d'ecran de l'utilisateur a l'appui :
        deux de ses phrases captees coup sur coup produisent deux reponses,
        et la seconde arrivait pendant que la premiere etait dite. L'ancienne
        version coupait NET la phrase en cours — « elle se coupe toute
        seule ». Une presence ne s'interrompt pas elle-meme au milieu d'un
        mot : elle finit, puis elle enchaine.
        On ne garde qu'UNE replique en attente : si une troisieme arrive, la
        deuxieme est perimee — mieux vaut la reponse a la question la plus
        recente qu'un monologue de rattrapage.
        """
        with self._verrou:
            if self._active:
                self._en_attente = (texte, deja_prepare)
                return
        self._demarrer(texte, deja_prepare)

    def _demarrer(self, texte: str, deja_prepare: str = "") -> None:
        self.arreter(garder_prepare=bool(deja_prepare), muet=True)
        with self._verrou:
            self._generation += 1
            generation = self._generation
            self._active = True
        self.debut.emit()
        threading.Thread(
            target=self._travailler,
            args=(texte, generation, deja_prepare),
            name="voix-interface",
            daemon=True,
        ).start()

    def arreter(self, *, garder_prepare: bool = False,
                muet: bool = False) -> None:
        """Coupe la parole en cours.

        `garder_prepare` : ne jette pas l'audio prepare quand `parler`
        s'apprete justement a s'en servir.

        `muet` : n'annonce PAS la fin. ⚠️ CE DETAIL EST UN CORRECTIF, pas une
        commodite (07/08/2026). La fenetre met le micro en pause sur `debut`
        et le ROUVRE sur `fin`. Quand une replique en remplace une autre,
        `parler` appelle `arreter` puis emet `debut` : l'ancienne version
        annoncait donc fin PUIS debut, ce qui rouvrait le micro une fraction
        de seconde. Les commandes passent par l'hote, qui peut etre occupe a
        generer — la fraction devient des secondes, et une phrase de l'utilisateur
        s'y engouffre : le cerveau repond, et la nouvelle reponse COUPE celle
        qu'Alice etait en train de dire. Mesure de la session de 05:08 : une
        capture sur 28 est tombee pendant sa voix, et c'est exactement celle
        qui l'a coupee. On ne rouvre plus le micro pour le refermer aussitot.
        """
        with self._verrou:
            self._generation += 1
            etait_active = self._active
            self._active = False
            sortie = self._sortie
            if not garder_prepare:
                self._annuler_prepare()   # plus personne n'attend cette avance
        if muet:
            etait_active = False          # on enchaine : le micro reste ferme
        if sortie is not None:
            # waveOutReset : elle se tait NET, pas en fin de phrase.
            sortie.arreter()
        if etait_active:
            self.fin.emit()

    @staticmethod
    def _etiqueter(texte: str, blocs):
        """Colle a chaque bloc audio LA PHRASE QU'IL PRONONCE.

        Le service de voix emet un bloc par unite de parole, dans l'ordre
        (`service_voix_chatterbox.do_POST`). On refait donc le meme decoupage
        et on apparie par rang. Les deux cotes lisent le MEME fichier de
        regles (`commun.decoupe_voix`) : c'est ce qui rend l'appariement sur.

        ⚠️ ET SI LES DEUX NE S'ACCORDAIENT QUAND MEME PAS, on rend une
        etiquette VIDE plutot qu'une phrase au hasard. Le sous-titre garde
        alors la precedente : en direct, un sous-titre en retard se remarque
        a peine, un sous-titre qui dit autre chose que sa voix se voit tout
        de suite.
        """
        attendues = decoupe_voix.morceaux(texte)
        for rang, bloc in enumerate(blocs):
            yield (attendues[rang] if rang < len(attendues) else ""), bloc

    def _blocs(self, texte: str, deja_prepare: str):
        """Les morceaux audio à jouer, en profitant de ce qui est déjà prêt.

        Rend des couples (phrase prononcée, PCM) — voir `_etiqueter`.

        Si `deja_prepare` a été fabriqué d'avance et que le texte commence
        bien par lui, on joue ces morceaux tout de suite, puis on fabrique
        seulement la SUITE. Sinon on fabrique tout, comme avant : une
        préparation ratée ou périmée ne fait jamais dire autre chose.
        """
        prepares = None
        if deja_prepare and texte.startswith(deja_prepare):
            prepares = self._reprendre_prepare(deja_prepare)
        else:
            self._reprendre_prepare("")        # on jette ce qui traîne
        if prepares is None:
            yield from self._etiqueter(texte, self._source(texte))
            return
        # Deux demandes distinctes au service : il a decoupe chacune de son
        # cote, on etiquette donc chacune de son cote — sinon la couture
        # entre l'avance et la suite decalerait toutes les phrases.
        yield from self._etiqueter(deja_prepare, prepares)
        suite = texte[len(deja_prepare):].strip()
        if suite:
            yield from self._etiqueter(suite, self._source(suite))

    def _annoncer(self, texte: str, dans: float, generation: int) -> None:
        """Annonce `texte` quand il commencera REELLEMENT a sonner.

        🔴 POURQUOI CE CALCUL VIT ICI ET PAS DANS L'HOTE (08/08/2026). L'hote
        connait la replique, mais pas le rythme : la carte son joue les blocs
        a la suite, alors que le service les fabrique parfois moins vite
        qu'on ne les ecoute (facteur mesure jusqu'a 2,15x sur une phrase
        courte, journal du 08/08 a 07:57). Elle se retrouve alors a sec entre
        deux phrases, et l'horloge du son n'a plus rien a voir avec la duree
        de l'audio. Sur une replique reelle de 3 phrases, un minuteur pose au
        depart avait 4,6 s d'avance sur sa voix.
        Ici, on ne devine rien : on sait quand le bloc a ete confie a la carte
        et combien d'audio l'attend encore devant lui. `dans` est cet ecart.
        """
        if not texte:
            return                      # etiquette incertaine : on ne dit rien
        if dans <= 0.001:
            self.phrase.emit(texte)
            return

        def _plus_tard():
            with self._verrou:
                perimee = generation != self._generation
            if not perimee:             # coupee entre-temps : on se tait
                self.phrase.emit(texte)

        minuteur = threading.Timer(dans, _plus_tard)
        minuteur.daemon = True          # ne retient jamais la fermeture
        minuteur.start()

    def _travailler(self, texte: str, generation: int,
                    deja_prepare: str = "") -> None:
        sortie = None
        # Instant (horloge monotone) où tout l'audio déjà confié à la carte
        # aura fini de sonner. C'est lui qui date les sous-titres.
        fin_du_son = None
        premier_bloc = True
        try:
            for dite, bloc in self._blocs(texte, deja_prepare):
                with self._verrou:
                    if generation != self._generation:
                        return
                if sortie is None:
                    # La carte son n'est ouverte qu'au PREMIER morceau reçu :
                    # une panne du service ne laisse pas un peripherique pris.
                    sortie = self._fabrique_sortie()
                    sortie.ouvrir()
                    with self._verrou:
                        self._sortie = sortie
                sortie.ecrire(bloc)
                # waveOut joue dès l'écriture : ce bloc commence donc soit
                # tout de suite (la carte était à sec), soit quand le
                # précédent aura fini. 2 octets par échantillon, mono.
                maintenant = time.perf_counter()
                depart = maintenant if fin_du_son is None else max(
                    maintenant, fin_du_son)
                fin_du_son = depart + len(bloc) / 2 / FREQUENCE
                self._annoncer(dite, depart - maintenant, generation)
                # Sa bouche suit CE bloc-ci, a partir de l'instant ou il
                # sonnera vraiment. Meme horloge que le sous-titre.
                if premier_bloc:
                    premier_bloc = False
                    corps().parle(True)
                corps().bouche_selon(bloc, FREQUENCE, depart)
            if sortie is None:
                raise VoixIndisponible("audio PCM vide")
            sortie.attendre_la_fin(
                lambda: generation != self._generation)
        except (VoixIndisponible, RuntimeError) as souci:
            with self._verrou:
                encore_actuelle = generation == self._generation
            if encore_actuelle:
                self.erreur.emit(str(souci))
        finally:
            with self._verrou:
                if self._sortie is sortie:
                    self._sortie = None
                actuelle = generation == self._generation and self._active
                if actuelle:
                    self._active = False
            if sortie is not None:
                # Toujours rendre la carte son, meme coupee ou en panne :
                # un peripherique laisse ouvert rendrait Alice muette ensuite.
                sortie.fermer()
            with self._verrou:
                suivante = self._en_attente if actuelle else None
                if suivante:
                    self._en_attente = None
            if not premier_bloc:
                # ⚠️ SA BOUCHE SE REFERME A CHAQUE SORTIE, y compris coupee et
                # y compris en panne — d'ou le `finally`. Sans ca, le dernier
                # niveau resterait affiche : bouche ouverte, figee, jusqu'a la
                # replique suivante. C'est le genre de defaut qu'on ne voit
                # qu'en direct, et qui ridiculise tout le reste du rendu.
                # Elle enchaine ? Le prochain bloc rouvrira dans la foulee.
                corps().parle(False)
            if actuelle and not suivante:
                self.fin.emit()      # elle se tait vraiment : le micro rouvre
            if suivante:
                # Elle enchaine sans rendre la main : le micro reste ferme.
                self._demarrer(*suivante)
