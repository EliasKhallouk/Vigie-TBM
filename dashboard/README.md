# Dashboard Vigie TBM

Prototype local de visualisation des données collectées dans [data/vigie_tbm.db](/home/elias/PROJECT/Vigie-TBM/data/vigie_tbm.db).

## 1) Installer la dépendance

```bash
pip install streamlit
```

(`pandas` est déjà utilisé par le projet.)

## 2) Appliquer la migration `departure_time` (une fois)

```bash
python3 src/scripts/migrate_departure_time.py --db-path data/vigie_tbm.db
```

## 3) Lancer le dashboard

```bash
streamlit run dashboard/app.py
```

Le dashboard couvre:
- classement des lignes par `% retard > 5 min`,
- évolution journalière par ligne,
- vue par tranche horaire (si `departure_time` non NULL),
- distribution des retards,
- taux d'arrêts sautés.
