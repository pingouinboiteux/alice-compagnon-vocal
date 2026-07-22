# -*- coding: utf-8 -*-
"""
QUE RIEN NE SURVIVE À LA FERMETURE DE LA FENÊTRE.

╔══════════════════════════════════════════════════════════════════════════════╗
║ CE QUI S'EST PASSÉ LE 18/07/2026 — et qui a fausse une soiree entiere        ║
║                                                                               ║
║ En allant verifier autre chose, on a trouve QUATRE services d'anciennes       ║
║ sessions encore vivants : trois cerveaux (17h50, 18h36, 18h49) qui se         ║
║ disputaient le port 8082, et un llama-server orphelin de 15 Go.               ║
║ A eux seuls, ils occupaient 23 Go de memoire vive : en les arretant, la       ║
║ machine est passee de 29,3 Go (92 %) a 6,1 Go (19 %).                         ║
║                                                                               ║
║ Consequences, toutes constatees :                                             ║
║   - le lag de Utilisateur etait aggrave par des services qu'il croyait fermes ;  ║
║   - un essai a parle a un VIEUX service au lieu du nouveau, et j'ai failli    ║
║     conclure que ma correction ne marchait pas. Un zombie ment.               ║
╚══════════════════════════════════════════════════════════════════════════════╝

POURQUOI ILS SURVIVAIENT : `p.terminate()` tue le processus qu'on a lance —
c'est-a-dire le python du service. Mais llama-server est l'enfant DE CE python,
et Windows ne tue pas les arbres : l'enfant reste, orphelin, avec ses 15 Go.
Et si la fenetre est fermee brutalement (croix, plantage, coupure), `terminate`
n'est meme jamais appele.

LA PARADE, EN DEUX TEMPS :
  1. UN « JOB » WINDOWS — un enclos. Tout processus lance dans l'enclos, et tout
     ce qu'il lance a son tour, meurt automatiquement quand l'enclos se ferme.
     Et l'enclos se ferme des que notre programme disparait, QUELLE QUE SOIT la
     façon dont il disparait — meme tue net, meme plante. C'est Windows lui-meme
     qui s'en charge : rien ne depend de notre code de fermeture.
  2. UN MENAGE AU DEMARRAGE — au cas ou des restes d'avant cette correction
     traineraient encore. Ceinture et bretelles.
"""
import ctypes
import time
import subprocess
from ctypes import wintypes

k32 = ctypes.windll.kernel32

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def creer_enclos():
    """Crée l'enclos. Tout ce qu'on y met mourra avec nous, quoi qu'il arrive."""
    try:
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            return None
        return job
    except Exception:
        return None            # jamais bloquant : au pire on retombe sur l'ancien
                               # comportement, on ne casse pas le démarrage


def mettre_dans_lenclos(job, proc):
    """Range un service (et sa descendance) dans l'enclos."""
    if not job or proc is None:
        return False
    try:
        PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
        h = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, proc.pid)
        if not h:
            return False
        try:
            return bool(k32.AssignProcessToJobObject(job, h))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


# Les programmes qui n'ont AUCUNE raison de survivre à une session : ce sont les
# nôtres, lancés par nous. On ne touche à rien d'autre (surtout pas LM Studio,
# que Utilisateur peut ouvrir lui-même).
NOTRES = ("llama-server.exe", "whisper-server.exe")


