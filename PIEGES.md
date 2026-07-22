# Les pièges — ce que ce projet a payé pour vous

Tout ce qui suit a été **mesuré sur la machine**, pas supposé. C'est la vraie
valeur de ce dépôt : chaque point représente des heures perdues.

---

## Le TTS (Kyutai Pocket TTS)

**`max_tokens` ne compte PAS des trames audio.** Ce sont des jetons de **texte**
par morceau, pour le découpeur interne. Le défaut (50 ≈ 16 s de parole) est
l'horizon d'entraînement du modèle. En mettant une grande valeur pour « enlever
une limite », on désactive le découpage qui protège des mots sautés et on pousse
le modèle hors de sa distribution. Symptômes : plafond dur vers 15 s d'audio, et
des mots avalés. **Ne passez pas ce paramètre.**

**Le rembourrage « `. . . .` » en fin de texte est PRONONCÉ.** Ce contournement
circule pour éviter les fins avalées ; le modèle lit ces pauses, et elles
deviennent des queues de silence. Le vrai paramètre est **`frames_after_eos`**
(la config française recommande 8 trames de 80 ms).

**Les textes de moins de ~40 caractères sont peu fiables.** Le code amont le
reconnaît. Il existe une parade officielle (`pad_with_spaces_for_short_inputs`)
**désactivée par défaut pour le français** : activez-la, et groupez vos phrases.

**Le modèle reproduit la qualité de la référence de clonage, silence compris.**
Un demi-seconde de blanc en tête de votre échantillon = des démarrages lents à
chaque réplique. Rognez la référence, et remontez son volume : une référence
molle donne un ancrage d'identité mou (dérives de timbre, voix qui change).

**Aucun normaliseur de texte.** Les chiffres sortent déformés : convertissez
« 15h30 » en « quinze heures trente » avant l'envoi.

**Corruptions intermittentes** (mots répétés, changement de voix) : bug amont
connu, sans correctif. La seule parade fiable est de **réécouter chaque unité
produite avec un moteur de reconnaissance vocale, avant de la jouer**, et de la
refaire si trop de mots manquent. C'est ce que fait le « portier » de ce projet.

---

## La reconnaissance vocale et la fin de tour

**smart-turn : les exports `v3.1-cpu` et `v3.2-cpu` répondent ~0,98 à TOUT**
(bruit blanc et silence compris) avec le prétraitement officiel de Pipecat. Seul
le **v3.0** discrimine réellement (0,98 phrase finie / 0,06 phrase coupée).
Vérifié en comparant les trois exports sur les mêmes échantillons.

**Il faut normaliser l'audio (`do_normalize`) avant le mel-spectrogramme.** Sans
cette étape, même résultat : « oui » à tout, en silence. Un auto-test avec du
bruit blanc (qui doit sortir un score bas) attrape ça immédiatement.

**whisper.cpp complète toujours l'audio à 30 secondes** avant de le traiter : le
coût ne dépend pas de la longueur de la phrase, seulement du nombre de cœurs.
L'option `-ac` raccourcit cette fenêtre (6,6 s → 3,4 s par phrase ici) ; ne
descendez pas trop bas, le modèle repasse en repli et devient plus lent.

**Un filtre anti-hallucinations écrit pour whisper devient nuisible avec un
modèle qui n'hallucine pas.** Les listes de formules fantômes (« merci »,
« voilà ») jetaient de vraies paroles courtes de l'utilisateur sous Parakeet.
Réservez ces règles au moteur pour lequel elles ont été écrites.

---

## Le modèle de langage (llama.cpp)

**`--no-mmap` a libéré 12,5 Go de RAM système.** Le jeu ramait, la VRAM semblait
en cause : c'était la mémoire vive, bloquée à 98 %. Vitesse identique avec ou
sans. Ajoutez `-fa on` et un cache KV en `q8_0` pour un gigaoctet de plus.

**`--parallel 1`** donne **+48 % de vitesse** quand il n'y a qu'un utilisateur :
les emplacements de conversation supplémentaires coûtent cher pour rien.

