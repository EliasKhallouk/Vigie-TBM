# Rapports mensuels

Le rapport place une **synthèse exécutive d'une page** en tête : indicateurs clés, évolution par rapport au mois précédent et trois alertes prioritaires. Le détail par ligne, les graphiques de fiabilité, la carte de risque, le profil horaire, la distribution des retards et le détail par arrêt sont placés en annexe.

## Seuils et catégories utilisés

### Message exécutif (première phrase de la synthèse)

La phrase d'ouverture s'adapte à la ponctualité globale du périmètre (commune ou réseau) :

| Ponctualité | Message |
|---|---|
| ≥ 95 % | « affiche une ponctualité excellente (X %). » |
| ≥ 90 % | « enregistre un bon niveau de ponctualité (X %). » |
| ≥ 85 % | « présente une fiabilité correcte (X %), encore perfectible. » |
| ≥ 80 % | « montre une fiabilité intermédiaire (X %). » |
| ≥ 75 % | « connaît des difficultés de ponctualité notables (X %). » |
| ≥ 65 % | « enregistre une ponctualité insuffisante (X %). » |
| < 65 % | « subit des retards critiques (X % de passages à l'heure). » |

Si le taux d'arrêts sautés dépasse 5 %, une phrase complémentaire l'indique.

### Couleurs des indicateurs KPI

Chaque KPI du haut de page est coloré selon sa propre valeur :

| Métrique | Vert (bon) | Orange (moyen) | Rouge (alerte) |
|---|---|---|---|
| **Ponctualité** | ≥ 90 % | ≥ 80 % | < 80 % |
| **Retard moyen / médian** | ≤ 60 s | ≤ 120 s | > 120 s |
| **Arrêts sautés** | ≤ 1 % | ≤ 5 % | > 5 % |

Les couleurs LaTeX utilisées : `vigiegreen` (vert), `vigieorange` (orange), `alert` (rouge), `vigieblue` (bleu par défaut).

### Score de fiabilité

\[
\text{Score} = \max(0,\; \text{Ponctualité} - 2 \times \text{Taux d'arrêts sautés})
\]

- **Ponctualité** : part des passages avec un retard de départ ≤ 5 min (0–100 %).
- **Taux d'arrêts sautés** : part des passages marqués `SKIPPED` parmi les passages programmés (0–100 %).
- Le score est borné entre 0 et 100. Plus il est bas, plus la ligne est prioritaire.

### Comparaison réseau (rapports communaux uniquement)

Chaque valeur affichée est accompagnée de la valeur **Réseau TBM global** pour la même ligne et la même période, en petit texte gris. Cela permet de relativiser la performance locale par rapport à la moyenne du réseau.

## Rapport réseau

```bash
.venv/bin/python reports/generate_monthly_report.py --month 2026-07 --recipient "Bordeaux Métropole et TBM" --compile
```

Sans `--month`, le dernier mois présent dans la base est choisi. Le script produit toujours un fichier `.tex`; l'option `--compile` produit aussi un PDF si `pdflatex` est installé.

## Version destinée à une commune

Les rapports territoriaux sont filtrés sur les **arrêts réellement situés dans la commune**, et non sur les lignes : une ligne peut traverser plusieurs villes. Calculez d'abord le rattachement géographique, à partir des coordonnées GTFS et des limites communales officielles :

```bash
.venv/bin/python src/scripts/assign_stop_municipalities.py
```

Copiez ensuite le modèle de profils et indiquez la commune concernée :

```bash
cp reports/recipients.example.json reports/recipients.json
# éditer communes et description du profil concerné
.venv/bin/python reports/generate_monthly_report.py --month 2026-07 --profile mairie_exemple --compile
```

On peut aussi faire une version ponctuelle sans profil :

```bash
.venv/bin/python reports/generate_monthly_report.py --month 2026-07 \
  --recipient "Mairie de Mérignac" --communes "Mérignac" --compile
```

Les rapports générés sont placés dans `reports/output/`, qui est volontairement ignoré par Git. L'envoi doit rester une étape séparée et validée manuellement avant diffusion.

## Générer tous les rapports communaux

Après le rattachement des arrêts aux communes, une seule commande génère les rapports de toutes les communes détectées :

```bash
.venv/bin/python reports/generate_all_municipal_reports.py --month 2026-07 --compile
```

Les documents sont rangés dans `reports/output/2026-07/communes/`, un dossier par commune. Avec `--compile`, seuls les fichiers PDF sont conservés (les fichiers `.tex`, `.aux`, `.log` sont automatiquement supprimés). Sans `--compile`, seuls les fichiers LaTeX sont produits, ce qui est pratique pour vérifier ou adapter la mise en page avant la compilation PDF.
