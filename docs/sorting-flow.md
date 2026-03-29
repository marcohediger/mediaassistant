flowchart TD
    START([Neue Datei in Inbox]) --> EXIF[IA-01: EXIF extrahieren]
    EXIF --> DUPES[IA-02: Duplikate erkennen]
    DUPES --> DUPECHECK{Duplikat?}
    DUPECHECK -->|Ja| STOP([Pipeline stopp])
    DUPECHECK -->|Nein| GEO[IA-03: Geocoding\nGPS → Ortsname]

    GEO --> CONVERT[IA-04: Temp. Konvertierung für KI]

    CONVERT --> COLLECT[Alle Metadaten sammeln]
    COLLECT --> M1[Kamera & Datum]
    COLLECT --> M2[GPS + Ortsname]
    COLLECT --> M3[Dateigrösse in KB]
    COLLECT --> M4[Dateiname-Muster]
    COLLECT --> M5[Messenger-Herkunft]

    M1 & M2 & M3 & M4 & M5 --> AI[IA-05: KI-Analyse\nmit ALLEN Metadaten]

    AI --> OCR[IA-06: OCR Text-Erkennung]
    OCR --> SORT[IA-08: Sortierung]

    SORT --> CHECK1{ai_type = screenshot\nODER 'screenshot' im Dateinamen?}
    CHECK1 -->|Ja| CAT_SCREENSHOT[📂 screenshot]
    CHECK1 -->|Nein| CHECK2

    CHECK2{ai_type = meme?}
    CHECK2 -->|Ja| CAT_SOURCELESS[📂 sourceless]
    CHECK2 -->|Nein| CHECK3

    CHECK3{ai_type = internet_image?}
    CHECK3 -->|Ja| CAT_SOURCELESS
    CHECK3 -->|Nein| CHECK4

    CHECK4{ai_type = document?}
    CHECK4 -->|Ja| CAT_SOURCELESS
    CHECK4 -->|Nein| CHECK5

    CHECK5{ai_type = personal\noder personal_photo?}
    CHECK5 -->|Ja| CHECK_VIDEO1{Video?}
    CHECK_VIDEO1 -->|Ja| CAT_VIDEO[📂 video]
    CHECK_VIDEO1 -->|Nein| CAT_PHOTO[📂 photo]
    CHECK5 -->|Nein| CHECK6

    CHECK6{Messenger-Datei\nUND kein EXIF?}
    CHECK6 -->|Ja| CAT_UNKNOWN[📂 unknown — Review]
    CHECK6 -->|Nein| CHECK7

    CHECK7{ai_type leer\nUND kein EXIF?}
    CHECK7 -->|Ja| CAT_UNKNOWN
    CHECK7 -->|Nein| CHECK_VIDEO2

    CHECK_VIDEO2{Video?}
    CHECK_VIDEO2 -->|Ja| CAT_VIDEO
    CHECK_VIDEO2 -->|Nein| CAT_PHOTO

    CAT_SCREENSHOT --> TEMPLATE[Pfad-Template anwenden\nz.B. photos/YYYY/YYYY-MM/]
    CAT_SOURCELESS --> TEMPLATE
    CAT_PHOTO --> TEMPLATE
    CAT_VIDEO --> TEMPLATE
    CAT_UNKNOWN --> TEMPLATE

    TEMPLATE --> ROUTE{Routing}
    ROUTE -->|Dry-Run| DRYRUN[Zielpfad berechnen\nNichts verschieben]
    ROUTE -->|Immich Replace| IMMICH_R[Asset in Immich ersetzen]
    ROUTE -->|Immich Upload| IMMICH_U[Nach Immich hochladen\nQuelle löschen]
    ROUTE -->|Lokal| MOVE[Safe Move:\nKopieren → SHA256 prüfen → Quelle löschen]

    MOVE --> CLEANUP[Leere Inbox-Ordner aufräumen]
    IMMICH_U --> CLEANUP

    style CAT_PHOTO fill:#4CAF50,color:#fff
    style CAT_VIDEO fill:#2196F3,color:#fff
    style CAT_SCREENSHOT fill:#FF9800,color:#fff
    style CAT_SOURCELESS fill:#f44336,color:#fff
    style CAT_UNKNOWN fill:#9E9E9E,color:#fff
    style START fill:#673AB7,color:#fff
    style AI fill:#E91E63,color:#fff
    style GEO fill:#00BCD4,color:#fff
    style COLLECT fill:#FFC107,color:#000
