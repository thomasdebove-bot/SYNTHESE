# SYNTHESE

## Script de détection de conflits Revit

Le fichier `revit_conflicts_viewer.py` est un script **pyRevit / Dynamo** qui permet de :

- détecter les conflits géométriques entre **maquettes liées**,
- générer une **vue 3D avec coupe (section box)** pour chaque conflit,
- parcourir les conflits via une **fenêtre de navigation**,
- afficher des **propositions de contournement** selon les catégories en conflit.

## Pré-requis

- Revit avec des maquettes liées chargées,
- pyRevit ou Dynamo (Python 3) disponible,
- script lancé dans un document hôte ouvert dans Revit.

## Utilisation

1. Lancer `revit_conflicts_viewer.py` depuis une extension pyRevit **ou** un nœud Python Dynamo.
2. Vérifier que le document actif contient des maquettes liées.
3. Cliquer sur **Suivant/Précédent** pour parcourir les conflits.
4. Cliquer sur **Ouvrir coupe** pour ouvrir la vue 3D dédiée au conflit sélectionné.

## Notes

- Le script cible en priorité les catégories structure et MEP (murs, planchers, gaines, tuyaux, chemins de câble, etc.).
- Les suggestions affichées sont des pistes de résolution rapide à valider en coordination BIM.
