# Vigie-TBM

GTFS statique (horaires théoriques) :
https://bdx.mecatran.com/utw/ws/gtfsfeed/static/bordeaux?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt

GTFS-RT TripUpdates (retards/annulations) :
https://bdx.mecatran.com/utw/ws/gtfsfeed/realtime/bordeaux?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt

GTFS-RT VehiclePositions (position des véhicules) :
https://bdx.mecatran.com/utw/ws/gtfsfeed/vehicles/bordeaux?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt

#### POUR CE CONNECTER
elias@hp-info-01:~/PROJECT/Vigie-TBM$ ssh -i ~/.ssh/oracle-ek-hub.key ubuntu@88.96.51.44

#### POUR CE METTRE DANS L'ENVIRONNEMENT 
ubuntu@ek-hub-vnic:~$ cd Vigie-TBM/
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ source .venv/bin/activate

#### CRÉER LA BDD
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ python3 src/scripts/db.py

#### POUR LANCER LE SCRIPTE DE COLLECT DE DONNÉES
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo nano /etc/systemd/system/vigie-tbm-collect.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl daemon-reload
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl enable vigie-tbm-collect.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl start vigie-tbm-collect.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl status vigie-tbm-collect.service

#### POUR LANCER LE SCRIPTE DE COLLECT D'ALERTES
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo nano /etc/systemd/system/vigie-tbm-collect.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl daemon-reload
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl enable vigie-tbm-collect-alerts.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl start vigie-tbm-collect-alerts.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl status vigie-tbm-collect-alerts.service

#### POUR AFFICHER LE STATUS DES SERVICES
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl status vigie-tbm-collect.service vigie-tbm-collect-alerts.service vigie-tbm-dashboard.service

#### POUR AFFICHER LES LOG DE LA COLLECT
ubuntu@ek-hub-vnic:~/Vigie-TBM$ tail -f data/collect.log

#### LANCER LE DASHBOARD
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl daemon-reload
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl enable vigie-tbm-dashboard.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl start vigie-tbm-dashboard.service
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ sudo systemctl status vigie-tbm-dashboard.service 

//avant :(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ .venv/bin/streamlit run dashboard/app.py


#### GÉNERER LES RAPPORTS
##### Rapport réseau
.venv/bin/python ./reports/generate_single_report.py --month 2026-07 --network --compile

##### Rapport commune
.venv/bin/python ./reports/generate_single_report.py --month 2026-07 --commune "Mérignac" --compile

##### Rapports de toutes les communes + Bordeaux Métropole :
./reports/generate_all_reports.py --month 2026-07 --compile

(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$ .venv/bin/python reports/generate_all_municipal_reports.py --month 2026-07 --compile
(.venv) ubuntu@ek-hub-vnic:~/Vigie-TBM$  bash /home/ubuntu/Vigie-TBM/reports/output/2026-07/communes/compile_all.sh
