---
name: anon-log-an
description: Protokollierung im lokalen Anonymisierungs-Gateway einschalten.
disable-model-invocation: true
allowed-tools: Bash(curl -s -X POST http://127.0.0.1:8787/_gateway/logging/on)
---
Antwort des Gateways (leer = nicht erreichbar):

!`curl -s -X POST http://127.0.0.1:8787/_gateway/logging/on`

Bestätige in einem Satz auf Deutsch, ob die Protokollierung jetzt an ist. Keine weiteren Aktionen.
