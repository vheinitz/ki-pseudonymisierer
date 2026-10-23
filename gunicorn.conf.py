# Ein Worker: jedes spaCy-Modell belegt ~0,5–1 GB RAM pro Prozess.
# Parallelität über Threads; Engines werden pro Prozess einmal geladen.
bind = "127.0.0.1:5000"
workers = 1
threads = 4
timeout = 900  # KI-Anfragen an Agenten können Minuten dauern
