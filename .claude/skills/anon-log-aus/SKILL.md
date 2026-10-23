---
name: anon-log-aus
description: Protokollierung im lokalen Anonymisierungs-Gateway ausschalten (Weiterleitung läuft weiter).
disable-model-invocation: true
allowed-tools: Bash(curl -s -X POST http://127.0.0.1:8787/_gateway/logging/off)
---
Antwort des Gateways (leer = nicht erreichbar):

!`curl -s -X POST http://127.0.0.1:8787/_gateway/logging/off`

Bestätige in einem Satz auf Deutsch, ob die Protokollierung jetzt aus ist. Keine weiteren Aktionen.