**Un modèle qui rame quand un jeu tourne, c'est le partage du temps de calcul
GPU, pas la VRAM.** Mesuré : -43 % / -75 % / -95 % selon la charge, et tout
remonte dès que la charge s'arrête. Aucun réglage de priorité GPU n'existe sous
Windows/AMD : le seul levier est de réduire la demande.

**Les finetunes de jeu de rôle 12B n'ont pas tenu** (Mag Mell, Rocinante,
RPMax) : entraînés sur des corpus anglais, ils dégradent la grammaire française
et aucun ne tient un personnage sous attaque. Un modèle généraliste 24B les bat
nettement sur ce terrain.

---

## La mémoire (Mem0)

**Mem0 2.0.x est en ajout seul** : le mécanisme à quatre opérations
(ajouter / corriger / supprimer / ignorer) n'est plus branché, aucun réglage ne
le rallume. Résultat : les doublons s'empilent indéfiniment. Il faut
dédoublonner et réconcilier soi-même (ce projet le fait avec un juge LLM à
température 0, dont la consigne est « dans le doute, garde les deux »).

**`get_all()` plafonne silencieusement à 20 résultats**, quelle que soit la
limite demandée. Un outil de diagnostic bâti dessus ment sans le dire.

**Les hallucinations deviennent des souvenirs si on n'y prend garde.** Le
mécanisme : l'IA invente un détail, l'utilisateur y répond, sa réponse est
mémorisée comme un fait. C'est auto-entretenu et invisible. Parade : un portier
à l'entrée qui écarte les phrases méta et les échos de ce que l'IA vient de
dire, et une règle de tri explicite (« si le sujet vient de l'IA, on ne retient
pas »).

---

## Les voix clonées

**Une référence à l'accent anglais dans un modèle TTS multilingue le fait
TRADUIRE au lieu de lire** (6 dérapages sur 10 mesurés : « Because you're
putting a dépourvu with that question »). Il faut une référence **dans la langue
cible**. La position des mainteneurs amont est claire là-dessus.

**Vérifiez la qualité de la source avant de soupçonner le moteur.** Un timbre
« métallique » attribué au moteur venait en réalité d'une référence
ré-échantillonnée à 24 kHz. Le moteur clonait fidèlement les dégâts.

**Jouer un son à une fréquence différente de celle de sa fabrication change sa
hauteur.** Évident, mais c'est un défaut silencieux : aucune erreur nulle part,
juste une voix qui sonne « plus grave » sans raison apparente.

---

## Windows

**Windows ne tue pas les arbres de processus.** Arrêter un service Python
laisse vivre le serveur qu'il avait lancé. 23 Go de zombies retrouvés — dont un
qui **répondait sur le port à la place du vrai service** et corrompait les
mesures. Parade : un **Job Object** Windows ; tout ce qui y est lancé meurt avec
le parent, quelle que soit la façon dont le parent disparaît.

**PowerShell 5.1 lit les `.ps1` en CP1252**, pas en UTF-8. Un seul caractère
accentué, même dans un commentaire, casse le script entier dès la première
ligne. Gardez vos scripts PowerShell en pur ASCII.

**La console Windows en CP1252 peut tuer un service.** Un caractère exotique
dans une ligne de trace lève une exception en pleine réponse. Mettez
`sys.stdout.reconfigure(errors="replace")` en tête de chaque service.

**Le « démarrage rapide » de Windows fait que « Arrêter » n'est pas un vrai
redémarrage** : le compteur d'uptime ment, et certains bugs survivent aux
extinctions.

---

## Les deux leçons de méthode

**Une interdiction dans un prompt ne tient pas ; un filtre déterministe tient.**
Vrai pour les didascalies, les fuites de consignes, les répétitions, les surnoms
mal orthographiés, l'écriture inclusive prononcée. À chaque fois, sans
exception, la consigne a cédé et le code a tenu.

**Vos instruments de mesure mentiront.** Douze fois dans ce projet : un compteur
VRAM qui ne voit que son propre processus, des bancs d'essai tournant en
parallèle qui étouffent la machine qu'ils mesurent, un test qui tient lui-même
la ressource qu'il teste, une mauvaise unité de mesure, des seuils faux, un
lancement en arrière-plan qui n'a jamais démarré. **Règle : quand une mesure
contredit le vécu, vérifiez l'instrument avant de soupçonner le système.**
