# -*- coding: utf-8 -*-
"""
ALICE — la fenêtre unique.

UNE seule fenêtre, où Utilisateur peut au choix PARLER au micro ou ÉCRIRE au clavier.
Les trois services (oreille, cerveau, voix) tournent cachés derrière : ils écrivent
toujours leur propre journal, on ne perd donc aucune trace.

POURQUOI CE PROGRAMME EXISTE : avant, il fallait jongler entre trois consoles noires
et on ne savait jamais où en était la machine. Ici tout est au même endroit, et une
bande d'état dit en permanence ce qu'elle fait : j'écoute / je réfléchis / je parle.

LES DEUX ENTRÉES SONT ÉQUIVALENTES : que la phrase vienne du micro ou du clavier,
elle traverse exactement le même chemin (mémoire, cerveau, voix). Écrire n'est pas
un mode dégradé — c'est juste une autre façon de lui adresser la parole.

À LA VOIX  : il faut dire « Alice » pour la réveiller, puis on parle librement.
AU CLAVIER : pas besoin de son nom — si on écrit, c'est qu'on lui parle.
"""
import queue
import sys
import os
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from tkinter import scrolledtext

import numpy as np
import sounddevice as sd
from openwakeword.vad import VAD

sys.path.insert(0, os.path.join(PROJET, r"ecoute"))
import boucle_alice as B  # noqa: E402

FOND = "#14121a"
FOND_CHAMP = "#1e1b26"
TEXTE = "#d8d4e0"
COUL_LUI = "#7fb3d5"
COUL_ELLE = "#c9a0dc"
COUL_INFO = "#6b6478"
COUL_ALERTE = "#e07a5f"


