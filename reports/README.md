# Rapports mensuels

Le rapport place une **synthèse exécutive d'une page** en tête : indicateurs clés, évolution par rapport au mois précédent et trois alertes prioritaires. Le détail par ligne, les graphiques de fiabilité, la carte de risque, le profil horaire et la distribution des retards sont placés en annexe.

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

Après le rattachement des arrêts aux communes, une seule commande génère les rapports de toutes les communes détectées et crée un index CSV :

```bash
.venv/bin/python reports/generate_all_municipal_reports.py --month 2026-07 --compile
```

Les documents sont rangés dans `reports/output/2026-07/communes/`, un dossier par commune. Sans `--compile`, seuls les fichiers LaTeX sont produits, ce qui est pratique pour vérifier ou adapter la mise en page avant la compilation PDF.
