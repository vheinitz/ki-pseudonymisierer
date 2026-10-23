---
name: anon
description: Status des lokalen Anonymisierungs-Gateways anzeigen (läuft es, Protokoll an/aus, Zahl der Anfragen).
disable-model-invocation: true
allowed-tools: Bash(curl -s http://127.0.0.1:8787/_gateway/status)
---
Antwort des Gateways (leer = nicht erreichbar, dann läuft `gateway.py` nicht):

!`curl -s http://127.0.0.1:8787/_gateway/status`

Gib den Status in einem Satz auf Deutsch wieder. Keine weiteren Aktionen.
