flowchart TD
    START([Neue Datei in Inbox]) --> EXIF[IA-01: EXIF extrahieren\nVideos: + ffprobe Metadaten\nDatum, GPS/ISO 6709, Dauer,\nAuflösung, Codec, Framerate,\nBitrate, Rotation]
    EXIF --> DUPES[IA-02: Duplikate erkennen\nSHA256 exakt + pHash ähnlich\nVideos: nur SHA256 hier]
    DUPES --> DUPECHECK{Duplikat?}
    DUPECHECK -->|Ja| STOP([Pipeline stopp])
    DUPECHECK -->|Nein| GEO[IA-03: Geocoding\nGPS → Ortsname]

    GEO --> CONVERT[IA-04: Temp. Konvertierung für KI\nVideos: N Frames extrahiert\nvia ffmpeg]
    CONVERT --> VPHASH{Video?\npHash aus Frames}
    VPHASH -->|pHash-Match| STOP
    VPHASH -->|OK| COLLECT

    COLLECT[Alle Metadaten sammeln]
    COLLECT --> M1[Kamera & Datum]
    COLLECT --> M2[GPS + Ortsname]
    COLLECT --> M3[Dateigrösse in KB]
    COLLECT --> M4[Dateiname-Muster]
    COLLECT --> M5[Messenger-Herkunft]

    M1 & M2 & M3 & M4 & M5 --> AI[IA-05: KI-Analyse\nmit ALLEN Metadaten\n+ Kategorien aus DB\n+ Statische Regel-Vorklassifikation]

    AI --> OCR[IA-06: OCR Text-Erkennung\nSmart: prüft AI source-Feld]
    OCR --> TAGS[IA-07: EXIF-Tags schreiben\nAI-Tags + Source + Geo + Ordner]
    TAGS --> SORT[IA-08: Sortierung]

    SORT --> RULES{Statische Regeln\nauswerten\nmit media_type Filter}
    RULES -->|Regel matcht| RULE_CAT[Kategorie aus\nstatischer Regel]
    RULES -->|Keine Regel| DEFAULT_CAT[Default-Kategorie\nBild: personliches_foto\nVideo: personliches_video]

    RULE_CAT --> AI_CHECK{KI-Verifikation\nAI type valid + anders?}
    DEFAULT_CAT --> AI_CHECK

    AI_CHECK -->|KI korrigiert| AI_CAT[Kategorie von KI\naus DB validiert]
    AI_CHECK -->|KI bestätigt/leer| KEEP_CAT[Kategorie beibehalten]

    AI_CAT --> CAT_TAG[Kategorie-Label + Source\nals EXIF-Keywords schreiben]
    KEEP_CAT --> CAT_TAG

    CAT_TAG --> UNKNOWN_CHECK{Kategorie = unknown?}
    UNKNOWN_CHECK -->|Ja| REVIEW[Status → review\nManuelle Klassifikation]
    UNKNOWN_CHECK -->|Nein| TEMPLATE[Pfad-Template aus DB\nanwenden]

    REVIEW --> TEMPLATE

    TEMPLATE --> ROUTE{Routing}
    ROUTE -->|Dry-Run| DRYRUN[Zielpfad berechnen\nNichts verschieben]
    ROUTE -->|Immich Replace| IMMICH_R[Asset in Immich ersetzen]
    ROUTE -->|Immich Upload| IMMICH_U[Nach Immich hochladen\nArchivieren wenn immich_archive=true\nQuelle löschen]
    ROUTE -->|Lokal| MOVE[Safe Move:\nKopieren → SHA256 prüfen → Quelle löschen]

    MOVE --> CLEANUP[Leere Inbox-Ordner aufräumen]
    IMMICH_U --> CLEANUP

    style RULE_CAT fill:#4CAF50,color:#fff
    style AI_CAT fill:#E91E63,color:#fff
    style REVIEW fill:#9E9E9E,color:#fff
    style START fill:#673AB7,color:#fff
    style AI fill:#E91E63,color:#fff
    style GEO fill:#00BCD4,color:#fff
    style COLLECT fill:#FFC107,color:#000
    style RULES fill:#FF9800,color:#fff
    style AI_CHECK fill:#E91E63,color:#fff
