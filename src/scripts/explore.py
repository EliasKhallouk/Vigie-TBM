import json

import requests
from google.transit import gtfs_realtime_pb2
from google.protobuf.json_format import MessageToDict

URL_TRIPUPDATES = "https://bdx.mecatran.com/utw/ws/gtfsfeed/realtime/bordeaux?apiKey=opendata-bordeaux-metropole-flux-gtfs-rt"
OUTPUT_FILE = "../../data/explore_output.json"

session = requests.Session()
session.trust_env = False

response = session.get(URL_TRIPUPDATES, timeout=30)
response.raise_for_status()

feed = gtfs_realtime_pb2.FeedMessage()
feed.ParseFromString(response.content)

output = {
    "entity_count": len(feed.entity),
    "timestamp": feed.header.timestamp,
    "preview_entities": [
        MessageToDict(entity, preserving_proto_field_name=True)
        for entity in feed.entity[:]
    ],
}

with open(OUTPUT_FILE, "w", encoding="utf-8") as file_handle:
    json.dump(output, file_handle, ensure_ascii=False, indent=2)

print(f"Sortie écrite dans {OUTPUT_FILE}")