def nettoyer_restes(tracer=None):
    """Tue les services d'une session precedente qui traineraient encore."""
    tues = []
    for nom in NOTRES:
        try:
            # encoding/errors explicites : taskkill repond en francais dans
            # l'encodage de la console (CP1252), pas en UTF-8 — sans ca, un
            # accent dans son message fait planter la lecture de la sortie.
            r = subprocess.run(["taskkill", "/F", "/IM", nom], capture_output=True,
                               text=True, encoding="cp1252", errors="replace",
                               timeout=60,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode == 0:
                tues.append(nom)
        except Exception:
            pass
    if tues and tracer:
        tracer("MÉNAGE", f"restes d'une session précédente arrêtés : {', '.join(tues)}")
    liberer_les_ports(tracer)
    return tues


# Les ports d'Alice. Personne d'autre sur cette machine n'a le droit d'y être :
# tout processus qui y écoute est un des nôtres, d'une session précédente.
PORTS_ALICE = (8080, 8081, 8082, 8095, 8096)


def liberer_les_ports(tracer=None):
    """Tue tout processus qui écoute déjà sur un port d'Alice.

    POURQUOI (20/07/2026, vécu par Utilisateur) : un service de test à moi avait
    survécu sur le port 8082 — taskkill par NOM ne l'attrapait pas, c'était un
    python.exe comme les autres. Quand Utilisateur a lancé Alice, SA boucle s'est
    connectée à MON service : elle avait les souvenirs de mon test en tête, puis
    elle est devenue muette quand les moteurs se sont emmêlés. Fenêtre ouverte,
    aucune erreur nulle part.

    Un zombie qui tient un port est pire qu'un zombie qui mange de la RAM : il
    RÉPOND à la place du vrai service, avec un autre passé. On ne peut pas le
    chasser par nom (python.exe est trop commun pour être abattu à l'aveugle),
    mais par PORT c'est sans ambiguïté : ces cinq ports sont à nous.
    """
    ps = ("$p=@(" + ",".join(str(p) for p in PORTS_ALICE) + ");"
          "foreach($x in $p){Get-NetTCPConnection -LocalPort $x -State Listen "
          "-ErrorAction SilentlyContinue | ForEach-Object {"
          "'{0}:{1}' -f $x, $_.OwningProcess;"
          "Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue}}")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, encoding="cp1252",
                           errors="replace", timeout=60,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        pris = [l.strip() for l in (r.stdout or "").splitlines() if ":" in l]
        if pris and tracer:
            tracer("MÉNAGE", f"port(s) repris à un survivant : {', '.join(pris)} "
                             f"(port:pid) — c'était lui, le « plantage » silencieux")
        if pris:
            time.sleep(1.5)          # laisser Windows refermer les ports
    except Exception:
        pass


def purger_vieux_enregistrements(tracer=None, jours_wav=7, jours_journaux=14):
    """Fait de la place : les copies-témoins et journaux ne servent qu'à
    l'enquête récente.

    POURQUOI (audit du 22/07/2026) : la copie-témoin de la voix, la copie du
    micro et les journaux horodatés s'accumulaient SANS AUCUNE purge — déjà
    plus de 230 wav et ~40 journaux pour une seule journée. Une enquête porte
    sur hier, pas sur le mois dernier : au-delà de `jours_wav` jours pour
    l'audio et `jours_journaux` pour les journaux, on jette.
    """
    import os
    base = os.path.join(PROJET, r"tests\logs")
    maintenant = time.time()
    jetes = 0
    try:
        for dossier in ("voix_sessions", "micro_sessions"):
            chemin = os.path.join(base, dossier)
            if not os.path.isdir(chemin):
                continue
            for f in os.listdir(chemin):
                p = os.path.join(chemin, f)
                try:
                    if os.path.isfile(p) and \
                            maintenant - os.path.getmtime(p) > jours_wav * 86400:
                        os.remove(p)
                        jetes += 1
                except Exception:
                    pass
        for f in os.listdir(base):
            p = os.path.join(base, f)
            try:
                if os.path.isfile(p) and f.endswith(".txt") and \
                        maintenant - os.path.getmtime(p) > jours_journaux * 86400:
                    os.remove(p)
                    jetes += 1
            except Exception:
                pass
    except Exception:
        pass
    if jetes and tracer:
        tracer("MÉNAGE", f"{jetes} vieux enregistrement(s)/journal(aux) purgés "
                         f"(audio > {jours_wav} j, journaux > {jours_journaux} j)")
