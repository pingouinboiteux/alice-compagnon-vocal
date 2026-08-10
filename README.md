# Alice - notes et extraits rares pour un compagnon vocal local

Ce depot public n'est pas le depot prive complet d'Alice. C'est une sortie
preparee a la main pour partager :

- des choix d'architecture qui ont vraiment compte ;
- des scripts et tests difficiles a reinventer du premier coup ;
- des pieges payes sur AMD / Windows / francais ;
- des morceaux de code montrables sans fuite de donnees privees.

Le but n'est pas de vendre un faux "one click install". Le but est d'aider les
gens qui cherchent des reperes reels sur un terrain encore peu documente, sans
inonder GitHub de bruit.

## Regle editoriale

Un element ne sort en public que s'il coche au moins un de ces cas :

- il montre un probleme rare sous Windows ou AMD ;
- il a change une decision grace a une mesure nette ;
- il pose un garde-fou difficile a improviser ;
- il peut faire gagner un vrai bloc de temps a quelqu'un.

Si un fichier n'apporte qu'un nom d'outil, une integration banale, ou du code
facile a retrouver ailleurs, il reste dans le depot prive.

## Ce qui tourne aujourd'hui sur la machine de reference

| organe | choix valide |
| --- | --- |
| oreille | Canary 1B v2 |
| voix | Chatterbox |
| cerveau | Ministral-3-8B via llama.cpp Vulkan |
| memoire | `memoire_v3` |
| vision | Qwen3-VL-4B |
| OS | Windows 11 |
| GPU | AMD Radeon RX 7900 XTX |

## Ce depot public contient

- des extraits de code sur l'audio Windows, la fermeture propre des processus,
  la selection GPU et quelques gardes de securite ;
- des tests qui montrent comment verrouiller des frontieres techniques avant de
  tout casser ;
- une synthese des pieges et des mesures qui ont vraiment compte ;
- seulement une petite partie du projet, choisie pour sa rarete utile.

## Ce depot public ne contient pas

- la memoire vivante d'Alice ;
- les modeles ;
- les caches ;
- les secrets ;
- les chemins prives de la machine d'origine ;
- l'integralite du depot source prive ;
- le chantier complet du corps, qui suit une specification separee et un
  developpement actif a part.

## Fichiers a regarder en premier

- `PIEGES.md` : les lecons les plus utiles
- `RESULTATS.md` : quelques mesures qui ont vraiment change le projet
- `ecoute/menage.py` : tuer proprement un arbre de processus sous Windows
- `controle_alice/interface/voix.py` : audio en flux sous Windows
- `controle_alice/tests/test_frontiere.py` : garder une interface legere
- `outils/configurer_affectation_gpu.ps1` : epingler les bons programmes sur la
  bonne carte Windows
- `vision_alice/vision/hardware/dxgi.py` : lecture hardware cote Windows

## Position franche

Ce projet est tres specifique :

- francais ;
- local ;
- Windows ;
- AMD ;
- compagnon vocal, pas simple assistant utilitaire.

Justement, c'est pour cela qu'il peut aider : cette combinaison est peu
referencee publiquement. Le filtre public doit donc rester strict, sinon on
perd l'interet principal du depot.

## A propos du corps

Le corps d'Alice suit une specification separee et plus large que ce paquet
public :

- base de reference : `ALICE_CORPS_specification.md`
- chantier actif distinct
- publication a traiter a part, quand cette partie sera assez stable

Ce depot public se concentre donc sur ce qui est deja partageable sans brouiller
un sous-projet encore en mouvement.

## Usage recommande

Utilise ce depot comme :

- une carte ;
- une boite a idees ;
- une collection de garde-fous ;
- un point de depart pour tes propres tests.

Pas comme une promesse de reproduction parfaite a l'identique.