class Interface:
    def __init__(self, racine):
        self.racine = racine
        self.file = queue.Queue()
        self.elle_parle = threading.Event()   # met l'écoute en pause
        self.verrou = threading.Lock()        # un échange à la fois
        self.procs = []
        self.eveillee_jusqua = 0.0
        self.n = 0
        self.fini = False
        # Pour la relance spontanée — mêmes règles que la boucle console.
        self.derniere_parole = time.time()
        self.palier_relance = 0

        racine.title("Alice")
        racine.configure(bg=FOND)
        racine.geometry("880x620")
        racine.minsize(640, 420)

        self.etat = tk.Label(racine, text="  Démarrage...", anchor="w",
                             bg="#241f2e", fg=TEXTE, font=("Segoe UI", 11),
                             padx=10, pady=8)
        self.etat.pack(fill="x")

        self.vue = scrolledtext.ScrolledText(
            racine, bg=FOND, fg=TEXTE, font=("Segoe UI", 11), wrap="word",
            borderwidth=0, padx=16, pady=12, state="disabled",
            insertbackground=TEXTE, selectbackground="#3a3348")
        self.vue.pack(fill="both", expand=True)
        self.vue.tag_config("lui", foreground=COUL_LUI, font=("Segoe UI", 11, "bold"))
        self.vue.tag_config("elle", foreground=COUL_ELLE, font=("Segoe UI", 11))
        self.vue.tag_config("info", foreground=COUL_INFO, font=("Segoe UI", 9, "italic"))
        self.vue.tag_config("alerte", foreground=COUL_ALERTE, font=("Segoe UI", 10))

        bas = tk.Frame(racine, bg=FOND_CHAMP)
        bas.pack(fill="x")
        self.champ = tk.Entry(bas, bg=FOND_CHAMP, fg=TEXTE, font=("Segoe UI", 12),
                              borderwidth=0, insertbackground=TEXTE)
        self.champ.pack(side="left", fill="x", expand=True, padx=14, pady=12, ipady=6)
        self.champ.bind("<Return>", self.envoyer_texte)
        self.bouton = tk.Button(bas, text="Envoyer", command=self.envoyer_texte,
                                bg="#3a3348", fg=TEXTE, font=("Segoe UI", 10),
                                borderwidth=0, padx=18, pady=6, activebackground="#4a4258")
        self.bouton.pack(side="right", padx=(0, 14), pady=12)

        racine.protocol("WM_DELETE_WINDOW", self.fermer)
        self.champ.focus_set()

        threading.Thread(target=self.demarrer, daemon=True).start()
        threading.Thread(target=self.travailleur, daemon=True).start()

    # ─── affichage (toujours appelé depuis le fil graphique) ───────────────
    def _ecrire(self, texte, tag):
        self.vue.configure(state="normal")
        self.vue.insert("end", texte, tag)
        self.vue.configure(state="disabled")
        self.vue.see("end")

    def dire(self, texte, tag="info"):
        self.racine.after(0, lambda: self._ecrire(texte, tag))

    def statut(self, texte, couleur=TEXTE):
        self.racine.after(0, lambda: self.etat.configure(text="  " + texte, fg=couleur))

    # ─── démarrage des services ────────────────────────────────────────────
    def demarrer(self):
        self.statut("Démarrage : oreille, cerveau et voix se chargent (~1 min)...")
        self.dire("Alice se réveille. Les trois modèles se chargent, "
                  "compte une minute.\n\n", "info")
        self.procs, ok = B.lancer_services(fenetres=False)
        if not ok:
            self.statut("Un service n'a pas démarré — voir le journal", COUL_ALERTE)
            self.dire("Un des services n'a pas répondu. Le détail est dans "
                      f"{B.LOG}\n", "alerte")
            return
        self.dire("Elle est là.\n\n"
                  "  · Au micro : dis « Alice » pour la réveiller, "
                  "puis parle librement.\n"
                  "  · Au clavier : écris simplement, son nom n'est pas nécessaire.\n\n",
                  "info")
        self.statut("Je t'écoute — parle, ou écris ci-dessous", "#8fbf8f")
        threading.Thread(target=self._ecouter_protege, daemon=True).start()
        threading.Thread(target=self._relance_protegee, daemon=True).start()

    def _ecouter_protege(self):
        """Enveloppe l'ecoute : sans ca, une erreur tue le fil EN SILENCE.

        C'est exactement ce qui s'est passe le 18/07/2026 : apres avoir renomme le
        mot de reveil, l'interface appelait une fonction qui n'existait plus. Le fil
        d'ecoute est mort au premier mot de Utilisateur, sans un message, sans une
        trace a l'ecran. Le journal montrait qu'elle avait entendu, puis plus rien.
        Une panne muette est bien pire qu'une panne bruyante.
        """
        import traceback
        try:
            self.ecouter()
        except Exception as e:
            B.tracer("MICRO", f"ECOUTE INTERROMPUE : {type(e).__name__}: {e}", ecran=False)
            with open(B.LOG, "a", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            self.statut(f"L'ecoute s'est arretee ({type(e).__name__}) - voir le journal",
                        COUL_ALERTE)
            self.dire(f"  [le micro s'est arrete : {type(e).__name__}: {e}] "
                      f"Le detail est dans {B.LOG}", "alerte")

    # ─── entrée clavier ────────────────────────────────────────────────────
    def envoyer_texte(self, _=None):
        msg = self.champ.get().strip()
        if not msg:
            return
        self.champ.delete(0, "end")
        # Au clavier, le nom n'est pas requis : écrire, c'est déjà s'adresser à elle.
        self.file.put(("clavier", msg))

    # ─── entrée micro ──────────────────────────────────────────────────────
    def ecouter(self):
        micro = None
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and ("le micro" in d["name"]
                                                or "micro" in d["name"]):
                micro = i
                break
        if micro is None:
            micro = sd.default.device[0]
            self.statut("⚠ Micro préféré non trouvé — j'écoute le micro par "
                        "défaut. Si je n'entends rien, vérifie ton casque.",
                        "#e0a050")
        nom_micro = sd.query_devices(micro)["name"]
        B.tracer("MICRO", f"peripherique #{micro} {nom_micro}", ecran=False)

        vad = VAD(n_threads=1)
        preroll = int(B.PREROLL * B.SR / B.FRAME)
        ring = deque(maxlen=preroll)
        en_parole, tampon, compteur = False, [], 0

        # La fin de tour à l'intonation + la transcription anticipée — les
        # mêmes que la boucle console (module partagé ecoute\fin_de_tour.py),
        # mêmes réglages hérités de B. Repli automatique : silence fixe.
        import fin_de_tour
        juge_tour = fin_de_tour.FinDeTour()
        anticipee = fin_de_tour.OreilleAnticipee(B.transcrire)
        verdict_tour = None
        B.tracer("MICRO", "fin de tour à l'intonation active" if juge_tour.disponible
                 else f"smart-turn absent — silence fixe {B.SILENCE_FIN} s",
                 ecran=False)

        # ═══ LE DÉTECTEUR DE MICRO MUET — 21/07/2026 ═══════════════════════
        # Session de 10h57 : 48 s de surdité totale, aucun message d'erreur
        # nulle part. Cause : le micro coupé À LA SOURCE (bouton mute du
        # casque) — il livre des zéros parfaits que personne ne signalait.
        # Un vrai micro ouvert capte TOUJOURS un souffle. Après ~12 s de
        # zéros, Alice le DIT dans la fenêtre au lieu de rester sourde.
        frames_muettes = 0
        seuil_muet = int(12 * B.SR / B.FRAME)
        alerte_muet_faite = False

        # ═══ LES MÊMES GARDE-FOUS QUE LA BOUCLE CONSOLE — 21/07/2026 ════════
        # L'audit du jour a montré que la fenêtre — l'entrée PRINCIPALE — était
        # moins protégée que la console : pas de plafond de capture (un
        # détecteur coincé en « il parle » enregistrait sans fin), et un flux
        # micro mort était SIGNALÉ mais jamais RELANCÉ. C'est la 3e fois que
        # les deux entrées divergent ; tant qu'elles ne partagent pas leur
        # boucle d'écoute, chaque garde-fou doit être porté DANS LES DEUX.
        frames_zero = 0
        frames_zero_mort = int(B.MICRO_MORT_S * B.SR / B.FRAME)
        derniere_pulsation = time.time()

        flux = sd.InputStream(samplerate=B.SR, channels=1, dtype="int16",
                              blocksize=B.FRAME, device=micro)
        flux.start()
        try:
            while not self.fini:
                bloc, _ = flux.read(B.FRAME)
                if self.elle_parle.is_set():
                    ring.clear()
                    en_parole, tampon = False, []
                    verdict_tour = None
                    anticipee.oublier()
                    continue
                audio = bloc[:, 0]

                # ── GARDE-FOU : flux mort (que des zéros parfaits) ──
                if not audio.any():
                    frames_zero += 1
                    if frames_zero >= frames_zero_mort:
                        B.tracer("VEILLE", f"micro MUET depuis {B.MICRO_MORT_S} s "
                                           "(zéros parfaits) — je relance le flux",
                                 ecran=False)
                        try:
                            flux.stop(); flux.close()
                        except Exception:
                            pass
                        flux = sd.InputStream(samplerate=B.SR, channels=1,
                                              dtype="int16", blocksize=B.FRAME,
                                              device=micro)
                        flux.start()
                        frames_zero = 0
                        ring.clear(); tampon = []; en_parole = False
                        verdict_tour = None; anticipee.oublier()
                        continue
                else:
                    frames_zero = 0

                if int(np.abs(audio).max()) <= 2:
                    frames_muettes += 1
                    if frames_muettes >= seuil_muet and not alerte_muet_faite:
                        alerte_muet_faite = True
                        self.statut("⚠ Je n'entends QUE du silence : ton micro "
                                    "semble coupé (bouton mute du casque ? "
                                    "volume d'entrée Windows ?). Je te lis "
                                    "toujours au clavier.", "#e05050")
                        B.tracer("MICRO", "silence total ≥ 12 s — micro "
                                          "probablement coupé à la source",
                                 ecran=False)
                else:
                    if alerte_muet_faite:
                        self.statut("Micro de retour — je t'écoute.", "#8fbf8f")
                        B.tracer("MICRO", "signal revenu", ecran=False)
                    frames_muettes = 0
                    alerte_muet_faite = False

                parle = vad.predict(audio, frame_size=B.FRAME) > B.SEUIL_PAROLE

                # ── PULSATION : une trace de vie par minute d'écoute ──
                if time.time() - derniere_pulsation >= B.PULSATION_S:
                    niveau = float(np.abs(audio).mean())
                    B.tracer("VEILLE", f"micro vivant · niveau {niveau:.0f} · "
                                       f"vad {'PAROLE' if parle else 'silence'} · "
                                       f"capture {'EN COURS' if en_parole else 'non'}",
                             ecran=False)
                    derniere_pulsation = time.time()

                if not en_parole:
                    ring.append(audio.copy())
                    if parle:
                        en_parole, tampon, compteur = True, list(ring), 0
                        tampon.append(audio.copy())
                    continue
                tampon.append(audio.copy())
                # ── GARDE-FOU : capture sans fin (même règle que la console) ──
                if parle and len(tampon) * B.FRAME / B.SR >= B.CAPTURE_MAXI:
                    B.tracer("MICRO", f"capture forcée à {B.CAPTURE_MAXI} s — le "
                                      "détecteur ne voyait plus de silence",
                             ecran=False)
                elif parle:
                    compteur = 0
                    verdict_tour = None      # il reparle : verdict et pari caducs
                    anticipee.oublier()
                    continue
                else:
                    compteur += 1
                    s_silence = compteur * B.FRAME / B.SR
                    base_parole = len(tampon) - compteur
                    # le pari : la transcription part dès le début du silence
                    if s_silence >= 0.15 and not anticipee.deja_lancee(base_parole):
                        anticipee.lancer(np.concatenate(tampon), base_parole)
                    # le juge d'intonation : une seule fois par silence
                    if (verdict_tour is None and juge_tour.disponible
                            and s_silence >= B.SILENCE_COURT):
                        verdict_tour = juge_tour.a_fini(
                            np.concatenate(tampon[-int(8 * B.SR / B.FRAME):]))
                        if verdict_tour is not None:
                            B.tracer("TOUR", ("fini" if verdict_tour >= B.SEUIL_TOUR
                                              else "il cherche ses mots — j'attends")
                                     + f" (probabilité {verdict_tour:.2f})",
                                     ecran=False)
                    if verdict_tour is None:
                        attente = B.SILENCE_FIN
                    elif verdict_tour >= B.SEUIL_TOUR:
                        attente = B.SILENCE_COURT
                    else:
                        attente = B.SILENCE_PLAFOND
                    if s_silence < attente:
                        continue

                en_parole = False
                verdict_tour = None
                base_recolte = len(tampon) - compteur
                ring.clear()
                duree = (len(tampon) - preroll) * B.FRAME / B.SR
                if duree < B.MIN_PAROLE:
                    anticipee.oublier()
                    continue
                self.statut("Je transcris ce que tu viens de dire...")
                B.tracer("MICRO", f"parole captée ({duree:.1f} s)", ecran=False)
                t0 = time.time()
                texte = anticipee.recolter(base_recolte)
                if texte is not None:
                    B.tracer("OREILLE", f"({time.time()-t0:.1f} s, anticipée) "
                                        f"\"{texte}\"", ecran=False)
                else:
                    try:
                        texte = B.transcrire(np.concatenate(tampon))
                    except Exception as e:
                        B.tracer("OREILLE", f"ERREUR : {type(e).__name__}: {e}", ecran=False)
                        self.statut("Je t'écoute", "#8fbf8f")
                        continue
                    B.tracer("OREILLE", f"({time.time()-t0:.1f} s) \"{texte}\"", ecran=False)
                if not texte:
                    self.statut("Je t'écoute", "#8fbf8f")
                    continue

                # Whisper invente des formules de fin de vidéo devant le silence.
                # Ce filtre existait dans la boucle console mais MANQUAIT ici
                # (audit du 19/07/2026) : par la fenêtre, un « merci » fantôme
                # réveillait Alice et partait dans sa mémoire — exactement le
                # défaut qu'on croyait corrigé.
                if B.est_une_hallucination(texte, duree):
                    B.tracer("OREILLE", "texte fantôme (whisper a meublé du "
                                        "silence) — ignoré", ecran=False)
                    self.statut("Je t'écoute", "#8fbf8f")
                    continue

                eveillee = time.time() < self.eveillee_jusqua
                detectee, mot = B.contient_alice(texte)
                if not detectee and not eveillee:
                    B.tracer("RÉVEIL", "nom non prononcé — j'ignore", ecran=False)
                    self.dire(f"  (entendu, mais tu ne l'as pas appelée : « {texte} »)\n",
                              "info")
                    self.statut("Je t'écoute", "#8fbf8f")
                    continue
                B.tracer("RÉVEIL", f"appelée (sur « {mot} »)" if detectee
                         else "déjà éveillée", ecran=False)
                self.file.put(("micro", texte))
        finally:
            # Le flux n'est plus tenu par un « with » (on doit pouvoir le
            # relancer en route) : on le referme donc nous-mêmes en partant.
            try:
                flux.stop(); flux.close()
            except Exception:
                pass

    # ─── la relance : elle prend la parole après un long silence ───────────
    def _relance_protegee(self):
        try:
            self.relance_veilleur()
        except Exception as e:
            B.tracer("RELANCE", f"veilleur arrêté : {type(e).__name__}: {e}",
                     ecran=False)

    def relance_veilleur(self):
        """Elle prend la parole d'elle-même — mêmes paliers que la boucle console.

        Cette relance MANQUAIT à la fenêtre (audit du 19/07/2026) : la boucle
        console l'avait, la fenêtre non — deux comportements pour la même Alice,
        alors que la fenêtre est l'entrée principale. Les règles sont celles de
        boucle_alice.py : paliers qui s'allongent (2, 4 puis 8 min), remise
        à zéro dès qu'il parle, et silence définitif au bout des paliers.
        """
        while not self.fini:
            time.sleep(5)
            if (self.n == 0 or self.elle_parle.is_set()
                    or self.palier_relance >= len(B.PALIERS_RELANCE)):
                continue
            silence = time.time() - self.derniere_parole
            if silence < B.PALIERS_RELANCE[self.palier_relance]:
                continue
            # Un échange en cours ? On n'interrompt jamais : on repassera dans 5 s.
            if not self.verrou.acquire(blocking=False):
                continue
            try:
                self.palier_relance += 1
                self.derniere_parole = time.time()
                self.eveillee_jusqua = time.time() + B.FENETRE_CONVERSATION
                self.elle_parle.set()          # micro en pause : elle va parler
                try:
                    mots = B.relancer(int(silence))
                finally:
                    time.sleep(0.25)
                    self.elle_parle.clear()
                if mots:
                    self.dire("\nALICE  ", "elle")
                    self.dire(f"{mots}\n", "elle")
                    B.tracer("RELANCE", f"elle a pris la parole : « {mots[:70]} »",
                             ecran=False)
                    self.statut(f"Je t'écoute — parle librement pendant "
                                f"{B.FENETRE_CONVERSATION} s, ou écris", "#8fbf8f")
            finally:
                self.verrou.release()

    # ─── le fil qui traite les échanges, d'où qu'ils viennent ──────────────
    def travailleur(self):
        while not self.fini:
            try:
                origine, texte = self.file.get(timeout=0.3)
            except queue.Empty:
                continue
            with self.verrou:
                self.traiter(origine, texte)

    def traiter(self, origine, texte):
        self.n += 1
        marque = "" if origine == "micro" else "  (écrit)"
        self.dire(f"\nTOI{marque}  ", "lui")
        self.dire(f"{texte}\n", "lui")

        demande = B.retirer_nom(texte) if origine == "micro" else texte
        if demande != texte:
            B.tracer("RÉVEIL", f"transmis sans son nom : \"{demande}\"", ecran=False)

        self.statut("Elle réfléchit...", "#c9a0dc")
        t0 = time.time()
        try:
            res = B.demander_au_cerveau(demande)
        except Exception as e:
            B.tracer("CERVEAU", f"ERREUR : {type(e).__name__}: {e}", ecran=False)
            self.dire("  [le cerveau n'a pas répondu — voir le journal]\n", "alerte")
            self.statut("Je t'écoute", "#8fbf8f")
            return
        t_cerveau = time.time() - t0
        if res.get("erreur") or not res.get("texte"):
            B.tracer("CERVEAU", f"ERREUR : {res.get('erreur')}", ecran=False)
            self.dire("  [le cerveau a renvoyé une erreur — voir le journal]\n", "alerte")
            self.statut("Je t'écoute", "#8fbf8f")
            return

        reponse = res["texte"]
        B.tracer("CERVEAU", f"({res['t_llm']:.1f} s · {len(reponse.split())} mots · "
                            f"{res['n_souvenirs']} souvenir(s) en "
                            f"{res['t_recup']*1000:.0f} ms)", ecran=False)
        with open(B.LOG, "a", encoding="utf-8") as f:
            f.write(f"           RÉPONSE   \"{reponse}\"\n")

        self.dire("ALICE  ", "elle")
        self.dire(f"{reponse}\n", "elle")

        # ── elle parle ──
        self.statut("Elle parle...", "#c9a0dc")
        self.elle_parle.set()
        t0 = time.time()
        premier = total = 0.0
        try:
            premier, total = B.parler(reponse)
            B.tracer("VOIX", f"1er son après {premier:.1f} s · {total:.1f} s dites "
                             f"· {time.time()-t0:.1f} s au total", ecran=False)
        except Exception as e:
            B.tracer("VOIX", f"ERREUR : {type(e).__name__}: {e}", ecran=False)
            self.dire("  [la voix a échoué — voir le journal]\n", "alerte")
        finally:
            time.sleep(0.25)
            self.elle_parle.clear()

        self.dire(f"  cerveau {t_cerveau:.1f} s · 1er son {premier:.1f} s · "
                  f"{total:.1f} s de parole · {res['n_souvenirs']} souvenir(s)\n", "info")
        # Il vient de parler : la relance repart du premier palier.
        self.derniere_parole = time.time()
        self.palier_relance = 0
        self.eveillee_jusqua = time.time() + B.FENETRE_CONVERSATION
        self.statut(f"Je t'écoute — parle librement pendant "
                    f"{B.FENETRE_CONVERSATION} s, ou écris", "#8fbf8f")

    # ─── fermeture propre ──────────────────────────────────────────────────
    def fermer(self):
        if self.fini:
            return
        self.fini = True
        B.tracer("ARRÊT", "fermeture demandée par la fenêtre", ecran=False)

        # LE RANGEMENT DE LA MÉMOIRE — c'est ICI qu'il a lieu maintenant, et nulle
        # part ailleurs. Pendant la conversation, les phrases sont juste mises de
        # côté (gratuit). Le tri, qui coûte de la carte graphique, ne tourne donc
        # plus pendant que Utilisateur joue : c'était la cause de son lag.
        # Mesuré : ~10 s pour une session de 12 échanges.
        self.statut("Elle range ses souvenirs... (une dizaine de secondes)", COUL_INFO)
        self.racine.update()
        try:
            import requests as _rq
            r = _rq.post("http://127.0.0.1:8082/ranger", timeout=600).json()
            B.tracer("MÉMOIRE", f"session rangée : {r.get('souvenirs')} souvenir(s) "
                                f"en {r.get('duree', 0):.1f} s", ecran=False)
            self.statut(f"{r.get('souvenirs')} souvenir(s) retenu(s). Au revoir.", COUL_INFO)
        except Exception as e:
            # Pas de panique : les phrases sont sur le disque, le prochain
            # démarrage les rangera avant de commencer.
            B.tracer("MÉMOIRE", f"rangement impossible ({type(e).__name__}) — "
                                f"ce sera fait au prochain démarrage", ecran=False)
            self.statut("Rangement reporté au prochain démarrage.", COUL_ALERTE)
        self.racine.update()

        for p in self.procs:
            try:
                p.terminate()
            except Exception:
                pass
        # Plus d'appel à LM Studio ici (retiré le 19/07/2026) : le cerveau tourne
        # sur notre llama.cpp, qui vit dans l'enclos — Windows le tue, et rend la
        # mémoire vidéo, dès que cette fenêtre disparaît. Et décharger LM Studio
        # d'office touchait un logiciel que Utilisateur peut utiliser lui-même.
        self.racine.destroy()


def main():
    racine = tk.Tk()
    Interface(racine)
    racine.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
